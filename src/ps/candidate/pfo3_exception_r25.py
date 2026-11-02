"""Explicit same-Raw V3 exception outcome; never a normal image frontend.

The caller installs this coordinator and separately admits any CPU observation
to geometry. An archive or CPU result is never represented as PL success.
"""
from dataclasses import asdict,dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
from types import MappingProxyType

import numpy as np

from .pfo3_processor_r25 import BoundedV3Fallback
from .pfo3_r25 import (COMPLETE,RAW_REQUIRED,UNSUPPORTED,OVERFLOW,PROTOCOL,DIAG_OVERFLOW,
    IDENTITY_FIELDS,PFO3Error,decode,identity)
from .pfo3_stream_r25 import assemble
from .v3_reference_r25 import V3Config,V3Result,extract_v3

CPU_DIAGNOSTIC_LIMIT=8192


@dataclass(frozen=True)
class TakeoverOutcomeR25:
    state:str
    cause:object
    archive_dir:Path
    reference_result:object
    cpu_provenance:object
    measurement_allowed:bool=False
    preview_allowed:bool=False
    stop_required:bool=False
    error:object=None

    @property
    def PL_success(self):return False

    def as_dict(self):
        return dict(state=self.state,cause=self.cause,archive_dir=str(self.archive_dir),
            backend='PS_V3_EXPLICIT_FALLBACK',PL_success=False,
            measurement_allowed=self.measurement_allowed,preview_allowed=self.preview_allowed,
            stop_required=self.stop_required,error=self.error,cpu_provenance=dict(self.cpu_provenance),
            reference_frame=None if self.reference_result is None else asdict(self.reference_result.frame))


class PFO3ExceptionCoordinatorR25:
    """One synchronous caller, one bounded CPU worker, third failure stops.

    Only known V3Config/full native ROI is a CPU support domain. Test injection
    is explicitly MOCK_ONLY and cannot accept actual hardware admissions.
    """
    def __init__(self,output,storage_budget,*,config=None,test_only=False,test_extractor=None):
        self.config=V3Config() if config is None else config
        if not isinstance(self.config,V3Config):raise TypeError('explicit V3Config required')
        if type(test_only) is not bool:raise TypeError('explicit test-only boolean required')
        if test_extractor is not None and not test_only:raise PFO3Error('extractor injection is MOCK_ONLY')
        self.output=Path(output);self.output.mkdir(parents=True,exist_ok=False)
        self.budget=storage_budget;self.test_only=test_only
        self._lock=threading.Lock();self._sequence=0;self._cpu_calls=0;self.stopped=False
        self._calls_lock=threading.Lock()
        def cpu(image):
            with self._calls_lock:self._cpu_calls+=1
            return test_extractor(image) if test_extractor is not None else extract_v3(image,self.config)
        self.fallback=BoundedV3Fallback(cpu,deadline_s=.5,max_consecutive=3)

    @property
    def cpu_calls(self):
        with self._calls_lock:return self._cpu_calls

    def normal_frame(self):
        if not self._lock.acquire(blocking=False):raise PFO3Error('exception coordinator still owns a frame')
        try:
            if self.stopped or self.fallback.stopped:raise PFO3Error('degraded coordinator requires explicit new session')
            self.fallback.normal_frame()
        finally:self._lock.release()

    def _write(self,path,data):
        token=self.budget.reserve((len(data)*5+3)//4)
        with path.open('xb') as stream:
            if stream.write(data)!=len(data):raise OSError('short exception archive write')
            stream.flush();os.fsync(stream.fileno())
        self.budget.commit(token,len(data))
        return dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())

    def _json(self,path,value):
        return self._write(path,(json.dumps(value,indent=2,allow_nan=False)+'\n').encode('utf8'))

    @staticmethod
    def _directory_sync(directory):
        if os.name=='nt':return False
        descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
        return True

    def _verify(self,capture,failed_frame,primary,receipt,admission):
        admission.validate()
        if self.test_only:
            if not admission.fixture_only or receipt.get('actual_PL_execution') is True:
                raise PFO3Error('MOCK_ONLY exception cannot admit hardware evidence')
        elif (admission.fixture_only or admission.hardware_completed is not True
                or receipt.get('actual_PL_execution') is not True):
            raise PFO3Error('actual transport completion required before exceptional takeover')
        if type(capture.raw) is not bytes or len(capture.raw)!=752*480:
            raise PFO3Error('exception requires complete immutable native Raw')
        raw=capture.raw;raw_sha=hashlib.sha256(raw).hexdigest()
        if (raw_sha!=capture.raw_sha256 or raw_sha!=admission.input_raw_sha256
                or receipt.get('input_sha256')!=raw_sha):raise PFO3Error('exception same-Raw SHA mismatch')
        m=capture.motion_metadata or {}
        mapping={'cfg_version':'feature_config_revision','exposure_version':'exposure_revision',
            'motor_id':'motor_accepted_id','motor_epoch':'motor_epoch','motor_steps':'emitted_command_count',
            'FV_ticks':'fv_ticks','exposure_start':'exposure_start_ticks','exposure_end':'exposure_end_ticks'}
        if any(key not in m for key in mapping.values()):raise PFO3Error('exception capture snapshot missing')
        expected=dict(frame_id=capture.request_id,epoch=capture.epoch,source=admission.identity['source'],
            **{field:m[key] for field,key in mapping.items()})
        if (set(expected)!=set(IDENTITY_FIELDS) or any(type(value) is not int or value<0 for value in expected.values())
                or expected!=dict(admission.identity)):
            raise PFO3Error('exception capture/config/exposure/motion identity mismatch')
        diagnostics=getattr(capture,'diagnostics',None)
        if (diagnostics is None or diagnostics.request_id!=capture.request_id or diagnostics.epoch!=capture.epoch
                or (diagnostics.width,diagnostics.height,diagnostics.row_count)!=(752,480,480)):
            raise PFO3Error('exception original capture diagnostic identity/dimensions missing')
        capture_fault=(diagnostics.ready is not True or diagnostics.error!=0
            or getattr(getattr(capture,'profile',None),'error_flags',0)!=0)
        if type(primary) is not bytes or type(failed_frame.payload) is not bytes:
            raise PFO3Error('immutable complete failed hardware sources required')
        digest=hashlib.sha256(failed_frame.payload).hexdigest()
        if (digest!=admission.stream_sha256 or digest!=receipt.get('assembled_sha256')
                or hashlib.sha256(primary).hexdigest()!=receipt.get('primary_stream_sha256')
                or receipt.get('bit_sha256')!=admission.bit_sha256):
            raise PFO3Error('exception source/admission/receipt SHA mismatch')
        frame=decode(failed_frame.payload,expected_identity=expected,
                     expected_fingerprint=admission.parameter_fingerprint,allow_fixture=self.test_only)
        restored,_=assemble(primary,expected_identity=expected,expected_fingerprint=admission.parameter_fingerprint)
        if restored.payload!=frame.payload:raise PFO3Error('exception primary differs from final archive')
        return raw,raw_sha,expected,frame,capture_fault

    def _cpu_archive(self,directory,result):
        arrays={'candidate_'+name:value for name,value in result.candidates.as_dict().items()}
        arrays.update(raw_count_by_row=result.raw_count_by_row,local_count_by_row=result.local_count_by_row,
                      slot_count_by_row=result.slot_count_by_row,row_data=result.row_data)
        if any(not isinstance(value,np.ndarray) or value.dtype.hasobject for value in arrays.values()):
            raise PFO3Error('complete numeric CPU V3 arrays required')
        bound=sum(value.nbytes for value in arrays.values())+len(arrays)*2048+65536
        token=self.budget.reserve((bound*5+3)//4)
        path=directory/'CPU_V3_reference_arrays.npz'
        with path.open('xb') as stream:
            np.savez(stream,**arrays);stream.flush();os.fsync(stream.fileno());size=stream.tell()
        if size>bound:raise PFO3Error('CPU archive exceeded reserved size')
        self.budget.commit(token,size)
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
        return dict(bytes=size,sha256=digest.hexdigest(),arrays=len(arrays))

    def __call__(self,capture,failed_frame,primary,receipt,admission):
        if not self._lock.acquire(blocking=False):
            raise PFO3Error('exception coordinator capacity; caller must retain unadmitted capture')
        try:
            if self.stopped:raise PFO3Error('exception coordinator stopped; caller must retain unadmitted capture')
            raw,raw_sha,expected,frame,capture_fault=self._verify(capture,failed_frame,primary,receipt,admission)
            self._sequence+=1
            directory=self.output/f'failure_{self._sequence:06d}_request_{capture.request_id:010d}_epoch_{capture.epoch:05d}'
            directory.mkdir()
            files={name:self._write(directory/name,data) for name,data in
                   (('Raw.gray8.bin',raw),('PL_primary.bin',primary),('PL_final.pfo3',frame.payload))}
            provenance=dict(evidence_type='MOCK_ONLY_NO_HARDWARE' if self.test_only else 'ACTUAL_PL_FAILED_FRAME_ARCHIVE',
                backend='PS_V3_EXPLICIT_FALLBACK',PL_success=False,actual_PL_result_success=False,
                actual_PL_failed_attempt_completed=not self.test_only,identity=expected,raw_sha256=raw_sha,
                algorithm_revision=25,fixed_revision=1,policy_revision=1,config=asdict(self.config),
                config_fingerprint=self.config.fingerprint,failed_parameter_fingerprint=admission.parameter_fingerprint,
                failed_flags=int(frame.header['flags']),failed_error_code=int(frame.header['error_code']),
                original_capture_fault=bool(capture_fault),
                transport_receipt=dict(receipt),files=files,CPU_deadline_s=.5,max_consecutive=3,max_outstanding=1,
                CPU_diagnostic_limit=CPU_DIAGNOSTIC_LIMIT,
                cpu_calls_before=self.cpu_calls,geometry_invoked=False,physical_accuracy_verified=False)
            self._json(directory/'FAILED_INPUT.json',provenance)
            self._directory_sync(directory);self._directory_sync(self.output)
            # All source bytes are fsynced before checking eligibility or starting
            # CPU work. Identity/hash failures above never reach this archive.
            flags=frame.header['flags'];cause=None;state=None;error=None;result=None
            allowed_flags=COMPLETE|RAW_REQUIRED|UNSUPPORTED|OVERFLOW
            if (capture_fault or flags&~allowed_flags or flags&(PROTOCOL|DIAG_OVERFLOW) or frame.header['error_code']
                    or not flags&RAW_REQUIRED or not flags&(UNSUPPORTED|OVERFLOW)):
                state='FATAL_SOURCE_FAILURE';error='hardware/protocol/diagnostic fault or no legal takeover cause'
            elif (admission.parameter_fingerprint!=self.config.fingerprint or
                  tuple(frame.header[key] for key in ('width','height','roi_x0','roi_y0','roi_x1','roi_y1'))!=(752,480,0,0,752,480)):
                state='CPU_SUPPORT_DOMAIN_REJECTED';error='unknown V3 config or non-native/full ROI; no substitute policy'
            elif frame.header['diagnostic_count']>CPU_DIAGNOSTIC_LIMIT:
                state='CPU_RESOURCE_DOMAIN_REJECTED'
                error='complete failed source exceeds8192diagnostic CPU support bound; no truncation or CPU attempt'
            else:
                cause='qualified_overflow' if flags&OVERFLOW else 'unsupported_config'
                try:
                    fallback=self.fallback.run(np.frombuffer(raw,np.uint8).reshape(480,752),cause=cause,
                        capture_identity=expected,expected_identity=identity(frame.header),expected_raw_sha256=raw_sha)
                    result=fallback['value'];provenance['CPU_wall_s']=fallback['wall_s']
                    if (not isinstance(result,V3Result) or result.config_fingerprint!=self.config.fingerprint
                            or (result.frame.width,result.frame.height)!=(752,480)):
                        raise PFO3Error('CPU V3 result/config domain mismatch')
                    provenance['CPU_arrays']=self._cpu_archive(directory,result)
                    provenance['CPU_frame']=asdict(result.frame)
                    state=('CPU_REJECTED_K16_OVERFLOW' if result.frame.overflow_rows else
                           'CPU_V3_READY' if result.frame.measurement_usable or result.frame.preview_usable else 'CPU_REJECTED_IMAGE_QUALITY')
                except BaseException as exc:
                    state='CPU_DEGRADED_STOP';error=type(exc).__name__+': '+str(exc);result=None
            measurement=bool(state=='CPU_V3_READY' and result.frame.measurement_usable)
            preview=bool(state=='CPU_V3_READY' and result.frame.preview_usable)
            stop=state in ('FATAL_SOURCE_FAILURE','CPU_SUPPORT_DOMAIN_REJECTED','CPU_RESOURCE_DOMAIN_REJECTED','CPU_DEGRADED_STOP')
            self.stopped=stop
            provenance.update(cpu_calls_after=self.cpu_calls,CPU_state=state,CPU_error=error,
                CPU_result_source='MOCK_ONLY_ARM_V3' if self.test_only else 'ARM_V3_EXPLICIT_FALLBACK',
                permissions_scope='CPU_IMAGE_QUALITY_ONLY; original geometry/exposure/motion admission still required',
                measurement_allowed=measurement,preview_allowed=preview,stop_required=stop,
                directory_fsync_supported=os.name!='nt')
            outcome=TakeoverOutcomeR25(state,cause,directory,result,MappingProxyType(provenance),measurement,preview,stop,error)
            self._json(directory/'TAKEOVER_OUTCOME.json',outcome.as_dict());self._directory_sync(directory)
            return outcome
        except BaseException:
            self.stopped=True
            raise
        finally:self._lock.release()
