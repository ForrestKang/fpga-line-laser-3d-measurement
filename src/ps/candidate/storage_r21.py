"""MBR1 image observations and v3 lossless chunk persistence, never a PL ABI."""
from __future__ import annotations

from dataclasses import fields, is_dataclass, asdict
from functools import lru_cache
import hashlib
import io
import json
import math
import queue
import shutil
from pathlib import Path
import struct
import sys
import threading
import time
from types import MappingProxyType
import zlib

import numpy as np

from structured_light.scan3d.stream_wait_r20 import CapacityWaitWriter
from structured_light.scan3d.stream_writer import WriteItem, _json_line, _timeout, WriterFailure
from resource_policy import StorageBudget, accounted_file_bytes

from .storage_seal_r26 import (ADMISSION_KEY, AdmissionSealR26,
    PointBindingR26, SourceBindingR26, _attach_mbr1, _issue_admission,
    _new_mbr1, is_sealed, open_mbr1, verify_mbr1_digest)

SCHEMA = 'scan3d_multibranch_chunk_v3'
RAW_BYTES = 752*480
MAX_POINTS = 7680
_SOURCE_PAYLOADS=('source_profile_payload','source_pmc2_payload','source_pfo3_payload','source_pfo3_primary',
                  'source_cpu_v3_npz','source_cpu_v3_manifest','source_r30_frame_aux')
_POINTS=('points_camera_mm','points_axis_mm')
_ARRAYS = ('rows','center_q12','stripe_accepted','valid_points','branch_ids',
           'points_camera_mm','points_axis_mm')


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate JSON key: '+key)
        result[key]=value
    return result


def _json(data):
    return json.loads(data,object_pairs_hook=_unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def _check_profile(result):
    n=len(result['rows'])
    if n>MAX_POINTS:raise ValueError('at most7680 qualified observations per frame; full diagnostics stored separately')
    for key in _ARRAYS:
        a=result[key]
        if a.shape!=((n,3) if key.startswith('points_') else (n,)):
            raise ValueError('unaligned MBR1 array: '+key)
        expected='b' if key in ('stripe_accepted','valid_points') else 'iu'
        if key.startswith('points_'):
            if a.dtype.kind!='f' or a.dtype.itemsize!=8: raise ValueError('XYZ requires float64')
        elif a.dtype.kind not in expected: raise ValueError('invalid MBR1 dtype: '+key)
    for key,value in result['observation_quality'].items():
        if value.shape!=(n,): raise ValueError('unaligned quality: '+key)
    for key in ('observation_reasons','reasons'):
        if len(result[key])!=n: raise ValueError('unaligned reasons')
    if n:
        rows=result['rows']
        if int(rows.min())<0 or int(rows.max())>=480: raise ValueError('invalid sensor row')
        centers=result['center_q12']
        if int(centers.min())<0 or int(centers.max())>=752*4096:
            raise ValueError('invalid Q12 center')


def _header_schema(header):
    """Validate the MBR1 header envelope exactly once for both decode paths."""
    if (header.get('schema')!='MBR1' or header.get('PL_wire') is not False
            or header.get('contract')!='PS_IMAGE_MULTIBRANCH_Q12_V1'):
        raise ValueError('invalid MBR1 schema')
    descriptors=header['arrays'];table=header['reason_table']
    if not isinstance(descriptors,dict) or not isinstance(table,list) or not table or table[0] is not None:
        raise ValueError('invalid MBR1 header')
    if any(not isinstance(s,str) for s in table[1:]) or len(set(table))!=len(table):
        raise ValueError('invalid MBR1 reason table')
    if not (set(_ARRAYS)|{'reasons_ids','observation_reasons_ids'}).issubset(descriptors):
        raise ValueError('missing MBR1 arrays')
    return descriptors,table


def _parse_header(payload):
    if not isinstance(payload,bytes) or len(payload)<8 or payload[:4]!=b'MBR1':
        raise ValueError('invalid MBR1 magic/length')
    length=struct.unpack_from('<I',payload,4)[0]
    if length<2 or length>4*1024*1024 or 8+length>len(payload): raise ValueError('invalid MBR1 header length')
    header=_json(payload[8:8+length])
    _header_schema(header)
    return header,length


_SAFE_DTYPES={s:np.dtype(s) for s in ('<i8','<f8','<i4','|b1','<u4','|u1','<i2','<u2','<f4')}


def _profile_from_header(payload,header,length,*,expand_reasons,views=None):
    """Build the read-only profile view; schema has already been checked."""
    descriptors=header['arrays'];table=header['reason_table']
    result=dict(header['metadata']);quality={};offset=0
    data_start=8+length
    blob=memoryview(payload)[data_start:]
    blob_len=len(blob)
    base=None if views is None else np.frombuffer(payload,dtype=np.uint8,count=0).ctypes.data
    items=descriptors.items()
    prev_off=-1
    for _,d in items:
        off=d.get('offset') if isinstance(d,dict) else None
        if type(off) is not int or off<prev_off:
            items=sorted(items,key=lambda item:item[1]['offset'])
            break
        prev_off=off
    for name,d in items:
        d_str=d['dtype']
        dtype=_SAFE_DTYPES.get(d_str)
        if dtype is None:
            dtype=np.dtype(d_str)
            if dtype.hasobject or dtype.kind not in 'biuf' or dtype.itemsize not in (1,2,4,8):
                raise ValueError('unsafe MBR1 dtype')
        shape=d['shape'];count=d['bytes']
        if type(shape) is not list:
            raise ValueError('invalid MBR1 array offset/length')
        ndim=len(shape)
        if ndim==1:
            s0=shape[0]
            valid_shape=(type(s0) is int and s0>=0 and type(count) is int and count==s0*dtype.itemsize)
            shape_tuple=(s0,)
        elif ndim==2:
            s0,s1=shape[0],shape[1]
            valid_shape=(type(s0) is int and type(s1) is int and s0>=0 and s1>=0 and type(count) is int and count==s0*s1*dtype.itemsize)
            shape_tuple=(s0,s1)
        else:
            valid_shape=(not any(type(v) is not int or v<0 for v in shape) and type(count) is int and count==math.prod(shape)*dtype.itemsize)
            shape_tuple=tuple(shape)
        if not valid_shape or type(d['offset']) is not int or d['offset']!=offset or offset+count>blob_len:
            raise ValueError('invalid MBR1 array offset/length')
        if views is None:
            a=np.frombuffer(payload,dtype=dtype,count=count//dtype.itemsize,
                            offset=data_start+offset).reshape(shape_tuple)
        else:
            a=views[name]
            if (type(a) is not np.ndarray or a.dtype!=dtype or a.shape!=shape_tuple
                    or a.nbytes!=count or a.flags.writeable or not a.flags.c_contiguous
                    or (count and a.ctypes.data!=base+data_start+offset)):
                raise ValueError('invalid sealed MBR1 array view')
        offset+=count
        if name.startswith('observation_quality/'): quality[name[20:]]=a
        elif name in _ARRAYS or name in ('reasons_ids','observation_reasons_ids'): result[name]=a
        else: raise ValueError('unknown MBR1 array')
    if offset!=blob_len: raise ValueError('trailing MBR1 bytes')
    result['observation_quality']=quality
    n_table=len(table)
    for key in ('observation_reasons','reasons'):
        ids=result.pop(key+'_ids')
        if ids.ndim!=1 or ids.dtype.kind not in 'iu' or (ids.size and (int(ids.min())<0 or int(ids.max())>=n_table)):
            raise ValueError('invalid MBR1 reason index')
        # The persistence validator needs aligned reason counts and checked IDs,
        # while the public reader still reconstructs every original reason.
        if expand_reasons:
            result[key]=(None,)*len(ids) if (n_table==1 or not ids.any()) else tuple(np.asarray(table,dtype=object)[ids].tolist())
        else:
            result[key]=ids
    if not expand_reasons:
        result['_storage_reason_table15']=table
    return result


def _check_encoded_views(payload,header,length,views):
    """Validate the exact bytes once; both the seal and the header use them."""
    descriptors,_=_header_schema(header)
    if set(views)!=set(descriptors): raise ValueError('sealed MBR1 array set mismatch')
    data_start=8+length
    base=np.frombuffer(payload,dtype=np.uint8,count=0).ctypes.data
    for name,d in descriptors.items():
        a=views[name]
        dtype=np.dtype(d['dtype']);shape=tuple(d['shape'])
        if (type(a) is not np.ndarray or a.dtype!=dtype or tuple(a.shape)!=shape
                or a.nbytes!=d['bytes'] or a.flags.writeable or not a.flags.c_contiguous
                or (a.nbytes and a.ctypes.data!=base+data_start+d['offset'])):
            raise ValueError('invalid sealed MBR1 array view')
    if data_start+sum(d['bytes'] for d in descriptors.values())!=len(payload):
        raise ValueError('sealed MBR1 data length mismatch')
    result=_profile_from_header(payload,header,length,expand_reasons=False,views=views)
    _check_profile(result)


def _byte_view(array):
    """Zero-copy byte view of an already C-contiguous numeric array."""
    if array.size==0: return memoryview(b'')
    return memoryview(array.view(np.uint8).reshape(-1))


def _npy_header(points):
    """The one NPY header representation written by the chunk writer."""
    # Cache only the immutable layout, never arrays, content hashes or authority.
    # The bounded cache cannot retain measured points or caller-owned buffers.
    descr=np.lib.format.dtype_to_descr(points.dtype)
    if isinstance(descr,str):
        return _npy_header_layout(descr,tuple(points.shape))
    return _make_npy_header(descr,tuple(points.shape))


@lru_cache(maxsize=128)
def _npy_header_layout(descr,shape):
    return _make_npy_header(descr,shape)


def _make_npy_header(descr,shape):
    buffer=io.BytesIO()
    np.lib.format.write_array_header_1_0(buffer,
        dict(descr=descr,fortran_order=False,shape=shape))
    return buffer.getvalue()


def encode_profile(profile, summary=None) -> bytes:
    """Preserve every candidate and numeric bit, including rejected/NaN XYZ.

    The returned object is the immutable MBR1 encoding itself (a bytes
    subclass).  It is assembled exactly once into one pre-sized buffer; its
    SHA-256 is updated while that buffer is filled, and its read-only array
    views are validated once before a private receipt is attached.  Callers
    that need plain bytes may call ``bytes(payload)``; that loses the seal and
    therefore takes the full strict decode/validation path.
    """
    arrays={key:np.asarray(getattr(profile,key)) for key in _ARRAYS}
    arrays.update({'observation_quality/'+key:np.asarray(value)
                   for key,value in profile.observation_quality.items()})
    table=[None]; lookup={None:0}
    obs_reasons=getattr(profile,'observation_reasons')
    fin_reasons=getattr(profile,'reasons')
    for key,reasons in (('observation_reasons',obs_reasons),('reasons',fin_reasons)):
        n_r=len(reasons)
        if key=='reasons' and reasons==obs_reasons:
            arrays['reasons_ids']=arrays['observation_reasons_ids']
            continue
        if reasons==(None,)*n_r:
            arrays[key+'_ids']=np.zeros(n_r,dtype='<i4')
            continue
        for reason in dict.fromkeys(reasons):
            if reason is not None and not isinstance(reason,str): raise ValueError('invalid reason')
            if reason not in lookup: lookup[reason]=len(table);table.append(reason)
        arrays[key+'_ids']=np.fromiter(map(lookup.__getitem__,reasons),dtype='<i4',count=n_r)
    excluded=set(_ARRAYS)|{'observation_quality','observation_reasons','reasons'}
    names=[f.name for f in fields(profile)] if is_dataclass(profile) else list(vars(profile))
    metadata={key:getattr(profile,key) for key in names if key not in excluded}
    summary=profile.summary() if summary is None else summary
    # FrameQuality contains JSON scalars and a rejection-count dict. Its values
    # are immediately serialized, so another recursive asdict/deepcopy is not
    # needed; the optional summary never replaces the original quality fields.
    metadata={key:({f.name:getattr(value,f.name) for f in fields(value)} if key=='quality'
                  else asdict(value)) if is_dataclass(value) else value
              for key,value in metadata.items()}
    metadata['summary']=summary
    descriptors={};contiguous={};offset=0
    for key,value in arrays.items():
        if value.dtype.hasobject or value.dtype.kind not in 'biuf': raise ValueError('unsupported MBR1 dtype')
        data=np.ascontiguousarray(value)
        nbytes=data.size*data.dtype.itemsize
        descriptors[key]=dict(dtype=data.dtype.str,shape=list(data.shape),offset=offset,bytes=nbytes)
        contiguous[key]=data;offset+=nbytes
    header_doc=dict(schema='MBR1',contract='PS_IMAGE_MULTIBRANCH_Q12_V1',
        PL_wire=False,arrays=descriptors,reason_table=table,metadata=metadata)
    header=_json_line(header_doc)
    header_end=8+len(header)
    buffer=bytearray(header_end+offset)
    view=memoryview(buffer)
    view[:4]=b'MBR1';struct.pack_into('<I',buffer,4,len(header))
    view[8:header_end]=header
    at=header_end
    for key,data in contiguous.items():
        data_view=memoryview(data).cast('B') if data.size else memoryview(b'')
        view[at:at+len(data_view)]=data_view
        at+=len(data_view)
    # Hash the copied bytes in one pass: the receipt always binds
    # exactly what the immutable encoding contains even if a caller races us.
    digest=hashlib.sha256(view)
    payload=_new_mbr1(buffer)
    if len(payload)!=len(buffer): raise ValueError('MBR1 assembly length changed')
    from .storage_encode_fast_r26 import build_encoded_views_once
    views=build_encoded_views_once(payload,header_doc,len(header))
    return _attach_mbr1(payload,sha256=digest.hexdigest(),header_length=len(header),
        header_bytes=header,arrays=MappingProxyType(views))


def decode_profile(payload: bytes) -> dict:
    return _decode_profile_checked(payload)


def _decode_profile_checked(payload: bytes, *, expand_reasons=True) -> dict:
    try:
        return _decode_profile(payload,expand_reasons=expand_reasons)
    except (KeyError,TypeError,IndexError,AttributeError,OverflowError) as exc:
        raise ValueError('malformed MBR1 packet') from exc


def _decode_profile(payload: bytes, *, expand_reasons=True) -> dict:
    header,length=_parse_header(payload)
    result=_profile_from_header(payload,header,length,expand_reasons=expand_reasons)
    _check_profile(result)
    return result


def _sealed_profile(payload,receipt,*,expand_reasons):
    """Read-only prevalidated profile view; the digest was verified at admission."""
    header=_json(receipt.header_bytes)
    descriptors,_=_header_schema(header)
    data_start=8+receipt.header_length
    if data_start>len(payload): raise ValueError('sealed MBR1 header exceeds payload')
    if set(receipt.arrays)!=set(descriptors): raise ValueError('sealed MBR1 array set changed')
    return _profile_from_header(payload,header,receipt.header_length,
        expand_reasons=expand_reasons,views=receipt.arrays)


def _profile_for_record(record,*,verify_digest=True):
    """Return (profile view, sealed receipt); None profile means no MBR1 body."""
    payload=record['profile_payload']
    if not isinstance(payload,bytes): raise ValueError('profile_payload must be immutable bytes')
    if not payload.startswith(b'MBR1'):
        if any(key in record for key in ('source_cpu_v3_npz','source_cpu_v3_manifest')):
            raise ValueError('CPU takeover sources require an MBR1 observation record')
        _prestart_profile(record);return None,None
    expand_reasons=any(key in record for key in ('source_cpu_v3_npz','source_cpu_v3_manifest',
                                                 'source_pfo3_payload','source_pfo3_primary'))
    if (expand_reasons and not any(key in record for key in ('source_cpu_v3_npz','source_cpu_v3_manifest'))
            and any(key in record for key in ('source_pfo3_payload','source_pfo3_primary'))):
        from .storage_native15 import available
        expand_reasons=not available()
    if is_sealed(payload):
        # Admission verifies the assembly digest once; the writer worker reuses
        # the resulting admission receipt and never decodes these bytes again.
        receipt=verify_mbr1_digest(payload) if verify_digest else open_mbr1(payload)
        return _sealed_profile(payload,receipt,expand_reasons=expand_reasons),receipt
    return _decode_profile_checked(payload,expand_reasons=expand_reasons),None


def _validate_against_profile(record,profile):
    if (profile['request_id']!=record['request_id'] or
            profile['capture_epoch']!=record['capture_epoch']):
        raise ValueError('MBR1 frame identity mismatch')
    from candidate.pfo3_persistence_r25 import validate_sources
    validate_sources(record,profile)
    for key in _POINTS:
        points=record[key]
        if points is None: continue
        from .storage_native15 import compare_points
        if compare_points(profile,points,key):continue
        mask=profile['valid_points'].copy()
        if key=='points_axis_mm': mask &= np.isfinite(profile[key]).all(axis=1)
        expected=profile[key][mask]
        if (points.dtype!=expected.dtype or points.shape!=expected.shape
                or not _point_bytes_equal(points,expected)):
            raise ValueError('XYZ delta differs from MBR1 valid observations')


def _point_bytes_equal(left,right):
    """Bitwise compare, including signed zero, without two temporary bytes."""
    left=np.ascontiguousarray(left);right=np.ascontiguousarray(right)
    return bool(np.array_equal(left.view(np.uint8).reshape(-1),
                               right.view(np.uint8).reshape(-1)))


def _build_admission(record,receipt):
    raw=record['raw']
    completion=record.get('_pfo3_completion')
    from .pfo3_processor_r25 import PLFrameAdmission
    has_valid_completion=(type(completion) is PLFrameAdmission and not completion.fixture_only)
    sources=[]
    for key in _SOURCE_PAYLOADS:
        data=record.get(key)
        if data is None: continue
        if (has_valid_completion and key=='source_pfo3_payload'
                and completion._validated_frame is not None
                and data is completion._validated_frame.payload
                and len(completion.stream_sha256)==64):
            sha=completion.stream_sha256
        elif (has_valid_completion and key=='source_pfo3_primary'
                and data is completion._validated_primary
                and len(completion._primary_sha256)==64):
            sha=completion._primary_sha256
        else:
            sha=hashlib.sha256(data).hexdigest()
        sources.append(SourceBindingR26(key,data,len(data),sha))
    points=[]
    for key in _POINTS:
        array=record.get(key)
        if array is None:
            points.append(PointBindingR26(key,None,'',(),(),0,0,b'',0,''))
            continue
        header=_npy_header(array)
        digest=hashlib.sha256(header)
        digest.update(_byte_view(array))
        points.append(PointBindingR26(key,array,array.dtype.str,tuple(array.shape),
            tuple(array.strides),array.ctypes.data,array.nbytes,header,
            len(header)+array.nbytes,digest.hexdigest()))
    return _issue_admission(request_id=record['request_id'],capture_epoch=record['capture_epoch'],
        raw=raw,raw_sha256=record['raw_sha256'],profile=record['profile_payload'],
        profile_sha256=receipt.sha256,source_keys=_SOURCE_PAYLOADS,sources=sources,
        point_keys=_POINTS,points=points,completion=record.get('_pfo3_completion'))


def _admission_digests(record):
    seal=record.get(ADMISSION_KEY)
    if seal is None:return None
    if type(seal) is not AdmissionSealR26: raise ValueError('invalid storage admission seal')
    return seal.check(record)


def _profile_digest(payload):
    if is_sealed(payload): return open_mbr1(payload).sha256
    return hashlib.sha256(payload).hexdigest()


def _snapshot_points(points,key):
    if points is None: return None
    if not isinstance(points,np.ndarray): raise TypeError(f'{key} must be a numpy array or None')
    if points.dtype.hasobject or points.dtype.kind not in 'iuf':
        raise ValueError(f'{key} must have a non-object numeric dtype')
    if points.ndim!=2 or points.shape[1]!=3: raise ValueError(f'{key} must have shape (N, 3)')
    if len(points)>MAX_POINTS: raise ValueError('at most7680 multibranch XYZ points per frame')
    # OWNDATA + readonly does not prove ownership transfer: callers can toggle
    # WRITEABLE back on. Keep a detached private snapshot before caching its
    # digest; callers must never retain the array used by the writer thread.
    result=np.array(points,copy=True,order='C')
    result.setflags(write=False)
    if not np.isfinite(result).all(): raise ValueError(f'{key} contains non-finite coordinates')
    return result


def _validate_record(record):
    """Strict standalone validation entry used by offline readers and QA."""
    return MultiBranchChunkWriter._validate_record(record)


def _trace_compatible_group(group):
    """Give legacy trace hooks an exact-bytes MBR1 header view, not a mutation.

    FormalTraceR22 deliberately parses only the bounded MBR1 header with
    ``type(payload) is bytes``.  The persisted record stays sealed and
    unchanged; this one header-prefix copy exists only while a performance
    trace hook is attached.  Traces that ignore profile bytes are unaffected.
    """
    result=[]
    for item in group:
        payload=item.payload.get('profile_payload') if item.kind=='frame' else None
        if not is_sealed(payload):result.append(item);continue
        receipt=open_mbr1(payload)
        prefix=bytes(memoryview(payload)[:8+receipt.header_length])
        record=dict(item.payload);record['profile_payload']=prefix
        result.append(WriteItem(item.sequence,item.kind,record))
    return result


class MultiBranchChunkWriter(CapacityWaitWriter):
    """R20C durability order with MBR1 and up to 7680 valid XYZ per frame."""
    supports_multibranch_binary=True
    def __init__(self,output_dir,capacity=16,sink=None,*,group_size=4,max_delay_s=.05,
                 raw_codec='zlib1',byte_capacity=64*1024*1024,scratch_bytes=32*1024*1024,
                 storage_budget=None,data_root=None,validation_phase='submit',
                 preallocate_payload_bytes=0,preallocate_run_bound_bytes=0,
                 storage_backend='chunk_v3',journal_buffer_bytes=512*1024,capacity_limit=32):
        if storage_backend not in ('chunk_v3','journal15c'):
            raise ValueError('unknown storage backend')
        if storage_backend=='journal15c' and sink is not None:
            raise ValueError('journal15c requires default sink')
        if type(journal_buffer_bytes) is not int or not 0<=journal_buffer_bytes<=2*1024*1024:
            raise ValueError('journal buffer capacity must be 0..2MiB')
        self.journal_buffer_bytes=journal_buffer_bytes
        self.storage_backend=storage_backend;self._journal15c=None
        if validation_phase not in ('submit','worker'):
            raise ValueError('validation phase must be submit or worker')
        if validation_phase=='worker' and sink is not None:
            raise ValueError('worker validation requires the default atomic batch sink')
        if (type(preallocate_payload_bytes) is not int or type(preallocate_run_bound_bytes) is not int
                or not 0<=preallocate_payload_bytes<=preallocate_run_bound_bytes):
            raise ValueError('KEEP_SIZE extent must fit explicit run storage bound')
        if preallocate_payload_bytes and sink is not None:
            raise ValueError('KEEP_SIZE requires the default payload sink')
        self._preallocate_bytes=preallocate_payload_bytes
        self._preallocate_run_bound=preallocate_run_bound_bytes
        self._preallocate_ready=threading.Event();self._preallocate_error=None
        self.payload_preallocation=dict(enabled=bool(preallocate_payload_bytes),status='pending')
        self.validation_phase=validation_phase
        if type(capacity_limit) is not int or capacity_limit not in (32,128):raise ValueError('explicit supported capacity limit required')
        if type(capacity) is not int or not 1<=capacity<=capacity_limit: raise ValueError('capacity exceeds explicit limit')
        if type(group_size) is not int or not 1<=group_size<=32: raise ValueError('group_size must be 1..32')
        if raw_codec not in ('none','zlib1'): raise ValueError('raw codec must be none or zlib1')
        if type(byte_capacity) is not int or not 1<=byte_capacity<=64*1024*1024:
            raise ValueError('byte_capacity must be 1..64MiB')
        if type(scratch_bytes) is not int or not 1<=scratch_bytes<=32*1024*1024:
            raise ValueError('scratch_bytes must be 1..32MiB')
        self.raw_codec=raw_codec;self.byte_capacity=byte_capacity;self.scratch_bytes=scratch_bytes
        self._byte_tickets={};self.peak_pending_bytes=0;self.peak_pending_items=0
        managed_root=Path(data_root) if data_root is not None else Path(output_dir).parent
        if storage_budget is None:
            existing=sum(accounted_file_bytes(path.stat()) for path in managed_root.rglob('*') if path.is_file()) if managed_root.exists() else 0
            storage_budget=StorageBudget(existing,lambda:shutil.disk_usage(self.output_dir).free)
        self.storage_budget=storage_budget
        self._storage_tickets={};self._storage_actual={}
        self.performance_trace=None
        # Honest split of the one-time sealed admission validation (submitter
        # thread) from the worker's per-batch seal/full validation timing.
        self._admission_validation_s=0.;self._admission_validations=0
        if sink is not None and not callable(sink): raise TypeError('sink must be callable')
        if sink is not None:
            original_sink=sink
            def sink(path,item):
                if item.kind=='frame': self._validate_record(item.payload)
                return original_sink(path,item)
        super().__init__(output_dir,capacity=capacity,sink=sink,
                         group_size=group_size,max_delay_s=max_delay_s)

    def _open_storage(self,stack):
        if self.storage_backend=='journal15c':
            from .storage_journal15c import JournalSink15C,NAME
            stream=stack.enter_context((self.output_dir/NAME).open('x+b',buffering=0))
            self._journal15c=JournalSink15C(stream,buffer_bytes=min(self.journal_buffer_bytes,self.scratch_bytes//8))
            return self._journal15c.streams
        return tuple(stack.enter_context((self.output_dir/name).open('xb'))
                     for name in ('payloads.bin','frames.jsonl','events.jsonl'))

    @staticmethod
    def _sync(stream):
        journal=getattr(stream,'journal',None)
        if journal is not None:return journal.sync(stream)
        return CapacityWaitWriter._sync(stream)

    def _prepare_storage(self):
        from .storage_prealloc15b import prepare_payload
        try:
            if self._journal15c is not None:
                if self._preallocate_bytes:
                    from .storage_journal_prealloc_r28 import prepare_journal
                    self.payload_preallocation=prepare_journal(
                        self._journal15c,self.storage_budget,
                        self._preallocate_bytes,self._preallocate_run_bound)
                else:
                    self.payload_preallocation=dict(enabled=False,status='disabled',storage_backend='journal15c')
            else:
                self.payload_preallocation=prepare_payload(self._streams[0],self.storage_budget,
                    self._preallocate_bytes,self._preallocate_run_bound)
        except BaseException as exc:
            self.payload_preallocation=getattr(exc,'preallocation_report',
                dict(enabled=bool(self._preallocate_bytes),status='failed',
                     error=type(exc).__name__+': '+str(exc)))
            self._preallocate_error=exc
            raise
        finally:self._preallocate_ready.set()

    def wait_payload_preallocation(self,timeout_s=10.):
        if not self._preallocate_ready.wait(_timeout(timeout_s)):
            raise TimeoutError('payload initialization not confirmed; do not start producer')
        if self._preallocate_error is not None:raise self._preallocate_error
        return dict(self.payload_preallocation)

    @staticmethod
    def _resident_bytes(value):
        # Conservative charge includes Python containers and ndarray ownership.
        t=type(value)
        if t in (int,str,float,bool,bytes,type(None)):
            return sys.getsizeof(value)
        if t is np.ndarray:
            return sys.getsizeof(value)+value.nbytes
        if t is dict or isinstance(value,dict):
            total=sys.getsizeof(value)
            for k,v in value.items():
                total+=sys.getsizeof(k)
                tv=type(v)
                if tv in (int,str,float,bool,bytes,type(None)):
                    total+=sys.getsizeof(v)
                elif tv is np.ndarray:
                    total+=sys.getsizeof(v)+v.nbytes
                else:
                    total+=MultiBranchChunkWriter._resident_bytes(v)
            return total
        if t in (tuple,list) or isinstance(value,(tuple,list)):
            total=sys.getsizeof(value)
            for v in value:
                tv=type(v)
                if tv in (int,str,float,bool,bytes,type(None)):
                    total+=sys.getsizeof(v)
                elif tv is np.ndarray:
                    total+=sys.getsizeof(v)+v.nbytes
                else:
                    total+=MultiBranchChunkWriter._resident_bytes(v)
            return total
        # Charge every ndarray's bytes, including owned arrays.  The previous
        # base-only condition under-counted the exactly-owned point snapshots
        # and could admit more than the declared 64 MiB queue.  The capacity
        # remains 64 MiB; honest accounting makes the writer wait earlier.
        return sys.getsizeof(value)+(value.nbytes if isinstance(value,np.ndarray) else 0)

    def _pending_bytes_locked(self):
        for sequence in tuple(self._byte_tickets):
            if sequence<=self._written:
                del self._byte_tickets[sequence]
                token,bound=self._storage_tickets.pop(sequence)
                # Default sink reports exact fsynced payload + index bytes.
                # A custom sink has no size receipt, so retains a conservative
                # bound rather than inventing actual filesystem usage.
                actual=self._storage_actual.pop(sequence,bound)
                self.storage_budget.commit(token,actual)
        return sum(self._byte_tickets.values())

    @property
    def pending_bytes(self):
        with self._condition:return self._pending_bytes_locked()

    def _enqueue(self,kind,payload):
        if self._preallocate_bytes:
            if not self._preallocate_ready.is_set():raise RuntimeError('wait for payload preallocation before acceptance')
            if self._preallocate_error is not None:raise self._preallocate_error
        charge=self._resident_bytes(payload)+256
        if charge>self.byte_capacity or charge>self.scratch_bytes:
            raise ValueError('single persistence snapshot exceeds byte/scratch budget')
        with self._condition:
            if self._worker_error is not None:
                raise WriterFailure(self._error_text,self._receipt_locked())
            if not self._accepting or self._worker_stopped:raise RuntimeError('writer is closed')
            pending=self._pending_bytes_locked()
            if pending+charge>self.byte_capacity:raise BufferError('persistence byte capacity full; not accepted')
            self._check_enqueue_locked(kind,payload)
            # Raw zlib worst-case expansion, NPY headers and bounded indexes,
            # plus 25% headroom. Container charge overestimates persisted data.
            bound=(charge+8192)*5//4+1
            token=self.storage_budget.reserve(bound)
            try:sequence=super()._enqueue(kind,payload)
            except BaseException:
                self.storage_budget.cancel_unwritten(token);raise
            self._byte_tickets[sequence]=charge
            self._storage_tickets[sequence]=(token,bound)
            self.peak_pending_bytes=max(self.peak_pending_bytes,pending+charge)
            self.peak_pending_items=max(self.peak_pending_items,self._accepted-self._written)
            return sequence

    def submit_wait(self,record,timeout_s=30.):
        deadline=time.monotonic()+_timeout(timeout_s)
        started=time.perf_counter();cpu=time.thread_time()
        snapshot=self._snapshot_record(record);waited=False
        self.last_admission_timings=dict(snapshot=dict(wall_s=time.perf_counter()-started,
            thread_cpu_s=time.thread_time()-cpu),queue_accept=dict(wall_s=0.,thread_cpu_s=0.),
            backpressure_wait=dict(wall_s=0.,thread_cpu_s=0.))
        with self._condition:
            while True:
                started=time.perf_counter();cpu=time.thread_time()
                try:
                    self._enqueue('frame',snapshot)
                    return waited
                except BufferError:
                    waited=True;remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError('bounded persistence capacity wait expired; frame was not accepted')
                finally:
                    self.last_admission_timings['queue_accept']['wall_s']+=time.perf_counter()-started
                    self.last_admission_timings['queue_accept']['thread_cpu_s']+=time.thread_time()-cpu
                started=time.perf_counter();cpu=time.thread_time()
                try:
                    self._condition.wait(remaining)
                finally:
                    self.last_admission_timings['backpressure_wait']['wall_s']+=time.perf_counter()-started
                    self.last_admission_timings['backpressure_wait']['thread_cpu_s']+=time.thread_time()-cpu

    def wait_for_capacity(self,timeout_s=5.):
        deadline=time.monotonic()+_timeout(timeout_s)
        with self._condition:
            while True:
                if self._worker_error is not None:raise WriterFailure(self._error_text,self._receipt_locked())
                if not self._accepting or self._worker_stopped:raise RuntimeError('writer is closed')
                if self._accepted-self._written<self._capacity and self._pending_bytes_locked()<self.byte_capacity:return True
                remaining=deadline-time.monotonic()
                if remaining<=0:return False
                self._condition.wait(remaining)

    IO_KEYS=('payload_fsync_s','index_fsync_s','io_s','thread_cpu_s','validation_s',
             'raw_compress_s','payload_build_write_s','payload_bytes','raw_input_bytes',
             'raw_stored_bytes','payload_write_calls','journal_fsync_s',
             'journal_finalize_s','fsync_calls')

    def _account_batches(self):
        """Aggregate the per-batch sink timings that were already recorded.

        The sink has always measured payload fsync, index fsync, validation and
        compression inside the worker; this only surfaces them, so a drain stall
        can be attributed to fsync or to the queue from evidence instead of one
        wall time. No write, ordering or durability behaviour changes.
        """
        stats=self.batch_stats
        totals={key:float(sum(float(s.get(key,0.) or 0.) for s in stats)) for key in self.IO_KEYS}
        def q(key,fraction):
            values=sorted(float(s.get(key,0.) or 0.) for s in stats)
            if not values:return None
            return values[min(len(values)-1,int(fraction*(len(values)-1)))]
        frames=int(getattr(self,'_written',0))
        return dict(batches=len(stats),frames=frames,totals=totals,
            batches_completed=self.batch_count,
            batches_omitted=max(0,self.batch_count-len(stats)),
            timing_scope='last_1024_successful_batches',
            persistence_timeline_summary=self.persistence_timeline_summary,
            stage_quantiles={key:dict(p50_s=q(key,.5),p95_s=q(key,.95),p99_s=q(key,.99),max_s=q(key,1.))
                             for key in self.IO_KEYS+('accepted_to_worker_max_s','accepted_to_durable_max_s')},
            per_batch=dict(payload_fsync_p50_s=q('payload_fsync_s',.5),
                           payload_fsync_max_s=q('payload_fsync_s',1.),
                           index_fsync_p50_s=q('index_fsync_s',.5),
                           index_fsync_max_s=q('index_fsync_s',1.),
                           io_p50_s=q('io_s',.5),io_max_s=q('io_s',1.)),
            mean_payload_fsync_s=(None if not stats else totals['payload_fsync_s']/len(stats)),
            mean_index_fsync_s=(None if not stats else totals['index_fsync_s']/len(stats)),
            admission_validation_s=float(getattr(self,'_admission_validation_s',0.)),
            admission_validations=int(getattr(self,'_admission_validations',0)),
            validation_phase=getattr(self,'validation_phase','submit'),
            note=('timings are sink measurements taken inside the writer worker; '
                  'validation_s is worker seal/full-record validation and '
                  'admission_validation_s is the one-time sealed admission validation '
                  'performed on the submitter thread before enqueue'))

    def close(self,timeout_s=5.):
        receipt=super().close(timeout_s)
        with self._condition:self._pending_bytes_locked()
        self.io_accounting=self._account_batches()
        return receipt

    def _collect(self,first):
        group=[first];deadline=time.monotonic()+self._max_delay_s
        while len(group)<self._group_size:
            # Enqueue and close share this condition. Rechecking the queue
            # while holding it prevents close from splitting an already
            # admitted batch after an earlier empty observation.
            with self._condition:
                try:group.append(self._queue.get_nowait());continue
                except queue.Empty:pass
                if self._closing.is_set() or self._flush_target>self._written:break
                remaining=deadline-time.monotonic()
                if remaining<=0:break
                self._condition.wait(min(remaining,.01))
        return group

    def _snapshot_record(self,record):
        if self._resident_bytes(record)>self.scratch_bytes:
            raise ValueError('input record exceeds snapshot scratch budget')
        sources={key:record[key] for key in _SOURCE_PAYLOADS if key in record}
        if any(type(value) is not bytes for value in sources.values()):raise TypeError('source payloads must be immutable bytes')
        completion=record.get('_pfo3_completion')
        if completion is not None:
            from .pfo3_processor_r25 import PLFrameAdmission
            if type(completion) is not PLFrameAdmission:raise TypeError('transport completion capability required')
            completion.persistence_frame(record)
        storable={key:value for key,value in record.items()
            if key not in _SOURCE_PAYLOADS and key!='_pfo3_completion'}
        # Avoid the base class's first copy, then make exactly one detached
        # snapshot here. A caller-owned readonly array is not immutable storage.
        for key in _POINTS:
            if key in storable:storable[key]=None
        snapshot=super()._snapshot_record(storable)
        for key in _POINTS:snapshot[key]=_snapshot_points(record[key],key)
        snapshot.update(sources)
        if completion is not None:snapshot['_pfo3_completion']=completion
        if len(snapshot['raw'])!=RAW_BYTES: raise ValueError('native 752x480 raw required')
        if is_sealed(snapshot['profile_payload']):
            # Issuer/header/object binding remains synchronous in both modes.
            open_mbr1(snapshot['profile_payload'])
        if is_sealed(snapshot['profile_payload']) and getattr(self,'validation_phase','submit')=='submit':
            # The one strict cross-source/XYZ validation for a sealed record is
            # performed here, on the frozen snapshot, before enqueue.  The
            # worker later re-verifies only the exact object bindings, so the
            # durable writer never decodes or compares the same arrays again.
            started=time.perf_counter()
            profile,receipt=_profile_for_record(snapshot,verify_digest=True)
            _validate_against_profile(snapshot,profile)
            snapshot[ADMISSION_KEY]=_build_admission(snapshot,receipt)
            if not hasattr(self,'_admission_validation_s'):
                self._admission_validation_s=0.;self._admission_validations=0
            self._admission_validation_s+=time.perf_counter()-started
            self._admission_validations+=1
        return snapshot

    @staticmethod
    def _validate_record(record):
        # A sealed record's one strict validation already happened against the
        # frozen snapshot.  Re-verify every persisted binding, but never decode
        # the same MBR1 bytes or compare the same source/profile arrays again.
        if _admission_digests(record) is not None:return
        profile,_=_profile_for_record(record,verify_digest=True)
        if profile is None:return
        _validate_against_profile(record,profile)

    def _persist_group(self,group):
        result=self._persist_group_v3(group)
        if getattr(self,'_journal15c',None) is not None:
            # Existing admission allowance includes >8KiB/index overhead per
            # item. Charge the additional 128+16+64 transaction framing too.
            observed=self._journal15c.observed_file_bytes()
            with self._condition:
                self._storage_actual[group[0].sequence]+=208
                logical=sum(self._storage_actual[item.sequence] for item in group)
                # Account file-system block rounding conservatively. Never
                # release a pre-existing charge when a later write reuses it.
                target=self._journal15c.budget_accounted_bytes+logical
                excess=max(0,observed-target)
                self._storage_actual[group[0].sequence]+=excess
                self._journal15c.budget_accounted_bytes=target+excess
                for item in group:
                    if self._storage_actual[item.sequence]>self._storage_tickets[item.sequence][1]:
                        raise BufferError('journal actual allocation exceeds admitted storage bound')
            result.update(journal_file_accounted_bytes=observed,journal_block_extra_bytes=excess,
                          journal_budget_accounted_bytes=self._journal15c.budget_accounted_bytes)
            result.update(storage_backend='journal15c',**self._journal15c.metrics)
        else:result['storage_backend']='chunk_v3'
        return result

    def _persist_group_v3(self,group):
        # Validate the entire frozen batch before writing even its first byte.
        # The inherited worker reports any failure without advancing _written.
        self._persistence_phase(group, 'validate')
        validation_started=time.perf_counter();validation_cpu=time.thread_time()
        validated_bundles={}
        for item in group:
            if item.kind!='frame':continue
            record=item.payload
            if (getattr(self,'validation_phase','submit')=='worker'
                    and is_sealed(record['profile_payload']) and ADMISSION_KEY not in record):
                # Queue acceptance is not validation or durability. Validate
                # every detached snapshot before the first batch byte is written.
                # Any invalid item fails the whole group through WorkerFailure.
                profile,receipt=_profile_for_record(record,verify_digest=True)
                _validate_against_profile(record,profile)
                record[ADMISSION_KEY]=_build_admission(record,receipt)
            # Verify all retained object/descriptor bindings once in this
            # writer-owned batch. No record is returned to a producer between
            # this check and its write; disk readers remain fully independent.
            bundle=_admission_digests(record)
            if bundle is None:self._validate_record(record)
            validated_bundles[item.sequence]=bundle
        validation_s=time.perf_counter()-validation_started
        validation_thread_cpu_s=time.thread_time()-validation_cpu
        if getattr(self,'_journal15c',None) is not None:self._journal15c.begin(group)
        self._persistence_phase(group, 'build_write')
        build_started=time.perf_counter();build_cpu=time.thread_time()
        payload,frames,events=self._streams
        # Reuse at most 1MiB, within the existing scratch allowance. Legacy v3
        # syncs payload before index; journal v4 syncs both with its commit once.
        from .storage_blocks15 import PayloadBlocks15
        block_bytes=min(1024*1024,max(1,getattr(self,'scratch_bytes',32*1024*1024)//4))
        if not hasattr(self,'_payload_block_buffer') or len(self._payload_block_buffer)!=block_bytes:
            self._payload_block_buffer=bytearray(block_bytes)
        blocks=PayloadBlocks15(payload,self._payload_block_buffer)
        payload_start=payload.tell();frame_lines=[];event_lines=[]
        actual_bytes={}
        raw_compress_s=raw_compress_thread_cpu_s=0.;raw_input_bytes=raw_stored_bytes=0
        for item in group:
            common=dict(batch_first_sequence=group[0].sequence,batch_last_sequence=group[-1].sequence)
            if item.kind=='event':
                line=_json_line(dict(sequence=item.sequence,event=item.payload,**common))
                event_lines.append(line);actual_bytes[item.sequence]=len(line);continue
            record=item.payload
            # Admission receipts are reused only for the exact retained objects;
            # a plain/untrusted record has no receipt and takes the real hashes.
            bundle=validated_bundles[item.sequence]
            profile_payload=record['profile_payload']
            profile_digest=bundle.profile_sha256 if bundle is not None else _profile_digest(profile_payload)
            raw_digest=bundle.raw_sha256 if bundle is not None else record['raw_sha256']
            started=time.perf_counter();cpu_started=time.thread_time()
            item_start=blocks.tell()
            compressed=zlib.compress(record['raw'],level=1) if self.raw_codec=='zlib1' else record['raw']
            raw_compress_s+=time.perf_counter()-started
            raw_compress_thread_cpu_s+=time.thread_time()-cpu_started
            raw_input_bytes+=len(record['raw']);raw_stored_bytes+=len(compressed)
            raw_offset=blocks.tell();blocks.write(compressed)
            profile_offset=blocks.tell();blocks.write(profile_payload)
            source_payloads={}
            for key in _SOURCE_PAYLOADS:
                data=record.get(key)
                if data is not None:
                    digest=(bundle.source_sha256(key,data) if bundle is not None
                            else hashlib.sha256(data).hexdigest())
                    source_payloads[key]=dict(file='payloads.bin',offset=blocks.tell(),bytes=len(data),
                        sha256=digest)
                    blocks.write(data)
            point_files={}
            for key in _POINTS:
                points=record[key];point_files[key]=None
                if points is not None:
                    # Only the small NPY header is materialized; XYZ is already
                    # an owned contiguous snapshot and can be written directly.
                    if bundle is not None:
                        header,total_bytes,digest=bundle.point_file(key,points)
                    else:
                        header=_npy_header(points)
                        data=_byte_view(points)
                        hashed=hashlib.sha256(header);hashed.update(data)
                        total_bytes=len(header)+len(data);digest=hashed.hexdigest()
                    point_files[key]=dict(file='payloads.bin',offset=blocks.tell(),bytes=total_bytes,
                        sha256=digest,format='npy')
                    blocks.write(header)
                    blocks.write(_byte_view(points))
            frame_lines.append(_json_line(dict(kind='frame',sequence=item.sequence,storage_format=SCHEMA,
                request_id=record['request_id'],capture_epoch=record['capture_epoch'],
                raw_sha256=raw_digest,raw_codec=self.raw_codec,raw_stored_bytes=len(compressed),
                # The uncompressed bytes are the same immutable Raw already
                # independently verified by _snapshot_record before enqueue.
                # Readers still hash stored bytes independently from disk.
                raw_stored_sha256=(hashlib.sha256(compressed).hexdigest() if self.raw_codec=='zlib1'
                                   else raw_digest),
                profile_sha256=profile_digest,
                raw_bytes=len(record['raw']),profile_bytes=len(profile_payload),
                raw_file='payloads.bin',profile_file='payloads.bin',raw_offset=raw_offset,
                profile_offset=profile_offset,point_files=point_files,summary=record['summary'],
                source_payloads=source_payloads,
                motion=record['motion'],exposure=record['exposure'],**common)))
            actual_bytes[item.sequence]=blocks.tell()-item_start+len(frame_lines[-1])
        blocks.finish()
        payload_build_write_s=time.perf_counter()-build_started
        payload_build_write_thread_cpu_s=time.thread_time()-build_cpu
        self._persistence_phase(group, 'payload_sync')
        payload_fsync_s=self._sync(payload);index_fsync_s=0.
        self._persistence_phase(group, 'index_write_sync')
        if event_lines:
            events.write(b''.join(event_lines));index_fsync_s+=self._sync(events)
        commit=dict(kind='batch_commit',first_sequence=group[0].sequence,last_sequence=group[-1].sequence,
                    item_count=len(group),frame_count=len(frame_lines),event_count=len(event_lines),payload_end=payload.tell())
        commit_line=_json_line(commit)
        frames.write(b''.join(frame_lines)+commit_line);index_fsync_s+=self._sync(frames)
        actual_bytes[group[0].sequence]+=len(commit_line)
        with self._condition:self._storage_actual.update(actual_bytes)
        sync_done=time.perf_counter()
        if self.performance_trace is not None:
            self.performance_trace.durable_group(_trace_compatible_group(group),sync_done)
        return dict(payload_fsync_s=payload_fsync_s,index_fsync_s=index_fsync_s,
            validation_s=validation_s,validation_thread_cpu_s=validation_thread_cpu_s,
            payload_build_write_s=payload_build_write_s,payload_build_write_thread_cpu_s=payload_build_write_thread_cpu_s,
            payload_write_calls=blocks.write_calls,payload_block_bytes=block_bytes,
            payload_bytes=payload.tell()-payload_start,raw_compress_s=raw_compress_s,
            raw_compress_thread_cpu_s=raw_compress_thread_cpu_s,raw_input_bytes=raw_input_bytes,
            raw_stored_bytes=raw_stored_bytes,sync_done_perf_s=sync_done)


def _lines(path):
    with path.open('rb') as stream:
        while True:
            line=stream.readline(1024*1024+1)
            if not line:return
            if len(line)>1024*1024 or not line.endswith(b'\n'):raise ValueError('oversize/truncated JSONL index')
            yield _json(line)


def _prestart_profile(record):
    summary=record['summary']
    if (summary.get('reason')!='before_registered_motion_window' or summary.get('valid') is not False
            or summary.get('fused') is not False or summary.get('width')!=752 or summary.get('height')!=480
            or record['points_camera_mm'] is not None or record['points_axis_mm'] is not None):
        raise ValueError('only explicit native prestart observations may retain source PL payload')
    if record['profile_payload'].startswith(b'PMC2'):
        from candidate.pl_features_r22 import decode_packet
        meta=record.get('motion') or {}
        try:
            identity=dict(frame_id=record['request_id'],epoch=record['capture_epoch'],
                cfg_version=meta['feature_config_revision'],exposure_version=meta['exposure_revision'],
                motor_id=meta['motor_accepted_id'],motor_epoch=meta['motor_epoch'],
                motor_steps=meta['emitted_command_count'],timestamp=meta['fv_ticks'])
        except KeyError as exc:raise ValueError('PMC2 prestart motion/configuration snapshot missing') from exc
        packet=decode_packet(record['profile_payload'],expected_identity=identity,
                             expected_config={'width':752,'height':480})
        return dict(format='PMC2_PRESTART',request_id=record['request_id'],capture_epoch=record['capture_epoch'],
                    valid=False,fused=False,source_profile=packet)
    from structured_light.profile import decode_profile as decode_slp,encode_profile as encode_slp
    source=decode_slp(record['profile_payload'],frame_width=752,frame_height=480)
    if (source.format_version!=2 or source.profile_id!=record['request_id'] or source.error_flags!=0
            or encode_slp(source)!=record['profile_payload']):raise ValueError('invalid prestart SLP2 identity/error/layout')
    return dict(format='SLP2_PRESTART',request_id=record['request_id'],capture_epoch=record['capture_epoch'],
        valid=False,fused=False,source_profile=source)


def iter_records(output_dir):
    """Strictly audit a closed successful v3 stream; exhaust iterator for EOF audit."""
    root=Path(output_dir)
    if (root/'failures.jsonl').exists() and (root/'failures.jsonl').stat().st_size:
        raise ValueError('writer failure receipt: no durable success claim')
    from .storage_journal15c import JournalReader15C,NAME
    if (root/NAME).exists():
        if any((root/name).exists() for name in ('payloads.bin','frames.jsonl','events.jsonl')):
            raise ValueError('mixed authoritative storage backends')
        reader=JournalReader15C(root/NAME)
        with reader.payload_stream() as stream:
            yield from _iter_records_streams(reader.lines('frames'),iter(reader.lines('events')),
                                            stream,reader.logical_end)
    else:
        with (root/'payloads.bin').open('rb') as stream:
            yield from _iter_records_streams(_lines(root/'frames.jsonl'),iter(_lines(root/'events.jsonl')),
                                            stream,(root/'payloads.bin').stat().st_size)


def _iter_records_streams(frames,events,stream,size):
    next_event=next(events,None);last_event=0
    pending=[];expected=1;offset=0;last_request=-1;epoch=None
    def read_piece(at,count,digest):
        nonlocal offset
        if type(at) is not int or type(count) is not int or not 0<=count<=32*1024*1024 or at!=offset:
            raise ValueError('noncontiguous payload offset')
        data=stream.read(count)
        if len(data)!=count or hashlib.sha256(data).hexdigest()!=digest:
            raise ValueError('payload length/hash mismatch')
        offset+=count;return data
    for entry in frames:
        if entry.get('kind')=='frame':
            if len(pending)>=32:raise ValueError('batch frame capacity exceeded')
            if pending and entry['sequence']<=pending[-1]['sequence']: raise ValueError('frame index order mismatch')
            pending.append(entry);continue
        if entry.get('kind')!='batch_commit': raise ValueError('unknown index kind')
        first,last=entry['first_sequence'],entry['last_sequence']
        if (type(first) is not int or type(last) is not int or first!=expected or last<first
                or entry['item_count']!=last-first+1 or last-first>=32 or entry['frame_count']!=len(pending)):
            raise ValueError('invalid commit continuity/count')
        batch_events=[]
        while next_event is not None and next_event['sequence']<=last:
            if next_event['sequence']<first or next_event['sequence']<=last_event:
                raise ValueError('duplicate/out of order event')
            last_event=next_event['sequence'];batch_events.append(next_event);next_event=next(events,None)
            if len(batch_events)>32:raise ValueError('batch event capacity exceeded')
        if entry['event_count']!=len(batch_events): raise ValueError('commit event count mismatch')
        items=sorted(pending+batch_events,key=lambda e:e['sequence'])
        if [e['sequence'] for e in items]!=list(range(first,last+1)):
            raise ValueError('missing/repeated sequence')
        for meta in items:
            if meta['batch_first_sequence']!=first or meta['batch_last_sequence']!=last:
                raise ValueError('batch boundary mismatch')
            if 'event' in meta:
                yield dict(kind='event',event=meta['event'],metadata=meta);continue
            if (meta['storage_format']!=SCHEMA or meta['raw_codec'] not in ('zlib','zlib1','none')
                    or meta['raw_file']!='payloads.bin' or meta['profile_file']!='payloads.bin'
                    or meta['raw_bytes']!=RAW_BYTES): raise ValueError('unsupported frame schema')
            if meta['request_id']<=last_request or (epoch is not None and epoch!=meta['capture_epoch']):
                raise ValueError('frame identity ordering mismatch')
            last_request=meta['request_id'];epoch=meta['capture_epoch']
            compressed=read_piece(meta['raw_offset'],meta['raw_stored_bytes'],meta['raw_stored_sha256'])
            if meta['raw_codec']=='none':raw=compressed;codec_valid=True
            else:
                decoder=zlib.decompressobj();raw=decoder.decompress(compressed,RAW_BYTES+1)
                codec_valid=decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail
            if (len(raw)!=RAW_BYTES or not codec_valid or hashlib.sha256(raw).hexdigest()!=meta['raw_sha256']):
                raise ValueError('invalid zlib raw frame')
            packet=read_piece(meta['profile_offset'],meta['profile_bytes'],meta['profile_sha256'])
            if packet.startswith(b'MBR1'):profile=decode_profile(packet)
            else:
                if any(meta['point_files'][key] is not None for key in ('points_camera_mm','points_axis_mm')):
                    raise ValueError('prestart XYZ delta forbidden')
                profile=_prestart_profile(dict(profile_payload=packet,summary=meta['summary'],motion=meta.get('motion'),request_id=meta['request_id'],
                    capture_epoch=meta['capture_epoch'],points_camera_mm=None,points_axis_mm=None))
            if profile['request_id']!=meta['request_id'] or profile['capture_epoch']!=epoch:
                raise ValueError('profile identity mismatch')
            sources={}
            source_desc=meta.get('source_payloads',{})
            if set(source_desc)-set(_SOURCE_PAYLOADS):raise ValueError('unknown source payload descriptor')
            for key in _SOURCE_PAYLOADS:
                if key not in source_desc:continue
                desc=source_desc[key]
                if desc['file']!='payloads.bin':raise ValueError('invalid source payload descriptor')
                sources[key]=read_piece(desc['offset'],desc['bytes'],desc['sha256'])
            if 'source_r30_frame_aux' in sources:
                from .frame_persistence_r30 import decode_aux
                decode_aux(sources['source_r30_frame_aux'],request_id=meta['request_id'],epoch=epoch)
            points={}
            for key in ('points_camera_mm','points_axis_mm'):
                desc=meta['point_files'][key];points[key]=None
                if desc is None: continue
                if desc['file']!='payloads.bin' or desc['format']!='npy': raise ValueError('invalid XYZ file')
                buffer=io.BytesIO(read_piece(desc['offset'],desc['bytes'],desc['sha256']))
                a=np.load(buffer,allow_pickle=False)
                if (buffer.tell()!=desc['bytes'] or a.ndim!=2 or a.shape[1]!=3 or len(a)>MAX_POINTS
                        or a.dtype.kind not in 'iuf' or not np.isfinite(a).all()):
                    raise ValueError('invalid XYZ array/length')
                a.setflags(write=False);points[key]=a
                mask=profile['valid_points'].copy()
                if key=='points_axis_mm': mask &= np.isfinite(profile[key]).all(axis=1)
                expected_points=np.ascontiguousarray(profile[key][mask])
                if a.dtype!=expected_points.dtype or a.shape!=expected_points.shape or a.tobytes()!=expected_points.tobytes():
                    raise ValueError('XYZ delta differs from MBR1 valid observations')
            if (any(key in sources for key in ('source_cpu_v3_npz','source_cpu_v3_manifest',
                                              'source_pfo3_payload','source_pfo3_primary'))
                    or profile.get('frame_metadata',{}).get('processing_backend') in
                    ('PL_FINAL_V3','PS_V3_EXPLICIT_FALLBACK','MOCK_ONLY_PS_V3_EXPLICIT_FALLBACK')):
                from candidate.pfo3_persistence_r25 import validate_sources
                validate_sources(dict(request_id=meta['request_id'],capture_epoch=meta['capture_epoch'],
                    raw_sha256=meta['raw_sha256'],**sources),profile)
            yield dict(kind='frame',raw=raw,profile=profile,profile_payload=packet,
                       summary=meta['summary'],metadata=meta,**points,**sources)
        if entry['payload_end']!=offset: raise ValueError('commit payload watermark mismatch')
        pending=[];expected=last+1
    if pending or next_event is not None or offset!=size or stream.read(1): raise ValueError('uncommitted/trailing storage bytes')


def iter_recovered_records15c(output_dir):
    """Strict semantic audit of complete journal prefix; exhaust for EOF audit.

    This explicit recovery API may inspect a failed run. It never changes its
    failure status, acknowledges original durability, or includes a torn tail.
    """
    from .storage_journal15c import JournalReader15C,NAME
    root=Path(output_dir);reader=JournalReader15C(root/NAME,recover_incomplete_tail=True)
    with reader.payload_stream() as stream:
        for record in _iter_records_streams(reader.lines('frames'),iter(reader.lines('events')),
                                            stream,reader.logical_end):
            yield dict(record,recovery_status='VALIDATED_PREFIX_NOT_ORIGINAL_DURABLE_ACK')
