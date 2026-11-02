"""Candidate same-frame observation/geometry sharing; not an admission shortcut.

This module does not modify the production processor or base class. Integration
through PLGeometryPairProcessorR25.process_prepared_final requires the base
instance hook _reconstruct_final_section(rows, centers, accepted), with
supports_final_geometry_hook_r25=True. The base now carries that hook and the
default hook calls the existing function with self.calibration; this module
still fails closed when the marker is absent. No module-global patching is
used here. PairedModeExecutor is an opt-in overlap layer; it does not change
either processor's admission, mode policy or persistence.

R26 adds a serial prepared fast path behind the same process_prepared_final
signature (see pfo3_fastpath_r26.py). The pair derives immutable per-mode
acceptance/validity/reason maps from the one union section; the fast path uses
them only under each processor's own lock/state/cloud. The unchanged R25 body
remains at _process_prepared_final_cold for non-replay and custom callers.

All capture/admission checks, registration, working-volume gates, separate cloud
state and preview dimension restrictions remain in the existing processor.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import struct
import threading
from time import perf_counter, thread_time
from types import MappingProxyType
import numpy as np

from reference_candidate.multi_stripe import MultiProfile
from structured_light.scan3d.continuous_quality import FrameQuality
from structured_light.scan3d.multibranch_processor_r21 import MultiBranchProcessor
from .geometry_fast_r21 import reconstruct_section_fast
from .pfo3_processor_r25 import PLFinalProcessorR25
from .observation_columns_r28 import convert as _native_observation_columns
from .observation_modes_r29 import convert as _fused_observation_modes
from .pfo3_r25 import (FinalFrame,PFO3Error,REASON_NAMES,STATUS_NAMES,HEADER_FIELDS,
                       RECORD_DTYPE,ROW_DTYPE,MAX_QUALIFIED)

_REASON_TABLE=np.asarray(REASON_NAMES,dtype=object)


def _reason_names(codes):
    """Vectorized code->name conversion; returns the exact ordered tuple."""
    arr=np.asarray(codes)
    if not arr.any():
        return (None,)*len(arr)
    return tuple(_REASON_TABLE[arr].tolist())


def _sorted_unique_count(rows):
    n=len(rows)
    if n<=1:
        return n
    return int(1+np.count_nonzero(rows[1:]!=rows[:-1]))


def _ordered_reason_counts(reasons):
    """Preserve Counter insertion order without a Python generator per point.

    PFO3 and geometry reasons come from a small fixed vocabulary. Both the
    unique-key scan and tuple.count run in C; results retain the first-seen
    order used by the frozen Counter-based encoder, including quality gates.
    """
    return tuple((reason, reasons.count(reason)) for reason in dict.fromkeys(reasons)
                 if reason is not None and reason != 'roi_excluded')


def _immutable(value):
    """Numeric arrays cannot be made writable again by a consumer."""
    if type(value) is not np.ndarray or not value.flags.c_contiguous:
        value=np.ascontiguousarray(value)
    return np.frombuffer(value.tobytes(),dtype=value.dtype).reshape(value.shape)


def _observation_columns(records):
    """Materialize numeric observations in two immutable, row-major slabs.

    The decoded wire fields are strided uint32 views. Building an independent
    astype + bytes owner for every field allocates/copies each observation
    twice. Assigning into typed rows performs the same exact conversion once;
    each returned row is a contiguous view with a permanently immutable bytes
    owner. No shared mutable workspace survives this function or crosses frames.
    """
    n=len(records)
    native=_native_observation_columns(records)
    if native is None:
        integer=np.empty((7,n),dtype=np.int64)
        integer[0]=records['row'];integer[1]=records['center_q12']
        integer[4]=records['start'];integer[5]=records['end']
        np.subtract(integer[5],integer[4],out=integer[2])
        integer[2]+=1
        integer[3]=records['aux']>>24
        integer[6]=records['candidate_id']
        floating=np.empty((2,n),dtype=np.float64)
        # Binary Q12 division is exact at the uint32 contract's magnitude.
        np.divide(records['contrast_q12'],4096.,out=floating[0])
        np.divide(records['edge_drop_q12'],4096.,out=floating[1])
    else:
        integer,floating=native
    integer=_immutable(integer)
    floating=_immutable(floating)
    return integer[0],integer[1],dict(
        width_px=integer[2],contrast_dn=floating[0],edge_drop_dn=floating[1],
        peak_count=integer[3],start=integer[4],end=integer[5],
        source_candidate_id=integer[6])


def _array_key(value):
    if value is None:return None
    if type(value) is np.ndarray and value.dtype==np.float64 and value.flags.c_contiguous:
        return value.shape,value.tobytes()
    if type(value) is tuple and len(value)==2 and type(value[0]) is float and type(value[1]) is float:
        return (2,),struct.pack('=2d',value[0],value[1])
    array=np.ascontiguousarray(value,dtype=np.float64)
    return array.shape,array.tobytes()


def _axis_key(calibration):
    """Numeric axis/basis identity, independent of array object identity."""
    return tuple(_array_key(getattr(calibration,name,None)) for name in
                 ('axis_point','axis_direction','basis_camera'))


@dataclass(frozen=True)
class PreparedModeFastR26:
    """Read-only per-mode data derived once from one immutable PFO3 frame.

    ``valid_pre``/``reasons_pre`` are the strict geometry result before the
    frame-quality gate; ``valid_gate``/``reasons_gate`` are the exact gated
    alternative.  The fast processor selects one pair per mode after asking
    its own ``_image_usable``.  Counts are stored as tuples so a caller cannot
    mutate prepared state; each result receives its own dict.
    """
    accepted: np.ndarray
    branch_ids: np.ndarray
    observation_reasons: tuple
    valid_pre: np.ndarray
    valid_gate: np.ndarray
    reasons_pre: tuple
    reasons_gate: tuple
    counts_pre: tuple
    counts_gate: tuple
    usable: bool
    stats_items: tuple


def _calibration_key(calibration):
    return tuple(_array_key(getattr(calibration,name)) for name in
                 ('camera_matrix','distortion','plane','ray_range_mm','training_bounds_mm'))


class PreparedGeometryPairR25:
    """One bounded frame, two independent policies, one camera-geometry union.

    Input must come from the strict decoder/transport. This object is only a
    derived cache, never evidence of hardware completion or permission to skip
    process_final admission. The same exact FinalFrame object must be bound by
    the processor (actual transport already retains it in PLFrameAdmission).
    Fixture replay can explicitly pass its decoded frame to the test-only seam.
    """
    def __init__(self,frame,calibration,*,geometry=reconstruct_section_fast):
        if (type(frame) is not FinalFrame or type(frame.payload) is not bytes
                or type(frame.header) is not MappingProxyType
                or frame.records.flags.writeable or frame.rows.flags.writeable
                or frame.records.base is not frame.payload or frame.rows.base is not frame.payload
                or frame.records.dtype!=RECORD_DTYPE or frame.rows.dtype!=ROW_DTYPE
                or frame.records.shape!=(frame.header['diagnostic_count'],)
                or frame.rows.shape!=(frame.header['row_count'],)):
            raise PFO3Error('pair requires immutable decoded FinalFrame')
        if frame.raw_required:raise PFO3Error('whole-frame fallback cannot use geometry pair')
        self.frame=frame
        self._frame_refs=(frame.payload,frame.header,frame.records,frame.rows)
        self._frame_header_dict=dict(frame.header)
        self._frame_words=tuple(self._frame_header_dict[name] for name in HEADER_FIELDS)
        self._frame_arrays=tuple((a.dtype,a.shape,a.strides,a.ctypes.data) for a in (frame.records,frame.rows))
        self._calibration_key=_calibration_key(calibration)
        self._axis_key_r26=_axis_key(calibration)
        qualified=frame.header['qualified_count']
        if type(qualified) is not int or not 0<=qualified<=MAX_QUALIFIED:
            raise PFO3Error('pair qualified observations exceed capacity or differ from PL count')
        fused=_fused_observation_modes(frame.records,qualified)
        if fused is None:
            a=frame.records[(frame.records['decisions']&255)==0]
            if len(a)!=qualified:
                raise PFO3Error('pair qualified observations exceed capacity or differ from PL count')
            rows,centers,common=_observation_columns(a)
        else:
            integer,floating,mode_columns,mode_counts,accepted_counts=fused
            integer=_immutable(integer);floating=_immutable(floating)
            mode_columns=_immutable(mode_columns)
            rows,centers=integer[0],integer[1]
            common=dict(width_px=integer[2],contrast_dn=floating[0],edge_drop_dn=floating[1],
                peak_count=integer[3],start=integer[4],end=integer[5],source_candidate_id=integer[6])
        self._observation_quality=MappingProxyType(common)
        self._profiles={};self._quality={}
        h=frame.header;roi_h=h['roi_y1']-h['roi_y0']
        saturated_rows=int(np.count_nonzero(frame.rows['sat250_count']))
        for mode,shift,bit,branch_shift,prefix in (
                ('measurement',8,2,0,'measure'),('preview',16,4,16,'preview')):
            index=0 if mode=='measurement' else 1
            if fused is None:
                accepted=_immutable(((a['decisions']>>24)&bit)!=0)
                codes=(a['decisions']>>shift)&255
                branch=((a['branches']>>branch_shift)&65535).astype(np.int64)
                branch[branch==65535]=-1
                branch=_immutable(branch)
                accepted_count=int(accepted.sum())
                counts=np.bincount(((frame.records['decisions']>>shift)&255).astype(np.intp),minlength=8)
            else:
                accepted=mode_columns[index].view(np.bool_)
                codes=mode_columns[2+index]
                branch=integer[7+index]
                accepted_count=int(accepted_counts[index])
                counts=mode_counts[index]
            stats=MappingProxyType(dict(candidate_count=qualified,
                accepted=accepted_count,center_policy='PL_V3_BACKGROUND_RAW_Q12',
                branch_policy='PL_FINAL_V3',CAD_truth_reads=0,ARM_image_algorithm_calls=0))
            self._profiles[mode]=MultiProfile(rows,centers,accepted,_reason_names(codes),
                branch,MappingProxyType(common),stats,source='PL_FINAL_V3')
            valid=int(h[prefix+'_valid_rows'])
            self._quality[mode]=FrameQuality(STATUS_NAMES[h[prefix+'_status']],valid/roi_h,
                h[prefix+'_median_peak_x2']/2 if valid else None,
                h[prefix+'_median_width_x2']/2 if valid else None,
                h['sat250_count']/h['roi_pixels'],h['ambiguous_rows']/roi_h,valid,roi_h,
                {REASON_NAMES[code]:int(value) for code,value in enumerate(counts) if code and value},
                True,saturated_rows)
        union=self._profiles['measurement'].accepted|self._profiles['preview'].accepted
        section=geometry(rows,centers,union,calibration.camera_matrix,calibration.distortion,
            calibration.plane,calibration.ray_range_mm,calibration.training_bounds_mm)
        n=len(rows)
        if (np.asarray(section['points_mm']).shape!=(n,3)
                or any(np.asarray(section[key]).shape!=(n,) for key in
                       ('valid','reasons','outside_training_bounds'))):
            raise PFO3Error('union geometry returned mismatched arrays')
        # Keep the union private and immutable. Mode sections are detached,
        # because callers may apply volume/quality gates after reconstruction.
        # Numeric arrays use a bytes owner so a consumer cannot flip the write
        # flag back; the object reason array only needs its flag cleared.
        self._section={}
        for key,value in section.items():
            if key=='reasons':
                array=np.asarray(value).copy();array.setflags(write=False)
            else:
                array=_immutable(value)
            self._section[key]=array
        points=self._section['points_mm']
        union_valid=np.asarray(self._section['valid'],bool)
        union_outside=np.asarray(self._section['outside_training_bounds'],bool)
        union_reasons=np.asarray(self._section['reasons'],dtype=object)
        union_finite=np.isfinite(points).all(axis=1)
        union_pass=union_valid&~union_outside&union_finite
        # The fast path derives both mode validity maps from the one union
        # section. Only the rows accepted by a mode can be valid there, so the
        # per-mode result is exactly the old detached section + strict mask.
        self._fast_rows=rows;self._fast_centers=centers
        self._raw_fallback_rows=_sorted_unique_count(rows)
        modes={}
        for mode in ('measurement','preview'):
            profile=self._profiles[mode]
            accepted=profile.accepted
            valid_pre=_immutable(union_pass&accepted)
            failed_geom=accepted&~union_pass
            if failed_geom.any():
                base_reasons=np.asarray(profile.reasons,dtype=object)
                index=np.flatnonzero(accepted)
                sec_valid=union_valid[index]
                sec_out=union_outside[index]
                fin_valid=union_pass[index]
                sec_reason=union_reasons[index]
                replaced=np.where(~sec_valid,sec_reason,
                    np.where(sec_out,'outside_training_bounds',
                        np.where(~fin_valid,'nonfinite_geometry',None)))
                hits=np.flatnonzero(replaced!=None)
                if len(hits):
                    base_reasons[index[hits]]=replaced[hits]
                reasons_pre=tuple(base_reasons.tolist())
            else:
                base_reasons=None
                reasons_pre=profile.reasons
            usable=frame.frame_usable(mode)
            if reasons_pre==(None,)*len(reasons_pre):
                counts_pre=()
            else:
                counts_pre=_ordered_reason_counts(reasons_pre)
            if usable:
                valid_gate=valid_pre;reasons_gate=reasons_pre;counts_gate=counts_pre
            else:
                valid_gate=_immutable(np.zeros_like(valid_pre))
                gated=np.asarray(reasons_pre,dtype=object) if base_reasons is None else base_reasons.copy()
                gated[valid_pre]='PL_frame_quality_gate'
                reasons_gate=tuple(gated.tolist())
                counts_gate=_ordered_reason_counts(reasons_gate)
            modes[mode]=PreparedModeFastR26(accepted=accepted,branch_ids=profile.branch_ids,
                observation_reasons=profile.reasons,valid_pre=valid_pre,valid_gate=valid_gate,
                reasons_pre=reasons_pre,reasons_gate=reasons_gate,counts_pre=counts_pre,
                counts_gate=counts_gate,usable=usable,
                stats_items=tuple(dict(profile.stats).items()))
        self._fast_modes=MappingProxyType(modes)
        chosen=lambda mode:(modes[mode].valid_pre if modes[mode].usable
                            else modes[mode].valid_gate)
        self._fast_union_valid=_immutable(chosen('measurement')|chosen('preview'))
        # These reductions depend only on the immutable frame and geometry;
        # the two processors still apply their own limits and mutate their
        # own cloud/state. Keep the counts beside the masks instead of
        # repeating sum/any/unique scans in the public mode calls.
        self._fast_mode_counts=MappingProxyType({mode:(
            int(np.count_nonzero(chosen(mode))),_sorted_unique_count(rows[chosen(mode)]))
            for mode in modes})
        self._fast_union_count=int(np.count_nonzero(self._fast_union_valid))
        self._fast_camera={}
        union_count=int(np.count_nonzero(union))
        for mode in modes:
            accepted=self._profiles[mode].accepted
            # The pinned reconstruction guarantees canonical NaNs outside
            # the union acceptance mask. Custom reconstruction callables
            # retain an explicit mask, matching the cold public contract.
            self._fast_camera[mode]=(points if (geometry is reconstruct_section_fast
                and self._profiles[mode].stats['accepted']==union_count)
                else _immutable(np.where(accepted[:,None],points,np.nan)))
        self._fast_lock=threading.Lock();self._fast_context=None
        # Reuse the counts already derived from these private immutable masks;
        # reporting them must not rescan both modes and their union again.
        self.counts=MappingProxyType(dict(qualified=len(rows),
            measurement=self._profiles['measurement'].stats['accepted'],
            preview=self._profiles['preview'].stats['accepted'],
            union=union_count,geometry_calls=1))

    def _mode(self,mode):
        if mode not in self._profiles:raise PFO3Error('unknown pair mode')
        return self._profiles[mode]

    def profile(self,mode):
        profile=self._mode(mode)
        # Separate mutable dictionaries match the original public contract;
        # only numeric immutable values are shared across modes/calls.
        return replace(profile,quality=dict(profile.quality),stats=dict(profile.stats))

    def quality(self,mode):
        self._mode(mode)
        value=self._quality[mode]
        return replace(value,rejection_counts=dict(value.rejection_counts))

    def fast_mode(self,mode):
        if mode not in self._fast_modes:raise PFO3Error('unknown pair mode')
        return self._fast_modes[mode]

    def mode_valid_counts(self,mode):
        """Frame-derived reductions; no processor state or admission is cached."""
        if mode not in self._fast_modes:raise PFO3Error('unknown pair mode')
        cached=getattr(self,'_fast_mode_counts',None)
        if cached is not None:return cached[mode]
        fast=self._fast_modes[mode]
        valid=fast.valid_pre if fast.usable else fast.valid_gate
        return int(valid.sum()),_sorted_unique_count(self._fast_rows[valid])

    def mode_camera_points(self,mode):
        if mode not in self._fast_modes:raise PFO3Error('unknown pair mode')
        cached=getattr(self,'_fast_camera',None)
        if cached is not None:return cached[mode]
        return _immutable(np.where(self._fast_modes[mode].accepted[:,None],
                                   self.section_points,np.nan))

    def mode_covers_union(self,mode):
        # Both mode masks are subsets of the union by construction, so equal
        # cardinality proves equality without scanning both masks again.
        total=getattr(self,'_fast_union_count',None)
        if total is not None:return self.mode_valid_counts(mode)[0]==total
        fast=self.fast_mode(mode)
        valid=fast.valid_pre if fast.usable else fast.valid_gate
        return np.array_equal(valid,self.fast_union_valid)

    @property
    def fast_rows(self):
        return self._fast_rows

    @property
    def fast_centers(self):
        return self._fast_centers

    @property
    def raw_fallback_rows(self):
        return self._raw_fallback_rows

    @property
    def observation_quality(self):
        return self._observation_quality

    @property
    def section_points(self):
        return self._section['points_mm']

    @property
    def fast_union_valid(self):
        return self._fast_union_valid

    def axis_matches(self,calibration):
        """The union axis is bindable only when both processors share it."""
        return _axis_key(calibration)==self._axis_key_r26

    def fast_context(self,capture,admission):
        """Return the per-frame capture/axis cache; shared only with a lock."""
        from .pfo3_fastpath_r26 import PreparedFastContextR26
        with self._fast_lock:
            context=self._fast_context
            if context is None or not context.matches(capture,admission):
                context=PreparedFastContextR26(self,capture,admission)
                self._fast_context=context
            return context

    def assert_binding(self,frame,calibration):
        if frame is not self.frame:raise PFO3Error('pair belongs to a different decoded frame')
        if any(actual is not expected for actual,expected in zip(
                (frame.payload,frame.header,frame.records,frame.rows),self._frame_refs)):
            raise PFO3Error('pair retained frame references changed')
        header_dict=getattr(self,'_frame_header_dict',None)
        if header_dict is not None:
            if frame.header!=header_dict:
                raise PFO3Error('pair retained frame header changed')
        elif tuple(frame.header.get(name) for name in HEADER_FIELDS)!=self._frame_words:
            raise PFO3Error('pair retained frame header changed')
        for array,layout in zip((frame.records,frame.rows),self._frame_arrays):
            if (array.base is not frame.payload or array.flags.writeable
                    or (array.dtype,array.shape,array.strides,array.ctypes.data)!=layout):
                raise PFO3Error('pair retained frame array layout changed')
        if _calibration_key(calibration)!=self._calibration_key:
            raise PFO3Error('pair calibration changed')

    def section(self,mode,*,rows,centers,accepted,calibration):
        self.assert_binding(self.frame,calibration)
        profile=self._mode(mode)
        if any(not np.array_equal(actual,expected) for actual,expected in
                ((rows,profile.rows),(centers,profile.center_q12),(accepted,profile.accepted))):
            raise PFO3Error('pair observations differ from mode observations')
        result={key:value.copy() for key,value in self._section.items()}
        rejected=~profile.accepted
        result['points_mm'][rejected]=np.nan
        result['valid'][rejected]=False
        result['reasons'][rejected]='invalid_stripe'
        result['outside_training_bounds'][rejected]=False
        return result


class PLGeometryPairProcessorR25(PLFinalProcessorR25):
    """Opt-in candidate. Separate instances are required for the two clouds.

    ``process_prepared_final`` keeps its R25 signature. For the replay path it
    dispatches to the R26 serial prepared fast path, which consumes the pair's
    already-derived union/mode maps and still uses each processor's own lock,
    state and cloud. Non-replay captures, custom extension points and flat-
    metadata violations fall back to the unchanged R25 cold implementation.
    """
    def process_prepared_final(self,capture,metadata=None,*,pair,payload,admission,
                               mode='measurement',**kwargs):
        if not getattr(MultiBranchProcessor,'supports_final_geometry_hook_r25',False):
            raise PFO3Error('base instance geometry hook has not been integrated')
        # Actual transport's retained frame is required; no capability is
        # manufactured for fixtures or mutable payloads.
        if admission.fixture_only:
            raise PFO3Error('prepared processor requires actual retained transport frame')
        pair.assert_binding(admission._validated_frame,self.calibration)
        if payload is not pair.frame.payload:
            raise PFO3Error('pair payload is not retained transport archive')
        if (getattr(self,'replay',False) is True and not kwargs
                and isinstance(metadata,dict)):
            from .pfo3_fastpath_r26 import process_prepared_final_fast
            return process_prepared_final_fast(self,capture,metadata,pair=pair,
                payload=payload,admission=admission,mode=mode)
        return self._process_prepared_final_cold(capture,metadata,pair=pair,payload=payload,
            admission=admission,mode=mode,**kwargs)

    def _process_prepared_final_cold(self,capture,metadata=None,*,pair,payload,admission,
                                     mode='measurement',**kwargs):
        """Unchanged R25 prepared implementation; explicit cold fallback."""
        if not getattr(MultiBranchProcessor,'supports_final_geometry_hook_r25',False):
            raise PFO3Error('base instance geometry hook has not been integrated')
        if admission.fixture_only:
            raise PFO3Error('prepared processor requires actual retained transport frame')
        pair.assert_binding(admission._validated_frame,self.calibration)
        if payload is not pair.frame.payload:
            raise PFO3Error('pair payload is not retained transport archive')
        with self._lock:
            if getattr(self,'_r25_pair',None) is not None:raise PFO3Error('nested pair processing')
            self._r25_pair=pair
            try:
                return super().process_final(capture,metadata,payload=payload,
                    admission=admission,mode=mode,**kwargs)
            finally:self._r25_pair=None

    def _observations(self,image,observed=None):
        pair=getattr(self,'_r25_pair',None)
        if pair is None:return super()._observations(image,observed)
        pair.assert_binding(self._r25_frame,self.calibration)
        # This profile is deterministically derived from the same strictly
        # decoded FinalFrame retained by process_final admission. Public arrays
        # have permanent immutable bytes owners. As in the production strict
        # FinalFrame shortcut, no generic extractor contract needs revalidation.
        profile=pair.profile(self._r25_mode)
        return profile,profile.rows,profile.center_q12,profile.accepted

    def _quality(self,image,observed,rows,accepted,reasons):
        pair=getattr(self,'_r25_pair',None)
        if pair is None:return super()._quality(image,observed,rows,accepted,reasons)
        pair.assert_binding(self._r25_frame,self.calibration)
        return pair.quality(self._r25_mode)

    def _reconstruct_final_section(self,rows,centers,accepted):
        """Mode section from the one union geometry; default path preserved."""
        pair=getattr(self,'_r25_pair',None)
        if pair is not None:
            pair.assert_binding(self._r25_frame,self.calibration)
            return pair.section(self._r25_mode,rows=rows,centers=centers,accepted=accepted,
                                calibration=self.calibration)
        c=self.calibration
        return reconstruct_section_fast(rows,centers,accepted,c.camera_matrix,c.distortion,
                                        c.plane,c.ray_range_mm,c.training_bounds_mm)


class PairedModeExecutor:
    """Run the two *independent* processors concurrently and join both.

    Both processors retain their own locks, admission checks, axis/volume
    state, cloud and result. Only the immutable ``PreparedGeometryPairR25`` is
    shared, and it is read-only after construction. The call returns only after
    both modes completed or one failed; a failure never publishes a mixed pair.

    The executor is opt-in and must be closed. It does not change the serial
    call sequence or any rejection reason; it only overlaps the two calls.
    Native geometry releases the GIL, so the camera section and independent
    CPU/IO portions can actually overlap on a multi-core ARM.
    """
    def __init__(self):
        self._pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='r26-pair-mode')
        self._closed=False

    @staticmethod
    def _timed(call,*args,**kwargs):
        started=perf_counter();cpu=thread_time();result=call(*args,**kwargs)
        return result,dict(wall_s=perf_counter()-started,thread_cpu_s=thread_time()-cpu)

    def process_timed(self,measure_processor,preview_processor,capture,metadata=None,*,pair,payload,admission,**kwargs):
        """Return ``(measured, preview, timings)`` after both modes joined."""
        if self._closed:raise PFO3Error('paired mode executor is closed')
        if measure_processor is preview_processor:
            raise PFO3Error('measurement and preview require distinct processors/clouds')
        frame=getattr(admission,'_validated_frame',None)
        if frame is None:raise PFO3Error('paired mode executor requires a retained validated frame')
        pair.assert_binding(frame,measure_processor.calibration)
        pair.assert_binding(frame,preview_processor.calibration)
        started=perf_counter()
        measurement=self._pool.submit(self._timed,measure_processor.process_prepared_final,capture,metadata,
            pair=pair,payload=payload,admission=admission,mode='measurement',**kwargs)
        preview=self._pool.submit(self._timed,preview_processor.process_prepared_final,capture,metadata,
            pair=pair,payload=payload,admission=admission,mode='preview',**kwargs)
        try:
            measured,measurement_timing=measurement.result()
        except BaseException:
            # Do not leave the other mode running behind a failed target.
            try:preview.result()
            except BaseException:pass
            raise
        previewed,preview_timing=preview.result()
        return measured,previewed,dict(measurement=measurement_timing,preview=preview_timing,
                                       overlap_wall_s=perf_counter()-started,
                                       overlap_thread_cpu_s=(measurement_timing['thread_cpu_s']
                                                             +preview_timing['thread_cpu_s']))

    def process(self,measure_processor,preview_processor,capture,metadata=None,*,pair,payload,admission,**kwargs):
        measured,previewed,_=self.process_timed(measure_processor,preview_processor,capture,metadata,
            pair=pair,payload=payload,admission=admission,**kwargs)
        return measured,previewed

    def close(self):
        # Mark closed only after shutdown returned, so a shutdown failure can be
        # retried and cannot silently turn a later close into a no-op.
        if not self._closed:
            self._pool.shutdown(wait=True)
            self._closed=True

    def __enter__(self):return self
    def __exit__(self,*args):self.close()


def process_both_modes(measure_processor,preview_processor,capture,metadata=None,*,pair,payload,admission,**kwargs):
    """One-shot convenience wrapper; production should reuse the executor."""
    with PairedModeExecutor() as executor:
        return executor.process(measure_processor,preview_processor,capture,metadata,pair=pair,
                                payload=payload,admission=admission,**kwargs)
