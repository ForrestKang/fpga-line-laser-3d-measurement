"""PMC2 bounded PL observations. Local support is provisional; PS owns linking.

The wire packet is distinct from the PS MBR1 storage format. No import/build or
device access occurs here. A RAW_REQUIRED packet carries no partial candidates.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import ctypes
import os
from pathlib import Path
from types import MappingProxyType
import struct
import numpy as np

from reference_candidate.multi_stripe import MultiPolicy, MultiProfile, _peaks
from structured_light.stripe_v1.candidates import extract_candidates

MAGIC = int.from_bytes(b'PMC2', 'little')
VERSION = 0x00020000
HEADER_BYTES, RECORD_BYTES = 128, 48
MAX_ROWS, MAX_WIDTH, MAX_PER_ROW, MAX_CANDIDATES = 480, 752, 8, 3840
COMPLETE, RAW_REQUIRED, UNSUPPORTED, OVERFLOW, PROTOCOL = 1, 2, 4, 8, 16
LOCAL_SUPPORT, LOCAL_AMBIGUOUS = 1 << 16, 1 << 17
HEADER_FIELDS = ('magic version header_bytes record_bytes total_bytes count flags '
    'frame_id epoch cfg_version exposure_version motor_id motor_epoch motor_steps '
    'timestamp_lo timestamp_hi width height roi_x0 roi_y0 roi_x1 roi_y1 threshold '
    'min_width max_width min_sum min_peak saturation quality_packed step_q12 '
    'residual_q12 reserved').split()
RECORD_FIELDS = ('row start end sum_weight sum_weight_x center_q12 peak_raw '
    'saturated_count peak_count contrast_x2 edge_drop flags').split()
DTYPE = np.dtype([(name, '<i4' if name=='contrast_x2' else '<u4') for name in RECORD_FIELDS])
IDENTITY_FIELDS = ('frame_id','epoch','cfg_version','exposure_version','motor_id',
                   'motor_epoch','motor_steps','timestamp')
REASONS = (None, 'candidate_width', 'insufficient_contrast', 'saturated_support',
    'roi_truncated', 'overflow', 'candidate_overflow', 'background_window_truncated',
    'ridge_contrast', 'ridge_shape', 'merged_peak_ambiguous',
    'ambiguous_branch_direction', 'insufficient_branch_support')
_NATIVE_PATH=Path(__file__).resolve().parent/'native'/(
    'link_candidates_r22.dll' if os.name=='nt' else 'liblink_candidates_r22.so')
try:
    _NATIVE=ctypes.CDLL(str(_NATIVE_PATH))
except OSError:
    _NATIVE=None
if _NATIVE is not None:
    _NATIVE.link_candidates_r22.argtypes=[np.ctypeslib.ndpointer(dtype=np.uint32,flags='C_CONTIGUOUS'),
        ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,
        np.ctypeslib.ndpointer(dtype=np.int32,flags='C_CONTIGUOUS')]
    _NATIVE.link_candidates_r22.restype=ctypes.c_int


class PacketError(ValueError):
    pass


@dataclass(frozen=True)
class CandidatePacket:
    header: dict
    records: np.ndarray

    @property
    def raw_required(self):
        return bool(self.header['flags'] & RAW_REQUIRED)


def _uint(name, value):
    if type(value) is not int or not 0 <= value <= 0xffffffff:
        raise PacketError(name+' must be uint32')
    return value


def _identity(header):
    return {**{name:header[name] for name in IDENTITY_FIELDS if name!='timestamp'},
            'timestamp':header['timestamp_lo'] | header['timestamp_hi'] << 32}


def decode_packet(data, *, expected_identity, expected_config=None):
    """Bounded vectorized validation before returning a read-only packet.

    Callers must supply all capture identities, including motion and timestamp.
    Config/exposure revisions refer to immutable snapshots, never current knobs.
    """
    # Snapshot mutable buffers before checking anything; read-only ndarray flags
    # alone do not prevent a bytearray owner from changing a validated record.
    view=memoryview(data if isinstance(data,bytes) else bytes(data))
    if view.nbytes < HEADER_BYTES or view.nbytes > HEADER_BYTES+MAX_CANDIDATES*RECORD_BYTES:
        raise PacketError('packet size outside bounded contract')
    h=dict(zip(HEADER_FIELDS,struct.unpack_from('<32I',view)))
    if (h['magic'],h['version'],h['header_bytes'],h['record_bytes']) != (MAGIC,VERSION,HEADER_BYTES,RECORD_BYTES):
        raise PacketError('unknown PMC2 schema')
    if h['reserved'] or h['flags'] & ~31 or not h['flags'] & COMPLETE:
        raise PacketError('incomplete packet or reserved bits')
    if h['count'] > MAX_CANDIDATES or h['total_bytes'] != HEADER_BYTES+h['count']*RECORD_BYTES or view.nbytes != h['total_bytes']:
        raise PacketError('count/length mismatch')
    identity=_identity(h)
    if (set(expected_identity) != set(IDENTITY_FIELDS) or
            any(type(v) is not int or v<0 for v in expected_identity.values()) or
            identity != expected_identity):
        raise PacketError('capture identity mismatch')
    if expected_config is not None:
        if any(key not in h or h[key]!=value for key,value in expected_config.items()):
            raise PacketError('configuration snapshot mismatch')
    if not (1<=h['width']<=MAX_WIDTH and 1<=h['height']<=MAX_ROWS and
            0<=h['roi_x0']<h['roi_x1']<=h['width'] and
            0<=h['roi_y0']<h['roi_y1']<=h['height']):
        raise PacketError('invalid image/ROI dimensions')
    if not (0<=h['threshold']<=255 and 1<=h['min_width']<=h['max_width']<=1023 and
            h['min_sum']<=262143 and h['min_peak']<=255 and 1<=h['saturation']<=255 and
            h['quality_packed']>>24==0 and all((h['quality_packed']>>shift)&255 for shift in (0,8,16)) and
            0<h['step_q12']<=MAX_WIDTH*4096 and 0<h['residual_q12']<=2*MAX_WIDTH*4096):
        raise PacketError('invalid configuration encoding')
    if bool(h['flags']&RAW_REQUIRED) != bool(h['flags']&(UNSUPPORTED|OVERFLOW|PROTOCOL)):
        raise PacketError('fallback cause and RAW_REQUIRED disagree')
    if h['flags']&RAW_REQUIRED and h['count']:
        raise PacketError('partial fallback is forbidden')
    a=np.frombuffer(view,DTYPE,count=h['count'],offset=HEADER_BYTES)
    if len(a):
        r,s,e=a['row'],a['start'],a['end']
        width=e.astype(np.int64)-s+1
        flags=a['flags'];reason=flags&255;options=(flags>>8)&255
        if (np.any((r<h['roi_y0'])|(r>=h['roi_y1'])|(s<h['roi_x0'])|(e>=h['roi_x1'])) or
                np.any(width<=0) or np.any(r[1:]<r[:-1]) or
                np.any((r[1:]==r[:-1]) & (s[1:]<=e[:-1]+1)) or
                np.any(np.bincount(r.astype(int),minlength=h['height'])>MAX_PER_ROW)):
            raise PacketError('candidate ordering/geometry/cap violation')
        sw=a['sum_weight'].astype(np.uint64);sx=a['sum_weight_x'].astype(np.uint64)
        if (np.any(sw==0) or np.any(sw>262143) or np.any(sx>134217727) or
                np.any(sw>width.astype(np.uint64)*(255-h['threshold'])) or
                np.any(sx<sw*s) or np.any(sx>sw*e) or
                np.any((sx*4096)//sw!=a['center_q12']) or
                np.any(a['peak_raw']<=h['threshold']) or np.any(a['peak_raw']>255) or
                np.any(a['saturated_count']>width) or np.any(a['peak_count']>(width-1)//2) or
                np.any(np.abs(a['contrast_x2'].astype(np.int64))>510) or np.any(a['edge_drop']>255) or
                np.any(reason>10) or np.any(flags&np.uint32(0xfffc0000)) or np.any(options>64) or
                np.any((reason!=0)&(options!=0)) or
                np.any(((r==h['roi_y0'])|(r==h['roi_y1']-1))&(options!=0)) or
                np.any(((flags&LOCAL_SUPPORT)!=0)!=(options==1)) or
                np.any(((flags&LOCAL_AMBIGUOUS)!=0)!=(options>1))):
            raise PacketError('candidate numeric/flag invariant violation')
    a.setflags(write=False)
    return CandidatePacket(MappingProxyType(h),a)


def local_hints(records, step_q12, residual_q12):
    """Exact R21 first three-row hypothesis pass; no final branch decision."""
    a=records.copy();a['flags'] &= 255
    rows=a['row'];q=a['center_q12'].astype(np.int64);valid=(a['flags']&255)==0
    for y in np.unique(rows):
        before=np.flatnonzero((rows==int(y)-1)&valid)
        middle=np.flatnonzero((rows==y)&valid)
        after=np.flatnonzero((rows==int(y)+1)&valid)
        for j in middle:
            left=before[np.abs(q[before]-q[j])<=step_q12]
            right=after[np.abs(q[after]-q[j])<=step_q12]
            n=int(np.count_nonzero(np.abs(q[right,None]-2*q[j]+q[left])<=residual_q12))
            a['flags'][j] |= n<<8 | (LOCAL_SUPPORT if n==1 else LOCAL_AMBIGUOUS if n>1 else 0)
    return a


def link_candidates(packet, policy=None, *, use_native=True):
    """Frozen global R21 association from PL numeric fields; reads no pixels."""
    if packet.raw_required:
        raise PacketError('whole-frame sameRaw fallback required')
    p=policy or MultiPolicy();a=packet.records;n=len(a)
    h=packet.header
    if (h['quality_packed'] != p.min_contrast_dn | p.min_edge_drop_dn<<8 | p.split_peak_drop_dn<<16 or
            h['step_q12']!=p.max_branch_step_px*4096 or h['residual_q12']!=p.max_direction_residual_px*4096):
        raise PacketError('policy snapshot differs from PL')
    rows=a['row'].astype(int);q=a['center_q12'].astype(np.int64)
    offsets=np.searchsorted(rows,np.arange(h['height']+1))
    reasons=[REASONS[int(code)] for code in a['flags']&255]
    if use_native and _NATIVE is not None:
        output=np.empty((n,3),np.int32)
        result=_NATIVE.link_candidates_r22(np.ascontiguousarray(a).view(np.uint32),n,h['height'],
            h['step_q12'],h['residual_q12'],p.minimum_branch_rows,output)
        if result:raise PacketError('native linker rejected PMC2 hints: '+str(result))
        return _profile(packet,p,output[:,0],output[:,1],output[:,2].astype(bool))
    parent=np.arange(n);triples=[];ambiguous=set()
    def find(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]];i=int(parent[i])
        return i
    for j in range(n):
        if reasons[j] is not None:continue
        y=rows[j]
        left=np.arange(offsets[y-1],offsets[y]) if y>0 else ()
        right=np.arange(offsets[y+1],offsets[y+2]) if y+1<h['height'] else ()
        options=[(int(i),j,int(k)) for i in left if reasons[i] is None for k in right
                 if reasons[k] is None and abs(int(q[i])-int(q[j]))<=h['step_q12'] and
                 abs(int(q[k])-int(q[j]))<=h['step_q12'] and
                 abs(int(q[k])-2*int(q[j])+int(q[i]))<=h['residual_q12']]
        if len(options)>1:ambiguous.add(j)
        elif len(options)==1:triples.append(options[0])
        encoded=int((a['flags'][j]>>8)&255)
        if encoded!=len(options):raise PacketError('three-row hint mismatch')
    triples=[t for t in triples if not any(i in ambiguous for i in t)]
    forward={};backward={}
    for i,j,k in triples:
        for a0,b0 in ((i,j),(j,k)):
            forward.setdefault(a0,set()).add(b0);backward.setdefault(b0,set()).add(a0)
    ambiguous.update(i for i,v in forward.items() if len(v)>1)
    ambiguous.update(i for i,v in backward.items() if len(v)>1)
    for i in ambiguous:reasons[i]='ambiguous_branch_direction'
    for i,j,k in triples:
        if any(v in ambiguous for v in (i,j,k)):continue
        parent[find(j)]=find(i);parent[find(k)]=find(i)
    branches=np.array([find(i) if reasons[i] is None else -1 for i in range(n)],int)
    counts=Counter(branches[branches>=0].tolist())
    for i in range(n):
        if reasons[i] is None and counts[int(branches[i])]<p.minimum_branch_rows:
            reasons[i]='insufficient_branch_support'
    accepted=np.array([r is None for r in reasons],bool)
    return _profile(packet,p,[REASONS.index(r) for r in reasons],branches,accepted)


def _profile(packet,p,codes,branches,accepted):
    a=packet.records;h=packet.header;n=len(a)
    rows=a['row'].astype(int);q=a['center_q12'].astype(np.int64)
    reasons=tuple(REASONS[int(code)] for code in codes)
    def ro(x,dtype=None):
        x=np.asarray(x,dtype=dtype);x.setflags(write=False);return x
    row_counts=np.bincount(rows[accepted],minlength=h['height'])
    return MultiProfile(ro(rows),ro(q),ro(accepted),tuple(reasons),ro(branches),
        {'width_px':ro(a['end'].astype(int)-a['start']+1),
         'contrast_dn':ro(a['contrast_x2'].astype(float)/2),'edge_drop_dn':ro(a['edge_drop'],int),
         'peak_count':ro(a['peak_count'],int),'start':ro(a['start'],int),'end':ro(a['end'],int)},
        {'candidate_count':n,'accepted':int(accepted.sum()),
         'rejection_counts':dict(Counter(r for r in reasons if r)),
         'multiple_accepted_rows':int(np.count_nonzero(row_counts>1)),
         'center_policy':'original_threshold_run_Q12','CAD_truth_reads':0,
         'branch_policy':'unique_three_row_direction_v2','branch_policy_parameters':asdict(p)},
        source='PL_PMC2_LOCAL_CANDIDATES_PS_GLOBAL_R22')


def reference_packet(gray, config, identity, policy=None):
    """Simulation oracle/packet fixture only. Never used as a PL performance claim."""
    p=policy or MultiPolicy();image=np.asarray(gray);height,width=image.shape
    x0,y0,x1,y1=config.roi or (0,0,width,height)
    stamp=identity['timestamp']
    values=[MAGIC,VERSION,128,48,128,0,COMPLETE,identity['frame_id'],identity['epoch'],
        identity['cfg_version'],identity['exposure_version'],identity['motor_id'],
        identity['motor_epoch'],identity['motor_steps'],stamp&0xffffffff,stamp>>32,
        width,height,x0,y0,x1,y1,config.fixed_threshold,config.min_width,config.max_width,
        config.min_sum,config.min_peak,config.saturation_level,
        p.min_contrast_dn | p.min_edge_drop_dn<<8 | p.split_peak_drop_dn<<16,
        int(p.max_branch_step_px*4096),int(p.max_direction_residual_px*4096),0]
    fallback=0
    if (config.threshold_mode!='fixed' or config.max_candidates_per_row!=8 or
            p.max_branch_step_px*4096!=values[29] or p.max_direction_residual_px*4096!=values[30]):
        fallback=UNSUPPORTED
    records=[]
    frame=extract_candidates(image,config)
    for row in frame.rows:
        if len(row.candidates)>8:fallback|=OVERFLOW
        for c in row.candidates:
            v=image[c.row,c.start:c.end+1].astype(int);peaks=_peaks(v)
            reason=REASONS.index(c.reasons[0]) if c.reasons else 0
            contrast2=drop=0
            if c.start<=x0 or c.end>=x1-1:
                reason=reason or 7
            else:
                left=image[c.row,max(x0,c.start-3):c.start]
                right=image[c.row,c.end+1:min(x1,c.end+4)]
                contrast2=int(2*(int(v.max())-max(float(np.median(left)),float(np.median(right)))))
                drop=int(min(v.max()-v[0],v.max()-v[-1]))
                if contrast2<2*p.min_contrast_dn:reason=reason or 8
                if len(v)<3 or drop<p.min_edge_drop_dn or not peaks:reason=reason or 9
            for pa,pb in zip(peaks[:-1],peaks[1:]):
                if min(v[pa[0]],v[pb[0]])-v[pa[1]:pb[0]+1].min()>=p.split_peak_drop_dn:
                    reason=10;break
            weight=v-config.fixed_threshold
            records.append((c.row,c.start,c.end,int(weight.sum()),int(weight@np.arange(c.start,c.end+1)),
                            c.center_q12,int(v.max()),int(np.count_nonzero(v>=config.saturation_level)),
                            len(peaks),contrast2,drop,reason))
    if len(records)>MAX_CANDIDATES:fallback|=OVERFLOW
    a=np.asarray(records,dtype=DTYPE)
    if fallback:
        values[6]|=RAW_REQUIRED|fallback;a=np.empty(0,DTYPE)
    else:a=local_hints(a,values[29],values[30])
    values[5]=len(a);values[4]=128+48*len(a)
    return struct.pack('<32I',*values)+a.tobytes()
