"""Only unpack/select actual FPGA XYZ; no ray, distortion or rotation work."""
import time
from collections import Counter
import numpy as np
from geometry_reference import REASONS,parameters
from .geometry_golden_r34 import GeometryResult

def reconstruct_pair(frame,calibration,*,angle_rad=0.):
    start=time.perf_counter()
    binding=getattr(frame,'geometry_binding',None)
    if not binding or binding['calibration_sha256']!=calibration.source_sha256 or binding['angle_rad']!=float(angle_rad):
        raise ValueError('actual PL geometry parameter binding required')
    a=frame.geometry_records
    flags=a[:,7];valid=(flags&256)!=0
    m=(flags&1)!=0;p=(flags&2)!=0
    camera=a[:,1:4].astype('<u4').view('<i4').astype(np.float64)/65536.
    axis=a[:,4:7].astype('<u4').view('<i4').astype(np.float64)/65536.
    reasons=(flags>>16)&255
    reject={}
    for name,mask in [('measurement',m),('preview',p)]:
        reject[name]=dict(Counter(REASONS[int(x)] for x in reasons[mask&~valid]))
        bit=1 if name=='measurement' else 2
        if not int(frame.header['frame_decisions'])&bit:
            count=int(np.count_nonzero(frame.accepted(name)))
            if count:reject[name]['frame_quality']=count
    mi=m&valid;pi=p&valid
    finish=time.perf_counter()
    return GeometryResult(camera[mi].copy(),camera[pi].copy(),axis[mi].copy(),axis[pi].copy(),
        a[mi,0].copy(),a[pi,0].copy(),reject,
        dict(prepare=0.,section=0.,axis=0.,select=(finish-start)*1000,total=(finish-start)*1000),
        calibration.calibration_id,calibration.source_sha256)
