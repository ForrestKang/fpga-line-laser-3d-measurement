"""Experimental spawned v1 writer. No production entrypoint uses this module.

Records cross a private byte pipe, never pickle. Live transport capabilities and
MBR object seals stay in the parent; the child revalidates plain source bytes.
Acceptance is not durability. Only the original writer's post-fsync callback
advances the parent's durable watermark. No device commands occur here.
"""
from __future__ import annotations

from dataclasses import asdict
from collections.abc import Mapping
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import shutil
import struct
import sys
import threading
import time
import uuid

import numpy as np

from . import storage_r21 as legacy
from resource_policy import StorageBudget
from structured_light.scan3d.stream_writer import (WriteItem,WriterFailure,WriterReceipt,
    _json_line,_timeout,_validate_json_value)

MAX_PACKET=4*1024**2
MAX_JSON=64*1024
GLOBAL_LIMIT=64*1024**2
TRANSIENT_RESERVE=8*1024**2
MIN_AVAILABLE=128*1024**2
CHILD_STARTUP_ALLOWANCE=64*1024**2
HEADER=struct.Struct('<8sI32s')
MAGIC=b'R26IPC1\0'


def _memory_gate(required):
    """Linux board gate; host Windows checks do not certify board free memory."""
    if not sys.platform.startswith('linux'):
        return dict(status='UNMEASURED_HOST_PLATFORM',required_bytes=required)
    with Path('/proc/meminfo').open(encoding='ascii') as stream:
        for line in stream:
            if line.startswith('MemAvailable:'):
                available=int(line.split()[1])*1024
                if available<required:raise BufferError('process writer memory preflight refused: available='+str(available)+' required='+str(required))
                return dict(status='PASS',available_bytes=available,required_bytes=required)
    raise RuntimeError('Linux MemAvailable required for process writer admission')


def encode_record(snapshot):
    """Explicit bounded JSON directory + byte sections, without Python objects."""
    sections=[];descriptors={};offset=0
    for key in ('raw','profile_payload')+legacy._SOURCE_PAYLOADS+legacy._POINTS:
        value=snapshot.get(key)
        if value is None:continue
        desc=dict(offset=offset)
        if key in legacy._POINTS:
            if (not isinstance(value,np.ndarray) or value.shape!=(len(value),3)
                    or len(value)>legacy.MAX_POINTS or value.dtype.kind not in 'iuf'
                    or value.dtype.itemsize not in (1,2,4,8) or not np.isfinite(value).all()):
                raise ValueError('unsupported bounded numeric IPC points')
            blob=value.tobytes(order='C');desc.update(dtype=value.dtype.str,count=len(value))
        else:
            if not isinstance(value,bytes):raise TypeError('immutable IPC source bytes required')
            blob=bytes(value) # strips an in-process MBR seal, not its data.
        desc.update(bytes=len(blob),sha256=hashlib.sha256(blob).hexdigest())
        descriptors[key]=desc;sections.append(blob);offset+=len(blob)
    meta={key:snapshot[key] for key in ('request_id','capture_epoch','raw_sha256','summary','motion','exposure')}
    head=_json_line(dict(schema='R26_PROCESS_RECORD_V1',metadata=meta,sections=descriptors))
    if len(head)>MAX_JSON or HEADER.size+len(head)+offset>MAX_PACKET:
        raise BufferError('experimental IPC record exceeds supported4MiB/64KiB envelope')
    body=head+b''.join(sections)
    return HEADER.pack(MAGIC,len(head),hashlib.sha256(body).digest())+body


def decode_record(packet):
    if type(packet) is not bytes or not HEADER.size<=len(packet)<=MAX_PACKET:
        raise ValueError('bounded immutable IPC packet required')
    magic,head_size,digest=HEADER.unpack_from(packet)
    if magic!=MAGIC or not 0<head_size<=MAX_JSON or HEADER.size+head_size>len(packet):
        raise ValueError('IPC header/capacity mismatch')
    body=memoryview(packet)[HEADER.size:]
    if hashlib.sha256(body).digest()!=digest:raise ValueError('IPC checksum mismatch')
    header=legacy._json(bytes(body[:head_size]))
    if set(header)!=set(('schema','metadata','sections')) or header['schema']!='R26_PROCESS_RECORD_V1':
        raise ValueError('IPC schema mismatch')
    meta=header['metadata'];table=header['sections']
    if (type(meta) is not dict or set(meta)!=set(('request_id','capture_epoch','raw_sha256','summary','motion','exposure'))
            or type(table) is not dict or set(table)-set(('raw','profile_payload')+legacy._SOURCE_PAYLOADS+legacy._POINTS)
            or not {'raw','profile_payload'}.issubset(table)):
        raise ValueError('IPC metadata/section fields mismatch')
    offset=head_size;record=dict(meta)
    if any(type(desc) is not dict or type(desc.get('offset')) is not int for desc in table.values()):
        raise ValueError('IPC descriptor offset type mismatch')
    for key,desc in sorted(table.items(),key=lambda pair:pair[1]['offset']):
        point=key in legacy._POINTS
        if type(desc) is not dict or set(desc)!=set(('offset','bytes','sha256')+(('dtype','count') if point else ())):
            raise ValueError('IPC descriptor fields mismatch')
        size=desc['bytes']
        if (type(desc['offset']) is not int or type(size) is not int or desc['offset']!=offset-head_size
                or not 0<=size<=MAX_PACKET or offset+size>len(body)):
            raise ValueError('IPC offset/length mismatch')
        blob=bytes(body[offset:offset+size]);offset+=size
        if hashlib.sha256(blob).hexdigest()!=desc['sha256']:raise ValueError('IPC section checksum mismatch')
        if point:
            dtype=np.dtype(desc['dtype']);count=desc['count']
            if (dtype.hasobject or dtype.kind not in 'iuf' or dtype.itemsize not in (1,2,4,8)
                    or type(count) is not int or not 0<=count<=legacy.MAX_POINTS or size!=count*3*dtype.itemsize):
                raise ValueError('IPC numeric dtype/count mismatch')
            record[key]=np.frombuffer(blob,dtype=dtype).reshape(count,3)
            if not np.isfinite(record[key]).all():raise ValueError('IPC nonfinite XYZ')
        else:record[key]=blob
    if offset!=len(body):raise ValueError('IPC trailing bytes')
    for key in legacy._POINTS:record.setdefault(key,None)
    return record


class _Snapshotter(legacy.MultiBranchChunkWriter):
    """Original synchronous admission logic, without starting a local writer."""
    def __init__(self):self.scratch_bytes=32*1024**2;self.validation_phase='submit'


def _child(request,response,config_json,stop):
    """Spawn target. Connections are private OS handles; messages are bytes."""
    lock=threading.Lock();writer=None;native=None;native_calls=0;cpu_started=time.process_time()
    def send(value):
        data=_json_line(value)
        if len(data)>MAX_JSON:raise ValueError('process response exceeds bound')
        with lock:response.send_bytes(data)
    try:
        config=legacy._json(config_json)
        manifest=config.pop('validator_manifest');require_native=config.pop('require_native')
        native_identity=None
        if manifest is not None:
            from .pfo3_decode_native_r25 import NativeBodyValidatorR25
            native=NativeBodyValidatorR25.from_manifest(manifest)
            native_identity=dict(native.require_manifest_admission())
        elif require_native:raise ValueError('explicit child native validator manifest required')
        if stop.is_set():return
        # Child gets ordinary bytes, no _pfo3_completion or AdmissionSealR26.
        # Full original v1 validation remains active, including Python source
        # reconstruction. Optional native validation is additional, not a skip.
        writer=legacy.MultiBranchChunkWriter(**config)
        class Trace:
            def durable_group(self,group,at):
                send(dict(kind='durable',sequences=[item.sequence for item in group],at_perf_s=at,
                          actual_bytes={str(item.sequence):writer._storage_actual[item.sequence] for item in group}))
        writer.performance_trace=Trace()
        send(dict(kind='ready',pid=os.getpid(),native_identity=native_identity,
            validation='STRICT_NATIVE_ADDITIONAL_PLUS_FULL_V1' if native else 'FULL_V1_STRICT_PYTHON'))
        while not stop.is_set():
            if not request.poll(.1):continue
            message=request.recv_bytes(MAX_PACKET+9)
            command=message[:1]
            if command in (b'F',b'E'):
                sequence=struct.unpack_from('<Q',message,1)[0]
                if command==b'F':
                    record=decode_record(message[9:])
                    if native is not None and record.get('source_pfo3_payload') is not None:
                        from .pfo3_decode_native_r25 import decode_native
                        from .pfo3_r25 import HEADER_FIELDS,identity
                        payload=record['source_pfo3_payload']
                        ident=identity(dict(zip(HEADER_FIELDS,struct.unpack_from('<64I',payload))))
                        decode_native(payload,expected_identity=ident,validator=native);native_calls+=1
                    writer.submit_wait(record,timeout_s=10.)
                else:writer.submit_event(legacy._json(message[9:]))
                if writer._accepted!=sequence:raise ValueError('parent/child sequence mismatch')
                send(dict(kind='accepted',sequence=sequence))
            elif message==b'L':writer.flush(timeout_s=10.)
            elif message==b'C':
                receipt=writer.close(timeout_s=30.)
                send(dict(kind='closed',receipt=asdict(receipt),io_accounting=writer.io_accounting,
                          native_checks=native_calls,child_cpu_s=time.process_time()-cpu_started));return
            else:raise ValueError('unknown process writer command')
    except BaseException as exc:
        try:send(dict(kind='failed',error=type(exc).__name__+': '+str(exc)))
        except BaseException:pass
    finally:
        if writer is not None:
            try:writer.close(timeout_s=.1)
            except BaseException:pass
        request.close();response.close()


class ProcessStorageWriterR26:
    """Experimental small FIFO with conservatively shared parent/child budget.

    A daemon child prevents interpreter shutdown from an unbounded implicit
    join. Parent exit can terminate it and NEVER certifies pending durability.
    close() never kills it; timeout retains pending inputs and unconfirmed state.
    """
    def __init__(self,output_dir,*,capacity=2,byte_capacity=GLOBAL_LIMIT,
                 group_size=8,max_delay_s=.05,raw_codec='zlib1',
                 validator_manifest=None,require_native=False,startup_timeout_s=10.,
                 storage_budget=None,validation_phase='worker',**unsupported):
        if unsupported:raise ValueError('unsupported process prototype options: '+str(sorted(unsupported)))
        if type(capacity) is not int or not 1<=capacity<=4:raise ValueError('prototype capacity must be1..4')
        if type(byte_capacity) is not int or not TRANSIENT_RESERVE<byte_capacity<=GLOBAL_LIMIT:
            raise ValueError('global process budget must be8..64MiB')
        if type(require_native) is not bool:raise ValueError('require_native must be bool')
        if validation_phase!='worker':raise ValueError('prototype requires explicit child worker validation')
        self.memory_preflight=dict(before_spawn=_memory_gate(MIN_AVAILABLE+byte_capacity+CHILD_STARTUP_ALLOWANCE))
        self.output_dir=Path(output_dir);self._condition=threading.Condition(threading.RLock())
        self._submit_lock=threading.Lock();self._snapshotter=_Snapshotter()
        self._capacity=capacity;self.byte_capacity=byte_capacity;self._pending={}
        self.scratch_bytes=32*1024**2;self.validation_phase=validation_phase
        if storage_budget is None:
            parent=self.output_dir.parent
            if not parent.is_dir():raise ValueError('existing managed output parent required')
            used=sum(path.stat().st_size for path in parent.rglob('*') if path.is_file())
            storage_budget=StorageBudget(used,lambda:int(shutil.disk_usage(parent).free))
        self.storage_budget=storage_budget
        self._accepted=self._written=self.child_accepted=0;self._last_request=self._epoch=None
        self._error=None;self._closing=False;self._closed_message=False;self._evidence_thread=None
        self.failure_evidence=None;self.performance_trace=None;self.io_accounting=None
        self.failure_watermark=None;self._failure_pending={}
        self.peak_pending_items=0;self.peak_pending_bytes=TRANSIENT_RESERVE
        self._outgoing=queue.Queue(maxsize=capacity+2)
        context=mp.get_context('spawn');self._stop=context.Event()
        child_request,self._request=context.Pipe(duplex=False)
        self._response,child_response=context.Pipe(duplex=False)
        config=dict(output_dir=str(self.output_dir.resolve()),capacity=capacity,
            byte_capacity=min(byte_capacity//2,32*1024**2),scratch_bytes=32*1024**2,
            group_size=min(group_size,capacity),max_delay_s=max_delay_s,raw_codec=raw_codec,
            validation_phase='worker',validator_manifest=None if validator_manifest is None else str(Path(validator_manifest).resolve()),
            require_native=require_native)
        self._process=context.Process(target=_child,args=(child_request,child_response,_json_line(config),self._stop),daemon=True)
        self._process.start();child_request.close();child_response.close()
        self._sender=threading.Thread(target=self._send,name='r26-store-ipc-send',daemon=True)
        self._receiver=threading.Thread(target=self._receive,name='r26-store-ipc-recv',daemon=True)
        self._ready=None;self._sender.start();self._receiver.start()
        deadline=time.monotonic()+_timeout(startup_timeout_s)
        with self._condition:
            while self._ready is None and self._error is None:
                remaining=deadline-time.monotonic()
                if remaining<=0:self._fail('child startup deadline exceeded');break
                self._condition.wait(min(remaining,.1))
            if self._error:
                self._stop.set();self._outgoing.put_nowait(None)
                self._sender.join(.1);self._process.join(.1)
                raise WriterFailure(self._error,self._receipt())
        self.child_backend=self._ready
        try:self.memory_preflight['after_spawn']=_memory_gate(MIN_AVAILABLE+byte_capacity)
        except BaseException:
            self.close(timeout_s=2.)
            raise

    def _receipt(self):
        alive=self._process.is_alive()
        joined=not alive and not self._sender.is_alive() and not self._receiver.is_alive()
        return WriterReceipt(self._accepted,self._written,self._accepted-self._written,self._error,
            joined,self._written==self._accepted and self._error is None,
            not joined and self._accepted>self._written,
            None if self._error is None else bool(self.failure_evidence and self.failure_evidence.get('persisted')))

    def _fail(self,message):
        with self._condition:
            if self._error is None:
                self._error=str(message)
                self.failure_watermark=dict(accepted=self._accepted,written=self._written,
                    pending_sequences=sorted(self._pending),at_perf_s=time.perf_counter(),error=self._error)
                self._failure_pending=dict(self._pending)
            self._closing=True;self._condition.notify_all()

    def _send(self):
        try:
            while True:
                if self._error is not None:return
                try:command=self._outgoing.get(timeout=.1)
                except queue.Empty:continue
                if command is None:return
                if isinstance(command,int):
                    with self._condition:message=self._pending[command]['message']
                    self._request.send_bytes(message)
                    del message
                else:self._request.send_bytes(command)
        except BaseException as exc:self._fail('sender: '+type(exc).__name__+': '+str(exc))

    def _receive(self):
        try:
            while True:
                if not self._response.poll(.1):
                    if not self._process.is_alive():
                        if not self._closed_message:self._fail('child exited without final durable receipt')
                        return
                    continue
                message=legacy._json(self._response.recv_bytes(MAX_JSON));kind=message['kind']
                with self._condition:
                    if kind=='ready':self._ready=message
                    elif kind=='accepted':self.child_accepted=max(self.child_accepted,message['sequence'])
                    elif kind=='durable':
                        seqs=message['sequences']
                        if seqs!=list(range(self._written+1,self._written+len(seqs)+1)) or any(s not in self._pending for s in seqs):
                            raise ValueError('noncontiguous child durability watermark')
                        group=[self._pending[s]['trace'] for s in seqs]
                        if self.storage_budget is not None:
                            for sequence in seqs:
                                self.storage_budget.commit(self._pending[sequence]['storage_ticket'],message['actual_bytes'][str(sequence)])
                        self._written=seqs[-1]
                        for sequence in seqs:del self._pending[sequence]
                        if self.performance_trace is not None:self.performance_trace.durable_group(legacy._trace_compatible_group(group),message['at_perf_s'])
                    elif kind=='closed':
                        receipt=message['receipt']
                        if not receipt['durable'] or receipt['written']!=self._written:
                            raise ValueError('child final receipt differs from fsync watermark')
                        self.io_accounting=message['io_accounting'];self._closed_message=True
                        self.native_checks=message['native_checks'];self.child_cpu_s=message['child_cpu_s']
                        self.io_accounting['process_writer']=dict(child_cpu_s=self.child_cpu_s,
                            child_native_checks=self.native_checks,child_backend=self.child_backend,
                            parent_process_cpu_excludes_child=True,
                            shared_memory_accounting='8x(record IPC bytes + retained parent snapshot), plus8MiB reserve')
                        self._condition.notify_all();return
                    elif kind=='failed':self._fail(message['error']);return
                    else:raise ValueError('unknown child response')
                    self._condition.notify_all()
        except BaseException as exc:
            if not self._closed_message:self._fail('receiver: '+type(exc).__name__+': '+str(exc))

    @property
    def pending_bytes(self):
        with self._condition:
            retained=dict(self._failure_pending);retained.update(self._pending)
            return TRANSIENT_RESERVE+sum(item['charge'] for item in retained.values())

    @property
    def pending_inputs(self):
        """Independent immutable IPC bytes retained until a real durable ACK."""
        with self._condition:return {seq:item['message'][9:] for seq,item in self._pending.items()}

    @property
    def failure_inputs(self):
        with self._condition:return {seq:item['message'][9:] for seq,item in self._failure_pending.items()}

    @property
    def process_status(self):
        return dict(child_alive=self._process.is_alive(),sender_alive=self._sender.is_alive(),
            receiver_alive=self._receiver.is_alive(),
            failure_preserver_alive=self._evidence_thread is not None and self._evidence_thread.is_alive(),
            failure_watermark=self.failure_watermark,memory_preflight=self.memory_preflight,
            global_charge_bytes=self.pending_bytes)

    def _admit(self,kind,payload,trace,identity,timeout_s):
        deadline=time.monotonic()+_timeout(timeout_s)
        #8x accounts encoded bytes retained in parent, OS pipe, child receive,
        # decoded blobs, child snapshot/queue and staging. Reserve8MiB also for
        # JSON/control and one in-progress parent encoder. Small supported
        # envelope fails closed rather than claiming full diagnostic capacity.
        charge=8*(len(payload)+legacy.MultiBranchChunkWriter._resident_bytes(trace.payload))+8192
        if charge+TRANSIENT_RESERVE>self.byte_capacity:raise BufferError('record exceeds shared process memory budget')
        waited=False
        with self._condition:
            while len(self._pending)>=self._capacity or self.pending_bytes+charge>self.byte_capacity:
                if self._error:raise WriterFailure(self._error,self._receipt())
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('process writer capacity wait; input not accepted')
                waited=True;self._condition.wait(remaining)
            if self._error:raise WriterFailure(self._error,self._receipt())
            if self._closing:raise RuntimeError('process writer closed')
            self.memory_preflight['last_admission']=_memory_gate(MIN_AVAILABLE+self.pending_bytes+charge)
            if identity is not None:
                request,epoch=identity
                if self._epoch not in (None,epoch) or self._last_request is not None and request<=self._last_request:
                    raise ValueError('process frame identity regression/epoch change')
                self._last_request=request;self._epoch=epoch
            sequence=self._accepted+1
            # Keep room for the v1 output AND one retained failure-input copy.
            # Failed tickets stay reserved because partial child disk use is
            # unknown; external recovery may settle them after strict audit.
            ticket=(self.storage_budget.reserve((2*len(payload)+256*1024)*5//4+1)
                    if self.storage_budget is not None else None)
            self._pending[sequence]=dict(message=kind+struct.pack('<Q',sequence)+payload,
                charge=charge,trace=WriteItem(sequence,trace.kind,trace.payload),storage_ticket=ticket)
            self._accepted=sequence
            self.peak_pending_items=max(self.peak_pending_items,len(self._pending))
            self.peak_pending_bytes=max(self.peak_pending_bytes,self.pending_bytes)
            self._outgoing.put_nowait(sequence)
        return waited

    def submit_wait(self,record,timeout_s=30.):
        with self._submit_lock:
            # The actual completion, if present, is checked synchronously here.
            # It is intentionally excluded from the serialization schema.
            if legacy.MultiBranchChunkWriter._resident_bytes(record)>MAX_PACKET:
                raise BufferError('record exceeds supported process prototype snapshot envelope')
            self.memory_preflight['before_snapshot']=_memory_gate(MIN_AVAILABLE+self.pending_bytes+self.scratch_bytes)
            snapshot=self._snapshotter._snapshot_record(record)
            legacy.MultiBranchChunkWriter._validate_record(snapshot)
            packet=encode_record(snapshot)
            trace=WriteItem(0,'frame',snapshot)
            return self._admit(b'F',packet,trace,(snapshot['request_id'],snapshot['capture_epoch']),timeout_s)

    def submit_event(self,event):
        with self._submit_lock:
            if not isinstance(event,Mapping):raise TypeError('event must be a mapping')
            event=dict(event);_validate_json_value(event,path='event')
            data=_json_line(event)
            if len(data)>MAX_JSON:raise BufferError('event exceeds process JSON bound')
            return self._admit(b'E',data,WriteItem(0,'event',legacy._json(data)),None,5.)

    def flush(self,timeout_s=5.):
        deadline=time.monotonic()+_timeout(timeout_s)
        with self._condition:
            target=self._accepted
            if not self._closing:self._outgoing.put_nowait(b'L')
            while self._written<target and self._error is None:
                remaining=deadline-time.monotonic()
                if remaining<=0:break
                self._condition.wait(min(remaining,.1))
            if self._error:raise WriterFailure(self._error,self._receipt())
            return self._receipt()

    def _preserve_failure(self):
        try:
            with self._condition:
                pending={seq:item['message'] for seq,item in self._failure_pending.items()}
                state=asdict(self._receipt())
            directory=self.output_dir/('process_failure_'+uuid.uuid4().hex)
            directory.mkdir(parents=True,exist_ok=False)
            files=[]
            for seq,data in pending.items():
                path=directory/('%08d.ipc'%seq)
                with path.open('xb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
                files.append(dict(sequence=seq,file=path.name,bytes=len(data),sha256=hashlib.sha256(data).hexdigest()))
            report=dict(receipt=state,failure_watermark=self.failure_watermark,pending_files=files,automatic_retry=False,
                        kill_does_not_imply_durability=True)
            with (directory/'FAILURE.json').open('xb') as stream:
                stream.write(_json_line(report));stream.flush();os.fsync(stream.fileno())
            self.failure_evidence=dict(persisted=True,directory=str(directory),pending=len(files))
        except BaseException as exc:self.failure_evidence=dict(persisted=False,error=type(exc).__name__+': '+str(exc))

    def close(self,timeout_s=5.):
        deadline=time.monotonic()+_timeout(timeout_s)
        if self._closed_message and not self._process.is_alive():return self._receipt()
        with self._submit_lock,self._condition:
            if not self._closing:
                self._closing=True;self._outgoing.put_nowait(b'C')
        with self._condition:
            while not self._closed_message and self._error is None:
                remaining=deadline-time.monotonic()
                if remaining<=0:self._fail('process close deadline exceeded; child state unconfirmed');break
                self._condition.wait(min(remaining,.1))
        if self._error:
            self._stop.set() # Cooperative stop, never kill or durability proof.
            if self._evidence_thread is None:
                self._evidence_thread=threading.Thread(target=self._preserve_failure,daemon=True)
                self._evidence_thread.start()
        self._process.join(max(0.,deadline-time.monotonic()))
        if self._closed_message:
            self._outgoing.put_nowait(None)
            self._sender.join(max(0.,deadline-time.monotonic()))
            self._receiver.join(max(0.,deadline-time.monotonic()))
            if not self._sender.is_alive() and not self._receiver.is_alive():
                self._request.close();self._response.close()
        if self._error:
            self._sender.join(max(0.,deadline-time.monotonic()))
            self._receiver.join(max(0.,deadline-time.monotonic()))
            self._evidence_thread.join(max(0.,deadline-time.monotonic()))
            raise WriterFailure(self._error,self._receipt())
        return self._receipt()
