"""Geometry-only normal frontend adapter with explicit bounded exceptional fallback."""
from __future__ import annotations
from dataclasses import dataclass,replace
from types import MappingProxyType
import threading,time,hashlib
import numpy as np
from .pfo3_r25 import decode,PFO3Error
from structured_light.scan3d.multibranch_processor_r21 import MultiBranchProcessor
from structured_light.scan3d.payload_digest import payload_sha256

def _immutable_image_digest(image):
    """Reuse hash only for an exact full view of a strongly retained bytes.

    Mutable/offset/strided inputs are copied and hashed, never identity cached
    as an ndarray. This preserves the actual transport input SHA comparison.
    """
    array=np.asarray(image);owner=array
    while isinstance(owner,np.ndarray) and owner.base is not None:owner=owner.base
    if (type(owner) is bytes and array.dtype==np.uint8 and array.shape==(480,752)
            and array.flags.c_contiguous and len(owner)==array.nbytes
            and array.ctypes.data==np.frombuffer(owner,np.uint8).ctypes.data):
        return payload_sha256(owner)
    return payload_sha256(array.tobytes())

def _forbidden(*args,**kwargs):
    raise RuntimeError('PL_FINAL_V3 normal path cannot execute ARM image algorithms')

@dataclass(frozen=True)
class PLFrameAdmission:
    identity: dict
    parameter_fingerprint: str
    bit_sha256: str
    stream_sha256: str
    hardware_completed: bool
    fixture_only: bool=False
    input_raw_sha256: str=''
    _transport_capability: object=None
    _validated_frame: object=None
    _validated_primary: object=None
    _primary_sha256: str=''
    _pair_seal: object=None

    def __post_init__(self):
        object.__setattr__(self,'identity',MappingProxyType(dict(self.identity)))
        sealed=None
        if self._pair_seal is not None:
            from .immutable_pair_seal_r25 import ImmutablePairSealR25
            if type(self._pair_seal) is not ImmutablePairSealR25:
                raise PFO3Error('invalid immutable pair seal')
            sealed=self._pair_seal.check(self._validated_frame,self._validated_primary)
            if (sealed.assembled_sha256!=self.stream_sha256
                    or sealed.primary_stream_sha256!=self._primary_sha256):
                raise PFO3Error('pair seal differs from transport digests')
        if self._validated_frame is not None:
            from .pfo3_r25 import FinalFrame,identity
            import struct
            frame=self._validated_frame
            if self.fixture_only or type(frame) is not FinalFrame:
                raise PFO3Error('cached final frame requires actual transport archive')
            fingerprint=struct.pack('<8I',*(frame.header['fingerprint'+str(i)] for i in range(8))).hex()
            if (type(frame.payload) is not bytes
                    or identity(frame.header)!=dict(self.identity) or fingerprint!=self.parameter_fingerprint
                    or (sealed is None and hashlib.sha256(frame.payload).hexdigest()!=self.stream_sha256)
                    or frame.records.flags.writeable or frame.rows.flags.writeable):
                raise PFO3Error('cached final frame differs from immutable transport admission')
        if self._validated_primary is not None:
            if (self._validated_frame is None or type(self._validated_primary) is not bytes
                    or (sealed is None and hashlib.sha256(self._validated_primary).hexdigest()!=self._primary_sha256)):
                raise PFO3Error('cached primary differs from immutable transport admission')

    def persistence_frame(self,record):
        """Reuse one completed immutable pair; disk readers still fully recheck."""
        self.validate()
        frame=self._validated_frame
        if (frame is None or self._validated_primary is None
                or record.get('source_pfo3_payload') is not frame.payload
                or record.get('source_pfo3_primary') is not self._validated_primary
                or record.get('raw_sha256')!=self.input_raw_sha256
                or record.get('request_id')!=self.identity['frame_id']
                or record.get('capture_epoch')!=self.identity['epoch']):
            raise PFO3Error('persistence bytes differ from completed immutable PL pair')
        return frame

    def validate(self):
        if (self.hardware_completed is not True and not self.fixture_only):
            raise PFO3Error('unconfirmed actual hardware completion')
        for value in (self.parameter_fingerprint,self.bit_sha256,self.stream_sha256):
            if not isinstance(value,str) or len(value)!=64 or value.strip('0123456789abcdef'):
                raise PFO3Error('missing source hash')
        if not self.fixture_only and self.identity['source'] not in (1,2):raise PFO3Error('not actual PL source')
        if not self.fixture_only:
            from .pfo3_transport_r25 import _TRANSPORT_TOKEN
            if self._transport_capability is not _TRANSPORT_TOKEN:
                raise PFO3Error('hardware admission must originate in actual DMA transport')
            if not isinstance(self.input_raw_sha256,str) or len(self.input_raw_sha256)!=64 or self.input_raw_sha256.strip('0123456789abcdef'):
                raise PFO3Error('actual hardware input Raw hash missing')

class PLFinalProcessorR25(MultiBranchProcessor):
    """Keeps existing geometry; all image observations/quality come from admitted PL."""
    supports_pfo3=True

    @property
    def measurement_validity(self):
        validity=super().measurement_validity
        if getattr(self,'_r25_policy_mode',None)=='preview':
            return replace(validity,axis_valid=False,reference_basis='PREVIEW_ONLY_NOT_FOR_DIMENSIONS')
        return validity

    def _observations(self,image,observed=None):
        if self._r25_frame is None:raise PFO3Error('no current final observations')
        profile=self._r25_frame.profile(self._r25_mode,qualified_only=True)
        from .pfo3_r25 import FinalFrame
        if type(self._r25_frame) is FinalFrame:
            # process_final admitted and strictly decoded this immutable wire
            # frame first. profile() only converts its already checked uint32
            # fields/known reason codes; no user-supplied extractor runs here.
            # Keep the generic contract validator for explicit CPU takeover.
            return profile,profile.rows,profile.center_q12,profile.accepted
        return super()._observations(image,profile)

    def _quality(self,image,observed,rows,accepted,reasons):
        if self._r25_frame is None:raise PFO3Error('missing PL image quality')
        return self._r25_frame.quality(self._r25_mode)

    def _image_usable(self,quality):
        return self._r25_frame.frame_usable(self._r25_mode)

    def _geometry_quality_gate(self,valid,reasons,quality):
        if self._image_usable(quality):return valid,reasons
        values=np.asarray(list(reasons),dtype=object)
        values[np.flatnonzero(valid)]='PL_frame_quality_gate'
        return np.zeros_like(valid),values.tolist()

    def process_final(self,capture,metadata=None,*,payload,admission,mode='measurement',**kwargs):
        admission.validate()
        cached=admission._validated_frame
        if (not admission.fixture_only and cached is not None and payload is cached.payload):
            # Only the actual transport can issue this completion capability.
            # assemble() already fully validated this same immutable bytes
            # object. Reuse it across measurement/preview instead of parsing
            # its arrays and checking the same CRC three times.
            frame=cached
        else:
            if hashlib.sha256(payload).hexdigest()!=admission.stream_sha256:
                raise PFO3Error('PFO3 payload differs from admitted packet hash')
            frame=decode(payload,expected_identity=admission.identity,
                         expected_fingerprint=admission.parameter_fingerprint,allow_fixture=admission.fixture_only)
        if frame.raw_required:raise PFO3Error('explicit same-frame fallback required; no implicit fallback')
        if mode not in ('measurement','preview'):raise PFO3Error('unknown mode')
        if not admission.fixture_only:
            raw_hash=(_immutable_image_digest(capture) if self.replay
                      else capture.raw_sha256)
            if raw_hash!=admission.input_raw_sha256:
                raise PFO3Error('geometry/display Raw differs from actual PL input Raw')
        captured=metadata if self.replay else (capture.motion_metadata or {})
        mapping={'cfg_version':'feature_config_revision','exposure_version':'exposure_revision',
                 'motor_id':'motor_accepted_id','motor_epoch':'motor_epoch',
                 'motor_steps':'emitted_command_count','FV_ticks':'fv_ticks',
                 'exposure_start':'exposure_start_ticks','exposure_end':'exposure_end_ticks'}
        if any(captured.get(key)!=admission.identity[field] for field,key in mapping.items()):
            raise PFO3Error('captured configuration/exposure/motion differs from PL identity')
        with self._lock:
            if getattr(self,'_r25_policy_mode',mode)!=mode:
                raise PFO3Error('measurement and preview require separate processor/cloud instances')
            self._r25_policy_mode=mode
            # Shared geometry is unchanged. The only frontend/quality entrypoints
            # are overridden above; missing packets cannot trigger legacy extraction.
            self._r25_frame=frame;self._r25_mode=mode;self.extractor=_forbidden
            meta=dict(metadata or {}) if self.replay else metadata
            if meta is not None:
                if meta.get('request_id')!=frame.header['frame_id'] or meta.get('capture_epoch')!=frame.header['epoch']:
                    raise PFO3Error('geometry request differs from PL frame')
                meta.update(processing_backend='SOFTWARE_PFO3_FIXTURE_ONLY' if admission.fixture_only else 'PL_FINAL_V3',
                    pfo3_algorithm_revision=frame.header['algorithm_revision'],
                    pfo3_mode=mode,ARM_image_algorithm_calls=0,pfo3_stream_sha256=admission.stream_sha256,
                    pfo3_bit_sha256=admission.bit_sha256)
            else:
                if capture.request_id!=frame.header['frame_id'] or capture.epoch!=frame.header['epoch']:
                    raise PFO3Error('capture differs from PL frame')
            try:
                result=super().process(capture,meta,pmc2_payload=b'',**kwargs)
                if mode=='preview':result=replace(result,valid=False,fused=False,profile_valid=False)
                result.frame_metadata.update(pfo3_mode=mode,ARM_image_algorithm_calls=0,
                    PL_success=not admission.fixture_only,
                    pfo3_algorithm_revision=frame.header['algorithm_revision'],
                    processing_backend='SOFTWARE_PFO3_FIXTURE_ONLY' if admission.fixture_only else 'PL_FINAL_V3',
                    pfo3_stream_sha256=admission.stream_sha256,pfo3_bit_sha256=admission.bit_sha256,
                    pfo3_input_raw_sha256=admission.input_raw_sha256,
                    pfo3_source=frame.header['source'],pfo3_frame_usable=frame.frame_usable(mode),
                    pfo3_dimensions_allowed=(mode=='measurement'))
                return result
            finally:self._r25_frame=None

    def process_takeover(self,capture,*,outcome,mode='measurement',allow_test_outcome=False):
        """Explicit exceptional geometry after the coordinator, never PL success."""
        from .v3_exception_observations_r25 import CPUReferenceObservationsR25,takeover_sources
        if self.replay:raise PFO3Error('takeover entry requires actual admitted FeatureCapture')
        sources,reference,info=takeover_sources(outcome);provenance=info['cpu_provenance']
        mock=provenance['CPU_result_source']=='MOCK_ONLY_ARM_V3'
        if ((mock and allow_test_outcome is not True) or (not mock and allow_test_outcome)
                or (not mock and provenance.get('evidence_type')!='ACTUAL_PL_FAILED_FRAME_ARCHIVE')):
            raise PFO3Error('CPU takeover evidence domain is not admitted')
        raw_hash=hashlib.sha256(capture.raw).hexdigest()
        if raw_hash!=capture.raw_sha256 or raw_hash!=provenance['raw_sha256']:
            raise PFO3Error('CPU takeover differs from captured Raw')
        expected=provenance['identity'];captured=capture.motion_metadata or {}
        mapping={'cfg_version':'feature_config_revision','exposure_version':'exposure_revision',
            'motor_id':'motor_accepted_id','motor_epoch':'motor_epoch','motor_steps':'emitted_command_count',
            'FV_ticks':'fv_ticks','exposure_start':'exposure_start_ticks','exposure_end':'exposure_end_ticks'}
        if (expected['frame_id']!=capture.request_id or expected['epoch']!=capture.epoch
                or any(captured.get(key)!=expected[field] for field,key in mapping.items())
                or (not mock and expected['source']!=1)):
            raise PFO3Error('CPU takeover capture/config/exposure/motion identity mismatch')
        if mode not in ('measurement','preview'):raise PFO3Error('unknown exceptional mode')
        image=CPUReferenceObservationsR25(reference)
        with self._lock:
            if getattr(self,'_r25_policy_mode',mode)!=mode:raise PFO3Error('cannot mix policy clouds')
            self._r25_policy_mode=mode;self._r25_mode=mode;self._r25_frame=image;self.extractor=_forbidden
            try:
                result=super().process(capture,pmc2_payload=b'')
                if mode=='preview':result=replace(result,valid=False,fused=False,profile_valid=False)
                result.frame_metadata.update(processing_backend='MOCK_ONLY_PS_V3_EXPLICIT_FALLBACK' if mock else 'PS_V3_EXPLICIT_FALLBACK',
                    ARM_image_algorithm_calls=1,PL_success=False,pfo3_mode=mode,
                    pfo3_dimensions_allowed=mode=='measurement',pfo3_input_raw_sha256=capture.raw_sha256,
                    cpu_v3_takeover=info,image_frame_usable=image.frame_usable(mode),
                    cpu_v3_npz_sha256=hashlib.sha256(sources['source_cpu_v3_npz']).hexdigest(),
                    cpu_v3_manifest_sha256=hashlib.sha256(sources['source_cpu_v3_manifest']).hexdigest(),
                    failed_pfo3_stream_sha256=hashlib.sha256(sources['source_pfo3_payload']).hexdigest())
                return result
            finally:self._r25_frame=None

class BoundedV3Fallback:
    """Single outstanding fallback: depth1 is within the authorized max2 limit.

    A timed-out worker stays occupied; callers cannot queue more unbounded work.
    Hardware faults/identity failures must never be passed here as legal causes.
    """
    def __init__(self,extractor,*,deadline_s=.5,max_consecutive=3):
        self.extractor=extractor;self.deadline_s=deadline_s;self.max_consecutive=max_consecutive
        self.consecutive=0;self.calls=0;self._busy=False;self._lock=threading.Lock();self.stopped=False

    def normal_frame(self):self.consecutive=0

    def run(self,raw,*,cause,capture_identity,expected_identity,expected_raw_sha256):
        if cause not in ('unsupported_config','qualified_overflow'):raise PFO3Error('fault cannot be repaired by fallback')
        if capture_identity!=expected_identity or set(capture_identity)!=set(self_identity_fields()):
            raise PFO3Error('untrusted fallback identity')
        if any(type(v) is not int or v<0 for v in capture_identity.values()):raise PFO3Error('invalid fallback identity')
        image=np.asarray(raw)
        if image.dtype!=np.uint8 or image.shape!=(480,752):raise PFO3Error('fallback requires complete752x480Raw')
        immutable=image.tobytes(order='C')
        if hashlib.sha256(immutable).hexdigest()!=expected_raw_sha256:raise PFO3Error('fallback Raw hash mismatch')
        image=np.frombuffer(immutable,np.uint8).reshape(480,752)
        with self._lock:
            if self.stopped or self._busy:raise PFO3Error('fallback capacity/degraded stop')
            self.consecutive+=1;self.calls+=1
            if self.consecutive>=self.max_consecutive:
                self.stopped=True;raise PFO3Error('consecutive fallback limit; stop new measurements')
            self._busy=True
        result={};done=threading.Event()
        def worker():
            try:result['value']=self.extractor(image)
            except BaseException as exc:result['error']=exc
            finally:
                with self._lock:self._busy=False
                done.set()
        thread=threading.Thread(target=worker,daemon=True,name='v3_exceptional_fallback')
        started=time.perf_counter();thread.start()
        if not done.wait(self.deadline_s):
            self.stopped=True;raise PFO3Error('fallback deadline; worker retained, no further admissions')
        if 'error' in result:raise result['error']
        return dict(backend='PS_V3_EXPLICIT_FALLBACK',value=result['value'],cause=cause,
                    wall_s=time.perf_counter()-started,PL_success=False)

def self_identity_fields():
    from .pfo3_r25 import IDENTITY_FIELDS
    return IDENTITY_FIELDS
