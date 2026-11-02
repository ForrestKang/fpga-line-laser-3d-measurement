"""Deterministic, clearly synthetic fixture for the production application."""
from dataclasses import fields
import hashlib, math, threading, time
import numpy as np
from ..p2.types import DiagnosticRow, DiagnosticsSnapshot
from ..profile import Profile, ProfilePoint, encode_profile
from ..stripe_v1.types import StripeConfig
from ..stripe_v1.selection_v3 import SelectionConfigV3
from .pipeline import ScanCalibration, select_rows
from .turntable import TurntableModel, CALIBRATED
from .continuous_processor import AxisCalibration, WorkingVolume
from .continuous_timing import ValidatedTimingModel
from .stream_exposure import ExposureEffectModel
from .stream_capture import FeatureCapture


def synthetic_components():
    return dict(calibration=ScanCalibration(np.array([[100.,0,16],[0,100,12],[0,0,1]]),np.zeros(5),
        np.array([0.,0,1,-100]),(90.,120.),np.array([[-20.,-20,99],[20,20,101]]),'a'*64,'b'*64),
        stripe_config=StripeConfig(fixed_threshold=60,roi=(0,0,32,24),saturation_level=250),
        selection=SelectionConfigV3(),
        axis=AxisCalibration(TurntableModel((0,-12,100),(0,1,0),math.tau/3200,CALIBRATED,
             'synthetic only'),(1,0,0),'c'*64,'synthetic fixture, not physical evidence'),
        volume=WorkingVolume(20.,(-1.,50.),True,'d'*64,'synthetic fixture'),
        timing_models={n:ValidatedTimingModel('e'*64,'synthetic fixture',True,0,n,0.,10.,.5,3200) for n in (30,60,120)},
        effect_models={n:ExposureEffectModel(n,2,True,'f'*64,'synthetic fixture') for n in (30,60,120)})


class SyntheticSensor:
    def __init__(self): self.regs={7:0x188,15:0,175:0,53:16,11:60,213:0}
    def read(self, reg): return self.regs[reg]
    def write_verified(self, reg, value): self.regs[reg]=value; return value


class SyntheticBackend:
    def __init__(self,app,*,scenario,frames_per_turn):
        if scenario not in {'normal','weak_start','occluded','capture_fault','saturated_start'}:
            raise ValueError('unknown synthetic scenario')
        if type(frames_per_turn) is not int or not 12<=frames_per_turn<=480:
            raise ValueError('frames_per_turn must be 12..480')
        self.app,self.scenario,self.frames_per_turn=app,scenario,frames_per_turn
        self.sensor=SyntheticSensor();self.request=0;self.turn=-1;self.count=0
        self.completed=threading.Event();self.stopped=threading.Event();self.revoked=False
        self.bound=False
    def admit(self): return True
    def arm_guard(self):
        if self.stopped.is_set(): raise RuntimeError('fixture cannot restart after failure')
        self.turn+=1;self.count=0;self.completed.clear();self.revoked=False;self.bound=False
    def refresh_guard(self): pass
    def move(self,count,rpm):
        deadline=time.monotonic()+30
        while not self.completed.wait(.002):
            if self.stopped.is_set(): return self.receipt(done=False)
            if time.monotonic()>deadline: raise TimeoutError('synthetic capture budget not consumed')
        return self.receipt(done=True)
    def receipt(self,*,done):
        return dict(done=done, emitted=3200 if done else int(self.count*3200/self.frames_per_turn),
                    accepted_id=self.turn+1,epoch=2,error=0,busy=False,fault=False)
    def capture(self):
        if self.scenario=='capture_fault' and self.request==4:
            raise RuntimeError('injected same-frame capture fault')
        self.count+=1;self.request+=1
        q=self.request
        peak=40 if self.scenario=='weak_start' and self.sensor.regs[11]==60 else 180
        if self.scenario=='saturated_start' and self.sensor.regs[11]==60: peak=255
        if self.scenario=='occluded' and self.frames_per_turn//4 < self.count <= 3*self.frames_per_turn//4: peak=0
        gray=np.zeros((24,32),np.uint8);gray[:,19:22]=peak
        centers,accepted,_,_=select_rows(gray,self.app.stripe_config,self.app.selection)
        rows=tuple(DiagnosticRow(i,int(centers[i]),max(0,peak-60)*3,max(0,peak-60)*60,
            peak,3 if peak>60 else 0,int(peak>60),0,peak,bool(accepted[i]),0,q) for i in range(24))
        values={f.name:0 for f in fields(DiagnosticsSnapshot) if f.name!='rows'}
        values.update(width=32,height=24,epoch=1,request_id=q,row_count=24,ready=True,rows=rows)
        diag=DiagnosticsSnapshot(**values)
        profile=Profile(2,q,0,100,0,tuple(ProfilePoint(i,int(centers[i]),peak,bool(accepted[i])) for i in range(24)),32,24)
        meta=dict(request_id=q,capture_epoch=1,admitted_event_count=q,fv_ticks=q*10_000_000,
            motor_epoch=2,motor_accepted_id=self.turn+1,
            emitted_command_count=int(self.count*3200/self.frames_per_turn),clock_hz=100_000_000,
            stripe_config_fingerprint=self.app.stripe_config.fingerprint)
        if not self.bound:
            self.app.begin_motion(meta,self.turn);self.bound=True
        meta.update(self.app.coordinator.capture_metadata(q))
        raw=gray.tobytes()
        result=FeatureCapture(q,1,raw,hashlib.sha256(raw).hexdigest(),profile,diag,meta,
                              profile_payload=encode_profile(profile))
        if self.count==self.frames_per_turn:self.completed.set()
        return result
    def exposure_pending(self): return self.app.exposure_pending()
    def apply_exposure(self): return self.app.apply_exposure(self.sensor,camera_idle=True)
    def exposure_committed(self,change): self.app.exposure_committed(change)
    def turn_complete(self,*args): self.app.turn_complete(*args)
    def stop(self): self.stopped.set()
    def revoke(self): self.revoked=True
