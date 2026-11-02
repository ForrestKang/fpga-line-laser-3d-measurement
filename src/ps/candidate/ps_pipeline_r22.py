"""Disk-backed replay cloud; full data is never capped or silently truncated."""
from collections import deque
from pathlib import Path
import os
import threading
import time
import numpy as np
from structured_light.scan3d.multibranch_processor_r21 import MultiBranchProcessor
from structured_light.scan3d.continuous_processor import CloudSnapshot


class _SpoolStreamView:
    """Compatibility view whose flush() also drains admitted spool writes.

    External readers historically called ``spool.stream.flush()`` and then
    memory-mapped ``spool.path``. The background writer must be drained first,
    otherwise such a reader would observe a short file. This view preserves the
    old call site without exposing the worker's file for writes.
    """
    def __init__(self,spool):self._spool=spool
    def flush(self):
        if self._spool._close_error is not None:
            raise RuntimeError('cloud spool closed with an unconfirmed cloud: '+self._spool._close_error)
        if not self._spool._closed:self._spool.drain()
        if not self._spool._stream.closed:self._spool._stream.flush()
    def fileno(self):return self._spool._stream.fileno()
    @property
    def closed(self):return self._spool._closed or self._spool._stream.closed
    def __getattr__(self,name):return getattr(self._spool._stream,name)


class CloudSpool:
    """Ordered float64 XYZ spool with bounded background durability.

    ``append`` validates the complete finite float64 block, reserves the exact
    storage bound and then hands an owned copy to one FIFO worker. The worker
    writes in append order and settles the storage reservation only after the
    write returned. Failures are retained and raised by the next append, drain,
    snapshot, export or close; they are never converted into a short cloud.

    The queue is bounded by ``queue_bytes`` and applies backpressure (no drop).
    ``drain/close/export`` flush and fsync the complete file, and external
    readers that call ``stream.flush()`` first are drained transparently.
    Set ``background=False`` to use the previous synchronous write path.
    """
    def __init__(self,path,*,storage_budget,snapshot_bytes=64*1024*1024,scratch_bytes=32*1024*1024,
                 background=True,queue_bytes=8*1024*1024):
        self.path=Path(path);self._stream=self.path.open('x+b');self.stream=_SpoolStreamView(self)
        self.count=0;self.storage_budget=storage_budget
        self.snapshot_bytes=snapshot_bytes;self.scratch_bytes=scratch_bytes
        if not 1<=snapshot_bytes<=64*1024*1024 or not 1<=scratch_bytes<=32*1024*1024:
            self._stream.close();raise ValueError('cloud/scratch budget exceeded')
        if type(background) is not bool:raise ValueError('explicit background mode required')
        if type(queue_bytes) is not int or not 1<=queue_bytes<=64*1024*1024:
            self._stream.close();raise ValueError('cloud queue byte budget exceeded')
        self.background=background;self.queue_bytes=queue_bytes
        self._budget_lock=threading.Lock();self._condition=threading.Condition()
        self._queue=deque();self._pending_bytes=0;self._written_points=0
        self._error=None;self._close_error=None;self._closing=False;self._closed=False
        self._worker=None;self.last_append_timing=dict(wall_s=0.,thread_cpu_s=0.)
        self.last_write_timing=dict(wall_s=0.,points=0,bytes=0)
        if background:
            self._worker=threading.Thread(target=self._run_worker,name='r26-cloud-spool-writer',daemon=True)
            self._worker.start()

    def _budget_reserve(self,size):
        with self._budget_lock:return self.storage_budget.reserve(size)

    def _budget_commit(self,token,size):
        with self._budget_lock:self.storage_budget.commit(token,size)

    @property
    def pending_bytes(self):
        with self._condition:return self._pending_bytes

    def _failed_locked(self):
        if self._error is not None:raise RuntimeError('cloud spool background write failed: '+self._error)

    def _run_worker(self):
        while True:
            with self._condition:
                while not self._queue and not self._closing:self._condition.wait()
                if not self._queue and self._closing:return
                points,token=self._queue.popleft()
            try:
                write_started=time.perf_counter()
                self._stream.write(memoryview(points).cast('B'))
                # Match the old crash-visibility of each admitted append without
                # an fsync per frame; the final close/export still fsyncs.
                self._stream.flush()
                self._budget_commit(token,points.nbytes)
                self.last_write_timing=dict(wall_s=time.perf_counter()-write_started,
                                            points=len(points),bytes=points.nbytes)
            except BaseException as exc:
                with self._condition:
                    self._error=type(exc).__name__+': '+str(exc);self._condition.notify_all()
                return
            with self._condition:
                self._pending_bytes-=points.nbytes;self._written_points+=len(points)
                self._condition.notify_all()

    def append(self,points):
        started=time.perf_counter();cpu=time.thread_time()
        a=np.asarray(points)
        if a.dtype!=np.float64 or a.ndim!=2 or a.shape[1]!=3 or not np.isfinite(a).all():
            raise ValueError('full finite float64 XYZ required')
        if a.nbytes>self.scratch_bytes:raise ValueError('cloud delta exceeds scratch budget')
        if not len(a):return
        if not self.background:
            token=self._budget_reserve((a.nbytes*5+3)//4)
            # A partial write leaves its reservation live and propagates failure.
            self._stream.write(memoryview(np.ascontiguousarray(a)).cast('B'))
            self._stream.flush();self.count+=len(a);self._written_points+=len(a)
            self._budget_commit(token,a.nbytes)
            self.last_append_timing=dict(wall_s=time.perf_counter()-started,thread_cpu_s=time.thread_time()-cpu)
            return
        owned=np.array(a,dtype=np.float64,order='C',copy=True);owned.setflags(write=False)
        if owned.nbytes>self.queue_bytes:
            raise ValueError('cloud delta exceeds bounded queue; split or raise queue_bytes')
        with self._condition:
            while True:
                self._failed_locked()
                if self._closed or self._closing:raise RuntimeError('cloud spool is closed')
                if self._pending_bytes+owned.nbytes<=self.queue_bytes:break
                if not self._condition.wait(timeout=1.0):
                    self._failed_locked()
            token=self._budget_reserve((owned.nbytes*5+3)//4)
            self._queue.append((owned,token));self._pending_bytes+=owned.nbytes;self.count+=len(owned)
            self._condition.notify_all()
        self.last_append_timing=dict(wall_s=time.perf_counter()-started,thread_cpu_s=time.thread_time()-cpu)

    def drain(self,timeout_s=10.):
        """Wait until every admitted append has been written, then flush."""
        if type(timeout_s) in (int,float) and not .01<=float(timeout_s)<=60.:
            raise ValueError('bounded cloud drain deadline required')
        if self._closed:
            if self._close_error is not None:raise RuntimeError('cloud spool closed with an unconfirmed cloud: '+self._close_error)
            return
        deadline=time.monotonic()+float(timeout_s)
        with self._condition:
            while True:
                self._failed_locked()
                if not self._queue and self._pending_bytes==0:break
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('bounded cloud spool drain expired')
                self._condition.wait(remaining)
        if not self._stream.closed:self._stream.flush()

    def chunks(self,chunk_bytes=None):
        if chunk_bytes is None:chunk_bytes=min(1024*1024,self.scratch_bytes)
        if not 24<=chunk_bytes<=self.scratch_bytes:raise ValueError('invalid cloud chunk budget')
        self.drain()
        with self.path.open('rb') as source:
            while True:
                data=source.read(chunk_bytes//24*24)
                if not data:return
                yield np.frombuffer(data,np.float64).reshape(-1,3)

    def snapshot(self):
        if self.count*24>self.snapshot_bytes:
            raise BufferError('full cloud exceeds snapshot RAM budget; use chunks/export_npy')
        self.drain()
        a=np.fromfile(self.path,dtype=np.float64).reshape(-1,3);a.setflags(write=False);return a

    def export_npy(self,path):
        # Stream header + complete cloud without concatenate or a second array.
        self.drain()
        if self._written_points!=self.count:
            raise RuntimeError('cloud spool logical/written point count mismatch')
        token=self._budget_reserve((self.count*24+256)*5//4+1)
        with Path(path).open('xb') as output:
            np.lib.format.write_array_header_1_0(output,dict(descr='<f8',fortran_order=False,shape=(self.count,3)))
            for chunk in self.chunks():output.write(memoryview(chunk).cast('B'))
            output.flush();os.fsync(output.fileno());size=output.tell()
        self._budget_commit(token,size)

    def close(self,timeout_s=10.):
        """Drain/fsync once; repeat calls are no-ops and never hide an error."""
        if self._closed:return
        if type(timeout_s) in (int,float) and not .01<=float(timeout_s)<=60.:
            raise ValueError('bounded cloud close deadline required')
        with self._condition:
            self._closing=True;self._condition.notify_all()
        if self._worker is not None:
            self._worker.join(float(timeout_s))
            if self._worker.is_alive():
                # A write cannot be interrupted safely. Retain the file and the
                # worker so a caller can retry/close or report external recovery;
                # never claim a short cloud was durably closed.
                raise RuntimeError('cloud spool writer unconfirmed; file retained')
        with self._condition:
            error=self._error;unwritten=self._pending_bytes;points=self.count;written=self._written_points
        if not self._stream.closed:
            self._stream.flush();os.fsync(self._stream.fileno());self._stream.close()
        with self._condition:
            if error is not None:self._close_error='background write failed: '+error+'; unwritten_bytes='+str(unwritten)
            elif written!=points:self._close_error='logical/written point count mismatch'
            self._closed=True;self._condition.notify_all()
        if self._close_error is not None:
            raise RuntimeError('cloud spool closed with an unconfirmed cloud: '+self._close_error)


class ReplayProcessor(MultiBranchProcessor):
    @classmethod
    def with_spool(cls,config,spool,*,extractor,max_frames=240):
        self=cls.from_virtual_config(config,extractor=extractor,max_cloud_points=max_frames*3840)
        self._cloud=spool
        return self

    def _stage(self,name,call,*args,**kwargs):
        started=time.perf_counter();cpu=time.thread_time()
        result=call(*args,**kwargs)
        self.last_stage_timings[name]=dict(wall_s=time.perf_counter()-started,thread_cpu_s=time.thread_time()-cpu)
        return result

    def _admit_replay(self,*args,**kwargs):
        return self._stage('admit',super()._admit_replay,*args,**kwargs)

    def _observations(self,*args,**kwargs):
        return self._stage('observations',super()._observations,*args,**kwargs)

    def _quality(self,*args,**kwargs):
        return self._stage('quality',super()._quality,*args,**kwargs)

    def process(self,*args,**kwargs):
        # Instrument the shared frozen processor without copying its admission,
        # registration or geometry decisions into a second implementation.
        self.last_stage_timings={}
        self._cloud.last_append_timing=dict(wall_s=0.,thread_cpu_s=0.)
        result=self._stage('total',super().process,*args,**kwargs)
        from candidate.geometry_fast_r21 import last_call_info
        geometry=last_call_info().get('timing')
        if geometry is not None:self.last_stage_timings['geometry']=geometry
        self.last_stage_timings['cloud_append']=dict(self._cloud.last_append_timing)
        self.last_stage_timings['registration_reasons_result']={key:max(0.,value-sum(
            timing[key] for name,timing in self.last_stage_timings.items() if name!='total'))
            for key,value in self.last_stage_timings['total'].items()}
        return result

    def snapshot(self):
        with self._lock:return CloudSnapshot(self._revision,self._cloud.snapshot(),self.reference_basis)
