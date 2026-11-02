"""Bounded PL acquisition worker. Owns only the DMA path, never image algorithms.

One pending complete immutable frame, at most32MiB. The consumer owns a frame
only after take(). Every completed but unconsumed frame is preserved on stop.
No retry, implicit drop, forced thread termination or hardware reset occurs.
"""
from collections import deque
from dataclasses import dataclass
import threading,time
import numpy as np
from structured_light.scan3d.payload_digest import payload_sha256

RAW_BYTES=752*480

class _PreparationFailed(RuntimeError):pass

@dataclass(frozen=True)
class ProducedPLFrame:
    index:int
    raw:bytes
    identity:dict
    final:object
    primary:bytes
    receipt:dict
    admission:object
    timings:dict
    source_ready_monotonic_ns:int=0
    @property
    def bytes(self):return len(self.raw)+len(self.primary)+len(self.final.payload)+4096

class PLFrameProducerR25:
    def __init__(self,frames,source,transport,*,identity_factory,preserve,preserve_failure,stop_event=None,byte_capacity=32*1024**2,
                 prepare_capacity_frames=0,prepare_byte_capacity=4*RAW_BYTES):
        if type(byte_capacity) is not int or not 1<=byte_capacity<=32*1024**2:raise ValueError('bounded producer bytes required')
        if type(prepare_capacity_frames) is not int or not 0<=prepare_capacity_frames<=4:raise ValueError('bounded preparation frames required')
        if type(prepare_byte_capacity) is not int or not RAW_BYTES<=prepare_byte_capacity<=4*RAW_BYTES:raise ValueError('bounded preparation bytes required')
        self.frames=tuple(frames);self.source=source;self.transport=transport
        self.identity_factory=identity_factory;self.preserve=preserve;self.preserve_failure=preserve_failure;self.stop_event=stop_event
        self.byte_capacity=byte_capacity;self.peak_pending=0;self.peak_bytes=0;self.completed=0;self.delivered=0
        self._condition=threading.Condition();self._pending=deque();self._stop=False;self._done=False
        self.error=None;self.preservation_error=None;self._failed_preservations=[]
        # A reservation includes the source read currently in flight. Thus a
        # slow reader cannot allocate an uncharged fifth Raw frame. The DMA
        # owner receives immutable, already SHA-checked bytes, in source order.
        self.prepare_capacity_frames=prepare_capacity_frames;self.prepare_byte_capacity=prepare_byte_capacity
        self._prepared=deque();self._prepare_reserved_frames=0;self._prepare_reserved_bytes=0
        self._prepare_done=False;self._prepare_error=None;self._prepare_thread=None
        self.prepared=0;self.peak_prepared_frames=0;self.peak_prepared_bytes=0;self.prepared_preserved=0
        if prepare_capacity_frames:
            self._prepare_thread=threading.Thread(target=self._prepare_run,name='r30-bounded-Raw-preparation',daemon=True)
            self._prepare_thread.start()
        self._thread=threading.Thread(target=self._run,name='r25-bounded-PL-acquisition',daemon=True)
        self._thread.start()
    def _stopping(self):return self._stop or (self.stop_event is not None and self.stop_event.is_set())
    def _retain(self,packet):
        try:self.preserve(packet)
        except BaseException as exc:
            self._failed_preservations.append(packet)
            self.preservation_error=type(exc).__name__+': '+str(exc);raise
    def _retain_failure(self,index,raw,identity,error):
        try:self.preserve_failure(index,raw,identity,error)
        except BaseException as failure:
            self.preservation_error=type(failure).__name__+': '+str(failure)
            self._failed_preservations.append((index,raw,identity,error))
    def _read_source(self,saved):
        def checked_read():
            raw=self.source.read_frame(saved['frame_id'])
            if type(raw) is not bytes or len(raw)!=RAW_BYTES or payload_sha256(raw)!=saved['raw_sha256']:
                # Keep the original rejected bytes for the failure callback.
                self._source_rejected_raw=raw
                raise ValueError('producer source Raw identity mismatch')
            return raw
        raw,decode_time=self._timed(checked_read)
        return saved['frame_id'],raw,decode_time,time.monotonic_ns()
    def _prepare_run(self):
        entry=None;index=None;raw=None;reserved=False
        try:
            for saved in self.frames:
                with self._condition:
                    while (self._prepare_reserved_frames>=self.prepare_capacity_frames or
                           self._prepare_reserved_bytes+RAW_BYTES>self.prepare_byte_capacity) and not self._stopping():
                        self._condition.wait(.05)
                    if self._stopping():break
                    self._prepare_reserved_frames+=1;self._prepare_reserved_bytes+=RAW_BYTES;reserved=True
                    self.peak_prepared_frames=max(self.peak_prepared_frames,self._prepare_reserved_frames)
                    self.peak_prepared_bytes=max(self.peak_prepared_bytes,self._prepare_reserved_bytes)
                index=saved['frame_id'];raw=None;self._source_rejected_raw=None
                entry=self._read_source(saved);raw=entry[1];self.prepared+=1
                with self._condition:
                    if self._stopping():break
                    self._prepared.append(entry);entry=None;raw=None;reserved=False;self._condition.notify_all()
        except BaseException as exc:
            self._prepare_error=type(exc).__name__+': '+str(exc)
            self._retain_failure(index,raw if raw is not None else getattr(self,'_source_rejected_raw',None),None,self._prepare_error)
        finally:
            if entry is not None:
                self._retain_failure(entry[0],entry[1],None,'prepared Raw stopped before PL');self.prepared_preserved+=1
            with self._condition:
                if reserved:self._prepare_reserved_frames-=1;self._prepare_reserved_bytes-=RAW_BYTES
                self._prepare_done=True;self._condition.notify_all()
    def _next_source(self,saved):
        if self._prepare_thread is None:return self._read_source(saved)
        with self._condition:
            while not self._prepared:
                if self._prepare_error:raise _PreparationFailed(self._prepare_error)
                if self._stopping() or self._prepare_done:raise StopIteration
                self._condition.wait(.05)
            if self._stopping():raise StopIteration
            entry=self._prepared.popleft();self._prepare_reserved_frames-=1;self._prepare_reserved_bytes-=RAW_BYTES
            self._condition.notify_all()
            if entry[0]!=saved['frame_id']:raise RuntimeError('prepared Raw order mismatch')
            return entry
    @staticmethod
    def _timed(call,*args,**kwargs):
        start=time.perf_counter();cpu=time.thread_time();result=call(*args,**kwargs)
        return result,dict(wall_s=time.perf_counter()-start,thread_cpu_s=time.thread_time()-cpu)
    def _run(self):
        packet=None;index=None;raw=None;identity=None
        try:
            for saved in self.frames:
                with self._condition:
                    while self._pending and not self._stopping():self._condition.wait(.05)
                    if self._stopping():break
                index=saved['frame_id']
                raw=None;identity=None
                try:index,raw,decode_time,source_ready_ns=self._next_source(saved)
                except StopIteration:break
                identity=self.identity_factory(index)
                values,pl_time=self._timed(self.transport.run,np.frombuffer(raw,np.uint8).reshape(480,752),identity=identity)
                final,primary,receipt,admission=values
                packet=ProducedPLFrame(index,raw,identity,final,primary,receipt,admission,
                    dict(decode_hash=decode_time,actual_PL_transport=pl_time),source_ready_ns)
                self.completed+=1
                if packet.bytes>self.byte_capacity:raise BufferError('complete producer frame exceeds byte capacity')
                with self._condition:
                    if self._stopping():break
                    self._pending.append(packet);packet=None
                    self.peak_pending=max(self.peak_pending,len(self._pending))
                    self.peak_bytes=max(self.peak_bytes,self._pending[0].bytes);self._condition.notify_all()
        except BaseException as exc:
            self.error=type(exc).__name__+': '+str(exc)
            if packet is None and not isinstance(exc,_PreparationFailed):
                self._retain_failure(index,raw if raw is not None else getattr(self,'_source_rejected_raw',None),identity,self.error)
        finally:
            if packet is not None:
                try:self._retain(packet)
                except BaseException:pass
            with self._condition:self._done=True;self._condition.notify_all()
    def take(self,timeout_s=6.):
        if not .01<=timeout_s<=10:raise ValueError('bounded producer wait')
        deadline=time.monotonic()+timeout_s
        with self._condition:
            while not self._pending:
                if self._done:
                    if self.error or self.preservation_error:raise RuntimeError('producer failed: '+str(self.error)+'; preserve='+str(self.preservation_error))
                    raise StopIteration
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('bounded PL producer wait')
                self._condition.wait(remaining)
            packet=self._pending.popleft();self.delivered+=1;self._condition.notify_all();return packet
    def close(self,timeout_s=6.):
        deadline=time.monotonic()+timeout_s
        with self._condition:self._stop=True;self._condition.notify_all()
        self._thread.join(max(0.,deadline-time.monotonic()))
        if self._thread.is_alive():raise RuntimeError('PL acquisition worker unconfirmed; retain DMA/owner')
        if self._prepare_thread is not None:
            self._prepare_thread.join(max(0.,deadline-time.monotonic()))
            if self._prepare_thread.is_alive():raise RuntimeError('Raw preparation worker unconfirmed; retain source/owner')
        # No producer can append after joining. Pending output was never
        # accepted by the consumer and must be kept as complete source evidence.
        while self._pending:self._retain(self._pending.popleft())
        while self._prepared:
            entry=self._prepared.popleft()
            self._retain_failure(entry[0],entry[1],None,'prepared Raw stopped before PL');self.prepared_preserved+=1
            self._prepare_reserved_frames-=1;self._prepare_reserved_bytes-=RAW_BYTES
        if self.preservation_error:raise RuntimeError('unconsumed PL source preservation failed: '+self.preservation_error)
        return dict(worker_joined=True,completed=self.completed,delivered=self.delivered,
            peak_pending=self.peak_pending,peak_bytes=self.peak_bytes,capacity_frames=1,
            byte_capacity=self.byte_capacity,error=self.error,unconsumed_sources_preserved=True,
            preparation=dict(enabled=self._prepare_thread is not None,worker_joined=True,prepared=self.prepared,
                capacity_frames=self.prepare_capacity_frames,byte_capacity=self.prepare_byte_capacity,
                peak_frames=self.peak_prepared_frames,peak_bytes=self.peak_prepared_bytes,
                unconsumed_preserved=self.prepared_preserved,error=self._prepare_error))
