"""Finite concurrent motion/capture scheduler with a single permit owner.

Backend methods are injected so no PYNQ import or movement occurs on import.
The owner refreshes the guard independently of capture/processing. A failed
task is never rearmed. Queue pressure is a task failure, not dropped data.
"""
from concurrent.futures import ThreadPoolExecutor,wait
from dataclasses import dataclass
import math,time,threading
from typing import Any,Callable
from .continuous_policy import ContinuousScanPolicy

@dataclass(frozen=True)
class StreamConfig:
    profile_hz:float=10.0
    rpm:float=2.5
    pulses_per_turn:int=3200
    max_turns:int=2
    pending_frames:int=2
    permit_poll_s:float=.005
    task_timeout_s:float=90.0
    drain_timeout_s:float=5.0
    angle_bins:int=120
    def __post_init__(self):
        if self.pulses_per_turn!=3200 or self.max_turns not in (1,2):raise ValueError('finite one turn plus at most one rescan required')
        if not 0<self.profile_hz<=30 or not 0<self.rpm<=5 or self.pending_frames not in (1,2,3,4):raise ValueError('invalid stream configuration')
        if not 0<self.permit_poll_s<=.01 or not 0<self.drain_timeout_s<=10:raise ValueError('invalid polling/drain budget')
        if not 60/self.rpm<self.task_timeout_s<=120:raise ValueError('finite task timeout must exceed turn duration and be <=120s')
        if type(self.angle_bins) is not int or not 1<=self.angle_bins<=3200:raise ValueError('invalid quality angle bins')

class StreamSession:
    """Backend: admit, arm_guard, refresh_guard, move, capture, stop, revoke.

    ``process(frame)`` returns {angle_rad, valid, quality}. Rendering/writes
    must use their own bounded queues. Backend.admit must validate calibrated
    axis/volume/timing, actual hardware identity and current motion authority.
    """
    def __init__(self,backend:Any,process:Callable,*,config:StreamConfig=StreamConfig(),
                 stop_event:threading.Event|None=None,monotonic=time.monotonic,sleep=time.sleep):
        self.backend,self.process,self.config=backend,process,config
        self.stop_event=stop_event or threading.Event();self.clock,self.sleep=monotonic,sleep
        self.state='ready';self.report={}

    def run(self)->dict:
        if self.state!='ready':raise RuntimeError('one-shot session; no automatic fault restart')
        cfg=self.config;policy=ContinuousScanPolicy(angle_bins=cfg.angle_bins)
        records=[];turns=[];max_gap=0.;last_refresh=None;armed=False;failure=None
        futures=[];motor_futures=[];revoked_confirmed=False;exposure_changes=[];unregistered=0
        pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='continuous_io')
        processing_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='continuous_process')
        def check_stop():
            if self.stop_event.is_set():raise RuntimeError('operator stop requested')
        def submit_io(operation,*args):
            token_fn=getattr(self.backend,'worker_token',None)
            token=token_fn() if token_fn is not None else None
            def invoke():
                check_stop()
                bind=getattr(self.backend,'bind_worker',None)
                if bind is not None:bind(token)
                try:return operation(*args)
                finally:
                    unbind=getattr(self.backend,'unbind_worker',None)
                    if unbind is not None:unbind()
            return pool.submit(invoke)
        try:
            admission=self.backend.admit()
            if admission is not True:raise RuntimeError('continuous motion admission not confirmed')
            check_stop()
            policy.start();self.state='running'
            for turn in range(cfg.max_turns):
                if turn and policy.state!='rescan':break
                check_stop()
                self.backend.arm_guard();armed=True;revoked_confirmed=False
                check_stop()
                turn_start=self.clock();deadline=turn_start+cfg.task_timeout_s
                motor=submit_io(self.backend.move,cfg.pulses_per_turn,cfg.rpm)
                futures.append(motor);motor_futures.append(motor)
                capture=None;pending=[];next_capture=turn_start;count=0;motor_completed_at=None;exposure_future=None
                def collect_ready():
                    nonlocal pending,unregistered
                    keep=[]
                    for future in pending:
                        if not future.done():keep.append(future);continue
                        result=future.result()
                        if type(result.get('valid')) is not bool:raise ValueError('profile validity must be explicit bool')
                        angle=result.get('angle_rad')
                        if angle is None:
                            if result['valid']:raise ValueError('valid profile requires a registered angle')
                            unregistered+=1
                        else:
                            angle=float(angle)
                            if not math.isfinite(angle):raise ValueError('nonfinite registered profile angle')
                            policy.record_profile(angle_rad=angle,valid=result['valid'])
                        records.append(result)
                    pending=keep
                while True:
                    now=self.clock()
                    if self.stop_event.is_set():raise RuntimeError('operator stop requested')
                    if now>=deadline:raise TimeoutError('finite continuous task deadline exceeded')
                    self.backend.refresh_guard()
                    if last_refresh is not None:max_gap=max(max_gap,now-last_refresh)
                    last_refresh=now
                    collect_ready()
                    if capture is not None and capture.done():
                        frame=capture.result();capture=None
                        if len(pending)>=cfg.pending_frames:raise BufferError('processing queue full; finite scan stopped')
                        processing=processing_pool.submit(self.process,frame)
                        pending.append(processing);futures.append(processing);count+=1
                    if exposure_future is not None and exposure_future.done():
                        change=exposure_future.result()
                        if change.get('accepted') is not True:raise RuntimeError('exposure transaction was not verified')
                        committed=getattr(self.backend,'exposure_committed',None)
                        if committed is not None:committed(change)
                        exposure_changes.append(change);exposure_future=None
                    exposure_pending=bool(getattr(self.backend,'exposure_pending',lambda:False)())
                    if exposure_pending and capture is None and not pending and exposure_future is None:
                        check_stop();exposure_future=submit_io(self.backend.apply_exposure);futures.append(exposure_future)
                    if motor.done():
                        if motor_completed_at is None:motor_completed_at=now
                        receipt=motor.result()
                        if (receipt.get('done') is not True or receipt.get('emitted')!=3200 or receipt.get('error',0)
                            or receipt.get('busy',False) is not False or receipt.get('fault',False) is not False):
                            raise RuntimeError('motion did not complete the exact 3200-command turn')
                        if capture is None and not pending and exposure_future is None and not exposure_pending:break
                        if now-motor_completed_at>=cfg.drain_timeout_s:
                            raise TimeoutError('capture/processing/exposure did not finish within post-motion drain budget')
                    if not motor.done() and capture is None and not exposure_pending and exposure_future is None and now>=next_capture:
                        capture=submit_io(self.backend.capture)
                        futures.append(capture)
                        next_capture=max(next_capture+1/cfg.profile_hz,now)
                    self.sleep(cfg.permit_poll_s)
                drain_deadline=self.clock()+cfg.drain_timeout_s
                while pending:
                    check_stop()
                    if self.clock()>=drain_deadline:raise TimeoutError('processing drain deadline exceeded')
                    self.backend.refresh_guard();collect_ready();self.sleep(cfg.permit_poll_s)
                self.backend.revoke();armed=False;revoked_confirmed=True;last_refresh=None
                check_stop()
                missing=policy.finish_rotation(emitted_pulses=receipt['emitted'])
                turns.append({'turn':turn,'emitted':receipt['emitted'],'profiles':count,
                              'elapsed_s':self.clock()-turn_start,'missing_bins':missing})
                turn_complete=getattr(self.backend,'turn_complete',None)
                if turn_complete is not None:turn_complete(receipt,turn,missing)
                if policy.state=='complete':break
            if policy.state!='complete':raise RuntimeError(policy.error or 'quality coverage incomplete within motion budget')
            check_stop()
            self.state='complete'
        except BaseException as exc:
            failure={'type':type(exc).__name__,'message':str(exc)};self.state='failed';policy.fault(str(exc))
        finally:
            cleanup_errors=[]
            try:self.backend.stop()
            except BaseException as exc:cleanup_errors.append('stop: '+str(exc))
            try:self.backend.revoke();armed=False;revoked_confirmed=True
            except BaseException as exc:revoked_confirmed=False;cleanup_errors.append('revoke: '+str(exc))
            # Do not free hardware resources here. A caller must join backend
            # captures at its established DMA barrier before runtime.close().
            _,unfinished=wait(futures,timeout=cfg.drain_timeout_s) if futures else (set(),set())
            if unfinished:
                cleanup_errors.append('worker did not join within bounded cleanup; retain hardware memory')
                quarantine=getattr(self.backend,'quarantine_workers',None)
                if quarantine is not None:
                    try:quarantine()
                    except BaseException as exc:cleanup_errors.append('quarantine: '+str(exc))
            motion_receipts=[]
            for future in motor_futures:
                if not future.done():motion_receipts.append(None);continue
                try:motion_receipts.append(future.result())
                except BaseException as exc:motion_receipts.append(getattr(exc,'diagnostics',None))
            known=all(isinstance(x,dict) and type(x.get('emitted')) is int for x in motion_receipts)
            pool.shutdown(wait=False,cancel_futures=True)
            processing_pool.shutdown(wait=False,cancel_futures=True)
            self.report={'state':self.state,'passed':self.state=='complete' and not cleanup_errors,
                'failure':failure,'cleanup_errors':cleanup_errors,'turns':turns,'profiles':records,
                'max_guard_owner_loop_interval_s':max_gap,
                'total_emitted_pulses':sum(x['emitted'] for x in motion_receipts) if known else None,
                'motion_receipts':motion_receipts,'worker_unconfirmed':bool(unfinished),
                'exposure_changes':exposure_changes,'unregistered_profiles':unregistered,
                'motion_pulse_budget':3200*cfg.max_turns,'guard_revoked':revoked_confirmed,
                'physical_accuracy_verified':False}
        return self.report
