"""R21 ctypes acceleration of the frozen image-only multibranch contract.

Fixed thresholds use the portable C implementation. Local thresholds and
unavailable native libraries retain the frozen reference behavior. Compilation
is explicit; importing this module never builds or changes files.
"""
from __future__ import annotations

from collections import Counter
import ctypes
from dataclasses import asdict
import os
from pathlib import Path

import numpy as np

from reference_candidate.multi_stripe import MultiPolicy, MultiProfile
from reference_candidate.multi_stripe import extract_profiles as _reference
from structured_light.stripe_v1.candidates import CandidateError, _validate_gray
from structured_light.stripe_v1.types import StripeConfig

_REASONS = (None, 'candidate_width', 'insufficient_contrast', 'saturated_support',
            'roi_truncated', 'overflow', 'candidate_overflow', 'background_window_truncated',
            'ridge_contrast', 'ridge_shape', 'merged_peak_ambiguous',
            'ambiguous_branch_direction', 'insufficient_branch_support')
_REASON_ARRAY=np.asarray(_REASONS,dtype=object)
_LIBRARY_PATH = Path(__file__).resolve().parent/'native'/(
    'multibranch_r21.dll' if os.name=='nt' else 'libmultibranch_r21.so')
_LOAD_ERROR = None
try:
    _LIB = ctypes.CDLL(str(_LIBRARY_PATH))
except OSError as exc:
    _LIB = None
    _LOAD_ERROR = str(exc)
if _LIB is not None:
    _U8 = np.ctypeslib.ndpointer(dtype=np.uint8, ndim=2, flags='C_CONTIGUOUS')
    _I32 = np.ctypeslib.ndpointer(dtype=np.int32, flags='C_CONTIGUOUS')
    _I64 = np.ctypeslib.ndpointer(dtype=np.int64, flags='C_CONTIGUOUS')
    _F64 = np.ctypeslib.ndpointer(dtype=np.float64, flags='C_CONTIGUOUS')
    _LIB.multibranch_count.argtypes = [_U8,ctypes.c_int,ctypes.c_int,_I32]
    _LIB.multibranch_count.restype = ctypes.c_int64
    _LIB.multibranch_extract.argtypes = [_U8,ctypes.c_int,ctypes.c_int,_I32,_I32,_F64,
                                        ctypes.c_int,_I32,_I64,_F64]
    _LIB.multibranch_extract.restype = ctypes.c_int


def native_available() -> bool:
    return _LIB is not None


def native_library_path() -> str:
    return str(_LIBRARY_PATH)


def extract_profiles(gray: np.ndarray, stripe_config: StripeConfig,
                     policy: MultiPolicy | None = None) -> MultiProfile:
    p = policy or MultiPolicy()
    if not isinstance(p,MultiPolicy):
        raise ValueError('policy must be MultiPolicy')
    if not isinstance(stripe_config,StripeConfig):
        raise CandidateError('config must be a StripeConfig')
    image = _validate_gray(gray)
    h,w = image.shape
    x0,y0,x1,y1 = stripe_config.roi or (0,0,w,h)
    if not (0<=x0<x1<=w and 0<=y0<y1<=h):
        raise CandidateError('roi must lie within the gray image')
    values = [x0,y0,x1,y1,stripe_config.fixed_threshold,stripe_config.min_width,
              stripe_config.max_width,stripe_config.min_sum,stripe_config.min_peak,
              stripe_config.max_candidates_per_row,stripe_config.saturation_level]
    pol_values = [p.min_contrast_dn,p.min_edge_drop_dn,p.split_peak_drop_dn,p.minimum_branch_rows]
    if (_LIB is None or stripe_config.threshold_mode!='fixed' or
            max(values+pol_values+[w,h])>np.iinfo(np.int32).max):
        return _reference(gray,stripe_config,p)
    image = np.ascontiguousarray(image)
    config = np.asarray(values,dtype=np.int32)
    pol = np.asarray(pol_values,dtype=np.int32)
    limits = np.asarray([p.max_branch_step_px,p.max_direction_residual_px],dtype=np.float64)
    n = int(_LIB.multibranch_count(image,w,h,config))
    if n>np.iinfo(np.int32).max:
        return _reference(gray,stripe_config,p)
    output = np.empty((n,9),dtype=np.int32)
    centers = np.empty(n,dtype=np.int64)
    contrast = np.empty(n,dtype=np.float64)
    result = _LIB.multibranch_extract(image,w,h,config,pol,limits,n,output,centers,contrast)
    if result:
        raise RuntimeError('native multibranch extraction failed: '+str(result))
    reasons = tuple(_REASON_ARRAY[output[:,6]].tolist())
    accepted = output[:,8].astype(bool)
    def ro(value,dtype=None):
        array=np.asarray(value,dtype=dtype);array.setflags(write=False);return array
    row_counts=np.bincount(output[accepted,0],minlength=h)
    return MultiProfile(ro(output[:,0],int),ro(centers),ro(accepted),reasons,ro(output[:,7],int),
        {'width_px':ro(output[:,3],int),'contrast_dn':ro(contrast),
         'edge_drop_dn':ro(output[:,4],int),'peak_count':ro(output[:,5],int),
         'start':ro(output[:,1],int),'end':ro(output[:,2],int)},
        {'candidate_count':n,'accepted':int(accepted.sum()),
         'rejection_counts':dict(Counter(r for r in reasons if r)),
         'multiple_accepted_rows':int(np.count_nonzero(row_counts>1)),
         'center_policy':'original_threshold_run_Q12','CAD_truth_reads':0,
         'branch_policy':'unique_three_row_direction_v2','branch_policy_parameters':asdict(p)})
