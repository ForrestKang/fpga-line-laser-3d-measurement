"""Offline-only V3 oracle to PFO3 interface fixtures; never hardware provenance."""
import dataclasses
import numpy as np
from .pfo3_r25 import *

def records_from_reference(result):
    c=result.candidates;n=len(c.row);a=np.zeros(n,dtype=RECORD_DTYPE)
    for k in ('candidate_id','row','start','end','center_q12','sum_weight_q12','peak_raw',
              'saturated_count','contrast_q12','edge_drop_q12','detector_peak_q12'):
        a[k]=getattr(c,k)
    moment=np.asarray(c.sum_weight_x,dtype=np.uint64)
    a['moment_lo']=moment&0xffffffff;a['moment_hi']=moment>>32
    flags=(np.asarray(c.sum_weight_q12)>0).astype(np.uint32)
    flags|=np.asarray(c.measurement_usable,dtype=np.uint32)<<1
    flags|=np.asarray(c.preview_usable,dtype=np.uint32)<<2
    a['decisions']=np.asarray(c.local_reasons,dtype=np.uint32)
    a['decisions']|=np.asarray(c.measurement_reason,dtype=np.uint32)<<8
    a['decisions']|=np.asarray(c.preview_reason,dtype=np.uint32)<<16
    a['decisions']|=flags<<24
    a['branches']=np.asarray(c.measurement_branch_id,dtype=np.uint32)
    a['branches']|=np.asarray(c.preview_branch_id,dtype=np.uint32)<<16
    a['aux']=np.minimum(np.asarray(c.stability_q12,dtype=np.uint32),0xffffff)
    a['aux']|=np.minimum(np.asarray(c.peak_count,dtype=np.uint32),255)<<24
    return a

def fixture(result,*,capture_identity,config):
    h={x:0 for x in HEADER_FIELDS};f=result.frame
    for k,v in capture_identity.items():
        if k in ('motor_steps','FV_ticks','exposure_start','exposure_end'):
            h[k+'_lo']=v&0xffffffff;h[k+'_hi']=v>>32
        else:h[k]=v
    h.update(width=f.width,height=f.height,roi_x1=f.width,roi_y1=f.height,
        qualified_count=f.local_count,flags=COMPLETE|(RAW_REQUIRED|OVERFLOW if f.overflow_rows else 0),
        algorithm_revision=25,fixed_revision=1,policy_revision=1,measurement_count=f.measurement_count,
        preview_count=f.preview_count,roi_pixels=f.width*f.height,input_bytes=f.width*f.height,
        sat250_count=f.saturated_pixels,measure_valid_rows=f.measurement_rows,
        preview_valid_rows=f.preview_rows,ambiguous_rows=f.ambiguous_rows,
        measure_status=f.measure_status,preview_status=f.preview_status,
        measure_median_peak_x2=f.measure_median_peak_x2,measure_median_width_x2=f.measure_median_width_x2,
        preview_median_peak_x2=f.preview_median_peak_x2,preview_median_width_x2=f.preview_median_width_x2,
        raw_peak_median_x2=f.raw_peak_median_x2)
    h['frame_decisions']=int(f.measurement_usable)|(int(f.preview_usable)<<1)|(int(f.flags)<<8)
    for i,x in enumerate(struct.unpack('<8I',bytes.fromhex(config.fingerprint))):h['fingerprint'+str(i)]=x
    rows=np.zeros(f.height,dtype=ROW_DTYPE)
    for i,name in enumerate(ROW_FIELDS):rows[name]=result.row_data[:,i]
    h['sat255_count']=int(rows['sat255_count'].sum())
    return encode_fixture(h,records_from_reference(result),rows)
