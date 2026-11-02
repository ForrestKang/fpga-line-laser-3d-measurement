"""Strict, hardware-free validation of a complete real scanning evidence set.

Hashes are checked against the files actually consumed, and each validation
record is bound to the bitstream, sensor and optical calibration. A copied
synthetic fixture or a PENDING template cannot admit hardware motion.
"""
from dataclasses import dataclass
import hashlib, json, math
from pathlib import Path
from .continuous_processor import AxisCalibration, WorkingVolume
from .continuous_timing import ValidatedTimingModel
from .stream_exposure import ExposureEffectModel
from .pipeline import ScanCalibration
from .turntable import TurntableModel
from ..stripe_v1.types import StripeConfig
from ..stripe_v1.selection_v3 import SelectionConfigV3
from sl_geometry.section import reconstruct_section
import numpy as np


@dataclass(frozen=True)
class ScanBundle:
    path: Path
    sha256: str
    bit_sha256: str
    sensor_id: int
    calibration: ScanCalibration
    stripe_config: StripeConfig
    selection: SelectionConfigV3
    axis: AxisCalibration
    volume: WorkingVolume
    timing_models: dict
    effect_models: dict
    initial_exposure_lines: int
    height_bins_mm: tuple
    verified_files: tuple


def load_scan_bundle(path):
    path=Path(path).resolve();raw=path.read_bytes();p=json.loads(raw)
    if p.get('schema')!='continuous_scan_bundle_v1' or p.get('status')!='validated':
        raise ValueError('scan bundle not validated; missing real axis/timing/volume evidence')
    if p.get('source')!='real_board': raise ValueError('real_board validation required')
    if p.get('sensor_id')!=0x1324: raise ValueError('MT9V034 sensor identity required')
    bit=p['bit_sha256']
    if not isinstance(bit,str) or len(bit)!=64 or any(c not in '0123456789abcdef' for c in bit):
        raise ValueError('exact bit SHA-256 required')
    verified=[]
    def file(ref):
        target=(path.parent/ref['path']).resolve()
        if not target.is_relative_to(path.parent): raise ValueError('evidence path escapes bundle directory')
        data=target.read_bytes();sha=hashlib.sha256(data).hexdigest()
        if sha!=ref['sha256']: raise ValueError('evidence file hash mismatch: '+ref['path'])
        verified.append({'path':str(target),'sha256':sha})
        return target,json.loads(data),sha
    camera,camera_data,camera_sha=file(p['camera']);plane,plane_data,plane_sha=file(p['plane'])
    if tuple(camera_data.get('image_size',(752,480)))!=(752,480):
        raise ValueError('camera intrinsics must match 752x480 frames')
    if plane_data.get('camera_intrinsics_sha256') not in (None,camera_sha):
        raise ValueError('laser plane was fitted with different camera intrinsics')
    bounds=plane_data['training_point_bounds_mm']
    calibration=ScanCalibration(np.asarray(camera_data['camera_matrix'],float),
        np.asarray(camera_data['distortion_coefficients'],float),np.asarray(plane_data['plane_coefficients'],float),
        tuple(plane_data['training_ray_range_mm']),np.asarray([bounds['minimum'],bounds['maximum']],float),camera_sha,plane_sha)
    reconstruct_section(np.zeros(0,int),np.zeros(0,int),np.zeros(0,bool),
        calibration.camera_matrix,calibration.distortion,calibration.plane,
        calibration.ray_range_mm,calibration.training_bounds_mm)
    binding={'bit_sha256':bit,'sensor_id':0x1324,'camera_sha256':camera_sha,'plane_sha256':plane_sha}
    def evidence(ref,kind):
        target,r,sha=file(ref)
        if r.get('schema')!='continuous_'+kind+'_validation_v1' or r.get('source')!='real_board' or r.get('status')!='PASS':
            raise ValueError('real PASS evidence required: '+kind)
        if r.get('binding')!=binding: raise ValueError('evidence hardware/optical binding mismatch: '+kind)
        if not isinstance(r.get('samples'),int) or isinstance(r.get('samples'),bool) or r['samples']<3:
            raise ValueError('independent validation samples required: '+kind)
        metric,limit=r.get('maximum_error'),r.get('acceptance_limit')
        if (type(metric) not in (int,float) or type(limit) not in (int,float)
            or not math.isfinite(metric) or not math.isfinite(limit) or not 0<=metric<=limit or limit<=0):
            raise ValueError('measured validation error exceeds acceptance: '+kind)
        return r,sha,str(target)
    ar,sha,ref=evidence(p['axis'],'axis')
    axis=AxisCalibration(TurntableModel.from_json(ar['model']),tuple(ar['radial_reference_camera']),sha,ref)
    vr,sha,ref=evidence(p['volume'],'volume')
    if vr.get('units')!='mm' or vr['acceptance_limit']>.5:
        raise ValueError('independent volume validation must retain <=0.5mm gate')
    volume=WorkingVolume(vr['radius_mm'],tuple(vr['height_range_mm']),True,sha,ref)
    timing={};effects={}
    for exposure in p['exposures']:
        lines=exposure['lines']
        if lines in timing: raise ValueError('duplicate exposure model')
        tr,sha,ref=evidence(exposure['timing'],'timing')
        timing[lines]=ValidatedTimingModel(sha,ref,True,0,lines,tr['fv_to_exposure_start_ticks'],
            tr['line_period_ticks'],tr['command_sample_to_midpoint_pulses'],3200)
        er,sha,ref=evidence(exposure['effect'],'effect')
        if tr.get('exposure_lines')!=lines or er.get('exposure_lines')!=lines:
            raise ValueError('exposure line/model mismatch')
        effects[lines]=ExposureEffectModel(lines,er['discard_admitted_frames'],True,sha,ref)
    initial=p['initial_exposure_lines']
    if initial not in timing or len(timing)<2: raise ValueError('initial and target exposure models required')
    stripe=StripeConfig(fixed_threshold=60,roi=tuple(p['roi']),saturation_level=250)
    if stripe.roi[2]>752 or stripe.roi[3]>480: raise ValueError('ROI exceeds 752x480 sensor frame')
    bins=tuple(tuple(x) for x in p['height_bins_mm'])
    if not bins: raise ValueError('explicit measurement height bins required')
    for limits in bins:
        if (len(limits)!=2 or any(type(x) not in (int,float) or not math.isfinite(x) for x in limits)
            or not volume.height_range_mm[0]<=limits[0]<limits[1]<=volume.height_range_mm[1]):
            raise ValueError('height bins must be finite ordered intervals in validated volume')
    return ScanBundle(path,hashlib.sha256(raw).hexdigest(),bit,0x1324,calibration,
        stripe,SelectionConfigV3(),axis,volume,timing,effects,initial,bins,tuple(verified))
