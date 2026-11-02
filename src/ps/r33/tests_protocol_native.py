"""Native/reference differential coverage. Synthetic streams are not board proof."""
from concurrent.futures import ThreadPoolExecutor
import ctypes
import hashlib
import os
from pathlib import Path
import struct
import unittest
from unittest.mock import patch
import numpy as np
from .protocol import (HEADER_FIELDS, FINGERPRINT, identity,
                       assemble_r33 as assemble, assemble_reference, PFO3Error)
from .assemble_native import load_native

ROOT = Path(__file__).resolve().parents[2]


def synthetic(per_row=0, qualified=False):
    """All rejected or qualified-but-branch-rejected, including valid overflow."""
    n = per_row*480
    locals_ = np.zeros((n,17),dtype='<u4')
    locals_[:,0]=0x33520000
    locals_[:,1]=np.arange(n)
    locals_[:,2]=np.repeat(np.arange(480),per_row)
    locals_[:,3]=np.tile(np.arange(per_row)*2,480)
    locals_[:,4]=locals_[:,3]
    locals_[:,5]=locals_[:,3]<<12
    locals_[:,6]=locals_[:,2]<<12
    locals_[:,14]=(1<<24) if qualified else 1
    rows=np.zeros((480,17),dtype='<u4');rows[:,0]=0x33520002;rows[:,1]=np.arange(480)
    rows[:,5]=per_row;rows[:,6]=per_row if qualified else 0
    if qualified:
        pids=np.flatnonzero(np.tile(np.arange(per_row)<16,480))
        patches=locals_[pids].copy();patches[:,0]=0x33520001
    else:patches=np.empty((0,17),dtype='<u4')
    h={k:0 for k in HEADER_FIELDS}
    h.update(frame_id=7,source=1,epoch=33,cfg_version=1,width=752,height=480,
             roi_x1=752,roi_y1=480,roi_pixels=752*480,algorithm_revision=33,
             fixed_revision=1,policy_revision=1)
    for i,v in enumerate(struct.unpack('<8I',bytes.fromhex(FINGERPRINT))):h['fingerprint'+str(i)]=v
    start=np.array([h[k] for k in HEADER_FIELDS],dtype='<u4')
    start[:5]=[0x33544850,33,752,480,8]
    e=np.zeros(32,dtype='<u4');e[:4]=[0x33544650,33,n,n if qualified else 0]
    overflow=qualified and per_row>16
    e[11]=11 if overflow else 1;e[16]=752*480;e[23]=8;e[27]=7 if overflow else 3
    e[24]=96+17*(len(locals_)+len(patches)+480)
    return start.tobytes()+locals_.tobytes()+patches.tobytes()+rows.tobytes()+e.tobytes(),identity(h)


def stream_identity(data):
    return identity(dict(zip(HEADER_FIELDS,struct.unpack_from('<64I',data))))


def edit(data,index,value):
    data=bytearray(data);struct.pack_into('<I',data,index*4,value&0xffffffff);return bytes(data)


class NativeProtocolTests(unittest.TestCase):
    def equivalent(self,data,expected=None):
        expected=expected or stream_identity(data)
        results=[]
        for f in (assemble_reference,assemble):
            try:
                frame,receipt=f(data,expected_identity=expected)
                results.append(('ok',frame.payload,receipt))
            except PFO3Error as e:results.append(('error',str(e)))
        self.assertEqual(results[0][:2],results[1][:2])
        if results[0][0]=='ok':
            self.assertEqual(results[1][2]['assembly_backend'],'R33_STRICT_NATIVE_BODY_V1')
            self.assertEqual(len(results[1][2]['native_assembler_sha256']),64)
        return results[0]

    def test_actual_wrapper_empty_and_t05(self):
        for name in ('empty','T05_000'):
            with self.subTest(name=name):
                self.assertEqual(self.equivalent((ROOT/'pl/wrapper_tests/evidence02'/f'{name}.primary').read_bytes())[0],'ok')

    def test_patch_cannot_enable_invalid_center_in_actual_t05(self):
        data=(ROOT/'pl/wrapper_tests/evidence02/T05_000.primary').read_bytes()
        ident=stream_identity(data)
        a=np.frombuffer(data,'<u4')[64:-32].reshape(-1,17)
        patch_index=np.flatnonzero(((a[:,0]&3)==1)&((a[:,14]&(1<<25))!=0))[0]
        candidate_id=int(a[patch_index,1])
        local_index=np.flatnonzero(((a[:,0]&3)==0)&(a[:,1]==candidate_id))[0]
        # Local temporarily invalid, but unchanged valid+accepted PL patch is later applied.
        base=edit(data,64+17*int(local_index)+14,int(a[local_index,14])&~(1<<24))
        row=int(a[local_index,2])
        for field,value in ((5,0xffffffff),(6,0xffffffff),(6,(row<<12)+2049)):
            with self.subTest(field=field,value=value):
                changed=edit(base,64+17*int(local_index)+field,value)
                self.assertEqual(self.equivalent(changed,ident),('error','two dimensional center outside contract'))

    def test_valid_empty_16_and_17_slots(self):
        for p,q in ((0,False),(16,True),(17,True),(376,False)):
            with self.subTest(per_row=p,qualified=q):
                data,ident=synthetic(p,q)
                out=self.equivalent(data,ident)
                self.assertEqual(out[0],'ok')
                self.assertEqual(len(out[1]),256+64*(p*480+480))

    def test_header_errors(self):
        data,ident=synthetic(1)
        cases=[data[:30],data[:-1],edit(data,0,0),edit(data,4,100),
               edit(data,len(data)//4-32+17,5),edit(data,HEADER_FIELDS.index('algorithm_revision'),34),
               edit(data,HEADER_FIELDS.index('fingerprint0'),0),edit(data,2,751),
               edit(data,HEADER_FIELDS.index('roi_x0'),1),edit(data,len(data)//4-32+16,0)]
        for i,case in enumerate(cases):
            with self.subTest(i=i):self.assertEqual(self.equivalent(case,ident)[0],'error')
        bad=dict(ident);bad['frame_id']+=1
        self.assertEqual(self.equivalent(data,bad)[0],'error')

    def test_body_errors(self):
        data,ident=synthetic(1,True)
        words=len(data)//4;footer=words-32;n=480
        row=64+17*(n+n);patch0=64+17*n
        cases={
            'tag':(64,0), 'count':(footer+2,n+1), 'id':(65,1),
            'row_reserved':(row+15,1), 'geometry':(64+2,480),
            'uv':(64+5,752<<12), 'qualified':(footer+3,0),
            'duplicate_patch':(patch0+17+1,0), 'foreign_patch':(patch0+1,n),
            'patch_reject':(patch0+14,1), 'flags':(footer+11,0),
            'accept_without_preview':(patch0+14,(1<<24)|(1<<25)),
            'reserved_decision':(patch0+14,1<<31),
            'accepted_reason':(patch0+14,(7<<24)|256),
            'summary':(footer+4,1), 'row_summary':(row+3,1),
            'per_row_candidates':(row+5,0), 'per_row_qualified':(row+6,0),
            'per_row_accept':(row+7,1), 'overflow':(footer+11,11),
            'quality_decision':(footer+25,1), 'quality_flags':(footer+27,0)}
        for name,(idx,value) in cases.items():
            with self.subTest(name=name):self.assertEqual(self.equivalent(edit(data,idx,value),ident)[0],'error')
        # Swap row statistics without changing their totals, reaching per-row checks.
        d=edit(edit(data,row+5,0),row+17+5,2)
        self.assertIn('per-row candidate',self.equivalent(d,ident)[1])
        d=edit(edit(data,row+6,0),row+17+6,2)
        self.assertIn('per-row qualified',self.equivalent(d,ident)[1])

    def test_deterministic_corruptions(self):
        data,ident=synthetic(2,True);rng=np.random.default_rng(3301)
        for i in range(160):
            index=int(rng.integers(len(data)//4));old=struct.unpack_from('<I',data,index*4)[0]
            with self.subTest(i=i):self.equivalent(edit(data,index,old^(1<<int(rng.integers(32)))),ident)

    def test_native_direct_length_and_capacity(self):
        lib,_=load_native();stats=(ctypes.c_uint32*3)();out=(ctypes.c_uint32*16)()
        self.assertEqual(lib.r33_body(ctypes.c_char_p(b''),0,out,64,stats),90)
        data,_=synthetic()
        self.assertEqual(lib.r33_body(ctypes.c_char_p(data),len(data),out,64,stats),91)

    def test_missing_native_is_explicit_and_reference_works(self):
        data,ident=synthetic()
        with patch.dict(os.environ,{'R33_NATIVE_ASSEMBLER':str(ROOT/'missing_native_library')}):
            with self.assertRaisesRegex(PFO3Error,'required'):assemble(data,expected_identity=ident)
            self.assertEqual(assemble(data,expected_identity=ident,backend='reference')[0].header['diagnostic_count'],0)

    def test_independent_immutable_concurrent_results(self):
        data,ident=synthetic(16,True)
        frozen=assemble(data,expected_identity=ident)[0]
        digest=hashlib.sha256(frozen.payload).hexdigest()
        def run(_):return hashlib.sha256(assemble(data,expected_identity=ident)[0].payload).hexdigest()
        with ThreadPoolExecutor(max_workers=4) as pool:self.assertEqual(set(pool.map(run,range(80))),{digest})
        self.assertEqual(hashlib.sha256(frozen.payload).hexdigest(),digest)
        self.assertFalse(frozen.records.flags.writeable)


if __name__=='__main__':unittest.main()
