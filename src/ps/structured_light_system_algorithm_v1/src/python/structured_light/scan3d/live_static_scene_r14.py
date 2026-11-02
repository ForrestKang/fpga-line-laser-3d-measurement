"""Opt-in current-frame static observations rendered in the HDMI owner.

This transports computed camera XYZ with historical calibration declarations.
No axis, accumulated model, physical validation or motion is introduced here.
"""
import dataclasses
import time

import numpy as np

from structured_light.p2.shared_scene import LIVE_STATIC_TITLE,xyz_sha256
from .dashboard import DashboardStatus
from .processed_display import ProcessedObserver
from .scene_replay import SceneReplaySink


class LiveStaticSceneSink(SceneReplaySink):
    def __init__(self,remote):
        capability=getattr(remote,'scene_capability',{})
        if 'live_static' not in capability.get('supported_sources',()):
            raise ValueError('owner must explicitly advertise live_static scenes')
        super().__init__(remote)


class OwnerLiveStaticSceneObserver(ProcessedObserver):
    def __init__(self,**kwargs):
        if kwargs.get('source')!='live_static':raise ValueError('explicit live_static source required')
        if not isinstance(kwargs.get('sink'),LiveStaticSceneSink):raise ValueError('live static scene sink required')
        kwargs['renderer']=object()  # Native rendering belongs exclusively to owner.
        super().__init__(**kwargs)
        self._title=LIVE_STATIC_TITLE

    def _points(self,result,preview_angle):
        if result.fused or result.angle_rad is not None or result.angle_source!='unregistered':
            raise ValueError('live static scene requires unregistered current camera section')
        points,revision,snapshot_revision,kind,elapsed=super()._points(result,preview_angle)
        points=np.ascontiguousarray(points,dtype='<f8')
        points.setflags(write=False)
        return points,revision,snapshot_revision,kind,elapsed

    def _consume(self,item):
        capture,result,_,queued_at,_=item[:5]
        started=time.perf_counter()
        points,revision,snapshot_revision,kind,points_s=item[5]
        elapsed=max(started-self._started,1e-9)
        status=DashboardStatus(mode='STATIC',source='live_static',quality=result.quality.status,
            frame_number=capture.request_id,display_fps=self._counts['acknowledged']/elapsed)
        rejection=dict(getattr(result,'rejection_counts',{}))
        metadata=dict(source='live_static',title=self._title,request_id=capture.request_id,
            capture_epoch=capture.epoch,submission_sequence=item[8],cloud_revision=revision,
            preview_revision=None,preview_generation=None,raw_sha256=capture.raw_sha256,
            xyz_sha256=xyz_sha256(points),status=dataclasses.asdict(status),
            raw_shape=[480,752],canvas_shape=[720,1280,3],source_bounds=[0,0,752,480],
            destination_bounds=[24,128,752,480],point_source=kind,accumulated=False,fused=False,
            axis_calibrated=False,timing_calibrated=False,volume_calibrated=False,
            physical_accuracy_verified=False,current_optical_validation_passed=False,
            historical_calibration=True,preview_axis_fingerprint=None,angle_source='unregistered',
            rejection_counts=rejection)
        try:response=self.sink.show_scene(capture.raw,points,metadata)
        except BaseException as exc:
            partial=getattr(exc,'scene_receipt',None)
            if isinstance(partial,dict) and partial.get('rendered') is True:
                with self._condition:self._counts['rendered']+=1
            raise
        finished=time.perf_counter()
        if response.get('rendered') is not True or response.get('hdmi_acknowledged') is not True:
            raise RuntimeError('live static scene render/HDMI receipt unconfirmed')
        receipt=dict(metadata,rendered=True,acknowledged=True,hdmi_acknowledged=True,queued=False,
            points=len(points),snapshot_revision=snapshot_revision,
            display_state='observational_xyz' if len(points) else 'waiting_for_valid_xyz',
            quality=result.quality.status,original_valid=result.valid,original_fused=result.fused,
            queue_wait_s=max(0.,started-queued_at-points_s),points_s=points_s,snapshot_s=0.,stitch_s=0.,
            render_s=response['render_s'],show_s=response['show_s'],
            renderer_timings=response['renderer_timings'],sink_receipt=response,
            total_s=finished-started+points_s,submit_to_ack_s=finished-queued_at,
            completed_monotonic_s=finished,render_location='existing_hdmi_owner')
        with self._condition:
            self._counts['rendered']+=1;self._counts['acknowledged']+=1
            self._counts['hdmi_acknowledged']+=1;self._records.append(receipt)
            self._last_rendered=dict(receipt)
        return receipt

    def last_rendered_canvas(self):
        with self._condition:
            if not self._closed or self._worker_unconfirmed or self._active:
                raise RuntimeError('close and join processed display before retrieving canvas')
            if self._last_rendered is None:return None
            if self.sink.snapshot_error is not None:
                raise RuntimeError('owner scene snapshot failed: '+self.sink.snapshot_error)
            if self.sink.last_canvas is None:raise RuntimeError('acknowledged owner snapshot missing')
            snapshot=dict(self._last_rendered)
            snapshot['canvas']=self.sink.last_canvas.copy()
            snapshot['canvas'].setflags(write=False)
            snapshot['rejection_counts']=dict(snapshot['rejection_counts'])
            return snapshot
