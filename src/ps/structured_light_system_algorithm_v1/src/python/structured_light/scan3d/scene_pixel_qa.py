"""Independent saved-replay prefix pixel QA; never in production render path."""
import dataclasses
import hashlib
import json

import cv2
import numpy as np

from .dashboard import DashboardRenderer,DashboardStatus,CAMERA_IMAGE
from .pipeline import ScanCalibration,DEFAULT_STRIPE_CONFIG
from .ridge_replay_bench import prepare_ridge_manifest_replay,RidgeReplayProcessor
from .processed_display import ProcessedObserver,REPLAY_BANNER
from .scene_renderer import SceneRenderer
from .turntable import TurntableModel


def compare_scene_pixels(*,dataset,camera,plane,axis,native_manifest,repeats):
    class PixelOnlyOutput:
        def show_canvas(self,canvas):
            return dict(hdmi_acknowledged=True,mock_cpu_pixel_qa=True)
    data=prepare_ridge_manifest_replay(dataset,DEFAULT_STRIPE_CONFIG)
    calibration=ScanCalibration.load(camera,plane)
    model=TurntableModel.from_json(json.loads(axis.read_text()))
    parent=DashboardRenderer(point_raster_manifest=native_manifest)
    owner=SceneRenderer(PixelOnlyOutput(),native_manifest)
    observer=ProcessedObserver(source='replay',preview_axis=model,renderer=parent,asynchronous=False)
    records=[]
    try:
        for cycle in range(repeats):
            processor=RidgeReplayProcessor(variant='ridge_primary_v4_batch',calibration=calibration,stripe_config=data.stripe_config)
            for entry in data.entries:
                result=processor.process(entry.capture(),feature_config_fingerprint=data.stripe_config.fingerprint)
                points,revision,*_=observer._points(result,entry.angle_rad)
                status=DashboardStatus(mode='REPLAY',source='replay',quality=result.quality.status,
                    frame_number=entry.request_id,display_fps=0.,angle_deg=float(np.degrees(entry.angle_rad)))
                metadata=dict(source='replay',request_id=entry.request_id,capture_epoch=entry.epoch,
                    submission_sequence=len(records)+1,cloud_revision=revision,
                    preview_revision=observer._preview_revision,preview_generation=observer._preview_generation,
                    raw_sha256=entry.raw_sha256,status=dataclasses.asdict(status))
                # Match JSON transport's tuple/list conversion as well as pixels.
                metadata=json.loads(json.dumps(metadata,allow_nan=False))
                expected=parent.render(entry.raw,points,status=status,cloud_revision=revision)
                cv2.rectangle(expected,(0,0),(1279,25),(15,20,45),-1)
                cv2.putText(expected,REPLAY_BANNER,(20,19),cv2.FONT_HERSHEY_SIMPLEX,.62,(80,210,255),1,cv2.LINE_AA)
                receipt=owner.present(entry.raw,points,metadata)
                actual,_=owner.snapshot()
                x,y,w,h=CAMERA_IMAGE
                raw=np.frombuffer(entry.raw,np.uint8).reshape(480,752)
                if (actual.tobytes()!=expected.tobytes()
                        or not np.array_equal(actual[y:y+h,x:x+w],np.repeat(raw[:,:,None],3,axis=2))):
                    raise RuntimeError('scene full-canvas pixel mismatch at '+str((cycle,entry.request_id)))
                records.append(dict(cycle=cycle,request_id=entry.request_id,cloud_points=len(points),
                    canvas_sha256=hashlib.sha256(actual.tobytes()).hexdigest(),
                    full_canvas_pixels_equal=True,native_camera_equal=True,
                    actual_backend=receipt['renderer_timings']['point_raster_backend']))
        return dict(passed=True,frames=len(records),unique_real_frames=len(data.entries),
            repeats=repeats,final_preview_points=observer._preview_count,records=records,
            boundary='CPU pixel QA only, mock presentation ACK; every cumulative prefix including repeated cycles, matching production accumulation. Fixed equal FPS fields. Excluded from timed variants; no actual HDMI/capture/metrology claim.')
    finally:
        observer.close();parent.point_raster.close();owner.close()
