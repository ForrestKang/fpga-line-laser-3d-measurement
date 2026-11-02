"""Formal application integration for variable-length PS image observations.

Hardware admission remains in continuous_scan.run_scan. This application only
replaces the pure processor after the normal registered motion binding.
"""
import time

from structured_light.scan3d.multibranch_processor_r21 import MultiBranchProcessor
from structured_light.scan3d.stream_application import ScanApplication
from structured_light.scan3d.continuous_timing import TimingBinder


class MultiBranchScanApplication(ScanApplication):
    def _processor_type(self):
        if self.cloud_spool is not None:
            from candidate.formal_resources_r22 import DiskCloudProcessor
            return DiskCloudProcessor
        return MultiBranchProcessor

    def __init__(self,**kwargs):
        super().__init__(**kwargs)
        self.cloud_spool=None
        if getattr(self.writer,'supports_multibranch_binary',False) is True:
            from candidate.ps_pipeline_r22 import CloudSpool
            self.cloud_spool=CloudSpool(self.writer.output_dir.parent/'cloud_spool.raw',
                                       storage_budget=self.writer.storage_budget)

    def begin_motion(self, metadata, turn):
        if turn != 0:
            return super().begin_motion(metadata, turn)
        if self.processor is not None: raise RuntimeError('first motion already bound')
        ctrl=self.coordinator.controller
        binder=TimingBinder(capture_epoch=metadata['capture_epoch'],motor_epoch=metadata['motor_epoch'],
            motor_accepted_id=metadata['motor_accepted_id'],exposure_revision=ctrl.revision,
            model=self.timing_model(ctrl.lines,ctrl.revision))
        processor_class=self._processor_type();extra={}
        if self.cloud_spool is not None:
            extra['cloud_spool']=self.cloud_spool
        self.processor=processor_class(calibration=self.calibration,stripe_config=self.stripe_config,
            selection=self.selection,mode='continuous',exposure_lines=ctrl.lines,
            exposure_revision=ctrl.revision,axis=self.axis,volume=self.volume,
            timing_binder=binder,exposure_controller=ctrl,**extra)
        return True

    def _process_capture(self,capture):
        return self.processor.process(capture),{}

    def process(self, capture):
        started=time.monotonic()
        if self.processor is None: raise RuntimeError('motion must be explicitly bound before processing')
        metadata=capture.motion_metadata or {}
        for field in ('effect_known','measurement_eligible','transition','exposure_revision','exposure_lines'):
            if field not in metadata: raise ValueError('coordinator exposure metadata missing: '+field)
        result,source_extra=self._process_capture(capture)
        trace=getattr(self,'performance_trace',None)
        if trace is not None:trace.produced(capture,result)
        summary=result.summary()
        summary.update(source=self.source,width=capture.diagnostics.width,height=capture.diagnostics.height)
        self.coordinator.observe(result.quality.status,capture.request_id)
        exposure={k:metadata[k] for k in metadata if k.startswith('exposure_') or k in
            {'effect_known','measurement_eligible','transition','observation_only','effect_evidence_sha256','effect_evidence_reference'}}
        if not capture.profile_payload: raise ValueError('original SLP2 payload required for persistence')
        binary=getattr(self.writer,'supports_multibranch_binary',False) is True
        persisted_summary=summary if binary else {**summary,'multibranch_observations':result.persistence_payload()}
        payload=capture.profile_payload;extra={}
        if binary:
            from candidate.storage_r21 import encode_profile
            payload=encode_profile(result,summary=summary)
            extra=dict(source_profile_payload=capture.profile_payload,
                       source_pmc2_payload=getattr(capture,'pmc2_payload',b''))
            extra.update(source_extra)
        self.writer.submit(dict(request_id=capture.request_id,capture_epoch=capture.epoch,
            raw=capture.raw,raw_sha256=capture.raw_sha256,profile_payload=payload,
            points_camera_mm=result.delta_camera_mm,
            points_axis_mm=result.delta_axis_mm if result.fused else None,
            summary=persisted_summary,motion=metadata,exposure=exposure,**extra))
        self.last_request_id=capture.request_id
        if self.processed_observer is not None: self.processed_observer(capture,result)
        self.processing_seconds.append(time.monotonic()-started)
        return summary
