"""Formal application backend. Same-frame camera Raw passes actual PL via DDR.

Geometry/axis/exposure admission stays in the original formal processor. No
camera or motor commands occur here. Unsupported/overflow frames stop explicitly;
bounded V3 exceptional handling can be installed through exception_handler.
"""
import copy
import numpy as np
from .pfo3_processor_r25 import PLFinalProcessorR25
from .formal_resources_r22 import DiskCloudProcessor
from .pfo3_r25 import PFO3Error


from .pfo3_geometry_pair_r25 import PLGeometryPairProcessorR25
from .observation_core_r26 import ObservationProcessingCore


class DiskPLGeometryPairProcessorR26(PLGeometryPairProcessorR25, DiskCloudProcessor):
    pass


class DiskPLFinalProcessorR25(PLFinalProcessorR25,DiskCloudProcessor):
    pass


def capture_identity(capture):
    m=capture.motion_metadata or {}
    fields={'cfg_version':'feature_config_revision','exposure_version':'exposure_revision',
            'motor_id':'motor_accepted_id','motor_epoch':'motor_epoch',
            'motor_steps':'emitted_command_count','FV_ticks':'fv_ticks',
            'exposure_start':'exposure_start_ticks','exposure_end':'exposure_end_ticks'}
    missing=[name for name in fields.values() if name not in m]
    if missing:raise PFO3Error('PFO3 capture association missing: '+','.join(missing))
    return dict(frame_id=capture.request_id,epoch=capture.epoch,source=1,
                **{field:m[key] for field,key in fields.items()})


def application_type():
    # Imported by continuous_scan only after its existing pure preflight gates.
    from continuous_multibranch import MultiBranchScanApplication
    class PLFinalScanApplication(MultiBranchScanApplication):
        pl_transport=None
        exception_handler=None
        exception_coordinator=None
        exception_test_only=False
        shared_observations_enabled=False
        preview_processor=None
        preview_consumer=None
        _observation_core=None

        def enable_shared_observations(self, *, preview_consumer):
            """Enable before motion binding; consumer must retain preview separately.

            This is software wiring, not hardware or motion authorization.
            Original scheduler preflight remains mandatory.
            """
            if self.processor is not None:
                raise PFO3Error('enable shared observations before motion binding')
            if not callable(preview_consumer):
                raise PFO3Error('separate preview consumer required')
            self.shared_observations_enabled=True
            self.preview_consumer=preview_consumer

        def begin_motion(self, metadata, turn):
            result=super().begin_motion(metadata,turn)
            if not self.shared_observations_enabled:return result
            if turn == 0:
                ctrl=self.coordinator.controller
                # Independent binder/state/cloud; identical validated policy.
                self.preview_processor=PLGeometryPairProcessorR25(
                    calibration=self.calibration,stripe_config=self.stripe_config,
                    selection=self.selection,mode='continuous',exposure_lines=ctrl.lines,
                    exposure_revision=ctrl.revision,axis=self.axis,volume=self.volume,
                    timing_binder=copy.deepcopy(self.processor.timing_binder),
                    exposure_controller=ctrl)
                if self.processor.replay or self.preview_processor.replay:
                    raise PFO3Error('formal camera processing cannot use replay geometry')
                self._observation_core=ObservationProcessingCore(self.processor,self.preview_processor)
            else:
                self.preview_processor.begin_motion_epoch(
                    capture_epoch=metadata['capture_epoch'],motor_epoch=metadata['motor_epoch'],
                    motor_accepted_id=metadata['motor_accepted_id'],previous_receipt=self.previous_receipt,
                    capture_joined=True,processing_drained=True,guard_was_revoked=True)
            return result

        def exposure_committed(self, change):
            super().exposure_committed(change)
            if self.preview_processor is not None:
                self.preview_processor.begin_exposure_revision(change['lines'],change['revision'],
                    self.timing_model(change['lines'],change['revision']),
                    capture_drained=True,processing_drained=True,camera_idle=True)
        def bind_exception_coordinator(self,coordinator,*,allow_test=False):
            from .pfo3_exception_r25 import PFO3ExceptionCoordinatorR25
            if (not isinstance(coordinator,PFO3ExceptionCoordinatorR25) or
                    coordinator.test_only is not allow_test):raise PFO3Error('exception coordinator evidence domain mismatch')
            self.exception_coordinator=coordinator;self.exception_test_only=allow_test
            self.exception_handler=self._takeover

        def _takeover(self,capture,frame,primary,receipt,admission):
            from .v3_exception_observations_r25 import takeover_sources
            outcome=self.exception_coordinator(capture,frame,primary,receipt,admission)
            self.writer.submit_event(dict(event='PFO3_explicit_V3_takeover',outcome=outcome.as_dict(),
                PL_success=False,physical_accuracy_verified=False))
            if outcome.stop_required or outcome.reference_result is None:
                durable=self.writer.flush(timeout_s=5.)
                if not durable.durable:raise PFO3Error('exception stop outcome persistence unconfirmed')
                raise PFO3Error('explicit V3 takeover stopped: '+outcome.state)
            result=self.processor.process_takeover(capture,outcome=outcome,allow_test_outcome=self.exception_test_only)
            sources,_,_=takeover_sources(outcome)
            return result,sources
        def _processor_type(self):
            if self.shared_observations_enabled:
                return DiskPLGeometryPairProcessorR26 if self.cloud_spool is not None else PLGeometryPairProcessorR25
            return DiskPLFinalProcessorR25 if self.cloud_spool is not None else PLFinalProcessorR25

        def _process_capture(self,capture):
            if self.pl_transport is None:raise PFO3Error('actual PL transport not bound')
            identity=capture_identity(capture)
            raw=np.frombuffer(capture.raw,np.uint8).reshape(480,752)
            frame,primary,receipt,admission=self.pl_transport.run(raw,identity=identity)
            if frame.raw_required:
                if self.exception_handler is not None:
                    return self.exception_handler(capture,frame,primary,receipt,admission)
                raise PFO3Error('PL requires explicit whole-frame V3 takeover; no implicit CPU path')
            core=self._observation_core
            if core is None:
                if self.shared_observations_enabled:
                    raise PFO3Error('shared observations enabled without motion-bound processors')
                core=ObservationProcessingCore(self.processor)
            pair_result=core.process(capture,frame=frame,admission=admission)
            result=pair_result.measurement
            if pair_result.preview is not None:
                # Caller owns independent preview persistence; its failure
                # propagates and cannot silently produce successful scan status.
                self.preview_consumer(capture,pair_result.preview,frame,primary,receipt,admission)
                result.frame_metadata['shared_observation_core']='camera_measurement_preview'
                result.frame_metadata['shared_geometry_timings']=pair_result.timings
            result.frame_metadata['pfo3_transport']=receipt
            if self.exception_coordinator is not None:self.exception_coordinator.normal_frame()
            return result,dict(source_pfo3_payload=frame.payload,source_pfo3_primary=primary,
                _pfo3_completion=admission)
    return PLFinalScanApplication
