"""Opt-in exact prepared-pair candidate with lazy cold MultiProfile objects.

The immutable observation, binding and geometry checks are the frozenR25
constructor, copied here so no module-global patch or production edit is needed.
Only unused cold dataclass materialization and identical gatedCounter are
removed from the normal path. MultiProfile itself has no costly post-init;
therefore this candidate is not presumed to explain the whole39ms wall time.
"""
from collections import Counter,namedtuple
from dataclasses import replace
import threading
from types import MappingProxyType
import numpy as np
from .pfo3_geometry_pair_r25 import (PreparedGeometryPairR25,PreparedModeFastR26,
    reconstruct_section_fast,_immutable,_reason_names,_calibration_key,_axis_key,
    FinalFrame,PFO3Error,HEADER_FIELDS,RECORD_DTYPE,ROW_DTYPE,MAX_QUALIFIED,
    FrameQuality,REASON_NAMES,STATUS_NAMES,MultiProfile)

_ProfileSeed=namedtuple('_ProfileSeed','rows center_q12 accepted reasons branch_ids quality stats source',defaults=['PL_FINAL_V3'])


class PreparedLazyGeometryPairR26(PreparedGeometryPairR25):
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
        self._frame_words=tuple(frame.header[name] for name in HEADER_FIELDS)
        self._frame_arrays=tuple((a.dtype,a.shape,a.strides,a.ctypes.data) for a in (frame.records,frame.rows))
        self._calibration_key=_calibration_key(calibration)
        self._axis_key_r26=_axis_key(calibration)
        a=frame.records[(frame.records['decisions']&255)==0]
        if len(a)>MAX_QUALIFIED or len(a)!=frame.header['qualified_count']:
            raise PFO3Error('pair qualified observations exceed capacity or differ from PL count')
        rows=_immutable(a['row'].astype(np.int64))
        centers=_immutable(a['center_q12'].astype(np.int64))
        common={
            'width_px':_immutable(a['end'].astype(np.int64)-a['start']+1),
            'contrast_dn':_immutable(a['contrast_q12'].astype(np.float64)/4096),
            'edge_drop_dn':_immutable(a['edge_drop_q12'].astype(np.float64)/4096),
            'peak_count':_immutable((a['aux']>>24).astype(np.int64)),
            'start':_immutable(a['start'].astype(np.int64)),
            'end':_immutable(a['end'].astype(np.int64)),
            'source_candidate_id':_immutable(a['candidate_id'].astype(np.int64))}
        self._observation_quality=MappingProxyType(common)
        self._profiles={};self._quality={}
        h=frame.header;roi_h=h['roi_y1']-h['roi_y0']
        saturated_rows=int(np.count_nonzero(frame.rows['sat250_count']))
        for mode,shift,bit,branch_shift,prefix in (
                ('measurement',8,2,0,'measure'),('preview',16,4,16,'preview')):
            accepted=_immutable(((a['decisions']>>24)&bit)!=0)
            codes=(a['decisions']>>shift)&255
            branch=((a['branches']>>branch_shift)&65535).astype(np.int64)
            branch[branch==65535]=-1
            stats=MappingProxyType(dict(candidate_count=len(a),
                accepted=int(accepted.sum()),center_policy='PL_V3_BACKGROUND_RAW_Q12',
                branch_policy='PL_FINAL_V3',CAD_truth_reads=0,ARM_image_algorithm_calls=0))
            self._profiles[mode]=_ProfileSeed(rows,centers,accepted,_reason_names(codes),
                _immutable(branch),MappingProxyType(common),stats,source='PL_FINAL_V3')
            valid=int(h[prefix+'_valid_rows'])
            counts=np.bincount(((frame.records['decisions']>>shift)&255).astype(np.intp),minlength=8)
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
        self._raw_fallback_rows=int(len(np.unique(rows)))
        modes={}
        for mode in ('measurement','preview'):
            profile=self._profiles[mode]
            accepted=profile.accepted
            valid_pre=_immutable(union_pass&accepted)
            base_reasons=np.asarray(profile.reasons,dtype=object)
            index=np.flatnonzero(accepted)
            if len(index):
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
            usable=frame.frame_usable(mode)
            if usable:
                valid_gate=valid_pre;reasons_gate=reasons_pre
            else:
                valid_gate=_immutable(np.zeros_like(valid_pre))
                gated=base_reasons.copy()
                gated[valid_pre]='PL_frame_quality_gate'
                reasons_gate=tuple(gated.tolist())
            counts_pre=tuple(Counter(r for r in reasons_pre
                                     if r is not None and r!='roi_excluded').items())
            counts_gate=(counts_pre if reasons_gate is reasons_pre else
                tuple(Counter(r for r in reasons_gate
                              if r is not None and r!='roi_excluded').items()))
            modes[mode]=PreparedModeFastR26(accepted=accepted,branch_ids=profile.branch_ids,
                observation_reasons=profile.reasons,valid_pre=valid_pre,valid_gate=valid_gate,
                reasons_pre=reasons_pre,reasons_gate=reasons_gate,counts_pre=counts_pre,
                counts_gate=counts_gate,usable=usable,
                stats_items=tuple(dict(profile.stats).items()))
        self._fast_modes=MappingProxyType(modes)
        chosen=lambda mode:(modes[mode].valid_pre if modes[mode].usable
                            else modes[mode].valid_gate)
        self._fast_union_valid=_immutable(chosen('measurement')|chosen('preview'))
        self._fast_lock=threading.Lock();self._fast_context=None
        self.counts=MappingProxyType(dict(qualified=len(rows),
            measurement=int(self._profiles['measurement'].accepted.sum()),
            preview=int(self._profiles['preview'].accepted.sum()),
            union=int(union.sum()),geometry_calls=1))
        self._cold_profiles={}
        self._cold_profile_lock=threading.Lock()

    def _mode(self,mode):
        if mode not in self._profiles:
            raise PFO3Error('unknown pair mode')
        with self._cold_profile_lock:
            if mode not in self._cold_profiles:
                self._cold_profiles[mode]=MultiProfile(*self._profiles[mode])
            return self._cold_profiles[mode]

    def quality(self,mode):
        # Quality is needed on the fast path but does not need a cold profile.
        if mode not in self._quality:
            raise PFO3Error('unknown pair mode')
        value=self._quality[mode]
        return replace(value,rejection_counts=dict(value.rejection_counts))

    @property
    def cold_profiles_materialized(self):
        return tuple(self._cold_profiles)
