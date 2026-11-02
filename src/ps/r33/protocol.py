"""R33 two-dimensional observations. Never interpreted as old centroid PFO3.

PL owns image decisions/branches. Host only validates tagged IDs and applies
explicit final-field patches. The assembled archive has its own magic/version.
"""
from dataclasses import dataclass
from types import MappingProxyType
import hashlib,struct,zlib,json
import numpy as np
from candidate.pfo3_r25 import HEADER_FIELDS,IDENTITY_FIELDS,PFO3Error,identity

CONTRACT={'algorithm':33,'window':'shared_3x3','derivatives':'matched_Q2',
          'normal':'strongest_H_column_then_H_power_iteration_10bit',
          'center':'Newton_2D_truncate_Q12_bound_half_pixel',
          'branches':'PL_row_anchor_du6_width8_support_measure8_preview3',
          'thresholds':{'run':24,'peak':48,'width_max':17,'saturation':'3x3_any'},
          'local_slots':16,'records_max':180480}
FINGERPRINT=hashlib.sha256(json.dumps(CONTRACT,sort_keys=True,separators=(',',':')).encode()).hexdigest()
SEED_RETRY_CONTRACT=dict(CONTRACT, seed_retry={
    'version':1,'trigger':'sole_local_reason_32',
    'seed':'first_maximum_smoothed_intensity_within_same_run',
    'response_min':48,'attempts':1,'original_valid':'unchanged',
    'failure':'original_diagnostic_unchanged','normal_and_offset':'unchanged'})
SEED_RETRY_FINGERPRINT=hashlib.sha256(json.dumps(SEED_RETRY_CONTRACT,
    sort_keys=True,separators=(',',':')).encode()).hexdigest()
ARCHIVE_MAGIC=0x4f333352
# R41 2x horizontal binning mode (header policy_revision 2): R39 detector on the pair-mean
# 376x480 image, kind-0 coordinates and geometry u mapped back to 752 by the PL.
BIN_FINGERPRINT=hashlib.sha256(b'R41 PFO3 2x horizontal pair-mean binning: R39 detector on 376x480, '
                               b'kind-0 coordinates and geometry u mapped to 752 (2x, 2u+2048)').hexdigest()

def _mode(binning, expected_fingerprint=None):
    if expected_fingerprint==SEED_RETRY_FINGERPRINT:
        if binning:raise PFO3Error('seed retry does not support binning')
        return ((33,1,3),SEED_RETRY_FINGERPRINT)
    return ((33,1,2),BIN_FINGERPRINT) if binning else ((33,1,1),FINGERPRINT)

@dataclass(frozen=True)
class Frame:
    header: object
    records: np.ndarray
    rows: np.ndarray
    payload: bytes

    def accepted(self,mode):
        if mode not in ('measurement','preview'):raise PFO3Error('unknown mode')
        bit=25 if mode=='measurement' else 26
        # Do not reclassify rejected candidates, even for empty laser frames.
        return ((self.records[:,13]>>bit)&1).astype(bool)

def assemble_reference(primary_stream,*,expected_identity,expected_fingerprint=FINGERPRINT,
             body_validator=None,return_seal=False,binning=False):
    if body_validator is not None:raise PFO3Error('old PFO3 validator cannot admit R33')
    data=bytes(primary_stream)
    if len(data)<384 or len(data)%4:raise PFO3Error('truncated hardware stream')
    words=np.frombuffer(data,'<u4');s=words[:64];e=words[-32:]
    if tuple(map(int,(s[0],s[1],e[0],e[1])))!=(0x33544850,33,0x33544650,33):
        raise PFO3Error('not an R33 hardware stream')
    if s[4]!=e[23] or e[24]!=len(words):raise PFO3Error('stream sequence/length mismatch')
    if e[17]:raise PFO3Error('native hardware error '+str(int(e[17])))
    if (len(words)-96)%17:raise PFO3Error('truncated hardware record')
    h=dict(zip(HEADER_FIELDS,map(int,s)))
    if set(expected_identity)!=set(IDENTITY_FIELDS) or identity(h)!=expected_identity:
        raise PFO3Error('frame/config/exposure/motion identity mismatch')
    revision,mode_fp=_mode(binning,expected_fingerprint)
    if (h['algorithm_revision'],h['fixed_revision'],h['policy_revision'])!=revision:
        raise PFO3Error('unknown R33 algorithm/fixed/policy revision')
    fp=struct.pack('<8I',*(h['fingerprint'+str(i)] for i in range(8))).hex()
    if fp!=expected_fingerprint or fp!=mode_fp:raise PFO3Error('algorithm parameter fingerprint mismatch')
    w,t=map(int,s[2:4])
    if (w,t)!=(752,480) or (h['width'],h['height'])!=(w,t):raise PFO3Error('configured/native geometry mismatch')
    if (h['roi_x0'],h['roi_y0'],h['roi_x1'],h['roi_y1'],h['roi_pixels'])!=(0,0,w,t,w*t):
        raise PFO3Error('unsupported R33 ROI')
    if int(e[16])!=w*t:raise PFO3Error('input byte count mismatch')
    a=words[64:-32].reshape(-1,17)
    if np.any((a[:,0]&0xfffffffc)!=0x33520000) or np.any((a[:,0]&3)==3):
        raise PFO3Error('unknown native record tag')
    kinds=a[:,0]&3
    local=np.ascontiguousarray(a[kinds==0,1:]);patch=a[kinds==1,1:];rows=np.ascontiguousarray(a[kinds==2,1:])
    n=len(local)
    if n>180480 or n!=e[2] or len(rows)!=t or int(e[3])>n:raise PFO3Error('native output count/capacity mismatch')
    if not np.array_equal(local[:,0],np.arange(n)) or not np.array_equal(rows[:,0],np.arange(t)):
        raise PFO3Error('diagnostic/row IDs not complete ordered')
    if np.any(rows[:,14:]):raise PFO3Error('reserved row fields')
    if n:
        y=local[:,1];first=local[:,2];last=local[:,3]
        if np.any(y>=t) or np.any(first>last) or np.any(last>=w) or np.any(y[1:]<y[:-1]):
            raise PFO3Error('candidate geometry/order invalid')
        valid=(local[:,13]>>24)&1
        u=local[:,4].astype(np.int64);v=local[:,5].astype(np.int64)
        if np.any(valid & ((u<0)|(u>=(w<<12))|(v<0)|(v>=(t<<12))|(abs(v-(y.astype(np.int64)<<12))>2048))):
            raise PFO3Error('two dimensional center outside contract')
    q=np.flatnonzero((local[:,13]&255)==0)
    if len(q)!=int(e[3]):raise PFO3Error('qualified count mismatch')
    if len(q):
        _,index,count=np.unique(local[q,1],return_index=True,return_counts=True)
        expected=q[(np.arange(len(q))-np.repeat(index,count))<16]
    else:expected=q
    ids=patch[:,0].astype(np.int64)
    if len(ids)!=len(expected) or len(np.unique(ids))!=len(ids) or np.any(ids>=n) or not np.array_equal(np.sort(ids),expected):
        raise PFO3Error('missing/foreign/repeated qualified candidate patch')
    if len(ids):
        if np.any(patch[:,13]&255):raise PFO3Error('patch changes local rejection')
        local[ids,13]=(local[ids,13]&255)|(patch[:,13]&0xffffff00)
        local[ids,14]=patch[:,14];local[ids,15]=(local[ids,15]&0xff000000)|(patch[:,15]&0xffffff)
    # A patch can turn center_valid on. Revalidate final coordinates without
    # removing the earlier check or changing its error precedence.
    if n:
        final_valid=(local[:,13]>>24)&1
        if np.any(final_valid & ((u<0)|(u>=(w<<12))|(v<0)|(v>=(t<<12))|(abs(v-(y.astype(np.int64)<<12))>2048))):
            raise PFO3Error('two dimensional center outside contract')
    if int(e[11])&~63 or not(int(e[11])&1):raise PFO3Error('incomplete/reserved frame flags')
    m=((local[:,13]>>25)&1).astype(bool);p=((local[:,13]>>26)&1).astype(bool)
    if np.any(m&~p) or np.any((m|p)&((local[:,13]&255)!=0)) or np.any((m|p)&(((local[:,13]>>24)&1)==0)):
        raise PFO3Error('invalid PL acceptance')
    if np.any(local[:,13]&0xf8000000) or np.any(local[:,13]&192):
        raise PFO3Error('reserved candidate decision bits')
    if np.any(m&(((local[:,13]>>8)&255)!=0)) or np.any(p&(((local[:,13]>>16)&255)!=0)):
        raise PFO3Error('accepted candidate has rejection reason')
    if (int(m.sum()),int(p.sum()),len(np.unique(local[m,1])),len(np.unique(local[p,1])))!=tuple(map(int,e[4:8])):
        raise PFO3Error('final acceptance summary mismatch')
    if (int(rows[:,4].sum()),int(rows[:,5].sum()),int(rows[:,2].sum()),int(rows[:,3].sum()))!=(n,len(q),int(e[9]),int(e[10])):
        raise PFO3Error('row diagnostic quality summary mismatch')
    if np.any(np.bincount(local[:,1].astype(int),minlength=t)!=rows[:,4]):raise PFO3Error('per-row candidate count mismatch')
    if np.any(np.bincount(local[q,1].astype(int),minlength=t)!=rows[:,5]):raise PFO3Error('per-row qualified count mismatch')
    if np.any(np.bincount(local[m,1].astype(int),minlength=t)!=rows[:,6]) or np.any(np.bincount(local[p,1].astype(int),minlength=t)!=rows[:,7]):
        raise PFO3Error('per-row acceptance count mismatch')
    overflow_rows=rows[:,5]>16
    overflow=bool(np.any(overflow_rows))
    # R39: only an overflowed row is barred from acceptance; the rest of the frame stands.
    if int(e[11])!=(11 if overflow else 1) or np.any(overflow_rows&((rows[:,6]!=0)|(rows[:,7]!=0))):
        raise PFO3Error('overflow/completion flags inconsistent')
    measurement_usable=bool(int(e[4])>=8 and int(e[6])*4>=t and int(e[9])*1000<=w*t and int(e[8])*10<=len(ids))
    if int(e[25])!=int(measurement_usable) or int(e[26])!=int(bool(p.any())) or int(e[27])&~127:
        raise PFO3Error('frame quality decision inconsistent')
    if bool(int(e[27])&1)!=(not bool(p.any())) or bool(int(e[27])&2)!=(not measurement_usable) or bool(int(e[27])&4)!=overflow:
        raise PFO3Error('frame quality flags inconsistent')
    h.update(magic=ARCHIVE_MAGIC,version=33,header_bytes=256,record_bytes=64,row_record_bytes=64,
             diagnostic_count=n,qualified_count=len(q),row_count=t,flags=int(e[11]),
             measurement_count=int(e[4]),preview_count=int(e[5]),processing_cycles_lo=int(e[14]),
             processing_cycles_hi=int(e[15]),input_bytes=int(e[16]),error_code=int(e[17]),
             frame_decisions=int(e[25])|(int(e[26])<<1)|(int(e[27])<<8))
    body=local.tobytes()+rows.tobytes();h.update(total_bytes=256+len(body),body_crc32=zlib.crc32(body))
    payload=struct.pack('<64I',*(h[k] for k in HEADER_FIELDS))+body
    # Arrays are views of immutable archival bytes, never a reused DMA buffer.
    frame=Frame(MappingProxyType(h),np.frombuffer(payload,'<u4',16*n,256).reshape(-1,16),
                np.frombuffer(payload,'<u4',16*t,256+64*n).reshape(-1,16),payload)
    receipt=dict(primary_stream_sha256=hashlib.sha256(data).hexdigest(),assembled_sha256=hashlib.sha256(payload).hexdigest(),
                 native_sequence=int(s[4]),diagnostic_count=n,final_patch_count=len(ids),row_count=t,
                 ARM_pixel_algorithm_calls=0,assembly_policy='R33_PL_FINAL_PATCH_BY_ID',
                 assembly_backend='R33_STRICT_NUMPY',coordinate_contract='R33_UV_Q12',algorithm_fingerprint=fp)
    return (frame,receipt,None) if return_seal else (frame,receipt)


def assemble_r33(primary_stream, *, expected_identity, expected_fingerprint=FINGERPRINT,
             body_validator=None, return_seal=False, backend='native', binning=False):
    """Strict default native path; reference is an explicit diagnostic choice.

    Python validates header identity/configuration; native validates all body,
    patch, row, acceptance and quality semantics without any image computation.
    """
    if backend == 'reference':
        return assemble_reference(primary_stream, expected_identity=expected_identity,
                                  expected_fingerprint=expected_fingerprint,
                                  body_validator=body_validator, return_seal=return_seal, binning=binning)
    if backend != 'native':raise PFO3Error('unknown R33 assembly backend')
    if body_validator is not None:raise PFO3Error('old PFO3 validator cannot admit R33')
    data=bytes(primary_stream)
    if len(data)<384 or len(data)%4:raise PFO3Error('truncated hardware stream')
    s=struct.unpack_from('<64I',data);e=struct.unpack_from('<32I',data,len(data)-128)
    if (s[0],s[1],e[0],e[1])!=(0x33544850,33,0x33544650,33):
        raise PFO3Error('not an R33 hardware stream')
    if s[4]!=e[23] or e[24]!=len(data)//4:raise PFO3Error('stream sequence/length mismatch')
    if e[17]:raise PFO3Error('native hardware error '+str(e[17]))
    if (len(data)//4-96)%17:raise PFO3Error('truncated hardware record')
    h=dict(zip(HEADER_FIELDS,s))
    if set(expected_identity)!=set(IDENTITY_FIELDS) or identity(h)!=expected_identity:
        raise PFO3Error('frame/config/exposure/motion identity mismatch')
    revision,mode_fp=_mode(binning,expected_fingerprint)
    if (h['algorithm_revision'],h['fixed_revision'],h['policy_revision'])!=revision:
        raise PFO3Error('unknown R33 algorithm/fixed/policy revision')
    fp=struct.pack('<8I',*(h['fingerprint'+str(i)] for i in range(8))).hex()
    if fp!=expected_fingerprint or fp!=mode_fp:raise PFO3Error('algorithm parameter fingerprint mismatch')
    w,t=s[2:4]
    if (w,t)!=(752,480) or (h['width'],h['height'])!=(w,t):raise PFO3Error('configured/native geometry mismatch')
    if (h['roi_x0'],h['roi_y0'],h['roi_x1'],h['roi_y1'],h['roi_pixels'])!=(0,0,w,t,w*t):
        raise PFO3Error('unsupported R33 ROI')
    if e[16]!=w*t:raise PFO3Error('input byte count mismatch')
    from .assemble_native import assemble_body
    body,(n,q,np_patch),library_sha=assemble_body(data)
    h.update(magic=ARCHIVE_MAGIC,version=33,header_bytes=256,record_bytes=64,row_record_bytes=64,
             diagnostic_count=n,qualified_count=q,row_count=t,flags=e[11],
             measurement_count=e[4],preview_count=e[5],processing_cycles_lo=e[14],
             processing_cycles_hi=e[15],input_bytes=e[16],error_code=e[17],
             frame_decisions=e[25]|(e[26]<<1)|(e[27]<<8))
    h.update(total_bytes=256+len(body),body_crc32=zlib.crc32(body))
    payload=struct.pack('<64I',*(h[k] for k in HEADER_FIELDS))+body
    frame=Frame(MappingProxyType(h),np.frombuffer(payload,'<u4',16*n,256).reshape(-1,16),
                np.frombuffer(payload,'<u4',16*t,256+64*n).reshape(-1,16),payload)
    receipt=dict(primary_stream_sha256=hashlib.sha256(data).hexdigest(),assembled_sha256=hashlib.sha256(payload).hexdigest(),
                 native_sequence=s[4],diagnostic_count=n,final_patch_count=np_patch,row_count=t,
                 ARM_pixel_algorithm_calls=0,assembly_policy='R33_PL_FINAL_PATCH_BY_ID',
                 assembly_backend='R33_STRICT_NATIVE_BODY_V1',native_assembler_sha256=library_sha,
                 coordinate_contract='R33_UV_Q12',algorithm_fingerprint=fp)
    return (frame,receipt,None) if return_seal else (frame,receipt)

from .protocol_r35 import assemble_r35 as assemble
