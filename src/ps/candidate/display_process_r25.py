"""Opt-in spawn display: two native image/archive slots, one bounded cloud.

Only the child renders or calls the supplied HDMI owner. Shared PFO3 views
are display-only: header/length/CRC checks do not confer geometry admission.
No measurement, PL allocator, overlay or DMA object crosses this boundary.
Unconfirmed owner cleanup retains both the child and the parent's lock FD.
"""
from collections import deque
import ctypes
import inspect
import json
import math
import multiprocessing as mp
import os
import struct
import time
from types import MappingProxyType
import zlib
import numpy as np
from virtual_scan.display_r22 import SessionDisplayStartupError
from .display_fast_r26 import (performance_summary, sample_points_bounded,
                               validate_cadence, validate_sample_cap)

RAW_BYTES=752*480
ARCHIVE_SLOT_BYTES=12*1024*1024
MIN_ARCHIVE_SLOT_BYTES=256+480*64
MAX_EVENT_BYTES=8*1024
EVENT_SLOTS=8
CONTROL_TIMEOUT_S=.1
MAX_TARGET_BYTES=128


def startup_canvas_budget(config):
    budget=config.get('display_startup_canvas_timeout_s',20.)
    if isinstance(budget,bool) or not isinstance(budget,(int,float)) or not math.isfinite(budget) or not 5.<=budget<=30.:
        raise ValueError('startup canvas timeout must be in [5, 30] s')
    deadline=config.get('display_startup_deadline_monotonic')
    if deadline is not None:
        if isinstance(deadline,bool) or not isinstance(deadline,(int,float)) or not math.isfinite(deadline):
            raise ValueError('finite monotonic display startup deadline required')
        budget=min(float(budget),float(deadline)-time.monotonic())
    if budget<=0:raise TimeoutError('display startup deadline expired before warmup canvas')
    return float(budget)


def display_timing_summary(records) -> dict:
    """Actual ACK-update timing from immutable per-ACK display records.

    The acceptance goal is a *display update* rate, not the ratio of ACKs to
    submitted frames (dropped/coalesced frames are valid latest-snapshot
    behaviour). This helper therefore reports:

    * ``ack_update_rate_hz`` = (ACK count - 1) / (last ACK - first ACK),
    * ``ack_interval_s`` percentiles,
    * submit -> ACK age percentiles, i.e. how old the displayed image was when
      the HDMI owner acknowledged it,
    * source -> ACK age when a caller supplied a real source moment.

    Older records without the added timestamps are ignored rather than
    guessed. ACK order is preserved; a non-monotonic clock is an error.
    """
    rows=[]
    for row in records or ():
        if not isinstance(row,dict):
            continue
        submit=row.get('submit_monotonic_ns');ack=row.get('ack_monotonic_ns')
        if type(submit) is not int or type(ack) is not int or submit<=0 or ack<=0:
            continue
        if ack<submit:raise ValueError('display ACK precedes submit monotonic clock')
        rows.append((submit,ack,row))
    if not rows:return dict(frames=0,ack_update_rate_hz=None,active_span_s=None,
        submit_to_ack_p50_s=None,submit_to_ack_p95_s=None,submit_to_ack_p99_s=None,
        ack_interval_p50_s=None,ack_interval_p95_s=None,ack_interval_p99_s=None,
        source_to_ack_p50_s=None,source_to_ack_p95_s=None,source_to_ack_p99_s=None,
        timing_scope='NO_TIMED_DISPLAY_ACKS')
    def pct(values,fraction):
        values=sorted(float(v) for v in values)
        if not values:return None
        import math
        position=fraction*(len(values)-1);lower=int(math.floor(position));upper=int(math.ceil(position))
        if lower==upper:return values[lower]
        return values[lower]+(values[upper]-values[lower])*(position-lower)
    rows.sort(key=lambda item:item[1])
    first=rows[0][1];last=rows[-1][1];span=(last-first)/1e9
    submit_ages=[(ack-submit)/1e9 for submit,ack,_ in rows]
    intervals=[(rows[i][1]-rows[i-1][1])/1e9 for i in range(1,len(rows))]
    source_ages=[(ack-row['source_capture_monotonic_ns'])/1e9 for _,ack,row in rows
                 if type(row.get('source_capture_monotonic_ns')) is int
                 and row['source_capture_monotonic_ns']>0]
    return dict(frames=len(rows),ack_update_rate_hz=(len(rows)-1)/span if span>0 else None,
        active_span_s=span,first_ack_perf_s=rows[0][2].get('ack_perf_s'),last_ack_perf_s=rows[-1][2].get('ack_perf_s'),
        submit_to_ack_p50_s=pct(submit_ages,.5),submit_to_ack_p95_s=pct(submit_ages,.95),
        submit_to_ack_p99_s=pct(submit_ages,.99),
        ack_interval_p50_s=pct(intervals,.5),ack_interval_p95_s=pct(intervals,.95),
        ack_interval_p99_s=pct(intervals,.99),
        source_to_ack_p50_s=pct(source_ages,.5),source_to_ack_p95_s=pct(source_ages,.95),
        source_to_ack_p99_s=pct(source_ages,.99),timing_scope='MONOTONIC_SUBMIT_AND_ACK')


class ProcessDisplayCloseError(RuntimeError):
    def __init__(self,message,receipt,display):
        super().__init__(message);self.receipt=receipt;self.display=display


class _State:
    def __init__(self,context):
        self.values={name:context.RawValue(ctypes.c_double if name=='deadline' else ctypes.c_int64,initial)
            for name,initial in (('active',-1),('pending',-1),('count',0),('closing',0),('deadline',0),('generation',0),
                ('pipeline_revision',0),('computed_frame',-1),('durable_frame',-1),('displayed_frame',-1),
                ('queued_frames',0),('queued_bytes',0),('pipeline_phase',0),
                ('close_phase',0),('owner_close_begin_ns',0),('owner_close_end_ns',0),
                ('closed_event_begin_ns',0),('closed_event_end_ns',0))}
        # Every value needed to reconstruct the age of the frame that actually
        # reached HDMI travels with its slot. submit_ns/source_ns are
        # system-wide monotonic nanoseconds; both parent and spawn child can
        # compare them without sharing a perf_counter epoch. ``generation`` is
        # the parent submit ordinal used only for display coalescing evidence.
        self.slots=[{key:context.RawValue(ctypes.c_double if key in ('angle','submit_perf') else ctypes.c_int64,0)
            for key in ('index','angle','count','size','kind','actual','max_points','submit_ns','source_ns','submit_perf','generation')}
            for _ in range(2)]
    def __getitem__(self,key):return self.values[key].value
    def __setitem__(self,key,value):self.values[key].value=value


class _Events:
    """Single-writer completed-message ring, never a partial pipe receive.

    Semaphore release/acquire publishes completed bytes across processes. A
    dead writer before publish consumes a free slot but cannot block the
    parent. Parent polls/acquires ready only with explicit finite deadlines.
    """
    def __init__(self,context):
        self.data=context.RawArray(ctypes.c_ubyte,EVENT_SLOTS*MAX_EVENT_BYTES)
        self.lengths=context.RawArray(ctypes.c_uint32,EVENT_SLOTS)
        self.ready=context.Semaphore(0);self.free=context.Semaphore(EVENT_SLOTS)
        self._sent=self._read=0;self._reservation=False;self._closed=False
    def send_bytes(self,data):
        if len(data)>MAX_EVENT_BYTES:raise ValueError('bounded display event exceeded')
        while not self.free.acquire(timeout=CONTROL_TIMEOUT_S):pass
        slot=self._sent%EVENT_SLOTS;start=slot*MAX_EVENT_BYTES
        np.frombuffer(self.data,np.uint8,count=len(data),offset=start)[:]=np.frombuffer(data,np.uint8)
        self.lengths[slot]=len(data);self._sent+=1;self.ready.release()
    def poll(self,timeout=0.):
        if self._closed:return False
        if not self._reservation:self._reservation=self.ready.acquire(timeout=max(0.,timeout))
        return self._reservation
    def recv_bytes(self,maxlength):
        if not self._reservation:raise RuntimeError('only a completed display event may be read')
        slot=self._read%EVENT_SLOTS;length=self.lengths[slot]
        try:
            if not 0<length<=min(maxlength,MAX_EVENT_BYTES):raise ValueError('invalid display event length')
            return bytes(np.frombuffer(self.data,np.uint8,count=length,offset=slot*MAX_EVENT_BYTES))
        finally:self._read+=1;self._reservation=False;self.free.release()
    def close(self):self._closed=True


def _prepare_lock(config,*,platform_name=None,operations=None,ticket_factory=None):
    """Duplicate the *open file description*, then transfer via POSIX SCM_RIGHTS.

    Optional namespace arguments are for pure local FD-lifecycle unit tests.
    A raw parent FD number is never treated as an inherited child descriptor.
    """
    operations=os if operations is None else operations
    platform_name=os.name if platform_name is None else platform_name
    descriptor=config.pop('hardware_lock_fd',None)
    if descriptor is None:
        if config.get('test_only_display') is not True:raise ValueError('acquired hardware lock FD required')
        return None,None,None
    if platform_name!='posix':raise ValueError('actual hardware FD transfer requires POSIX spawn/DupFd')
    if type(descriptor) is not int or descriptor<0:raise ValueError('valid hardware lock FD required')
    retained=operations.dup(descriptor)
    try:
        stat=operations.fstat(retained);identity=(stat.st_dev,stat.st_ino,stat.st_mode)
        if ticket_factory is None:
            from multiprocessing.reduction import DupFd
            ticket_factory=DupFd
        return retained,ticket_factory(retained),identity
    except BaseException:
        operations.close(retained);raise


def _receive_lock(ticket,expected,*,operations=None):
    operations=os if operations is None else operations
    if ticket is None:return None
    descriptor=ticket.detach()
    try:
        stat=operations.fstat(descriptor)
        if (stat.st_dev,stat.st_ino,stat.st_mode)!=tuple(expected):raise ValueError('transferred lock FD identity mismatch')
        return descriptor
    except BaseException:
        operations.close(descriptor);raise


def _send(connection,message):
    data=json.dumps(message,separators=(',',':'),allow_nan=False).encode('utf8')
    if len(data)>MAX_EVENT_BYTES:raise ValueError('bounded display event exceeded')
    connection.send_bytes(data)


def _display_frame(view,index):
    """Rebuild only immutable display views, not a strict/geometry decoder."""
    from .pfo3_r25 import (HEADER_FIELDS,MAGIC,VERSION,RECORD_DTYPE,ROW_DTYPE,FinalFrame)
    if len(view)<256:raise ValueError('display archive truncated')
    h=dict(zip(HEADER_FIELDS,struct.unpack_from('<64I',view)))
    count=h['diagnostic_count'];rows=h['row_count']
    if ((h['magic'],h['version'],h['header_bytes'],h['record_bytes'],h['row_record_bytes'])!=(MAGIC,VERSION,256,64,64)
            or (h['width'],h['height'],rows)!=(752,480,480) or h['frame_id']!=index
            or count>180480 or h['qualified_count']>7680 or h['source'] not in (1,2,3)
            or h['total_bytes']!=len(view) or len(view)!=256+64*(count+rows)
            or h['measure_status']>=6 or h['preview_status']>=6):
        raise ValueError('display archive header/frame/length mismatch')
    if zlib.crc32(view[256:])!=h['body_crc32']:raise ValueError('display archive CRC mismatch')
    readonly=view.toreadonly()
    records=np.frombuffer(readonly,dtype=RECORD_DTYPE,count=count,offset=256)
    row_records=np.frombuffer(readonly,dtype=ROW_DTYPE,count=rows,offset=256+64*count)
    return FinalFrame(MappingProxyType(h),records,row_records,readonly)


def _marker_json_payload(annotations):
    """Serialize an already-validated frozen marker envelope without deepcopy.

    Tuple/scalar fields are immutable and JSON already serializes tuples as
    arrays. Keep dataclass field order and the strict JSON options identical to
    the former asdict path; this is not an archive decoder or validation bypass.
    """
    from dataclasses import fields
    from .display_pfo3_r25 import PLDisplayAnnotations
    if type(annotations) is not PLDisplayAnnotations:
        raise TypeError('PLDisplayAnnotations snapshot required')
    data={field.name:getattr(annotations,field.name) for field in fields(PLDisplayAnnotations)}
    return json.dumps(data,separators=(',',':'),allow_nan=False).encode('ascii')


def _annotations(buffer,metadata):
    from .display_pfo3_r25 import DeferredPLAnnotations,PLDisplayAnnotations
    if metadata['kind']==0:return None
    view=memoryview(buffer).cast('B')[:metadata['size']]
    if metadata['kind']==3:
        from .display_transport_r15 import unpack_annotations
        return unpack_annotations(view,metadata['index'])
    if metadata['kind']==4:
        from .display_transport_r15 import unpack_native_markers
        return unpack_native_markers(view,metadata['index'],actual=metadata['actual'],
            evidence=metadata['evidence'],max_points=metadata['max_points']).materialize_compact()
    if metadata['kind']==1:
        return DeferredPLAnnotations(_display_frame(view,metadata['index']),bool(metadata['actual']),
            metadata['evidence'],metadata['max_points'])
    if metadata['kind']==2:
        data=json.loads(bytes(view).decode('ascii'))
        for key in ('measurement','preview','rejected'):data[key]=tuple(tuple(row) for row in data[key])
        result=PLDisplayAnnotations(**data)
        if result.frame_id!=metadata['index']:raise ValueError('display marker/frame mismatch')
        return result
    raise ValueError('unknown display annotation kind')


def _quarantine():
    # Deliberately no terminate/kill/reset or timed auto-release. The parent
    # also retains an independent duplicate FD and reports external recovery.
    while True:time.sleep(.5)


def _worker(buffers,state,condition,connection,config,lock_ticket,lock_identity,owner_factory,renderer_factory):
    owner=None;lock_fd=None;clean=False;error=None;closed_sent=False
    gray_buffers,archives,cloud_buffer,target_buffer,evidence_buffers=buffers
    grays=[np.frombuffer(b,np.uint8).reshape(480,752) for b in gray_buffers]
    cloud=np.frombuffer(cloud_buffer,np.float64).reshape(-1,3)
    # Display-only cadence/coalescing evidence survives an early exception so
    # the close receipt can distinguish "fewer HDMI updates by policy" from a
    # lost measurement/save frame (the parent accounts every submit).
    min_interval=0.;sample_cap=1;coalesced_total=0
    try:
        lock_fd=_receive_lock(lock_ticket,lock_identity)
        settings=dict(config)
        if lock_fd is not None:settings['hardware_lock_fd']=lock_fd
        import cv2
        cv2.setNumThreads(1)
        min_interval=validate_cadence(settings.get('display_min_interval_s',0.))
        sample_cap=validate_sample_cap(settings.get('display_sample_cap'),settings['visible_points'])
        # PYNQ/allocator access, if needed by the supplied owner, begins only
        # here after spawn. Parent transport/geometry objects are not passed.
        owner=owner_factory(settings);owner.configure();renderer=renderer_factory(settings)
        # The native kernel already checks finite XYZ while finding bounds.
        # Opt in only after target-runtime A/B; the default retains R26 work.
        renderer.native_finite_validation_only=settings.get('display_native_finite_validation_only',False)
        from structured_light.scan3d.dashboard import DashboardStatus
        if settings.get('warmup',False):
            status=DashboardStatus(mode='WAITING',source='DISPLAY STARTUP WARMUP / NO CAPTURE',
                frame_number=0,angle_deg=0.,quality='waiting',progress_pct=0.)
            renderer.target='WAITING / DISPLAY WARMUP / NO CAPTURE';renderer.full_cloud_points=0
            started=time.perf_counter()
            canvas=renderer.render(np.zeros((480,752),np.uint8),np.empty((0,3)),status=status,
                cloud_revision=('startup_warmup',0,0))
            if canvas.shape!=(720,1280,3) or canvas.dtype!=np.uint8:raise ValueError('720p warmup canvas required')
            budget=startup_canvas_budget(settings)
            startup_show=getattr(owner,'show_canvas_startup',None)
            receipt=(startup_show(canvas,timeout_s=budget) if startup_show is not None else owner.show_canvas(canvas))
            _send(connection,dict(kind='warmup',startup_warmup=True,frame_id=None,
                counts_excluded=True,wall_s=time.perf_counter()-started,canvas_rpc_budget_s=budget,receipt=receipt))
        _send(connection,dict(kind='ready',pid=os.getpid(),display_only=True,
            lock_fd_transferred=lock_fd is not None,opencv_threads=cv2.getNumThreads(),
            visible_points=settings['visible_points'],display_min_interval_s=min_interval,
            display_native_finite_validation_only=renderer.native_finite_validation_only,
            display_release_storage_on_close=settings.get('display_release_storage_on_close',False),
            display_direct_shared_canvas=settings.get('display_direct_shared_canvas',True),
            display_sample_cap=sample_cap))
        last_render_start=None;last_generation=0;last_status_revision=0;last_model=None
        def pipeline_text():
            phases=('RUNNING','SAVING BACKLOG','ADMISSION PAUSED','DRAINING','CANCEL REQUESTED','FAILED','ANALYSIS')
            phase=int(state['pipeline_phase'])
            return (phases[phase] if 0<=phase<len(phases) else 'UNKNOWN')+(
                ' | computed %d displayed %d saved %d | queue %d / %.1f MiB' %
                (state['computed_frame'],state['displayed_frame'],state['durable_frame'],
                 state['queued_frames'],state['queued_bytes']/1024**2))
        while True:
            cadence_wait=0.
            with condition:
                while (state['pending']<0 and not state['closing'] and
                       state['pipeline_revision']==last_status_revision):condition.wait()
                if state['pending']<0 and state['closing']:break
                status_only=state['pending']<0
                last_status_revision=state['pipeline_revision']
                pipeline_label=pipeline_text() if last_status_revision else None
            if status_only:
                if last_model is not None:
                    # Status redraws are explicitly excluded from model ACKs.
                    status_render=getattr(renderer,'render_status_only',None)
                    if callable(status_render):
                        canvas=status_render(pipeline_label)
                        if canvas is not None:owner.show_canvas(canvas)
                continue
            with condition:
                # Bounded latest-only cadence: wait for the next render
                # deadline while the parent may overwrite the pending slot.
                # close()/begin_target never lose the final pending frame:
                # closing breaks this wait and the frame is rendered below.
                if min_interval>0. and last_render_start is not None and not state['closing']:
                    wait_started=time.monotonic();due=last_render_start+min_interval
                    while True:
                        now=time.monotonic()
                        if now>=due or state['closing']:break
                        condition.wait(min(due-now,.5))
                    cadence_wait=time.monotonic()-wait_started
                slot=state['pending'];state['pending']=-1;state['active']=slot
                meta={key:value.value for key,value in state.slots[slot].items()}
                target=bytes(target_buffer).split(b'\0',1)[0].decode('ascii')
                meta['evidence']=bytes(evidence_buffers[slot]).split(b'\0',1)[0].decode('ascii')
                generation=int(meta.get('generation',0))
                coalesced=max(0,generation-last_generation-1)
                last_generation=generation
                coalesced_total+=coalesced
                condition.notify_all()
            # Appends occur beyond this frame's count; begin_target waits for
            # active to clear. Only the bounded display sample is copied; the
            # shared cloud and all on-disk measurement points are untouched.
            count=meta['count']
            points,stride=sample_points_bounded(cloud,count,sample_cap)
            annotations=_annotations(archives[slot],meta)
            renderer.target=target;renderer.full_cloud_points=count
            status=DashboardStatus(mode='SIMULATION',source=pipeline_label or settings.get('display_source','display-only input/XYZ'),
                frame_number=meta['index'],angle_deg=float(np.degrees(meta['angle'])),quality='simulation',
                progress_pct=min(100.,100*(meta['index']+1)/settings.get('frame_total',240)))
            render_start_monotonic=time.monotonic();started=time.perf_counter();cpu=time.thread_time()
            options={} if annotations is None else {'annotations':annotations}
            direct=getattr(owner,'render_and_show',None)
            if settings.get('display_direct_shared_canvas',True) and callable(direct):
                receipt=direct(renderer,grays[slot],points,status=status,
                    cloud_revision=(target,meta['index'],count),**options)
                render_s=receipt['render_s'];render_cpu=receipt['render_thread_cpu_s']
                show_started=started+render_s;show_cpu=cpu+render_cpu
            else:
                canvas=renderer.render(grays[slot],points,status=status,cloud_revision=(target,meta['index'],count),**options)
                render_s=time.perf_counter()-started;render_cpu=time.thread_time()-cpu
                if canvas.shape!=(720,1280,3) or canvas.dtype!=np.uint8:raise ValueError('full720p uint8 canvas required')
                show_started=time.perf_counter();show_cpu=time.thread_time();receipt=owner.show_canvas(canvas)
            ack_perf=time.perf_counter();ack_ns=time.monotonic_ns();submit_ns=int(meta['submit_ns'])
            source_ns=int(meta['source_ns'])
            if submit_ns<=0 or ack_ns<submit_ns:raise ValueError('display submit/ACK monotonic clock mismatch')
            if source_ns>0 and source_ns>submit_ns:raise ValueError('display source capture follows submit monotonic clock')
            submit_to_ack_s=(ack_ns-submit_ns)/1e9
            # Project the parent submit moment onto the child's perf_counter
            # clock using the shared monotonic duration; this keeps
            # ack_perf_s - submit_perf_s exactly equal to submit_to_ack_s even
            # if the two processes had different perf_counter epochs.
            projected_submit_perf=ack_perf-submit_to_ack_s
            _send(connection,dict(kind='ack',frame_id=meta['index'],target=target,cloud_points=count,
                visible_points=len(points),sampled_points=len(points),sampling_stride=stride,
                sampled_nbytes=int(points.nbytes),sample_cap=sample_cap,receipt=receipt,
                ack_perf_s=ack_perf,ack_monotonic_ns=ack_ns,
                submit_perf_s=projected_submit_perf,parent_submit_perf_s=float(meta['submit_perf']),
                submit_monotonic_ns=submit_ns,submit_to_ack_s=submit_to_ack_s,ack_age_s=submit_to_ack_s,
                source_capture_monotonic_ns=(source_ns if source_ns>0 else None),
                source_to_ack_s=((ack_ns-source_ns)/1e9 if source_ns>0 else None),
                render_s=render_s,render_thread_cpu_s=render_cpu,owner_s=time.perf_counter()-show_started,
                owner_thread_cpu_s=time.thread_time()-show_cpu,renderer_timings=dict(getattr(renderer,'timings',{})),
                annotation_points=0 if annotations is None else annotations.point_count,
                coalesced=coalesced,coalesced_total=coalesced_total,cadence_wait_s=cadence_wait,
                display_min_interval_s=min_interval,display_only=True,child_pid=os.getpid()))
            # No views of this slot may survive its return to the parent.
            annotations=None
            if last_status_revision:
                last_model=True
            last_render_start=render_start_monotonic
            with condition:
                state['displayed_frame']=meta['index'];state['active']=-1;condition.notify_all()
    except BaseException as exc:
        error=type(exc).__name__+': '+str(exc)
        try:_send(connection,dict(kind='error',error=error[:4096]))
        except BaseException:pass
    finally:
        receipt=None;owner_close_error_receipt=None
        try:
            if owner is not None:
                with condition:
                    deadline=state['deadline'];state['close_phase']=1
                    state['owner_close_begin_ns']=time.monotonic_ns()
                receipt=owner.close(timeout_s=max(0.,deadline-time.monotonic()) if deadline else 5.)
                clean=isinstance(receipt,dict) and receipt.get('owner_closed',receipt.get('clean',False)) is True
        except BaseException as exc:
            error=error or type(exc).__name__+': '+str(exc)
            owner_close_error_receipt=getattr(exc,'receipt',None)
        with condition:
            state['owner_close_end_ns']=time.monotonic_ns();state['close_phase']=2
            state['closed_event_begin_ns']=time.monotonic_ns()
        try:
            _send(connection,dict(kind='closed',owner_closed=clean,receipt=receipt,error=error,
                owner_close_error_receipt=owner_close_error_receipt,
                coalesced_total=coalesced_total,display_min_interval_s=min_interval,
                display_sample_cap=sample_cap))
            closed_sent=True
        except BaseException:pass
        with condition:
            state['closed_event_end_ns']=time.monotonic_ns();state['close_phase']=3
            state['active']=-1;condition.notify_all()
        if not clean or not closed_sent:_quarantine()
        if lock_fd is not None:os.close(lock_fd)
        connection.close()


class ProcessSessionDisplayR25:
    """SessionDisplay-compatible optional spawn implementation.

    owner_factory/renderer_factory must be importable top-level classes or
    functions. Actual mode requires a POSIX hardware lock FD in config.
    Explicit config.test_only_display permits local fake owners without one.
    """
    def __init__(self,*,owner_factory,renderer_factory,config,cloud_bytes=64*1024*1024,
                 visible_points=24000,startup_timeout_s=5.,archive_slot_bytes=ARCHIVE_SLOT_BYTES):
        if type(cloud_bytes) is not int or not 24<=cloud_bytes<=64*1024*1024:raise ValueError('cloud budget exceeded')
        if type(visible_points) is not int or not 1<=visible_points<=24000:raise ValueError('preview scratch budget exceeded')
        if type(archive_slot_bytes) is not int or not MIN_ARCHIVE_SLOT_BYTES<=archive_slot_bytes<=ARCHIVE_SLOT_BYTES:
            raise ValueError('bounded native PFO3 archive slot required')
        if not math.isfinite(startup_timeout_s) or startup_timeout_s<=0:raise ValueError('finite startup deadline required')
        for factory in (owner_factory,renderer_factory):
            if not (inspect.isclass(factory) or inspect.isfunction(factory)) or '<locals>' in factory.__qualname__:
                raise TypeError('importable top-level display factories required')
        settings=dict(config)
        if settings.get('warmup',False):startup_canvas_budget(settings)
        cadence=validate_cadence(settings.get('display_min_interval_s',0.))
        sample_cap=validate_sample_cap(settings.get('display_sample_cap'),visible_points)
        settings['display_min_interval_s']=cadence;settings['display_sample_cap']=sample_cap
        if len(json.dumps(settings,allow_nan=False).encode('utf8'))>16384:raise ValueError('bounded scalar display config required')
        if type(settings.get('warmup',False)) is not bool:raise ValueError('explicit warmup boolean required')
        for key in ('display_native_finite_validation_only','display_release_storage_on_close','display_direct_shared_canvas'):
            if type(settings.get(key,key=='display_direct_shared_canvas')) is not bool:raise ValueError(key+' must be an explicit boolean')
        self.config=settings;self.visible_points=visible_points;self.target=None;self._count=0
        self.display_min_interval_s=cadence;self.display_sample_cap=sample_cap
        self.archive_slot_bytes=archive_slot_bytes;self.cloud_bytes=(cloud_bytes//24)*24
        self.error=None;self.owner_closed=False;self.ready=False;self._closing=False
        self.submitted=self.acknowledged=self.dropped=self.peak_frames=0
        self._records=deque(maxlen=1024);self.startup_warmup=None;self.startup_receipt=None;self._closed_message=None
        self._connection_closed=False;self._released=False;self._retained_lock_fd=None
        self._storage_released=False;self._storage_released_bytes=0
        context=mp.get_context('spawn');self.start_method='spawn'
        self._condition=context.Condition();self._state=_State(context)
        self._grays=[context.RawArray(ctypes.c_ubyte,RAW_BYTES) for _ in range(2)]
        self._archives=[context.RawArray(ctypes.c_ubyte,archive_slot_bytes) for _ in range(2)]
        self._cloud_buffer=context.RawArray(ctypes.c_double,(cloud_bytes//24)*3)
        self._target_buffer=context.RawArray(ctypes.c_ubyte,MAX_TARGET_BYTES+1)
        self._evidence_buffers=[context.RawArray(ctypes.c_ubyte,97) for _ in range(2)]
        self._cloud=np.frombuffer(self._cloud_buffer,np.float64).reshape(-1,3)
        # Reuse stable numpy views instead of rebuilding headers on every
        # submit; the raw gray and archive bytes still cross the same bounded
        # shared slots and are never zero-copy aliases of caller storage.
        self._gray_views=[np.frombuffer(b,np.uint8).reshape(480,752) for b in self._grays]
        self._archive_views=[np.frombuffer(b,np.uint8) for b in self._archives]
        self.connection=_Events(context);child=self.connection
        control_bytes=(ctypes.sizeof(self._target_buffer)+sum(ctypes.sizeof(b) for b in self._evidence_buffers)
            +ctypes.sizeof(self.connection.lengths)+sum(ctypes.sizeof(v) for v in self._state.values.values())
            +sum(ctypes.sizeof(v) for slot in self._state.slots for v in slot.values()))
        self._allocation=dict(cloud=self.cloud_bytes,archives=2*archive_slot_bytes,raw=2*RAW_BYTES,
            events=EVENT_SLOTS*MAX_EVENT_BYTES,control=control_bytes)
        self._allocation['logical_shared_total']=sum(self._allocation.values())
        self._retained_lock_fd,ticket,lock_identity=_prepare_lock(settings)
        self._lock_ticket=ticket
        self._test_only=settings.get('test_only_display') is True
        deadline=time.monotonic()+startup_timeout_s
        settings=dict(settings,visible_points=visible_points,display_startup_deadline_monotonic=deadline)
        self.process=context.Process(target=_worker,args=((self._grays,self._archives,self._cloud_buffer,
            self._target_buffer,self._evidence_buffers),self._state,self._condition,child,settings,
            ticket,lock_identity,owner_factory,renderer_factory),name='r25-spawn-hdmi-display',daemon=False)
        try:self.process.start()
        except BaseException as exc:
            if self._retained_lock_fd is not None:
                # A bootstrap error has no explicit HDMI-close receipt. Keep
                # both the parent duplicate and transfer ticket reachable;
                # never infer ownership release from start() raising.
                self._closing=True
                raise SessionDisplayStartupError('spawn bootstrap unconfirmed: '+str(exc),self) from exc
            self.connection.close();raise
        while not self.ready:
            self._drain()
            if self.error or not self.process.is_alive() or time.monotonic()>=deadline:
                self._closing=True
                if self._condition.acquire(timeout=.01):
                    try:self._state['closing']=1;self._condition.notify_all()
                    finally:self._condition.release()
                raise SessionDisplayStartupError(self.error or 'spawn display startup unconfirmed',self)
            self.connection.poll(min(.01,max(0.,deadline-time.monotonic())))

    def update_pipeline_status(self,*,computed_frame,durable_frame,queued_frames,queued_bytes,phase):
        """Bounded shared status; no disk, rendering, event consumption or ACK."""
        values=(computed_frame,durable_frame,queued_frames,queued_bytes,phase)
        if any(type(v) is not int for v in values) or not -1<=computed_frame<=0xffffffff or not -1<=durable_frame<=0xffffffff:
            raise ValueError('bounded integral pipeline status required')
        if not 0<=queued_frames<=128 or not 0<=queued_bytes<=64*1024**2 or not 0<=phase<=6:
            raise ValueError('pipeline status outside contract')
        with self._condition:
            for key,value in zip(('computed_frame','durable_frame','queued_frames','queued_bytes','pipeline_phase'),values):
                self._state[key]=value
            self._state['pipeline_revision']+=1;self._condition.notify_all()

    def _drain(self):
        while not self._connection_closed:
            try:
                if not self.connection.poll():break
                message=json.loads(self.connection.recv_bytes(MAX_EVENT_BYTES).decode('utf8'))
            except (EOFError,OSError):self._connection_closed=True;break
            if message['kind']=='ready':self.ready=True;self.startup_receipt=message
            elif message['kind']=='warmup':self.startup_warmup=message
            elif message['kind']=='error':self.error=message['error']
            elif message['kind']=='ack':
                self.acknowledged+=1;self._records.append({k:v for k,v in message.items() if k!='kind'})
            elif message['kind']=='closed':
                self._closed_message=message;self.owner_closed=message['owner_closed']
                self.error=self.error or message['error']

    @property
    def records(self):self._drain();return self._records

    @property
    def cleanup_confirmed(self):
        self._drain()
        return bool(self._closed_message and self.owner_closed and not self.process.is_alive() and self.process.exitcode==0)

    def begin_target(self,target,timeout_s=5.):
        if not math.isfinite(timeout_s) or timeout_s<0:raise ValueError('finite target deadline required')
        target=str(target)
        if not target.isascii() or not 1<=len(target.encode('ascii'))<=MAX_TARGET_BYTES or '\0' in target:
            raise ValueError('bounded ASCII display target required')
        deadline=time.monotonic()+timeout_s
        while True:
            self._drain()
            if not self._condition.acquire(timeout=min(CONTROL_TIMEOUT_S,max(0.,deadline-time.monotonic()))):
                self.error='display shared control lock unconfirmed during begin_target'
                raise RuntimeError(self.error)
            try:
                if self.error or self._closing or not self.process.is_alive():raise RuntimeError(self.error or 'display closed')
                if self._state['active']<0 and self._state['pending']<0:
                    self.target=target;self._count=0;self._state['count']=0
                    self._target_buffer[:]=target.encode('ascii').ljust(MAX_TARGET_BYTES+1,b'\0');return
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('target display drain timeout')
            finally:self._condition.release()
            time.sleep(min(.005,max(0.,deadline-time.monotonic())))

    def submit(self,gray,delta,index,angle,annotations=None,source_capture_monotonic_ns=None):
        from .display_pfo3_r25 import annotation_snapshot,DeferredPLAnnotations
        self._drain();image=np.asarray(gray);points=np.asarray(delta)
        if image.shape!=(480,752) or image.dtype!=np.uint8:raise ValueError('full native gray required')
        if points.ndim!=2 or points.shape[1:]!=(3,) or not np.isfinite(points).all():raise ValueError('finite XYZ required')
        if type(index) is not int or not 0<=index<2**32 or not np.isfinite(angle):raise ValueError('invalid frame identity/angle')
        if source_capture_monotonic_ns is not None and (type(source_capture_monotonic_ns) is not int
                or source_capture_monotonic_ns<=0):raise ValueError('source capture monotonic nanoseconds required')
        kind=0;payload=b'';actual=0;max_points=0;evidence=''
        if annotations is not None:
            annotations=annotation_snapshot(annotations)
            if annotations.frame_id!=index:raise ValueError('annotation frame identity mismatch')
            if self._test_only and annotations.actual_PL_execution:raise ValueError('test-only display cannot claim actual PL')
            actual=int(annotations.actual_PL_execution)
            if type(annotations) is DeferredPLAnnotations:
                frame=annotations.frame;payload=frame.payload;h=frame.header
                if (type(payload) is not bytes or h['width']!=752 or h['height']!=480
                        or h['total_bytes']!=len(payload) or len(payload)>self.archive_slot_bytes):
                    raise ValueError('bounded immutable full PFO3 display source required')
                # Validate the bytes that supplied this snapshot before reducing
                # them. A replaced payload must not reuse stale header/record
                # views to bypass the former child-side archive CRC check.
                # Validate the full source once. Transfer display-only fields;
                # classify/sample in the consumer only for rendered frames.
                from .display_transport_r15 import pack_native_markers
                payload=pack_native_markers(frame,index)
                kind=4;max_points=annotations.max_points;evidence=annotations.geometry_evidence_type
            else:
                from .display_compact_r14 import CompactPLAnnotations
                if type(annotations) is CompactPLAnnotations:
                    from .display_transport_r15 import pack_annotations
                    payload=pack_annotations(annotations);kind=3
                else:
                    payload=_marker_json_payload(annotations);kind=2
        if len(payload)>self.archive_slot_bytes:raise BufferError('display archive slot exceeded')
        if not self._condition.acquire(timeout=CONTROL_TIMEOUT_S):
            self.error='display shared control lock unconfirmed during submit'
            raise RuntimeError(self.error)
        try:
            if self.error or self._closing or self.target is None or not self.process.is_alive():
                raise RuntimeError(self.error or 'no active display target')
            if self._count+len(points)>len(self._cloud):raise BufferError('display cloud capacity exceeded; full storage separate')
            slot=self._state['pending']
            if slot<0:slot=0 if self._state['active']!=0 else 1
            # Every submission is either rendered (one ACK) or overwritten
            # while still pending (one dropped). The child's cadence wait can
            # only add overwrites; it never discards a submission silently.
            self.dropped+=self._state['pending']>=0
            self._gray_views[slot][:]=image
            self._archive_views[slot][:len(payload)]=np.frombuffer(payload,np.uint8)
            self._cloud[self._count:self._count+len(points)]=points;self._count+=len(points)
            self._evidence_buffers[slot][:]=evidence.encode('ascii').ljust(97,b'\0')
            generation=self.submitted+1
            values=dict(index=index,angle=float(angle),count=self._count,size=len(payload),kind=kind,actual=actual,
                max_points=max_points,generation=generation,
                submit_ns=time.monotonic_ns(),submit_perf=time.perf_counter(),
                source_ns=(-1 if source_capture_monotonic_ns is None else source_capture_monotonic_ns))
            for key,value in values.items():self._state.slots[slot][key].value=value
            self.submitted=generation;self._state['count']=self._count
            self._state['generation']=generation;self._state['pending']=slot
            self.peak_frames=max(self.peak_frames,int(self._state['active']>=0)+1);self._condition.notify_all()
        finally:self._condition.release()

    def _release_closed_storage(self):
        """Drop ordinary shared RAM only after owner close AND successful join.

        Do not close mmap/arenas manually: multiprocessing owns their lifetime.
        Retain small control state and immutable receipts for idempotent close.
        These are not CMA buffers and this receipt never claims CMA release.
        """
        if self._storage_released:return
        if not self.cleanup_confirmed:raise RuntimeError('display storage still owned by unconfirmed worker')
        self._cloud=None;self._gray_views=None;self._archive_views=None
        self._cloud_buffer=None;self._grays=None;self._archives=None
        self._target_buffer=None;self._evidence_buffers=None
        self._storage_released_bytes=sum(self._allocation[key] for key in ('cloud','archives','raw'))
        self._storage_released=True

    def close(self,timeout_s=5.):
        if not math.isfinite(timeout_s) or timeout_s<0:raise ValueError('finite close deadline required')
        close_started=time.monotonic();deadline=close_started+timeout_s;self._closing=True
        signaled=self._condition.acquire(timeout=max(0.,deadline-time.monotonic()))
        if signaled:
            try:self._state['closing']=1;self._state['deadline']=deadline;self._condition.notify_all()
            finally:self._condition.release()
        while self.process.is_alive() and time.monotonic()<deadline:
            self._drain();self.process.join(min(.01,max(0.,deadline-time.monotonic())))
        self._drain();joined=not self.process.is_alive();clean=self.cleanup_confirmed
        if clean and not self._released:
            if self._retained_lock_fd is not None:os.close(self._retained_lock_fd);self._retained_lock_fd=None
            self.connection.close();self._connection_closed=True;self._released=True
        if clean and self.config.get('display_release_storage_on_close',False):
            self._release_closed_storage()
        error=self.error
        if not clean:error=error or ('display/HDMI cleanup unconfirmed; retain parent lock and child; external recovery required'
            +(' (shared condition deadline)' if not signaled else ''))
        if clean and self.submitted!=self.acknowledged+self.dropped:error=error or 'display ACK accounting mismatch'
        closed_coalesced=None
        if isinstance(self._closed_message,dict):
            value=self._closed_message.get('coalesced_total')
            if type(value) is int and value>=0:closed_coalesced=value
        phase=int(self._state['close_phase']) if hasattr(self,'_state') else None
        lifecycle={key:int(self._state[key]) for key in ('owner_close_begin_ns','owner_close_end_ns',
            'closed_event_begin_ns','closed_event_end_ns')} if hasattr(self,'_state') else {}
        result=dict(owner_closed=self.owner_closed,worker_joined=joined,worker_exitcode=self.process.exitcode,
            close_timeout_s=float(timeout_s),close_elapsed_s=time.monotonic()-close_started,
            close_phase=({0:'awaiting_worker_close',1:'owner_close_including_diagnostic_persistence',
                2:'publishing_closed_event',3:'awaiting_spawn_exit'}.get(phase,'unknown')),
            close_stage_monotonic_ns=lifecycle,owner_close_message=self._closed_message,
            error=error,submitted=self.submitted,acknowledged=self.acknowledged,dropped=self.dropped,
            peak_frames=self.peak_frames,cloud_points=self._count,records=list(self._records),
            timing_summary=display_timing_summary(self._records),
            performance_summary=performance_summary(self._records),
            display_min_interval_s=getattr(self,'display_min_interval_s',0.),
            display_sample_cap=getattr(self,'display_sample_cap',None),
            coalesced=closed_coalesced,
            coalesced_accounting_matches=(None if closed_coalesced is None else closed_coalesced==self.dropped),
            startup_warmup=self.startup_warmup,startup_receipt=self.startup_receipt,start_method='spawn',cleanup_confirmed=clean,
            retained_lock_fd=self._retained_lock_fd is not None,measurement_frames_dropped=0,
            display_only=True,archive_slots=2,archive_slot_bytes=self.archive_slot_bytes,
            shared_storage_references_released=getattr(self,'_storage_released',False),
            shared_storage_released_logical_bytes=getattr(self,'_storage_released_bytes',0),
            shared_storage_release_scope='ORDINARY_RAM_REFERENCES_NOT_CMA_OR_OS_FREE_MEMORY',
            allocated_shared_bytes=dict(self._allocation))
        if error:raise ProcessDisplayCloseError(error,result,self)
        return result


SessionDisplay=ProcessSessionDisplayR25
