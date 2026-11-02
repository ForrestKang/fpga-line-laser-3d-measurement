"""Explicitly admitted PYNQ adapter for the finite continuous scheduler.

Construction/import do not touch hardware. A caller must own the existing
exclusive_capture_generation scope, validate optical evidence and prepare a
processor for each explicitly declared motor epoch. No fault clear is allowed.
"""
from __future__ import annotations
from dataclasses import dataclass,replace
import time,threading
from ..motion.motor_control import REGISTERS,COMMANDS
from .stream_capture import capture_features

class GenerationCheckedMMIO:
    """Every diagnostic/SCCB operation belongs to one admitted generation."""
    def __init__(self,mmio,gate):
        self.original=mmio;self.gate=gate;self.generation=gate.generation
        gate.check(self.generation)
    def read(self,offset):return self.gate.call(self.generation,self.original.read,offset)
    def write(self,offset,value):return self.gate.call(self.generation,self.original.write,offset,value)

@dataclass(frozen=True)
class ContinuousAdmission:
    boot_id:str
    bit_sha256:str
    authority_statement:str
    authority_created_unix:float
    pulse_budget:int
    power_on:bool
    target_fixed:bool
    stationary:bool
    hand_clear:bool
    cables_clear:bool
    laser_available:bool
    axis_evidence_verified:bool
    timing_evidence_verified:bool
    volume_evidence_verified:bool

    def validate(self,*,boot_id,bit_sha256,now=None):
        now=time.time() if now is None else now
        if self.boot_id!=boot_id or self.bit_sha256!=bit_sha256:
            raise ValueError('motion authority is bound to another boot or bit')
        if not self.authority_statement.strip() or not 0<=now-self.authority_created_unix<=300:
            raise ValueError('fresh human physical-state confirmation required')
        if type(self.pulse_budget) is not int or self.pulse_budget not in (3200,6400):
            raise ValueError('finite one-turn or one-turn-plus-one-rescan authority required')
        fields=('power_on','target_fixed','stationary','hand_clear','cables_clear','laser_available',
                'axis_evidence_verified','timing_evidence_verified','volume_evidence_verified')
        if any(getattr(self,k) is not True for k in fields):
            raise ValueError('continuous optical motion conditions/evidence are incomplete')

class NoFaultClearTransport:
    """Preserve every old motor operation except automatic CLEAR."""
    def __init__(self,transport):self.original=transport
    def __getattr__(self,name):return getattr(self.original,name)
    def read32(self,offset):return self.original.read32(offset)
    def write32(self,offset,value):
        if offset==REGISTERS['command'] and value&COMMANDS['clear']:
            raise RuntimeError('fault encountered: continuous path forbids automatic CLEAR/restart')
        self.original.write32(offset,value)
    def close(self):return self.original.close()

class RuntimeContinuousBackend:
    """Exact 2.5 RPM command path; no assumed/exposure-free angle mode.

    ``begin_motion(metadata, turn_index)`` explicitly binds a new processor
    epoch after the first CSM1 record. The caller must account for the prior
    turn's cumulative command offset while preserving the calibrated axis.
    ``frame_config`` must be built from checked PL configuration readbacks.
    This adapter never silently supplies optical validity or motion authority.
    """
    def __init__(self,runtime,*,admission:ContinuousAdmission,frame_config:dict,
                 verify_evidence,begin_motion,clock=time.monotonic,sleep=time.sleep):
        self.runtime=runtime;self.admission=admission;self.frame_config=dict(frame_config)
        self.verify_evidence=verify_evidence;self.begin_motion=begin_motion
        self.clock,self.sleep=clock,sleep;self.admitted=False;self.turn_index=-1
        self.expected_motor_id=None;self.bound=False;self.motion_started=threading.Event()
        self.commanded_budget=0;self._capture_epoch=None
        self.prestart_frames=[]

    def admit(self):
        from pathlib import Path
        rt=self.runtime
        if not rt.session._gate.active:raise RuntimeError('exclusive capture generation required')
        if rt.manifest_record.get('frame_motion_metadata',{}).get('magic')!=0x43534d31:
            raise RuntimeError('CSM1 manifest required before MMIO access')
        self.admission.validate(boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                                bit_sha256=rt.artifacts['files']['bit']['sha256'])
        if self.verify_evidence() is not True:raise RuntimeError('optical/timing evidence verification failed')
        if not isinstance(self.frame_config.get('stripe_config_fingerprint'),str):
            raise RuntimeError('readback-bound feature configuration missing')
        if not 1<=self.frame_config.get('exposure_lines',0)<=32765:
            raise RuntimeError('readback-bound exposure missing')
        g,m=rt.guard.status(),rt.motor.status()
        if any(g.get(k) for k in ('active','fault','motor_busy','step_pin','error')):
            raise RuntimeError('guard not clean idle; no fault clear performed')
        if any(m.get(k) for k in ('busy','fault','step','error')):
            raise RuntimeError('motor not clean idle; no fault clear performed')
        if g.get('enable_pin') is not True or m.get('enable_n') is not True:
            raise RuntimeError('disabled-output readback required before motion')
        rt.motor._transport=NoFaultClearTransport(rt.motor._transport)
        rt.transport.frame_motion_metadata_supported=True
        self.admitted=True;return True

    def arm_guard(self):
        if not self.admitted:raise RuntimeError('backend not admitted')
        if self.commanded_budget+3200>self.admission.pulse_budget:raise RuntimeError('motion budget exhausted')
        motor=self.runtime.motor.status();guard=self.runtime.guard.status()
        if motor['busy'] or motor['fault'] or motor['error'] or guard['fault'] or guard['error']:
            raise RuntimeError('new motion begins only from clean idle; no reset/retry')
        self.expected_motor_id=motor['accepted_id']+1
        if self.expected_motor_id>0xffffffff or guard['accepted_id']==0xffffffff:raise RuntimeError('request ID exhausted')
        self.turn_index+=1;self.bound=False;self.motion_started.clear()
        self.runtime.guard.arm(request_id=guard['accepted_id']+1,lease_cycles=10_000_000,timeout_ms=120_000)

    def refresh_guard(self):return self.runtime.guard.refresh()

    def worker_token(self):
        gate=self.runtime.session._gate
        token=gate.generation;gate.check(token);return token

    def bind_worker(self,token):
        gate=self.runtime.session._gate;gate.bind_worker(token);gate.check(token)

    def unbind_worker(self):self.runtime.session._gate.unbind_worker()

    def quarantine_workers(self):
        session=self.runtime.session
        session._poisoned=True;session._external_worker_unconfirmed=True
        session._gate.revoke(timeout_s=session.config.cleanup_timeout_s)

    def move(self,count,rpm):
        if not self.admitted or self.expected_motor_id is None or count!=3200 or rpm!=2.5:
            raise ValueError('only an admitted exact 3200-command turn at 2.5 RPM is supported')
        self.commanded_budget+=count;self.motion_started.set()
        return self.runtime.motor.move_pulses(count=3200,period_cycles=750_000,high_cycles=375_000,
            dir_setup_cycles=10_000_000,enable_setup_cycles=10_000_000,lease_cycles=100_000_000,
            timeout_cycles=3_200_000_000,direction=0,request_id=self.expected_motor_id,timeout_s=32.)

    def capture(self):
        if not self.admitted:raise RuntimeError('backend not admitted')
        deadline=self.clock()+2.
        # MotorController intentionally owns its status transport for the
        # whole move. Association comes from atomic CSM1 capture metadata,
        # never from racing a second motor.status() reader against that owner.
        while True:
            if self.clock()>=deadline:raise TimeoutError('new motor task did not start before capture deadline')
            if not self.motion_started.is_set():self.sleep(.002);continue
            result=capture_features(self.runtime.controller,require_motion_metadata=True)
            meta=dict(result.motion_metadata)
            if meta['motor_accepted_id']>self.expected_motor_id:raise ValueError('frame associated with unexpected later motion task')
            minimum=self.frame_config.get('minimum_registered_command_count',1)
            if meta['motor_accepted_id']==self.expected_motor_id and meta['emitted_command_count']>=minimum:break
            self.prestart_frames.append({'request_id':result.request_id,'capture_epoch':result.epoch,
                'raw_sha256':result.raw_sha256,'motion':meta,'reason':'before_registered_motion_window'})
            preserve=getattr(self,'record_prestart',None)
            if preserve is not None:preserve(result)
        if self._capture_epoch is not None and result.epoch!=self._capture_epoch:raise ValueError('capture epoch changed')
        self._capture_epoch=result.epoch
        if not self.bound:
            if self.begin_motion(dict(meta),self.turn_index) is not True:raise RuntimeError('motion epoch not explicitly bound')
            self.bound=True
        meta.update(self.frame_config)
        return replace(result,motion_metadata=meta)

    def stop(self):
        if self.admitted:return self.runtime.motor.stop()

    def revoke(self):return self.runtime.guard.revoke(deadline_s=2.)
