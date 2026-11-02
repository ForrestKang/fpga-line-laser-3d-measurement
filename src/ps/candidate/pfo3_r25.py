"""Strict PL-final observation archive ABI. No pixel processing or branch linking."""
from __future__ import annotations
from dataclasses import dataclass
from types import MappingProxyType
import struct,zlib
import numpy as np

MAGIC=0x334f4650
VERSION=0x00030001
HEADER_BYTES=256
RECORD_BYTES=64
MAX_WIDTH,MAX_HEIGHT,MAX_QUALIFIED=752,480,7680
COMPLETE,RAW_REQUIRED,UNSUPPORTED,OVERFLOW,PROTOCOL,DIAG_OVERFLOW=1,2,4,8,16,32
HEADER_FIELDS=('magic version header_bytes record_bytes row_record_bytes total_bytes '
 'diagnostic_count qualified_count row_count flags source frame_id epoch cfg_version '
 'exposure_version motor_id motor_epoch motor_steps_lo motor_steps_hi FV_ticks_lo FV_ticks_hi '
 'exposure_start_lo exposure_start_hi exposure_end_lo exposure_end_hi width height roi_x0 roi_y0 '
 'roi_x1 roi_y1 algorithm_revision fixed_revision policy_revision '
 'fingerprint0 fingerprint1 fingerprint2 fingerprint3 fingerprint4 fingerprint5 fingerprint6 fingerprint7 '
 'measurement_count preview_count sat255_count sat250_count roi_pixels processing_cycles_lo '
 'processing_cycles_hi source_cycles measure_status preview_status measure_median_peak_x2 '
 'measure_median_width_x2 preview_median_peak_x2 preview_median_width_x2 measure_valid_rows '
 'preview_valid_rows ambiguous_rows raw_peak_median_x2 body_crc32 error_code input_bytes frame_decisions').split()
FIELDS=('candidate_id row start end center_q12 sum_weight_q12 moment_lo moment_hi peak_raw '
 'saturated_count contrast_q12 edge_drop_q12 detector_peak_q12 decisions branches aux').split()
ROW_FIELDS=('row raw_peak sat250_count sat255_count raw_candidate_count qualified_count '
 'measurement_count preview_count measurement_max_width preview_max_width measurement_max_peak '
 'preview_max_peak measurement_ambiguous preview_ambiguous reserved0 reserved1').split()
RECORD_DTYPE=np.dtype([(x,'<u4') for x in FIELDS])
ROW_DTYPE=np.dtype([(x,'<u4') for x in ROW_FIELDS])
IDENTITY_FIELDS=('frame_id','epoch','cfg_version','exposure_version','motor_id','motor_epoch',
                 'motor_steps','FV_ticks','exposure_start','exposure_end','source')
REASON_NAMES=(None,'local_reject','ambiguous_branch_direction','insufficient_branch_support',
              'unstable_center','weak_response','frame_capacity','invalid_center')
_REASON_ARRAY=np.asarray(REASON_NAMES,dtype=object)
_ROW_ARANGE_480=np.arange(480,dtype=np.uint32)
_CAND_ARANGE=np.arange(MAX_QUALIFIED*2,dtype=np.uint32)
STATUS_NAMES=('good','saturated','ambiguous','weak','incomplete','unvalidated')

class PFO3Error(ValueError):pass

def identity(header):
    result={k:int(header[k]) for k in IDENTITY_FIELDS if k not in
            ('motor_steps','FV_ticks','exposure_start','exposure_end')}
    for k in ('motor_steps','FV_ticks','exposure_start','exposure_end'):
        result[k]=int(header[k+'_lo']) | int(header[k+'_hi'])<<32
    return result

@dataclass(frozen=True)
class FinalFrame:
    header: object
    records: np.ndarray
    rows: np.ndarray
    payload: bytes

    @property
    def raw_required(self):return bool(self.header['flags']&RAW_REQUIRED)

    def frame_usable(self,mode='measurement'):
        if mode not in ('measurement','preview'):raise PFO3Error('unknown mode')
        return bool(self.header['frame_decisions']&(1 if mode=='measurement' else 2))

    def profile(self,mode='measurement',*,qualified_only=False):
        """Decode PL decisions; never infer/recompute image acceptance."""
        if self.raw_required:raise PFO3Error('whole-frame V3 fallback required')
        if mode not in ('measurement','preview'):raise PFO3Error('unknown mode')
        from reference_candidate.multi_stripe import MultiProfile
        a=self.records
        if qualified_only:a=a[(a['decisions']&255)==0]
        shift=8 if mode=='measurement' else 16
        codes=(a['decisions']>>shift)&255
        bits=(a['decisions']>>24)&255
        accepted=(bits & (2 if mode=='measurement' else 4))!=0
        branch=(a['branches']>>(0 if mode=='measurement' else 16))&65535
        branch=branch.astype(np.int64);branch[branch==65535]=-1
        def ro(x):
            x=np.asarray(x);x.setflags(write=False);return x
        n_acc=int(accepted.sum())
        reasons=(None,)*len(codes) if (n_acc==len(a) and not codes.any()) else tuple(_REASON_ARRAY[codes].tolist())
        start_i64=ro(a['start'].astype(np.int64))
        end_i64=ro(a['end'].astype(np.int64))
        return MultiProfile(ro(a['row'].astype(np.int64)),ro(a['center_q12'].astype(np.int64)),
            ro(accepted),reasons,ro(branch),
            {'width_px':ro(end_i64-start_i64+1),
             'contrast_dn':ro(a['contrast_q12'].astype(np.float64)*(1.0/4096.0)),
             'edge_drop_dn':ro(a['edge_drop_q12'].astype(np.float64)*(1.0/4096.0)),
             'peak_count':ro((a['aux']>>24).astype(np.int64)),
             'start':start_i64,'end':end_i64,
             'source_candidate_id':ro(a['candidate_id'].astype(np.int64))},
            {'candidate_count':len(a),'accepted':n_acc,
             'center_policy':'PL_V3_BACKGROUND_RAW_Q12','branch_policy':'PL_FINAL_V3',
             'CAD_truth_reads':0,'ARM_image_algorithm_calls':0},source='PL_FINAL_V3')

    def quality(self,mode='measurement'):
        from collections import Counter
        from structured_light.scan3d.continuous_quality import FrameQuality
        if mode not in ('measurement','preview'):raise PFO3Error('unknown mode')
        h=self.header;prefix='measure' if mode=='measurement' else 'preview'
        valid=int(h[prefix+'_valid_rows']);roi_h=h['roi_y1']-h['roi_y0']
        status=STATUS_NAMES[h[prefix+'_status']]
        shift=8 if mode=='measurement' else 16
        reason_counts=np.bincount(((self.records['decisions']>>shift)&255).astype(np.intp),minlength=8)
        # These are protocol conversions of PL-final scalars, not Raw reductions.
        return FrameQuality(status,valid/roi_h,
            h[prefix+'_median_peak_x2']/2 if valid else None,
            h[prefix+'_median_width_x2']/2 if valid else None,
            h['sat250_count']/h['roi_pixels'],h['ambiguous_rows']/roi_h,valid,roi_h,
            {REASON_NAMES[code]:int(value) for code,value in enumerate(reason_counts) if code and value},True,
            int(np.count_nonzero(self.rows['sat250_count'])))

def decode(payload,*,expected_identity,expected_fingerprint=None,allow_fixture=False):
    """Validate framing, identity and numeric bounds, not the optical algorithm."""
    data=bytes(payload)
    if len(data)<HEADER_BYTES:raise PFO3Error('truncated header')
    h=dict(zip(HEADER_FIELDS,struct.unpack_from('<64I',data)))
    if (h['magic'],h['version'],h['header_bytes'],h['record_bytes'],h['row_record_bytes'])!=(MAGIC,VERSION,256,64,64):
        raise PFO3Error('unknown ABI')
    if (h['algorithm_revision'],h['fixed_revision'],h['policy_revision'])!=(25,1,1):
        raise PFO3Error('unknown algorithm/fixed/policy revision')
    if h['frame_decisions']&~0x7f03 or h['flags']&~63 or not h['flags']&COMPLETE:
        raise PFO3Error('incomplete/reserved flags')
    if h['source'] not in (1,2,3) or (h['source']==3 and not allow_fixture):raise PFO3Error('unadmitted source')
    if set(expected_identity)!=set(IDENTITY_FIELDS) or identity(h)!=expected_identity:
        raise PFO3Error('frame/config/exposure/motion identity mismatch')
    for name,val in expected_identity.items():
        if type(val) is not int or val<0:raise PFO3Error('invalid expected '+name)
    if expected_fingerprint is not None:
        fp=struct.pack('<8I',*(h['fingerprint'+str(i)] for i in range(8))).hex()
        if fp!=expected_fingerprint:raise PFO3Error('algorithm parameter fingerprint mismatch')
    w,t=h['width'],h['height'];n=h['diagnostic_count']
    if not (1<=w<=MAX_WIDTH and 1<=t<=MAX_HEIGHT and h['row_count']==t and
            0<=h['roi_x0']<h['roi_x1']<=w and 0<=h['roi_y0']<h['roi_y1']<=t):
        raise PFO3Error('invalid image dimensions')
    if (n>((w+1)//2)*t or h['qualified_count']>n or
            (h['qualified_count']>16*t and not h['flags']&OVERFLOW)):
        raise PFO3Error('candidate capacity')
    size=256+64*(n+t)
    if len(data)!=size or h['total_bytes']!=size:raise PFO3Error('length/count mismatch')
    if h['input_bytes']!=w*t or h['roi_pixels']!=(h['roi_x1']-h['roi_x0'])*(h['roi_y1']-h['roi_y0']):
        raise PFO3Error('pixel count mismatch')
    if zlib.crc32(data[256:])!=h['body_crc32']:raise PFO3Error('body checksum mismatch')
    if bool(h['flags']&RAW_REQUIRED)!=bool(h['flags']&(UNSUPPORTED|OVERFLOW|PROTOCOL|DIAG_OVERFLOW)):
        raise PFO3Error('fallback cause inconsistent')
    a=np.frombuffer(data,RECORD_DTYPE,n,256)
    r=np.frombuffer(data,ROW_DTYPE,t,256+64*n)
    expected_rows=_ROW_ARANGE_480 if t==480 else np.arange(t,dtype=np.uint32)
    if not (r['row']==expected_rows).all() or (r['reserved0']|r['reserved1']).any():
        raise PFO3Error('invalid row records')
    if ((r['raw_peak']>255).any() or (r['sat255_count']>r['sat250_count']).any() or
            (r['sat250_count']>w).any() or int(r['sat250_count'].sum())!=h['sat250_count'] or
            int(r['sat255_count'].sum())!=h['sat255_count']):raise PFO3Error('invalid saturation counters')
    if h['sat250_count']>h['roi_pixels'] or h['sat255_count']>h['sat250_count']:
        raise PFO3Error('invalid global counters')
    if h['measure_status']>5 or h['preview_status']>5:raise PFO3Error('unknown quality status')
    if h['ambiguous_rows']>t or h['raw_peak_median_x2']>510:raise PFO3Error('invalid quality bounds')
    if ((r['measurement_count']>r['qualified_count']).any() or (r['preview_count']>r['qualified_count']).any() or
        (r['measurement_max_width']>w).any() or (r['preview_max_width']>w).any() or
        (r['measurement_max_peak']>255).any() or (r['preview_max_peak']>255).any() or
        (r['measurement_ambiguous']>1).any() or (r['preview_ambiguous']>1).any()):
        raise PFO3Error('invalid per-row quality bounds')
    for prefix in ('measure','preview'):
        if h[prefix+'_valid_rows']>t or h[prefix+'_median_peak_x2']>510 or h[prefix+'_median_width_x2']>2*w:
            raise PFO3Error('invalid quality scalar')
    expected_cands=_CAND_ARANGE[:n] if n<=len(_CAND_ARANGE) else np.arange(n,dtype=np.uint32)
    if not (a['candidate_id']==expected_cands).all():raise PFO3Error('candidate identity/order')
    if n:
        y,s,e=a['row'],a['start'],a['end']
        if (y>=t).any() or (s>e).any() or (e>=w).any() or (y[1:]<y[:-1]).any():raise PFO3Error('candidate geometry')
        if ((y[1:]==y[:-1])&(s[1:]<=e[:-1])).any():raise PFO3Error('overlapping unordered candidates')
        dec=a['decisions']
        bits=(dec>>24)&255;center_valid=(bits&1)!=0
        m=(dec>>8)&255;p=(dec>>16)&255
        ma=(bits&2)!=0;pa=(bits&4)!=0
        if (bits&np.uint32(0xfffffff8)).any() or (m>7).any() or (p>7).any() or (ma!=(m==0)).any() or (pa!=(p==0)).any():
            raise PFO3Error('decision flag/reason mismatch')
        if ((ma|pa)&~center_valid).any() or (a['center_q12']>=w*4096).any():raise PFO3Error('invalid accepted center')
        moment=a['moment_lo'].astype(np.uint64)|(a['moment_hi'].astype(np.uint64)<<32)
        weight=a['sum_weight_q12'].astype(np.uint64)
        widths=e.astype(np.uint64)-s+1
        if ((weight>widths*(255*4096)).any() or (moment>weight*e).any() or
                (moment<weight*s).any() or (((dec&255)!=0) & (ma|pa)).any()):
            raise PFO3Error('moment/weight bounds')
        if (center_valid&(weight==0)).any():raise PFO3Error('zero accepted weight')
        valid_idx=np.flatnonzero(center_valid)
        if len(valid_idx) and ((moment[valid_idx]*4096)//weight[valid_idx]!=a['center_q12'][valid_idx]).any():
            raise PFO3Error('center not matching recorded integer statistics')
        mb=a['branches']&65535;pb=a['branches']>>16
        if (ma&(mb>=16*t)).any() or (pa&(pb>=16*t)).any():raise PFO3Error('invalid branch ID')
        if int(ma.sum())!=h['measurement_count'] or int(pa.sum())!=h['preview_count']:
            raise PFO3Error('header acceptance count mismatch')
        if h['flags']&RAW_REQUIRED and (ma|pa).any():raise PFO3Error('partial fallback forbidden')
    elif h['measurement_count'] or h['preview_count']:raise PFO3Error('empty candidate counts')
    y_int=a['row'].astype(np.intp)
    row_counts=np.bincount(y_int,minlength=t)
    if not (row_counts==r['raw_candidate_count']).all():raise PFO3Error('diagnostic completeness mismatch')
    if int(r['qualified_count'].sum())!=h['qualified_count']:raise PFO3Error('qualified count mismatch')
    if int(r['measurement_count'].sum())!=h['measurement_count'] or int(r['preview_count'].sum())!=h['preview_count']:
        raise PFO3Error('row acceptance count mismatch')
    if n:
        for name,selected in (('measurement_count',ma),('preview_count',pa)):
            if not (np.bincount(y_int[selected],minlength=t)==r[name]).all():
                raise PFO3Error('per-row acceptance mismatch')
    a.setflags(write=False);r.setflags(write=False)
    return FinalFrame(MappingProxyType(h),a,r,data)

def encode_fixture(header,records,rows):
    """Offline interface fixtures only; source3 cannot be real-PL admitted."""
    h=dict(header);a=np.asarray(records,dtype=RECORD_DTYPE);r=np.asarray(rows,dtype=ROW_DTYPE)
    body=a.tobytes()+r.tobytes()
    h.update(magic=MAGIC,version=VERSION,header_bytes=256,record_bytes=64,row_record_bytes=64,
             diagnostic_count=len(a),row_count=len(r),total_bytes=256+len(body),source=3,
             body_crc32=zlib.crc32(body))
    return struct.pack('<64I',*(int(h.get(x,0)) for x in HEADER_FIELDS))+body
