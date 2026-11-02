"""Session display owner: one rendering frame and one replaceable pending frame.

An explicit owner factory is required. Offline fake ownership never proves HDMI.
Full measurements must be persisted independently of display preview sampling.
"""
from collections import deque
import threading
import time
import numpy as np
from structured_light.scan3d.dashboard import DashboardStatus


class SessionDisplayStartupError(RuntimeError):
    def __init__(self,message,display):
        super().__init__(message)
        self.display=display


class SessionDisplayCloseError(RuntimeError):
    def __init__(self,message,receipt,display):
        super().__init__(message);self.receipt=receipt;self.display=display


class SessionDisplay:
    def __init__(self,*,owner_factory,renderer_factory,config,cloud_bytes=64*1024*1024,
                 visible_points=24000,startup_timeout_s=5.):
        if type(cloud_bytes) is not int or not 24<=cloud_bytes<=64*1024*1024:raise ValueError('cloud budget exceeded')
        if type(visible_points) is not int or not 1<=visible_points<=24000:raise ValueError('preview scratch budget exceeded')
        self._cloud=np.empty((cloud_bytes//24,3),np.float64);self._count=0
        self._pending_gray=np.empty((480,752),np.uint8)
        self._active_gray=np.empty((480,752),np.uint8)
        self.visible_points=visible_points;self.config=dict(config)
        self._condition=threading.Condition();self._pending=None;self._active=False;self._closing=False
        self.error=None;self.owner_closed=False;self.ready=False;self.target=None
        self.warmup_receipt=None
        self.submitted=self.acknowledged=self.dropped=0;self.peak_frames=0;self.records=deque(maxlen=1024)
        self.owner_factory=owner_factory;self.renderer_factory=renderer_factory
        self._thread=threading.Thread(target=self._run,name='r22-session-display',daemon=True);self._thread.start()
        deadline=time.monotonic()+startup_timeout_s
        failure=None
        with self._condition:
            while not self.ready and self.error is None:
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    failure='display owner startup unconfirmed'
                    break
                self._condition.wait(remaining)
            failure=failure or self.error
            if failure:
                self._closing=True;self._condition.notify_all()
        if failure:
            # Late configure completion must see closing and release ownership.
            # The exception retains the object if the owner cannot yet join.
            self._thread.join(min(1.,startup_timeout_s))
            raise SessionDisplayStartupError(failure,self)

    def _run(self):
        owner=None
        try:
            owner=self.owner_factory(self.config);owner.configure();renderer=self.renderer_factory(self.config)
            if self.config.get('warmup',False):
                # Force lazy VDMA/frame-store initialization into cold start.
                # Empty waiting canvas has no measurement/PL annotations and
                # its ACK is never included in target frame-rate accounting.
                import cv2
                warm_started=time.perf_counter()
                canvas=renderer.render(np.zeros((480,752),np.uint8),np.empty((0,3),np.float64),
                    status=DashboardStatus(mode='SIMULATION',source='INITIALIZATION_NO_MEASUREMENT',frame_number=0,
                        angle_deg=0.,quality='waiting',progress_pct=0.),
                    cloud_revision=('INITIALIZATION',0,0))
                cv2.rectangle(canvas,(0,0),(1279,23),(0,40,70),-1)
                cv2.putText(canvas,'DISPLAY READY / WAITING FOR CURRENT PL RESULTS / NO MEASUREMENT',
                    (12,17),cv2.FONT_HERSHEY_SIMPLEX,.48,(255,235,160),1,cv2.LINE_AA)
                receipt=owner.show_canvas(canvas)
                if not isinstance(receipt,dict):raise RuntimeError('display warmup ACK mapping missing')
                self.warmup_receipt=dict(duration_s=time.perf_counter()-warm_started,receipt=receipt,
                    included_in_measurement_rate=False,observed_points=0)
            with self._condition:self.ready=True;self._condition.notify_all()
            while True:
                with self._condition:
                    while self._pending is None and not self._closing:self._condition.wait()
                    if self._pending is None:return
                    index,angle,target,annotations,submit_perf,submit_ns=self._pending;self._pending=None;self._active=True
                    self._active_gray[:]=self._pending_gray;gray=self._active_gray
                    stride=max(1,(self._count+self.visible_points-1)//self.visible_points)
                    points=self._cloud[:self._count:stride].copy();count=self._count
                renderer.target=target;renderer.full_cloud_points=count
                status=DashboardStatus(mode='SIMULATION',source=self.config.get('display_source','offline fake display'),frame_number=index,
                    angle_deg=float(np.degrees(angle)),quality='simulation',progress_pct=min(100.,100*(index+1)/self.config.get('frame_total',240)))
                render_start=time.perf_counter();render_cpu=time.thread_time()
                options={} if annotations is None else {'annotations':annotations}
                canvas=renderer.render(gray,points,status=status,cloud_revision=(target,index,count),**options)
                render_s=time.perf_counter()-render_start;render_cpu_s=time.thread_time()-render_cpu
                owner_start=time.perf_counter();owner_cpu=time.thread_time()
                receipt=owner.show_canvas(canvas)
                ack_perf=time.perf_counter();ack_ns=time.monotonic_ns()
                if ack_ns<submit_ns:raise ValueError('display ACK precedes submit monotonic clock')
                with self._condition:
                    self.acknowledged+=1;self.records.append(dict(frame_id=index,target=target,cloud_points=count,receipt=receipt,
                        ack_perf_s=ack_perf,ack_monotonic_ns=ack_ns,submit_perf_s=submit_perf,
                        submit_monotonic_ns=submit_ns,submit_to_ack_s=(ack_ns-submit_ns)/1e9,
                        render_s=render_s,render_thread_cpu_s=render_cpu_s,
                        owner_s=time.perf_counter()-owner_start,owner_thread_cpu_s=time.thread_time()-owner_cpu,
                        renderer_timings=dict(getattr(renderer,'timings',{})),
                        annotation_points=0 if annotations is None else annotations.point_count))
                    self._active=False;self._condition.notify_all()
        except BaseException as exc:
            with self._condition:self.error=f'{type(exc).__name__}: {exc}';self._condition.notify_all()
        finally:
            try:
                if owner is not None:
                    receipt=owner.close(timeout_s=5.)
                    self.owner_closed=bool(receipt.get('owner_closed',receipt.get('clean',False)))
                    if not self.owner_closed:raise RuntimeError('owner close unconfirmed')
            except BaseException as exc:self.error=self.error or f'{type(exc).__name__}: {exc}'
            with self._condition:self._active=False;self._condition.notify_all()

    def begin_target(self,target,timeout_s=5.):
        deadline=time.monotonic()+timeout_s
        with self._condition:
            while self._active or self._pending is not None:
                if self.error:raise RuntimeError(self.error)
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('target display drain timeout')
                self._condition.wait(remaining)
            if self.error or self._closing:raise RuntimeError(self.error or 'display closed')
            self.target=str(target);self._count=0

    def submit(self,gray,delta,index,angle,annotations=None):
        image=np.asarray(gray);points=np.asarray(delta)
        if image.shape!=(480,752) or image.dtype!=np.uint8:raise ValueError('full native gray required')
        if points.ndim!=2 or points.shape[1:]!=(3,) or not np.isfinite(points).all():raise ValueError('finite XYZ required')
        if type(index) is not int or not np.isfinite(angle):raise ValueError('invalid frame identity/angle')
        if annotations is not None:
            from candidate.display_pfo3_r25 import annotation_snapshot
            annotations=annotation_snapshot(annotations)
            if annotations.frame_id!=index:raise ValueError('annotation frame identity mismatch')
        with self._condition:
            if self.error or self._closing or self.target is None:raise RuntimeError(self.error or 'no active display target')
            if self._count+len(points)>len(self._cloud):raise BufferError('display cloud capacity exceeded; full storage remains separate')
            self._cloud[self._count:self._count+len(points)]=points;self._count+=len(points)
            self.dropped+=self._pending is not None;self.submitted+=1
            self._pending_gray[:]=image
            # Timing only: this is the submit moment (after all copies, before
            # the renderer can observe the frame), not a physical capture time.
            self._pending=(index,float(angle),self.target,annotations,time.perf_counter(),time.monotonic_ns())
            self.peak_frames=max(self.peak_frames,int(self._active)+1);self._condition.notify_all()

    def close(self,timeout_s=5.):
        if not np.isfinite(timeout_s) or timeout_s<0:raise ValueError('finite close deadline required')
        with self._condition:self._closing=True;self._condition.notify_all()
        self._thread.join(timeout_s);joined=not self._thread.is_alive()
        timing_summary=None
        try:
            from candidate.display_process_r25 import display_timing_summary
            timing_summary=display_timing_summary(list(self.records))
        except Exception:
            timing_summary=None
        result=dict(owner_closed=self.owner_closed,worker_joined=joined,error=self.error,
            cleanup_confirmed=bool(joined and self.owner_closed),
            cold_start_warmup=self.warmup_receipt,
            submitted=self.submitted,acknowledged=self.acknowledged,dropped=self.dropped,
            peak_frames=self.peak_frames,cloud_points=self._count,records=list(self.records),
            timing_summary=timing_summary)
        if not joined or self.error or not self.owner_closed:
            raise SessionDisplayCloseError('display run failed or cleanup unconfirmed: '+str(result),result,self)
        if self.submitted!=self.acknowledged+self.dropped:
            raise SessionDisplayCloseError('display ACK accounting mismatch',result,self)
        return result
