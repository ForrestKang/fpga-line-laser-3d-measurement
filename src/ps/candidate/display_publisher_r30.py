"""Bounded, non-I/O model publication in front of an owned display backend.

The caller still owns begin_target()/display.close(). Call drain() before a
target change. Only this worker calls display.submit(). Coalescing replaces the
camera/annotation snapshot but retains *every* point increment in order. It is
never permission to drop measurement data, nor proof of an HDMI ACK.
"""
from collections import deque
from dataclasses import dataclass
import math
import threading
import time
import numpy as np

RAW_BYTES=752*480
MAX_BYTES=64*1024**2
DESCRIPTOR_BYTES=4096


@dataclass(frozen=True)
class _Snapshot:
    raw:bytes
    chunks:tuple
    index:int
    angle:float
    annotations:object
    annotation_bytes:int
    source_ns:object
    point_bytes:int
    sources:int=1
    @property
    def charged_bytes(self):
        # Charge both retained chunks and the contiguous worker merge. Shared
        # display memory belongs to the backend and has its own fixed budget.
        return RAW_BYTES+2*self.point_bytes+self.annotation_bytes+DESCRIPTOR_BYTES


class DisplayPublisherR30:
    """submit() snapshots RAM only; worker owns potentially blocking display I/O.

    max_pending is deliberately fixed at two. Byte capacity includes the active
    worker item and merge allocation, not just queued descriptors. Capacity
    exhaustion raises before acceptance; callers must stop or explicitly retry
    that exact delta. On backend failure all undelivered snapshots stay owned by
    this instance for diagnosis; close() raises and never claims a clean drain.
    """
    def __init__(self,display,*,byte_capacity=8*1024**2,max_delta_chunks=128):
        if type(byte_capacity) is not int or not RAW_BYTES+DESCRIPTOR_BYTES<=byte_capacity<=MAX_BYTES:
            raise ValueError('bounded display publisher byte capacity required')
        if type(max_delta_chunks) is not int or not 1<=max_delta_chunks<=128:
            raise ValueError('bounded display delta chunks required')
        self.display=display;self.byte_capacity=byte_capacity;self.max_delta_chunks=max_delta_chunks
        self._condition=threading.Condition();self._pending=deque();self._active=None
        self._retained_bytes=0;self._closing=False;self._done=False;self.error=None
        self.submitted=0;self.published=0;self.coalesced=0
        self.accepted_points=0;self.published_points=0;self.peak_pending=0;self.peak_bytes=0
        self._thread=threading.Thread(target=self._run,name='r30-display-publisher',daemon=True)
        self._thread.start()
    @staticmethod
    def _annotations(value,index):
        if value is None:return None,0
        from .display_pfo3_r25 import annotation_snapshot,DeferredPLAnnotations
        from r33.display import DeferredR33Annotations
        if type(value) is not DeferredR33Annotations:value=annotation_snapshot(value)
        if value.frame_id!=index:raise ValueError('display annotation/frame identity mismatch')
        if type(value) is DeferredR33Annotations:
            # The source stays retained while the worker allocates its bounded
            # compact sample. Charge both, including marker output scratch.
            size=value.retained_bytes+value.point_count*128+DESCRIPTOR_BYTES
        elif type(value) is DeferredPLAnnotations:
            if type(value.frame.payload) is not bytes:raise ValueError('immutable display source payload required')
            size=len(value.frame.payload)+DESCRIPTOR_BYTES
        else:size=value.point_count*128+DESCRIPTOR_BYTES
        return value,size
    def _raise_failed(self):
        if self.error:raise RuntimeError('display publisher failed: '+self.error)
    def submit(self,gray,delta,index,angle,annotations=None,source_capture_monotonic_ns=None):
        # Validated acquisition bytes already have immutable ownership. Keep
        # them directly; the ndarray compatibility path takes one owned copy.
        raw=gray if type(gray) is bytes else None
        image=None if raw is not None else np.asarray(gray)
        points=np.asarray(delta)
        if ((raw is not None and len(raw)!=RAW_BYTES) or
                (raw is None and (image.shape!=(480,752) or image.dtype!=np.uint8))):
            raise ValueError('native display image required')
        if points.ndim!=2 or points.shape[1]!=3 or points.dtype!=np.float64 or not np.isfinite(points).all():
            raise ValueError('finite float64 Nx3 display delta required')
        if type(index) is not int or not 0<=index<=0xffffffff or not math.isfinite(float(angle)):
            raise ValueError('bounded display frame metadata required')
        if source_capture_monotonic_ns is not None and (type(source_capture_monotonic_ns) is not int or source_capture_monotonic_ns<=0):
            raise ValueError('positive source monotonic timestamp required')
        annotations,annotation_bytes=self._annotations(annotations,index)
        with self._condition:
            self._raise_failed()
            if self._closing:raise RuntimeError('display publisher is closing')
            old=self._pending[-1] if len(self._pending)==2 else None
            chunks=old.chunks if old is not None else ()
            if len(points) and len(chunks)>=self.max_delta_chunks:raise BufferError('display delta chunk capacity reached')
            point_bytes=(old.point_bytes if old is not None else 0)+points.nbytes
            charge=RAW_BYTES+2*point_bytes+annotation_bytes+DESCRIPTOR_BYTES
            retained=self._retained_bytes-(old.charged_bytes if old is not None else 0)+charge
            if retained>self.byte_capacity:raise BufferError('display publisher byte capacity reached')
            # Immutable bytes break ownership ties even when the caller passed
            # a writable ndarray/view reused on the next acquisition.
            if len(points):chunks=chunks+(points.tobytes(order='C'),)
            snapshot=_Snapshot(raw if raw is not None else image.tobytes(order='C'),chunks,index,float(angle),annotations,annotation_bytes,
                source_capture_monotonic_ns,point_bytes,(old.sources if old is not None else 0)+1)
            if old is not None:self._pending[-1]=snapshot;self.coalesced+=1
            else:self._pending.append(snapshot)
            self._retained_bytes=retained;self.submitted+=1;self.accepted_points+=len(points)
            self.peak_pending=max(self.peak_pending,len(self._pending));self.peak_bytes=max(self.peak_bytes,retained)
            self._condition.notify_all()
    def _run(self):
        try:
            while True:
                with self._condition:
                    while not self._pending and not self._closing:self._condition.wait()
                    if not self._pending:break
                    snapshot=self._pending.popleft();self._active=snapshot
                chunks=[np.frombuffer(chunk,np.float64).reshape(-1,3) for chunk in snapshot.chunks]
                points=(chunks[0] if len(chunks)==1 else np.concatenate(chunks) if chunks else np.empty((0,3),np.float64))
                from r33.display import DeferredR33Annotations
                annotations=snapshot.annotations
                if type(annotations) is DeferredR33Annotations:
                    annotations=annotations.materialize_compact()
                self.display.submit(np.frombuffer(snapshot.raw,np.uint8).reshape(480,752),points,
                    snapshot.index,snapshot.angle,annotations=annotations,
                    source_capture_monotonic_ns=snapshot.source_ns)
                with self._condition:
                    self.published+=1;self.published_points+=snapshot.point_bytes//24
                    charge=snapshot.charged_bytes
                    chunks=None;points=None;annotations=None;snapshot=None;self._active=None
                    self._retained_bytes-=charge;self._condition.notify_all()
        except BaseException as exc:
            with self._condition:
                self.error=type(exc).__name__+': '+str(exc);self._closing=True;self._condition.notify_all()
        finally:
            with self._condition:self._done=True;self._condition.notify_all()
    def drain(self,timeout_s=6.):
        if not math.isfinite(timeout_s) or not 0<timeout_s<=120:raise ValueError('bounded display drain required')
        deadline=time.monotonic()+timeout_s
        with self._condition:
            while self._pending or self._active is not None:
                self._raise_failed()
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('display publisher drain unconfirmed; retain snapshots/owner')
                self._condition.wait(remaining)
            self._raise_failed()
        return self.summary()
    def summary(self):
        with self._condition:
            return dict(submitted=self.submitted,published=self.published,coalesced=self.coalesced,
                accepted_points=self.accepted_points,published_points=self.published_points,
                pending=len(self._pending),active=self._active is not None,retained_bytes=self._retained_bytes,
                peak_pending=self.peak_pending,peak_bytes=self.peak_bytes,byte_capacity=self.byte_capacity,
                capacity_pending=2,error=self.error,worker_joined=not self._thread.is_alive())
    def close(self,timeout_s=6.):
        if not math.isfinite(timeout_s) or not 0<timeout_s<=120:raise ValueError('bounded display close required')
        with self._condition:self._closing=True;self._condition.notify_all()
        self._thread.join(timeout_s)
        if self._thread.is_alive():raise RuntimeError('display publisher worker unconfirmed; retain snapshots/owner')
        with self._condition:self._raise_failed()
        return self.summary()
