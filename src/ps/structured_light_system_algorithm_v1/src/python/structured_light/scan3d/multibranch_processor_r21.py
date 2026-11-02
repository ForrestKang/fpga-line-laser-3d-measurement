"""Shared variable-length image observations for formal scan and saved-image replay.

The formal path retains ContinuousProcessor admission and physical evidence gates.
The separate SIMULATION_ONLY factory never constructs physical calibration proof.
Both paths use unchanged Q12 centroids and the frozen R19 numeric geometry chain.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import copy
import json
from pathlib import Path
import threading

import numpy as np

from sl_geometry.geometry import unrotate_points
try:
    from candidate.geometry_fast_r21 import reconstruct_section_fast
except ImportError:
    from sl_geometry.section_fast_r19 import reconstruct_section_fast
from ..stripe_v1.types import StripeConfig
from .continuous_pl_input import strict_geometry_mask
from .continuous_processor import ContinuousProcessor, ProcessedProfile, CloudSnapshot, _readonly
from .continuous_quality import assess_observed_quality, QualityThresholds
from .continuous_timing import _finite, _uint


OBSERVATION_CONTRACT = 'PS_IMAGE_MULTIBRANCH_Q12_V1'


def _default_extractor(gray, stripe_config):
    # This frozen extractor is deliberately separate from the deployed PL ABI.
    # A compatible native backend may be injected without changing this adapter.
    try:
        from candidate.multibranch_fast import extract_profiles
    except ImportError:
        from reference_candidate.multi_stripe import extract_profiles
    return extract_profiles(gray, stripe_config)


def _apply_geometry_reasons(reasons, accepted, section_valid, section_outside,
                            section_reasons, final_valid):
    """Apply the frozen accepted-only geometry reason priority in one vector step.

    Only accepted observations can replace their frontend reason.  For those
    rows the order is section-invalid (retain that reason), then outside
    training bounds, then non-finite geometry.  Rejected rows are untouched.
    The output is a plain list in the original observation order.
    """
    values = list(reasons)
    index = np.flatnonzero(accepted)
    if len(index):
        sec_valid = np.asarray(section_valid, bool)[index]
        sec_out = np.asarray(section_outside, bool)[index]
        fin_valid = np.asarray(final_valid, bool)[index]
        sec_reason = np.asarray(section_reasons, dtype=object)[index]
        replaced = np.where(~sec_valid, sec_reason,
                            np.where(sec_out, 'outside_training_bounds',
                                     np.where(~fin_valid, 'nonfinite_geometry', None)))
        # Section-invalid rows explicitly assign their section reason even if
        # that reason is None. Use the scalar reference's assignment mask.
        hits = np.flatnonzero(~sec_valid | sec_out | ~fin_valid)
        if len(hits):
            labels = np.asarray(values, dtype=object)
            labels[index[hits]] = replaced[hits]
            values = labels.tolist()
    return values


@dataclass(frozen=True)
class SimulationGeometry:
    camera_matrix: np.ndarray
    distortion: np.ndarray
    plane: np.ndarray
    ray_range_mm: tuple
    training_bounds_mm: np.ndarray
    axis_point: np.ndarray
    axis_direction: np.ndarray
    basis_camera: np.ndarray
    calibration_id: str
    evidence_type: str = 'SIMULATION_ONLY'


@dataclass(frozen=True)
class MultiBranchProfile(ProcessedProfile):
    branch_ids: np.ndarray
    observation_quality: dict
    observation_reasons: tuple
    observation_stats: dict
    frame_metadata: dict
    simulation_geometry_valid: bool
    simulation_registered: bool
    reference_basis: str

    @property
    def delta_camera_mm(self):
        arr = np.asarray(self.points_camera_mm, dtype=float)[self.valid_points]
        arr.setflags(write=False)
        return arr

    @property
    def delta_axis_mm(self):
        arr = np.asarray(self.points_axis_mm, dtype=float)[self.valid_points]
        if not np.isfinite(arr).all():
            arr = arr[np.isfinite(arr).all(axis=1)]
        arr.setflags(write=False)
        return arr

    def summary(self):
        summary = super().summary()
        valid_rows = self.rows[self.valid_points]
        if len(valid_rows) <= 1:
            distinct_valid_rows = len(valid_rows)
        else:
            row_diff = valid_rows[1:] - valid_rows[:-1]
            distinct_valid_rows = (int(1 + np.count_nonzero(row_diff))
                                   if int(row_diff.min()) >= 0 else len(np.unique(valid_rows)))
        summary.update(observation_contract=OBSERVATION_CONTRACT,
            observation_count=len(self.rows), stripe_observation_count=int(self.stripe_accepted.sum()),
            distinct_valid_rows=distinct_valid_rows,
            simulation_geometry_valid=self.simulation_geometry_valid,
            simulation_registered=self.simulation_registered,
            reference_basis=self.reference_basis,
            observation_source=self.frame_metadata.get('processing_backend',
                'PS_IMAGE_MULTIRUN_V1_NOT_DEPLOYED_PL'),
            geometry_numeric_policy='frozen_r19_fast_strict_geometry_unrotate',
            physical_accuracy_verified=False)
        return summary

    def persistence_payload(self):
        """JSON-safe all-candidate payload; array index is observation identity.

        Repeated sensor rows are intentional. Branch IDs only associate image
        support in this frame; they are never persistent physical surface IDs.
        Missing/rejected XYZ are null, retaining every observation and reason.
        """
        def points(value):
            return [[float(v) if np.isfinite(v) else None for v in row] for row in value]
        def builtin(value):
            if isinstance(value,np.generic):return value.item()
            if isinstance(value,dict):return {key:builtin(item) for key,item in value.items()}
            if isinstance(value,(list,tuple)):return [builtin(item) for item in value]
            return value
        return builtin(dict(schema=OBSERVATION_CONTRACT, request_id=self.request_id,
            capture_epoch=self.capture_epoch, frame_metadata=copy.deepcopy(self.frame_metadata),
            rows=self.rows.tolist(), center_q12=self.center_q12.tolist(),
            stripe_accepted=self.stripe_accepted.tolist(), branch_ids=self.branch_ids.tolist(),
            observation_reasons=list(self.observation_reasons), reasons=list(self.reasons),
            quality={k:np.asarray(v).tolist() for k,v in self.observation_quality.items()},
            stats=copy.deepcopy(self.observation_stats), valid_points=self.valid_points.tolist(),
            points_camera_mm=points(self.points_camera_mm), points_axis_mm=points(self.points_axis_mm),
            summary=self.summary()))


class MultiBranchProcessor(ContinuousProcessor):
    """Raw image multi-run observations with same formal capture admission.

    ``process(capture)`` is the formal path. Only ``from_virtual_config`` admits
    ``process(gray, metadata)`` for saved images with explicit assumed angles.
    Extractors must return the frozen MultiProfile variable-length contract.
    """
    supports_pmc2=True
    supports_final_geometry_hook_r25=True

    def _reconstruct_final_section(self,rows,centers,accepted):
        """Instance hook; the default retains the existing numerical path."""
        c=self.calibration
        return reconstruct_section_fast(rows,centers,accepted,c.camera_matrix,c.distortion,
            c.plane,c.ray_range_mm,c.training_bounds_mm)

    def _image_usable(self,quality):
        return quality.status=='good'

    def _geometry_quality_gate(self,valid,reasons,quality):
        return valid,reasons
    def __init__(self, *, extractor=None, **kwargs):
        super().__init__(**kwargs)
        self.extractor = extractor or _default_extractor
        if not callable(self.extractor): raise TypeError('extractor must be callable')
        self.replay = False
        self._last_time = None
        self.scan_angles_rad = []

    @classmethod
    def from_virtual_config(cls, config, *, extractor=None, exposure_lines=60, exposure_revision=0,
                            max_cloud_points=2_000_000):
        if isinstance(config, (str, Path)):
            config = json.loads(Path(config).read_text(encoding='utf-8'))
        if config.get('evidence_type') != 'SIMULATION_ONLY':
            raise ValueError('explicit SIMULATION_ONLY replay geometry required')
        if tuple(config.get('image_size', ())) != (752,480):
            raise ValueError('replay calibration must describe 752x480 gray8')
        if type(exposure_lines) is not int or not 1 <= exposure_lines <= 32765:
            raise ValueError('invalid replay exposure_lines')
        _uint('exposure_revision', exposure_revision)
        if type(max_cloud_points) is not int or max_cloud_points < 1:
            raise ValueError('positive max_cloud_points required')
        k=np.asarray(config['camera_matrix'],float)
        d=np.asarray(config['distortion_coefficients'],float)
        plane=np.asarray(config['laser_plane_camera'],float)
        bounds=np.asarray(config.get('camera_working_bounds_mm',[[-1000.,-1000.,1.],[1000.,1000.,2000.]]),float)
        limits=tuple(config.get('ray_range_mm',(1.,2000.)))
        point=np.asarray(config['axis_point_camera_mm'],float)
        direction=np.asarray(config['axis_direction_camera'],float)
        radial=np.asarray(config['radial_reference_camera'],float).copy()
        if any(v.shape!=(3,) or not np.isfinite(v).all() for v in (point,direction,radial)):
            raise ValueError('simulation axis requires finite 3-vectors')
        length=np.linalg.norm(direction)
        if length<=1e-12: raise ValueError('nonzero simulation axis required')
        direction=direction/length
        radial-=direction*(radial@direction)
        if np.linalg.norm(radial)<=1e-12: raise ValueError('radial basis parallel to simulation axis')
        radial/=np.linalg.norm(radial)
        basis=np.column_stack((radial,np.cross(direction,radial),direction))
        calibration_id=config['calibration_id']
        if not isinstance(calibration_id,str) or not calibration_id.strip():
            raise ValueError('simulation calibration_id required')
        geometry=SimulationGeometry(_readonly(k),_readonly(d),_readonly(plane),limits,_readonly(bounds),
            _readonly(point),_readonly(direction),_readonly(basis),calibration_id)
        reconstruct_section_fast(np.empty(0,int),np.empty(0,int),np.empty(0,bool),
            geometry.camera_matrix,geometry.distortion,geometry.plane,limits,geometry.training_bounds_mm)
        self=cls.__new__(cls)
        self.calibration=geometry
        self.calibration_id=calibration_id
        self.reference_basis='SIMULATION_ONLY axis XYZ mm; calibration='+calibration_id
        self.stripe_config=StripeConfig(fixed_threshold=60,min_width=1,max_width=32,roi=(0,0,752,480))
        self.exposure_lines,self.exposure_revision=exposure_lines,exposure_revision
        self.extractor=extractor or _default_extractor
        if not callable(self.extractor): raise TypeError('extractor must be callable')
        self.replay=True; self.mode='static'; self.axis=None; self.volume=None
        self.timing_binder=None; self.exposure_controller=None
        self.quality_thresholds=QualityThresholds(); self.min_valid_rows=3
        self.max_cloud_points=max_cloud_points; self._lock=threading.RLock()
        self._capture_epoch=None; self._last_request=None; self._last_time=None
        self._cloud=[]; self._revision=0; self._point_count=0; self.scan_angles_rad=[]
        return self

    def _admit_replay(self, gray, metadata):
        if not isinstance(metadata,dict): raise ValueError('explicit replay metadata required')
        image=np.asarray(gray)
        if image.shape!=(480,752) or image.dtype!=np.uint8:
            raise ValueError('replay requires 752x480 uint8 gray')
        meta=copy.deepcopy(metadata)
        request=meta.get('request_id',meta.get('frame_id'))
        _uint('request_id',request); _uint('capture_epoch',meta.get('capture_epoch'),16)
        if meta.get('frame_id',request)!=request: raise ValueError('mixed frame/request identity')
        timestamp=_finite('timestamp_s',meta.get('timestamp_s'))
        _finite('angle_rad',meta.get('angle_rad'))
        if meta.get('calibration_id',self.calibration_id)!=self.calibration_id:
            raise ValueError('frame/calibration identity mismatch')
        _uint('exposure_revision',meta.get('exposure_revision'))
        if (type(meta.get('exposure_lines')) is not int or meta.get('exposure_lines')!=self.exposure_lines
                or meta.get('exposure_revision')!=self.exposure_revision):
            raise ValueError('stale or missing replay exposure metadata')
        if self._capture_epoch is not None and meta['capture_epoch']!=self._capture_epoch:
            raise ValueError('capture epoch changed; construct a new processor')
        if self._last_request is not None and request<=self._last_request:
            raise ValueError('capture request repeated or out of order')
        if self._last_time is not None and timestamp<=self._last_time:
            raise ValueError('replay timestamps must strictly advance')
        meta.update(request_id=request,evidence_type='SIMULATION_ONLY',measurement_eligible=False)
        return image,meta

    def _observations(self,image, observed=None):
        if observed is None:
            observed=self.extractor(image,self.stripe_config)
        rows=np.asarray(observed.rows); centers=np.asarray(observed.center_q12)
        accepted=np.asarray(observed.accepted); branches=np.asarray(observed.branch_ids)
        if rows.ndim!=1: raise ValueError('observation rows must be a one-dimensional array')
        n=len(rows)
        if (rows.ndim!=1 or rows.dtype.kind not in 'iu' or centers.shape!=(n,)
                or centers.dtype.kind not in 'iu' or accepted.shape!=(n,) or accepted.dtype.kind!='b'
                or branches.shape!=(n,) or branches.dtype.kind not in 'iu'
                or len(observed.reasons)!=n or np.any(rows<0) or np.any(rows>=image.shape[0])
                or np.any(centers<0) or np.any(centers>=image.shape[1]*4096)
                or np.any(np.diff(rows)<0)):
            raise ValueError('invalid aligned variable-length observation contract')
        if any((reason is None)!=bool(ok) for reason,ok in zip(observed.reasons,accepted)):
            raise ValueError('observation acceptance/reason mismatch')
        if np.any(branches[accepted]<0): raise ValueError('accepted observations require branch IDs')
        if 'width_px' not in observed.quality: raise ValueError('width_px observation quality required')
        for name,value in observed.quality.items():
            a=np.asarray(value)
            if a.shape!=(n,) or a.dtype.kind not in 'iufb' or not np.isfinite(a).all():
                raise ValueError('invalid observation quality: '+name)
        return observed,rows,centers,accepted

    def _quality(self,image,observed,rows,accepted,reasons):
        try:
            from candidate.quality_fast import assess_multibranch_quality
        except ImportError:
            assess_multibranch_quality=None
        if assess_multibranch_quality is not None:
            native=assess_multibranch_quality(image,observed,rows,accepted,reasons,
                self.stripe_config,self.quality_thresholds)
            if native is not None:return native
        x0,y0,x1,y1=self.stripe_config.roi or (0,0,image.shape[1],image.shape[0])
        if x1>image.shape[1] or y1>image.shape[0]: raise ValueError('stripe ROI outside captured frame')
        if np.any(rows<y0) or np.any(rows>=y1): raise ValueError('extractor observation outside configured ROI')
        count=y1-y0
        offsets=rows.astype(np.intp,copy=False)-y0
        valid_rows=np.bincount(offsets[accepted],minlength=count)>0
        widths=np.zeros(count,float)
        np.maximum.at(widths,offsets[accepted],np.asarray(observed.quality['width_px'])[accepted])
        ambiguous=np.isin(np.asarray(observed.reasons,object),
            ('candidate_overflow','merged_peak_ambiguous','ambiguous_branch_direction'))
        ambiguity=(np.bincount(offsets[ambiguous],minlength=count)>0).astype(np.int64)*2
        roi=image[y0:y1,x0:x1]
        peaks=np.max(roi,axis=1)
        # Count actual saturated Raw pixels once. Per-row saturation is exactly
        # equivalent to any(Raw>=250), using the already observed row maximum.
        saturation_pixels=int(np.count_nonzero(roi>=250))
        saturated_rows=int(np.count_nonzero(peaks>=250))
        return assess_observed_quality(row_valid=valid_rows,widths_px=widths,
            intensities=peaks,candidate_counts=ambiguity,raw_row_peaks=peaks,
            saturation_pixels=saturation_pixels,saturated_rows=saturated_rows,roi_width=x1-x0,
            rejection_counts=dict(Counter(r for r in reasons if r is not None)),thresholds=self.quality_thresholds)

    def _pmc2_observations(self, payload, image, meta, request, epoch):
        """Bind a separate DMA packet to admitted Raw and immutable settings.

        Packet identity is derived from capture metadata, never trusted from the
        packet. Missing snapshot versions fail closed rather than guessing zero.
        A fixture packet in replay still remains SIMULATION_ONLY.
        """
        from candidate.pl_features_r22 import decode_packet, link_candidates
        from reference_candidate.multi_stripe import MultiPolicy
        required=('feature_config_revision','exposure_revision','motor_accepted_id',
                  'motor_epoch','emitted_command_count','fv_ticks')
        if any(key not in meta for key in required):
            raise ValueError('PMC2 requires captured configuration/exposure/motion snapshot')
        identity=dict(frame_id=request,epoch=epoch,cfg_version=meta['feature_config_revision'],
            exposure_version=meta['exposure_revision'],motor_id=meta['motor_accepted_id'],
            motor_epoch=meta['motor_epoch'],motor_steps=meta['emitted_command_count'],
            timestamp=meta['fv_ticks'])
        for key,value in identity.items():
            bits=64 if key=='timestamp' else 32
            if type(value) is not int or not 0<=value<2**bits:
                raise ValueError('invalid PMC2 capture snapshot '+key)
        c=self.stripe_config;p=MultiPolicy()
        x0,y0,x1,y1=c.roi or (0,0,image.shape[1],image.shape[0])
        config=dict(width=image.shape[1],height=image.shape[0],roi_x0=x0,roi_y0=y0,
            roi_x1=x1,roi_y1=y1,threshold=c.fixed_threshold,min_width=c.min_width,
            max_width=c.max_width,min_sum=c.min_sum,min_peak=c.min_peak,
            saturation=c.saturation_level,quality_packed=p.min_contrast_dn |
            p.min_edge_drop_dn<<8 | p.split_peak_drop_dn<<16,
            step_q12=int(p.max_branch_step_px*4096),
            residual_q12=int(p.max_direction_residual_px*4096))
        packet=decode_packet(payload,expected_identity=identity,expected_config=config)
        if packet.raw_required:
            meta['processing_backend']='PS_SAMEFRAME_RAW_FALLBACK_R22'
            meta['pmc2_fallback_flags']=packet.header['flags']
            return None
        if c.threshold_mode!='fixed' or c.max_candidates_per_row!=8:
            raise ValueError('unsupported configuration must request whole-frame Raw fallback')
        meta['processing_backend']=('PMC2_FIXTURE_PS_LINK_R22_SIMULATION_ONLY' if self.replay
                                    else 'PL_PMC2_LOCAL_CANDIDATES_PS_GLOBAL_R22')
        meta['pmc2_wire_version']=packet.header['version']
        return link_candidates(packet)

    def process(self, capture, metadata=None, *, feature_config_fingerprint=None, pmc2_payload=None):
        with self._lock:
            if self.replay:
                gray,meta=self._admit_replay(capture,metadata)
                request,epoch=meta['request_id'],meta['capture_epoch']
                frame_motion=None; exposure_eligible=False
            else:
                if metadata is not None: raise ValueError('formal path requires admitted FeatureCapture')
                gray,frame_motion,exposure_eligible,_=self._admit(capture,feature_config_fingerprint)
                request,epoch=capture.request_id,capture.epoch
                meta=copy.deepcopy(capture.motion_metadata or {})
            if pmc2_payload is None and not self.replay:
                pmc2_payload=getattr(capture,'pmc2_payload',b'')
            supplied=None
            if pmc2_payload:
                supplied=self._pmc2_observations(pmc2_payload,gray,meta,request,epoch)
            observed,rows,centers,accepted=self._observations(gray,supplied)
            c=self.calibration
            section=self._reconstruct_final_section(rows,centers,accepted)
            camera=np.asarray(section['points_mm'],float)
            valid,_=strict_geometry_mask(section['valid'],section['outside_training_bounds'],camera)
            reasons=_apply_geometry_reasons(observed.reasons,accepted,section['valid'],
                section['outside_training_bounds'],section['reasons'],valid)
            quality=self._quality(gray,observed,rows,accepted,reasons)
            valid,reasons=self._geometry_quality_gate(valid,reasons,quality)
            enough_rows=len(np.unique(rows[valid]))>=self.min_valid_rows
            profile_valid=self._image_usable(quality) and enough_rows
            angle=midpoint=None; registered=False; simulation_registered=False
            angle_source='unregistered'; registration_reason='static_mode'
            if self.replay:
                angle=float(meta['angle_rad']); angle_source='assumed_simulation_replay'
                registration_reason='SIMULATION_ONLY_not_physical_registration'
                point=c.axis_point; direction=c.axis_direction; basis=c.basis_camera
                simulation_registered=True
                reference_basis=self.reference_basis
                if self._point_count+int(valid.sum())>self.max_cloud_points:
                    raise BufferError('simulation point cloud capacity exceeded')
            else:
                exposure_eligible=exposure_eligible and (self.exposure_controller is None or
                    self.exposure_controller.frame_eligible(request_id=request,exposure_revision=self.exposure_revision))
                if (self.mode=='continuous' and self.measurement_validity.valid and profile_valid
                        and exposure_eligible and self._point_count+int(valid.sum())>self.max_cloud_points):
                    raise BufferError('fused point cloud capacity exceeded; scan must stop')
                registration_reason='static_mode' if self.mode=='static' else 'axis_or_timing_unvalidated'
                if self.mode=='continuous' and frame_motion is not None and self.timing_binder is not None:
                    binding=self.timing_binder.bind(frame_motion)
                    if binding.registered and self.axis is not None and exposure_eligible:
                        pulses=self._motion_offset_pulses+frame_motion.emitted_command_count+self.timing_binder.model.command_sample_to_midpoint_pulses
                        angle=pulses*self.axis.model.rad_per_step; midpoint=binding.exposure_mid_ticks
                        registered=True; angle_source='calibrated_command_estimate'; registration_reason=None
                if not exposure_eligible: registration_reason='exposure_transition_or_failure'
                reference_basis=self.measurement_validity.reference_basis
                if registered:
                    point=self.axis.model.axis_point_mm; direction=self.axis.model.axis_direction; basis=self.axis.basis_camera
            volume_changed=False
            axis_points=np.full_like(camera,np.nan)
            if (registered or simulation_registered) and valid.any():
                unrotated=unrotate_points(camera[valid],point,direction,angle)
                axis_points[valid]=(unrotated-np.asarray(point))@basis
                if not self.replay and self.volume is not None and self.volume.validated:
                    outside=valid & ~self.volume.contains(axis_points)
                    valid[outside]=False
                    volume_changed=bool(outside.any())
                    if volume_changed:
                        labels=np.asarray(reasons,dtype=object)
                        labels[np.flatnonzero(outside)]='outside_working_volume'
                        reasons=labels.tolist()
            if volume_changed or self.replay is False and not enough_rows:
                enough_rows=len(np.unique(rows[valid]))>=self.min_valid_rows
            profile_valid=self._image_usable(quality) and enough_rows
            fused=bool(not self.replay and registered and self.measurement_validity.valid and profile_valid and exposure_eligible)
            if fused or (self.replay and valid.any()):
                self._cloud.append(_readonly(axis_points[valid])); self._point_count+=int(valid.sum()); self._revision+=1
            result_valid=bool(not self.replay and profile_valid and exposure_eligible and (self.mode=='static' or fused))
            self._capture_epoch,self._last_request=epoch,request
            if self.replay:
                self._last_time=float(meta['timestamp_s']); self.scan_angles_rad.append(angle)
            elif frame_motion is not None:
                self._last_fv_ticks=frame_motion.fv_ticks; self._last_admitted_event_count=frame_motion.admitted_event_count
                self._last_command_count=frame_motion.emitted_command_count
                self._motion_identity=(frame_motion.motor_epoch,frame_motion.motor_accepted_id)
            return MultiBranchProfile(request,epoch,self.mode,_readonly(rows),_readonly(centers),_readonly(accepted),
                _readonly(camera),_readonly(axis_points),_readonly(valid),tuple(reasons),
                dict(Counter(r for r in reasons if r is not None)),quality,angle,angle_source,midpoint,
                registered,bool(profile_valid and not self.replay),result_valid,fused,self._revision,0,
                len(np.unique(rows)),(),registration_reason,_readonly(observed.branch_ids),
                {k:_readonly(v) for k,v in observed.quality.items()},tuple(observed.reasons),
                copy.deepcopy(observed.stats),meta,bool(self.replay and enough_rows),simulation_registered,reference_basis)

    def snapshot(self):
        if not self.replay: return super().snapshot()
        with self._lock:
            cloud=np.concatenate(self._cloud) if self._cloud else np.empty((0,3),float)
            return CloudSnapshot(self._revision,_readonly(cloud),self.reference_basis)

    def get_cloud(self):
        return self.snapshot().points_axis_mm
