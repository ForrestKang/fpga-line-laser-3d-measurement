"""Saved-replay observer using the existing owner for every render/present."""
import dataclasses
import math
import time

from .dashboard import DashboardStatus
from .processed_display import ProcessedObserver


class SceneReplaySink:
    def __init__(self,remote):
        self.remote=remote;self.last_canvas=None;self.last_receipt=None
        self.snapshot_error=None;self.snapshot_s=0.;self.close_receipt=None

    def show_scene(self,raw,points,metadata):
        result=self.remote.show_scene(raw,points,metadata)
        self.last_receipt=result
        return result

    def close(self):
        if self.close_receipt is not None:return self.close_receipt
        started=time.perf_counter()
        try:
            if self.last_receipt is not None:
                self.last_canvas=self.remote.fetch_scene_canvas(self.last_receipt)
        except BaseException as exc:
            self.snapshot_error=type(exc).__name__+': '+str(exc)
        finally:self.snapshot_s=time.perf_counter()-started
        receipt=self.remote.close()
        if (not isinstance(receipt,dict) or receipt.get('closed_confirmed') is not True
                or receipt.get('owner_exited') is not True):
            raise RuntimeError('scene owner close/exit unconfirmed; retain runtime and lock')
        self.close_receipt=receipt
        return dict(self.close_receipt,final_snapshot_s=self.snapshot_s,
            final_snapshot_saved=self.last_canvas is not None,snapshot_error=self.snapshot_error)


class OwnerSceneReplayObserver(ProcessedObserver):
    def __init__(self,**kwargs):
        if kwargs.get('source')!='replay':raise ValueError('owner scene observer is replay-only')
        if not isinstance(kwargs.get('sink'),SceneReplaySink):raise ValueError('scene sink required')
        # No DashboardRenderer construction or per-frame drawing in foreground.
        kwargs['renderer']=object()
        super().__init__(**kwargs)

    def _consume(self,item):
        capture,result,preview_angle,queued_at,preview_angle_source=item[:5]
        started=time.perf_counter()
        points,revision,snapshot_revision,kind,points_s=item[5]
        elapsed=max(time.perf_counter()-self._started,1e-9)
        status=DashboardStatus(mode='REPLAY',source='replay',quality=result.quality.status,
            frame_number=capture.request_id,display_fps=self._counts['acknowledged']/elapsed,
            angle_deg=math.degrees(preview_angle) if preview_angle is not None else
                math.degrees(result.angle_rad) if result.angle_rad is not None else None)
        metadata=dict(source='replay',request_id=capture.request_id,capture_epoch=capture.epoch,
            submission_sequence=item[8],cloud_revision=revision,preview_revision=item[6],
            preview_generation=item[7],raw_sha256=capture.raw_sha256,status=dataclasses.asdict(status))
        try:response=self.sink.show_scene(capture.raw,points,metadata)
        except BaseException as exc:
            partial=getattr(exc,'scene_receipt',None)
            if isinstance(partial,dict) and partial.get('rendered') is True:
                with self._condition:self._counts['rendered']+=1
            raise
        finished=time.perf_counter()
        if response.get('rendered') is not True or response.get('hdmi_acknowledged') is not True:
            raise RuntimeError('scene render/HDMI receipt unconfirmed')
        receipt=dict(source=self.source,title=self._title,request_id=capture.request_id,
            capture_epoch=capture.epoch,rendered=True,submission_sequence=item[8],cloud_revision=revision,
            acknowledged=True,hdmi_acknowledged=True,queued=False,point_source=kind,points=len(points),
            snapshot_revision=snapshot_revision,preview_revision=item[6],preview_generation=item[7],
            angle_source=preview_angle_source if preview_angle is not None else result.angle_source,
            preview_axis_fingerprint=self.preview_axis.fingerprint if self.preview_axis else None,
            axis_calibrated=False,physical_accuracy_verified=False,original_valid=result.valid,
            original_fused=result.fused,queue_wait_s=max(0.,started-queued_at-points_s),
            points_s=points_s,snapshot_s=0.,stitch_s=points_s,
            render_s=response['render_s'],show_s=response['show_s'],
            renderer_timings=response['renderer_timings'],sink_receipt=response,
            total_s=finished-started+points_s,submit_to_ack_s=finished-queued_at,
            completed_monotonic_s=finished,render_location='existing_hdmi_owner')
        with self._condition:
            self._counts['rendered']+=1;self._counts['acknowledged']+=1
            self._counts['hdmi_acknowledged']+=1;self._records.append(receipt)
        return receipt
