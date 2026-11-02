"""R30: one bounded owner and one commit for all per-frame evidence.

Preview and trace are versioned source payloads in the existing synced journal;
their familiar standalone files and cloud spool are derived AFTER drain. No
geometry/display caller writes those files. Old records remain readable.
"""
from __future__ import annotations
import copy
from dataclasses import fields, is_dataclass
from functools import lru_cache
import gc
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
from types import MappingProxyType
import numpy as np
from .storage_r21 import MultiBranchChunkWriter, encode_profile, iter_records
from .storage_seal_r26 import MBR1ReceiptR26, SealedMBR1
from structured_light.scan3d.stream_writer import WriteItem

HEADER=struct.Struct('<4sIIII')
MEMORY_LIMIT=64*1024**2
WORKING_RESERVE=16*1024**2

_IMMUTABLE_SCALAR_TYPES=(type(None),bool,int,float,complex,str,bytes)

@lru_cache(maxsize=128)
def _dataclass_field_names(cls):
    """Cache reflection once per dataclass type, not once per field value."""
    return tuple(field.name for field in fields(cls))

def _deeply_immutable(value):
    if type(value) in _IMMUTABLE_SCALAR_TYPES or isinstance(value,np.generic):return True
    if type(value) is tuple:return all(_deeply_immutable(item) for item in value)
    return False

def _trusted_transport_admission(value):
    """Return true only for a validated, strongly bound PL transport receipt.

    The admission is a frozen dataclass, but a generic dataclass clone would
    break the exact FinalFrame/primary references guarded by its pair seal.
    Check the transport token and immutable pair binding before retaining that
    authority object by identity. Fixture admissions are copied normally.
    """
    value_type=type(value)
    admission_module=__package__+'.pfo3_processor_r25'
    if (value_type.__name__!='PLFrameAdmission'
            or value_type.__module__!=admission_module):
        return False
    module=sys.modules.get(admission_module)
    admission_type=getattr(module,'PLFrameAdmission',None) if module is not None else None
    if admission_type is None or type(value) is not admission_type or value.fixture_only:
        return False
    value.validate()
    if type(value.identity) is not MappingProxyType:
        return False
    from .immutable_pair_seal_r25 import ImmutablePairSealR25
    from .pfo3_r25 import FinalFrame,IDENTITY_FIELDS
    frame=value._validated_frame
    primary=value._validated_primary
    seal=value._pair_seal
    if (type(frame) is not FinalFrame or type(frame.payload) is not bytes
            or type(primary) is not bytes or type(seal) is not ImmutablePairSealR25
            or set(value.identity)!=set(IDENTITY_FIELDS)
            or any(type(item) is not int for item in value.identity.values())):
        return False
    digests=seal.check(frame,primary)
    if (digests.assembled_sha256!=value.stream_sha256
            or digests.primary_stream_sha256!=value._primary_sha256):
        return False
    return True

class _SnapshotCharge:
    """Charge the completed owned graph while it is built, once per object.

    Container allocation sizes are read after filling them. An in-progress
    identity is marked before recursion, so cycles do not charge a partial
    allocation or recurse forever. Retained authority/unknown objects use the
    existing resident() traversal and its exact rules, never a size estimate.
    """
    def __init__(self):
        self.seen=set();self.total=0

    def start(self,value):
        identity=id(value)
        if identity in self.seen:return False
        self.seen.add(identity)
        return True

    def finish(self,value,started):
        if started:self.total+=sys.getsizeof(value)

    def retained(self,value):
        if type(value) in _IMMUTABLE_SCALAR_TYPES or isinstance(value,np.generic):
            started=self.start(value);self.finish(value,started)
        else:
            self.total+=resident(value,self.seen)


def _snapshot_owned(value,memo=None,*,normalize_array_ids=frozenset(),_charge=None):
    """Copy caller-owned mutable state while retaining safe immutable values.

    A single memo is shared across the whole envelope so aliases remain aliases.
    Dataclasses are allocated without invoking their constructor or
    ``__post_init__``; this preserves frozen profile values without repeating
    validation work at the writer ownership boundary.
    """
    memo={} if memo is None else memo
    identity=id(value)
    if identity in memo:
        clone=memo[identity]
        if _charge is not None:_charge.retained(clone)
        return clone
    # These exact storage objects are immutable authority values.  The receipt
    # contains a MappingProxyType over views backed by the sealed bytes, so a
    # generic deepcopy both fails and would break the identity binding checked
    # by the writer.  Preserve identity; the normal writer validation still
    # verifies the receipt and every bound source before persistence.
    if type(value) in (SealedMBR1,MBR1ReceiptR26):
        memo[identity]=value
        if _charge is not None:_charge.retained(value)
        return value
    if (type(value) in _IMMUTABLE_SCALAR_TYPES or isinstance(value,np.generic)
            or (type(value) is tuple and _deeply_immutable(value))):
        if type(value) is tuple:memo[identity]=value
        if _charge is not None:_charge.retained(value)
        return value
    if isinstance(value,np.ndarray):
        normalize=identity in normalize_array_ids
        clone=np.array(value,dtype=np.float64 if normalize else None,
                       copy=True,order='C' if normalize else 'K',subok=True)
        memo[identity]=clone
        if value.dtype.hasobject:
            for index in np.ndindex(value.shape):
                # resident() deliberately charges only ndarray allocation/base,
                # not object-array elements. Copy these values without charging;
                # aliases elsewhere in the envelope are charged on that path.
                clone[index]=_snapshot_owned(value[index],memo,
                    normalize_array_ids=normalize_array_ids)
        clone.setflags(write=False)
        if _charge is not None:_charge.retained(clone)
        return clone
    if isinstance(value,bytearray):
        clone=bytearray(value);memo[identity]=clone
        if _charge is not None:_charge.retained(clone)
        return clone
    if isinstance(value,memoryview):
        backing=bytearray(value) if not value.readonly else bytes(value)
        clone=memoryview(backing);memo[identity]=clone
        if _charge is not None:_charge.retained(clone)
        return clone
    if type(value) is MappingProxyType:
        # A proxy cannot be pickled/deepcopied. Rebuild its private backing
        # dictionary recursively, installing the proxy in memo first so
        # aliases and even cycles through a mutable backing value survive.
        backing={}
        clone=MappingProxyType(backing)
        memo[identity]=clone
        started=_charge.start(clone) if _charge is not None else False
        backing_started=_charge.start(backing) if _charge is not None else False
        for key,item in value.items():
            owned_key=_snapshot_owned(key,memo,normalize_array_ids=normalize_array_ids,_charge=_charge)
            owned_item=_snapshot_owned(item,memo,normalize_array_ids=normalize_array_ids,_charge=_charge)
            backing[owned_key]=owned_item
        if _charge is not None:
            _charge.finish(backing,backing_started);_charge.finish(clone,started)
        return clone
    if isinstance(value,dict):
        if type(value) is dict:
            clone={}
        else:
            try:
                clone=value.copy()
                if type(clone) is not type(value):raise TypeError('mapping copy changed type')
                clone.clear()
            except Exception:
                try:clone=type(value)()
                except Exception:clone={}
        memo[identity]=clone
        started=_charge.start(clone) if _charge is not None else False
        for key,item in value.items():
            owned_key=_snapshot_owned(key,memo,normalize_array_ids=normalize_array_ids,_charge=_charge)
            owned_item=_snapshot_owned(item,memo,normalize_array_ids=normalize_array_ids,_charge=_charge)
            clone[owned_key]=owned_item
        if _charge is not None:_charge.finish(clone,started)
        return clone
    if isinstance(value,list):
        clone=[];memo[identity]=clone
        started=_charge.start(clone) if _charge is not None else False
        clone.extend(_snapshot_owned(item,memo,normalize_array_ids=normalize_array_ids,
                                    _charge=_charge)
                     for item in value)
        if _charge is not None:_charge.finish(clone,started)
        return clone
    if isinstance(value,tuple):
        items=[_snapshot_owned(item,memo,normalize_array_ids=normalize_array_ids,
                               _charge=_charge)
               for item in value]
        # Tuples can participate in a cycle through a mutable member. The
        # recursive visit may already have installed the canonical tuple.
        if identity in memo:
            if _charge is not None:_charge.retained(memo[identity])
            return memo[identity]
        clone=type(value)(items) if type(value) is not tuple else tuple(items)
        memo[identity]=clone
        if _charge is not None:
            # Children have already been charged, including a canonical tuple
            # constructed during recursion through a mutable tuple member.
            started=_charge.start(clone);_charge.finish(clone,started)
        return clone
    if isinstance(value,set):
        clone=set();memo[identity]=clone
        started=_charge.start(clone) if _charge is not None else False
        clone.update(_snapshot_owned(item,memo,normalize_array_ids=normalize_array_ids,
                                    _charge=_charge)
                     for item in value)
        if _charge is not None:_charge.finish(clone,started)
        return clone
    if is_dataclass(value) and not isinstance(value,type):
        if _trusted_transport_admission(value):
            memo[identity]=value
            if _charge is not None:_charge.retained(value)
            return value
        clone=object.__new__(type(value));memo[identity]=clone
        started=_charge.start(clone) if _charge is not None else False
        try:
            namespace=vars(value)
        except TypeError:
            namespace=None
        if namespace is not None:
            owned_namespace=_snapshot_owned(namespace,memo,
                normalize_array_ids=normalize_array_ids,_charge=_charge)
            object.__setattr__(clone,'__dict__',owned_namespace)
            for name in _dataclass_field_names(type(value)):
                if name not in namespace:
                    object.__setattr__(clone,name,_snapshot_owned(getattr(value,name),memo,
                        normalize_array_ids=normalize_array_ids,_charge=_charge))
        else:
            for name in _dataclass_field_names(type(value)):
                object.__setattr__(clone,name,_snapshot_owned(getattr(value,name),memo,
                    normalize_array_ids=normalize_array_ids,_charge=_charge))
        if _charge is not None:_charge.finish(clone,started)
        return clone
    # Metadata occasionally contains a standard-library value outside the
    # JSON/NumPy cases above. Deepcopy remains the conservative compatibility
    # path and shares the same memo with the optimized cases.
    clone=copy.deepcopy(value,memo)
    if _charge is not None:_charge.retained(clone)
    return clone


def _snapshot_owned_with_resident(value,*,normalize_array_ids=frozenset()):
    """Return the detached snapshot and exactly resident(snapshot) in one pass."""
    charge=_SnapshotCharge()
    owned=_snapshot_owned(value,normalize_array_ids=normalize_array_ids,_charge=charge)
    return owned,charge.total

def resident(value, seen=None):
    """Unique retained Python/NumPy allocation bound (views charge their base)."""
    seen=set() if seen is None else seen
    if id(value) in seen:return 0
    seen.add(id(value));size=sys.getsizeof(value)
    if isinstance(value,np.ndarray):
        return size+(resident(value.base,seen) if value.base is not None else 0)
    if isinstance(value,memoryview):return size+resident(value.obj,seen)
    if type(value) is MappingProxyType:
        # CPython exposes the retained backing dictionary through GC
        # referents. Use that object identity so two proxies over one mapping
        # do not charge the same allocation twice. Keep a same-shape fallback
        # for runtimes that omit it from the referent list.
        referents=gc.get_referents(value)
        backing=referents[0] if len(referents)==1 and isinstance(referents[0],dict) else value.copy()
        return size+resident(backing,seen)
    if isinstance(value,dict):return size+sum(resident(k,seen)+resident(v,seen) for k,v in value.items())
    if isinstance(value,(tuple,list,set,frozenset)):return size+sum(resident(v,seen) for v in value)
    if is_dataclass(value) and not isinstance(value,type):
        try:namespace=vars(value)
        except TypeError:namespace=None
        if namespace is not None:
            size+=resident(namespace,seen)
            for name in _dataclass_field_names(type(value)):
                if name not in namespace:size+=resident(getattr(value,name),seen)
            return size
        return size+sum(resident(getattr(value,name),seen)
                        for name in _dataclass_field_names(type(value)))
    return size

def encode_aux(index,epoch,preview,trace):
    a=np.ascontiguousarray(preview,dtype='<f8')
    if a.ndim!=2 or a.shape[1]!=3 or len(a)>7680 or not np.isfinite(a).all():
        raise ValueError('R30 preview must be finite bounded Nx3')
    info=dict(schema='R30_FRAME_AUX_V1',frame=index,epoch=epoch,trace=trace,
              preview_count=len(a),preview_sha256=hashlib.sha256(memoryview(a.reshape(-1)).cast('B')).hexdigest())
    meta=json.dumps(info,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
    if len(meta)>1024**2:raise ValueError('R30 trace metadata bound')
    return HEADER.pack(b'R30A',1,len(meta),len(a),a.nbytes)+meta+a.tobytes()

def decode_aux(blob,*,request_id,epoch):
    if len(blob)<HEADER.size:raise ValueError('truncated R30 frame auxiliary')
    magic,version,nmeta,count,nxyz=HEADER.unpack_from(blob)
    if (magic!=b'R30A' or version!=1 or nmeta>1024**2 or count>7680 or nxyz!=count*24
            or len(blob)!=HEADER.size+nmeta+nxyz):raise ValueError('R30 auxiliary header/length mismatch')
    from .storage_r21 import _json
    meta=_json(blob[HEADER.size:HEADER.size+nmeta]);data=blob[HEADER.size+nmeta:]
    if (meta.get('schema')!='R30_FRAME_AUX_V1' or meta.get('frame')!=request_id
            or meta.get('epoch')!=epoch or meta.get('preview_count')!=count
            or meta.get('trace',{}).get('frame_id')!=request_id
            or hashlib.sha256(data).hexdigest()!=meta.get('preview_sha256')):
        raise ValueError('R30 auxiliary identity/hash mismatch')
    a=np.frombuffer(data,dtype='<f8').reshape(-1,3)
    if not np.isfinite(a).all():raise ValueError('R30 nonfinite preview')
    return meta,a

class DeferredCloudR30:
    """Logical geometry accumulator; journal XYZ is authoritative until export."""
    def __init__(self,path,*,storage_budget):
        self.path=Path(path);self.storage_budget=storage_budget;self.count=0
        self.last_append_timing=dict(wall_s=0.,thread_cpu_s=0.)
        self.materialized=False;self._closed=False;self.writer=None
    def append(self,points):
        a=np.asarray(points)
        if a.dtype!=np.float64 or a.ndim!=2 or a.shape[1]!=3 or not np.isfinite(a).all():
            raise ValueError('finite float64 cloud delta required')
        self.count+=len(a)
    def export_npy(self,path):
        if self.writer is None:raise RuntimeError('R30 journal owner missing')
        self.writer.materialize(Path(path),expected_points=self.count)
        self.materialized=True
    def close(self):self._closed=True

class UnifiedFrameWriterR30(MultiBranchChunkWriter):
    """Frozen envelopes -> background MBR/aux encoding -> strict v4 journal.

All queue and active envelope bytes remain charged until journal fsync. A
16MiB scratch reserve is inside the same 64MiB ceiling, not extra capacity.
Capacity reservations are acquired before taking a new measurement task.
"""
    def __init__(self,output_dir,*,capacity=128,**kwargs):
        self._reservation=0;self._last_reservation=0;self.admission_paused=False
        self._unified_peak=WORKING_RESERVE;self.materialization=None
        kwargs['capacity_limit']=128
        super().__init__(output_dir,capacity=capacity,**kwargs)

    def reserve_frame(self,bytes_bound,timeout_s=30.,cancelled=None):
        if type(bytes_bound) is not int or not 0<bytes_bound<=MEMORY_LIMIT-WORKING_RESERVE:
            raise ValueError('frame reservation exceeds total memory bound')
        deadline=time.monotonic()+timeout_s
        with self._condition:
            if self._reservation:raise RuntimeError('only one geometry owner may reserve')
            while True:
                if cancelled is not None and cancelled():
                    self.admission_paused=False
                    raise RuntimeError('R30 admission cancelled before accepting measurement')
                self._check_health()
                pending=self._pending_bytes_locked()
                if self._accepted-self._written<self._capacity and pending+bytes_bound+WORKING_RESERVE<=self.byte_capacity:
                    self._reservation=bytes_bound;self._last_reservation=bytes_bound
                    self.admission_paused=False
                    self._unified_peak=max(self._unified_peak,pending+bytes_bound+WORKING_RESERVE)
                    return
                left=deadline-time.monotonic()
                self.admission_paused=True
                if left<=0:raise TimeoutError('R30 paused admission: persistence memory exhausted')
                self._condition.wait(min(left,.1))

    def _check_health(self):
        if self._worker_error is not None:raise RuntimeError('R30 persistence worker failed: '+self._error_text)
        if not self._accepting or self._worker_stopped:raise RuntimeError('R30 persistence closed')

    def cancel_reservation(self):
        with self._condition:self._reservation=0;self.admission_paused=False;self._condition.notify_all()

    def _snapshot_record(self,record):
        if '_r30_profile' not in record:return super()._snapshot_record(record)
        # Detach all caller-owned arrays and mutable metadata at transfer. The
        # original measured result may be discarded immediately after submit.
        timing_enabled=os.environ.get('R30_ADMISSION_TIMING')=='1'
        thread_clock=getattr(time,'thread_time',time.process_time)
        wall_start=time.perf_counter() if timing_enabled else 0.
        cpu_start=thread_clock() if timing_enabled else 0.
        normalize_ids=frozenset(id(record[key]) for key in
            ('points_camera_mm','points_axis_mm','_r30_preview')
            if isinstance(record.get(key),np.ndarray))
        snapshot_charge=_SnapshotCharge()
        owned=_snapshot_owned(record,normalize_array_ids=normalize_ids,
                              _charge=snapshot_charge)
        if not isinstance(owned,dict):raise TypeError('R30 frame envelope must be a mapping')
        charge_fallback=False
        for key in ('points_camera_mm','points_axis_mm','_r30_preview'):
            array=owned[key]
            if not isinstance(array,np.ndarray) or array.dtype!=np.dtype(np.float64) or not array.flags.c_contiguous:
                array=np.array(array,dtype=np.float64,copy=True,order='C')
                array.setflags(write=False)
                owned[key]=array
                charge_fallback=True
        copy_wall=(time.perf_counter()-wall_start) if timing_enabled else 0.
        copy_cpu=(thread_clock()-cpu_start) if timing_enabled else 0.
        timing=None
        if timing_enabled:
            # Same-sized float placeholders are installed before resident()
            # so the optional diagnostic record itself is inside the charge.
            timing=dict(snapshot_copy_wall_s=copy_wall,snapshot_copy_thread_cpu_s=copy_cpu,
                resident_wall_s=0.,resident_thread_cpu_s=0.,snapshot_total_wall_s=0.,
                snapshot_total_thread_cpu_s=0.)
            prior_size=sys.getsizeof(owned)
            if '_r30_admission_timing' in owned:charge_fallback=True
            owned['_r30_admission_timing']=timing
            snapshot_charge.total+=sys.getsizeof(owned)-prior_size
            snapshot_charge.retained('_r30_admission_timing')
            snapshot_charge.retained(timing)
        resident_wall_start=time.perf_counter() if timing_enabled else 0.
        resident_cpu_start=thread_clock() if timing_enabled else 0.
        # Normal ndarray envelopes were fully charged as their private graph
        # was built. Rare list-to-array normalization/replaced diagnostic fields
        # retain the existing conservative traversal instead of estimating a
        # subtraction for a graph with aliases or cycles.
        charge=(resident(owned) if charge_fallback else snapshot_charge.total)+8192
        if timing_enabled:
            timing['resident_wall_s']=time.perf_counter()-resident_wall_start
            timing['resident_thread_cpu_s']=thread_clock()-resident_cpu_start
            timing['snapshot_total_wall_s']=time.perf_counter()-wall_start
            timing['snapshot_total_thread_cpu_s']=thread_clock()-cpu_start
        owned['_r30_charge']=charge
        if owned['_r30_charge']+256>self._reservation:
            raise BufferError('frozen envelope exceeds pre-admitted R30 byte bound')
        return owned

    @staticmethod
    def _resident_bytes(value):
        if isinstance(value,dict) and '_r30_charge' in value:return value['_r30_charge']
        return MultiBranchChunkWriter._resident_bytes(value)

    def _enqueue(self,kind,payload):
        if kind=='frame' and '_r30_charge' in payload:
            with self._condition:
                charge=payload['_r30_charge']+256  # parent queue-item allocation
                if self._pending_bytes_locked()+charge+WORKING_RESERVE>self.byte_capacity:
                    raise BufferError('R30 combined queue/active/scratch bound')
                result=super()._enqueue(kind,payload)
                self._reservation=0
                return result
        return super()._enqueue(kind,payload)

    def _persist_group(self,group):
        prepared=[];started=time.perf_counter()
        for item in group:
            if item.kind!='frame' or '_r30_profile' not in item.payload:
                prepared.append(item);continue
            source=item.payload
            packet=encode_profile(source['_r30_profile'],source['summary'])
            aux=encode_aux(source['request_id'],source['capture_epoch'],source['_r30_preview'],source['_r30_trace'])
            actual=(len(source['raw'])+len(source['source_pfo3_payload'])+len(source['source_pfo3_primary'])+
                len(packet)+len(aux)+source['points_camera_mm'].nbytes+3*source['points_axis_mm'].nbytes+
                source['_r30_preview'].nbytes+32*1024)
            if actual>source.get('_r30_frame_bound',48*1024**2):
                raise BufferError('serialized R30 transaction exceeds frozen per-frame disk bound')
            record={key:value for key,value in source.items() if not key.startswith('_r30_')}
            record['profile_payload']=packet;record['source_r30_frame_aux']=aux
            snapshot=super()._snapshot_record(record)
            # Only one frame is converted at a time; all converted records in
            # this bounded <=8 item batch plus encoder temporaries fit scratch.
            prepared.append(WriteItem(item.sequence,item.kind,snapshot))
        work_bytes=sum(resident(item.payload) for item in prepared)
        if work_bytes>WORKING_RESERVE:raise BufferError('R30 serialization batch exceeded scratch reserve')
        stats=super()._persist_group(prepared)
        stats.update(r30_encode_validate_commit_s=time.perf_counter()-started,
                     r30_working_bytes=work_bytes,all_frame_streams_same_commit=True)
        return stats

    def materialize(self,cloud_path,*,expected_points):
        if self._thread.is_alive() or self._written!=self._accepted or self._worker_error:
            raise RuntimeError('cannot export before durable writer join')
        import io
        import shutil
        import stat
        import tempfile
        from contextlib import ExitStack
        from resource_policy import accounted_file_bytes

        if type(expected_points) is not int or expected_points < 0:
            raise ValueError('nonnegative integer R30 cloud point count required')

        root=Path(os.path.abspath(self.output_dir.parent))
        paths=[Path(os.path.abspath(cloud_path)),root/'cloud.float64.bin',root/'preview.float64.bin',
               root/'preview_index.jsonl',root/'frames_trace.jsonl']
        keys=[os.path.normcase(str(path)) for path in paths]
        if len(set(keys))!=len(keys):raise ValueError('duplicate R30 derived export target')
        for path in paths:
            if not path.parent.is_dir():raise FileNotFoundError('R30 export parent directory missing: '+str(path.parent))
            if os.path.lexists(path):raise FileExistsError('R30 export target already exists: '+str(path))

        # Reserve from the exact journal contents. The old fixed 32 KiB/frame
        # allowance for trace rows was not a valid upper bound: encode_aux
        # permits up to 1 MiB of metadata per frame.
        header_buffer=io.BytesIO()
        np.lib.format.write_array_header_1_0(
            header_buffer,dict(descr='<f8',fortran_order=False,shape=(expected_points,3)))
        header_bytes=header_buffer.tell()
        count=preview_count=offset=frames=index_bytes=trace_bytes=0
        for record in iter_records(self.output_dir):
            if record['kind']!='frame':continue
            meta,points=decode_aux(record['source_r30_frame_aux'],
                request_id=record['metadata']['request_id'],epoch=record['metadata']['capture_epoch'])
            delta=record['points_axis_mm']
            count+=len(delta)
            blob=points.tobytes()
            row=dict(frame=meta['frame'],offset=offset,bytes=len(blob),count=len(points),
                     sha256=hashlib.sha256(blob).hexdigest())
            index_bytes+=len((json.dumps(row,sort_keys=True,allow_nan=False)+'\n').encode('utf8'))
            trace_bytes+=len((json.dumps(meta['trace'],allow_nan=False)+'\n').encode('utf8'))
            offset+=len(blob);preview_count+=len(points);frames+=1
        if count!=expected_points or frames!=self._written:
            raise ValueError('R30 export cloud/frame completeness mismatch')
        output_bound=header_bytes+expected_points*48+offset+index_bytes+trace_bytes
        # The reserve includes the bounded ERROR.json plus filesystem block
        # rounding. It is released on success, while a failed staging tree is
        # charged using accounted_file_bytes and retained as failure evidence.
        failure_reserve=64*1024
        token=self.storage_budget.reserve(output_bound+failure_reserve)
        stage_dirs=[]
        staged_targets=[]
        attempted_links=[]
        total=0

        def directory_sync(directory):
            if os.name=='nt':return
            fd=os.open(str(directory),os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
            try:os.fsync(fd)
            finally:os.close(fd)

        def same_file(path,identity):
            try:st=os.stat(path,follow_symlinks=False)
            except OSError:return False
            return (st.st_dev,st.st_ino)==identity

        def rollback_links():
            errors=[]
            for staged,target,identity in reversed(attempted_links):
                try:
                    if not same_file(target,identity):continue
                    if not os.path.lexists(staged):os.link(target,staged)
                    if same_file(staged,identity):target.unlink()
                except OSError as exc:
                    errors.append(type(exc).__name__+': '+str(exc)[:256])
            for directory in {path.parent for _,path,_ in attempted_links}:
                try:directory_sync(directory)
                except OSError as exc:errors.append(type(exc).__name__+': '+str(exc)[:256])
            return errors

        def retained_allocation_bytes():
            total_bytes=0;seen=set()
            candidates=[]
            for directory in stage_dirs:
                if directory.exists():candidates.extend(p for p in directory.rglob('*') if p.is_file())
            for _,target,identity in attempted_links:
                if same_file(target,identity):candidates.append(target)
            for path in candidates:
                try:st=os.stat(path,follow_symlinks=False)
                except OSError:continue
                if not stat.S_ISREG(st.st_mode):continue
                inode=(st.st_dev,st.st_ino)
                key=inode if st.st_ino else os.path.normcase(os.path.realpath(path))
                if key in seen:continue
                seen.add(key);total_bytes+=accounted_file_bytes(st)
            return total_bytes

        try:
            directories={}
            for target in paths:
                parent=target.parent
                if parent not in directories:
                    temporary=Path(tempfile.mkdtemp(prefix='.r30_materialize_',dir=str(parent)))
                    directories[parent]=temporary;stage_dirs.append(temporary)
                staged=directories[parent]/target.name
                staged_targets.append((staged,target))

            with ExitStack() as stack:
                cloud,spool,preview,index,trace=[stack.enter_context(staged.open('xb'))
                    for staged,_ in staged_targets]
                np.lib.format.write_array_header_1_0(
                    cloud,dict(descr='<f8',fortran_order=False,shape=(expected_points,3)))
                count=preview_count=offset=frames=0
                for record in iter_records(self.output_dir):
                    if record['kind']!='frame':continue
                    meta,points=decode_aux(record['source_r30_frame_aux'],
                        request_id=record['metadata']['request_id'],epoch=record['metadata']['capture_epoch'])
                    delta=record['points_axis_mm']
                    data=memoryview(np.ascontiguousarray(delta,dtype='<f8').reshape(-1)).cast('B')
                    cloud.write(data);spool.write(data);count+=len(delta)
                    blob=points.tobytes();preview.write(blob)
                    row=dict(frame=meta['frame'],offset=offset,bytes=len(blob),count=len(points),
                             sha256=hashlib.sha256(blob).hexdigest())
                    index.write((json.dumps(row,sort_keys=True,allow_nan=False)+'\n').encode('utf8'))
                    trace.write((json.dumps(meta['trace'],allow_nan=False)+'\n').encode('utf8'))
                    offset+=len(blob);preview_count+=len(points);frames+=1
                if count!=expected_points or frames!=self._written:
                    raise ValueError('R30 export cloud/frame completeness mismatch')
                for stream in (cloud,spool,preview,index,trace):stream.flush();os.fsync(stream.fileno())
                total=sum(stream.tell() for stream in (cloud,spool,preview,index,trace))
            if total!=output_bound:raise ValueError('R30 export size differed from preflight estimate')

            # Link each complete, fsynced staging file into place. os.link is
            # atomic and fails if a target appeared meanwhile; unlike replace,
            # it can never overwrite another run's output.
            for staged,target in staged_targets:
                st=os.stat(staged,follow_symlinks=False);identity=(st.st_dev,st.st_ino)
                attempted_links.append((staged,target,identity))
                os.link(staged,target)
            for directory in {path.parent for _,path,_ in attempted_links}:
                directory_sync(directory)

            # Retire the temporary names before settling the quota. If this or
            # commit fails, the exception path recreates staging links from the
            # just-published targets before rolling those names back.
            for staged,_ in staged_targets:staged.unlink()
            for directory in stage_dirs:directory_sync(directory)
            self.storage_budget.commit(token,total)
            token=None
        except BaseException as exc:
            rollback_errors=rollback_links()
            if stage_dirs:
                error_path=stage_dirs[0]/'ERROR.json'
                error=dict(schema='R30_MATERIALIZE_FAILURE_V1',exception=type(exc).__name__,
                    message=str(exc)[:1024],targets=[path.name for path in paths],
                    rollback_errors=rollback_errors)
                try:
                    payload=(json.dumps(error,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode('utf8')
                    if len(payload)<=8192:
                        with error_path.open('xb') as stream:
                            stream.write(payload);stream.flush();os.fsync(stream.fileno())
                except OSError:
                    pass
            if token is not None:
                try:
                    retained=retained_allocation_bytes()
                    if retained:
                        self.storage_budget.settle_allocation(token,retained)
                    else:
                        self.storage_budget.cancel_unwritten(token)
                except BaseException:
                    # settle_allocation removes the ticket before reporting an
                    # overrun. Do not call cancel_unwritten: that could hide a
                    # real allocation from the quota ledger.
                    pass
            raise

        cleanup_pending=[]
        for directory in stage_dirs:
            try:shutil.rmtree(directory)
            except OSError:cleanup_pending.append(str(directory))
        self.materialization=dict(schema='R30_DERIVED_EXPORT_V1',frames=frames,cloud_points=count,
            preview_points=preview_count,preview_bytes=offset,complete=True,
            authority='SYNCED_PER_FRAME_JOURNAL_AUX_AND_XYZ',bytes=total,
            temporary_cleanup_pending=cleanup_pending)
        return self.materialization

    def pipeline_summary(self):
        return dict(schema='R30_PERSISTENCE_OWNER_V1',capacity=self._capacity,
            memory_limit_bytes=self.byte_capacity,scratch_reserve_bytes=WORKING_RESERVE,
            peak_combined_bytes=self._unified_peak,peak_pending_frames=self.peak_pending_items,
            per_frame_streams_same_commit=True,standalone_exports_after_drain=True,
            materialization=self.materialization)
