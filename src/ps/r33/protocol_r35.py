"""R35 outer stream: retained R33 image observations + actual PL XYZ/moments.

R33 image validation is reused without weakening it. New records are validated
separately and full R35 primary bytes remain the archival source of truth.
"""
import struct,hashlib
import numpy as np
from candidate.pfo3_r25 import PFO3Error

class HardwareFrame:
    def __init__(self,base,geometry,moments,metadata):
        self.base=base;self.header=base.header;self.records=base.records;self.rows=base.rows;self.payload=base.payload
        self.geometry_records=geometry;self.moment_records=moments
        self.geometry_epoch=int(metadata[0])
        self.geometry_sha256=metadata[1:9].astype('<u4').tobytes().hex()
        self.moment_origin_q16=int(metadata[9:10].view('<i4')[0]);self.moment_shift=int(metadata[10])
        self.geometry_binding=None
    def accepted(self,mode):return self.base.accepted(mode)

def assemble_r35(primary,**kwargs):
    from .protocol import assemble_r33
    raw=bytes(primary)
    if len(raw)%4 or len(raw)<384:raise PFO3Error('R35 invalid stream size')
    w=np.frombuffer(raw,dtype='<u4')
    if w[0]!=0x35544850 or w[1]!=35 or w[-32]!=0x35544650 or w[-31]!=35:
        raise PFO3Error('R35 stream identity mismatch')
    if int(w[-8])!=len(w) or (len(w)-96)%17:raise PFO3Error('R35 output length mismatch')
    a=w[64:-32].reshape(-1,17)
    if np.any((a[:,0]&0xffffff00)!=0x35520000):raise PFO3Error('R35 record tag prefix')
    tags=a[:,0]&255
    if np.any(tags>6):raise PFO3Error('R35 unknown record type')
    geometry=a[tags==3,1:9].copy()
    if np.any(a[tags==3,9:]!=0):raise PFO3Error('R35 geometry padding')
    meta=a[tags==6,1:]
    if len(meta)!=1 or tuple(map(int,meta[0,11:13]))!=(1,2) or np.any(meta[0,13:]!=0):
        raise PFO3Error('R35 geometry metadata identity')
    halves=a[(tags==4)|(tags==5)]
    if len(halves)==0:
        # R40 b08 removed the PL moments block: no kind 4/5 records and a zero footer count.
        if int(w[-2])!=0:raise PFO3Error('R40 moment count without moment records')
        moments=np.zeros((0,32),np.uint32)
    elif len(halves)!=128 or not np.array_equal(halves[:,0]&255,np.tile([4,5],64)):
        raise PFO3Error('R35 moment halves/order incomplete')
    else:moments=halves[:,1:].reshape(64,32).copy()
    if int(w[-3])!=len(geometry) or int(w[-1])!=0:raise PFO3Error('R35 point count or moment overflow')
    kept=a[tags<3].copy();kept[:,0]=(kept[:,0]&255)|0x33520000
    h=w[:64].copy();h[0]=0x33544850;h[1]=33
    f=w[-32:].copy();f[0]=0x33544650;f[1]=33;f[24]=96+17*len(kept);f[29:]=0
    old=h.tobytes()+kept.tobytes()+f.tobytes()
    wantseal=kwargs.get('return_seal',False)
    result=assemble_r33(old,**kwargs)
    base,receipt=result[:2]
    decisions=int(base.header['frame_decisions'])
    m=base.accepted('measurement')&bool(decisions&1)
    p=base.accepted('preview')&bool(decisions&2)
    ids=base.records[m|p,0]
    modes=m[m|p].astype(np.uint32)|(p[m|p].astype(np.uint32)<<1)
    if not np.array_equal(geometry[:,0],ids) or not np.array_equal(geometry[:,7]&3,modes):
        raise PFO3Error('R35 PL geometry membership/frame gate mismatch')
    flags=geometry[:,7]
    reason=(flags>>16)&255
    if np.any(flags&0xff00fefc) or np.any(reason>9) or np.any(((flags&256)!=0)!=(reason==0)):
        raise PFO3Error('R35 invalid geometry status')
    if np.any(geometry[reason!=0,1:7]!=0):raise PFO3Error('R35 invalid coordinates must be zero')
    if int(moments[:,0].sum(dtype=np.uint64))!=int(w[-2]):raise PFO3Error('R35 moments count mismatch')
    if np.any(moments[:,1]!=0):raise PFO3Error('R35 bin overflow')
    # Immutable backing prevents a later consumer re-enabling writeability.
    geometry=np.frombuffer(geometry.astype('<u4').tobytes(),'<u4').reshape(-1,8)
    moments=np.frombuffer(moments.astype('<u4').tobytes(),'<u4').reshape(-1,32)
    frame=HardwareFrame(base,geometry,moments,meta[0])
    receipt.update(protocol='R35_PGO1',primary_stream_sha256=hashlib.sha256(raw).hexdigest(),
        primary_bytes=len(raw),geometry_records=len(geometry),moment_bins=len(moments),
        geometry_epoch=frame.geometry_epoch,geometry_sha256=frame.geometry_sha256,
        ARM_geometry_calls=0,geometry_backend='ACTUAL_PL_FIXED_Q30_Q16')
    return (frame,receipt,result[2]) if wantseal else (frame,receipt)
