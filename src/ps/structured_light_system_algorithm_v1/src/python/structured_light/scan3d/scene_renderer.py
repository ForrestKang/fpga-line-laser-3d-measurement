"""Dashboard rendering in the existing HDMI allocation owner; explicitly admitted replay or static observation."""
import hashlib
from pathlib import Path
import time

import cv2

from .dashboard import DashboardRenderer,DashboardStatus
from .processed_display import REPLAY_BANNER
from structured_light.p2.shared_scene import check_metadata,check_sources,LIVE_STATIC_TITLE


def check_scene_capability(capability,manifest,*,supported_sources=('replay',)):
    supported_sources=check_sources(supported_sources)
    from . import dashboard
    from .point_raster import file_sha256,runtime_identity
    font=DashboardRenderer._resolve_font(None)
    if (not isinstance(capability,dict) or type(capability.get('abi')) is not int or capability['abi']!=1
            or capability.get('source')!='replay'
            or not isinstance(capability.get('supported_sources',('replay',)),(tuple,list))
            or tuple(capability.get('supported_sources',('replay',)))!=supported_sources
            or capability.get('renderer_source_sha256')!=file_sha256(dashboard.__file__)
            or capability.get('font_sha256')!=(file_sha256(font) if font else None)):
        raise ValueError('owner scene renderer/font/source capability mismatch')
    native=capability.get('native',{})
    if (not isinstance(native,dict) or native.get('manifest_sha256')!=file_sha256(manifest)
            or native.get('runtime')!=runtime_identity()):
        raise ValueError('owner scene native/runtime capability mismatch')


class SceneRenderError(RuntimeError):
    def __init__(self,message,receipt):
        super().__init__(message);self.receipt=receipt


class SceneRenderer:
    def __init__(self,output,point_raster_manifest,*,supported_sources=('replay',)):
        self.supported_sources=check_sources(supported_sources)
        if point_raster_manifest is None:raise ValueError('explicit scene native manifest required')
        cv2.setNumThreads(1)
        self.output=output
        self.renderer=DashboardRenderer(point_raster_manifest=point_raster_manifest)
        # Font/native initialization belongs to the owner's startup budget.
        for size in (8,9,10,11,12,13,18):self.renderer._font(size)
        self.renderer.render(bytes(752*480),None,status=DashboardStatus(mode='REPLAY',source='replay'))
        self.last_canvas=None;self.last_metadata=None;self.last_receipt=None

    def capability(self):
        from . import dashboard
        font=self.renderer._resolved_font
        return dict(abi=1,source='replay',supported_sources=list(self.supported_sources),renderer_source_sha256=
            hashlib.sha256(Path(dashboard.__file__).read_bytes()).hexdigest(),
            font_sha256=hashlib.sha256(font.read_bytes()).hexdigest() if font else None,
            native=self.renderer.point_raster.identity)

    def present(self,raw,points,metadata):
        check_metadata(metadata,self.supported_sources)
        started=time.monotonic();cpu=time.process_time()
        status_fields=dict(metadata['status'])
        if isinstance(status_fields.get('debug_details'),list):
            status_fields['debug_details']=tuple(status_fields['debug_details'])
        status=DashboardStatus(**status_fields)
        receipt=dict(rendered=False,hdmi_acknowledged=False)
        try:
            canvas=self.renderer.render(raw,points,status=status,cloud_revision=tuple(metadata['cloud_revision']))
            timings=dict(self.renderer.timings)
            cv2.rectangle(canvas,(0,0),(1279,25),(15,20,45),-1)
            cv2.putText(canvas,LIVE_STATIC_TITLE if metadata['source']=='live_static' else REPLAY_BANNER,(20,19),cv2.FONT_HERSHEY_SIMPLEX,.62,(80,210,255),1,cv2.LINE_AA)
            if metadata['source']=='live_static':
                detail=(f"LIVE CAMERA / CURRENT FRAME / REQUEST {metadata['request_id']}"
                    f" / EPOCH {metadata['capture_epoch']} / XYZ {len(points)}")
                if not len(points):detail+=' / WAITING FOR VALID XYZ'
                detail+=f' / Q {status.quality.upper()[:12]}'
                rejection=metadata['rejection_counts']
                if rejection:
                    reason=max(rejection,key=rejection.get)
                    text=reason.encode('ascii',errors='replace').decode('ascii')[:28]
                    detail+=f' / {text}:{rejection[reason]}'
                cv2.rectangle(canvas,(0,70),(1279,91),(15,20,45),-1)
                width=cv2.getTextSize(detail,cv2.FONT_HERSHEY_SIMPLEX,.47,1)[0][0]
                cv2.putText(canvas,detail,(20,86),cv2.FONT_HERSHEY_SIMPLEX,
                    .47*min(1.,1240/max(width,1)),(80,210,255),1,cv2.LINE_AA)
            rendered=time.monotonic();render_cpu=time.process_time()-cpu
            receipt.update(rendered=True,render_s=rendered-started,renderer_timings=timings,
                scene_render_cpu_s=render_cpu)
            present_cpu=time.process_time()
            result=self.output.show_canvas(canvas)
            finished=time.monotonic()
            if not isinstance(result,dict) or result.get('hdmi_acknowledged') is not True:
                raise RuntimeError('owner scene output did not confirm HDMI ACK')
            receipt.update(hdmi_acknowledged=True,show_s=finished-rendered,
                scene_present_cpu_s=time.process_time()-present_cpu,sink_receipt=result)
            receipt=dict(result,**receipt)
            self.last_canvas=canvas;self.last_metadata=dict(metadata);self.last_receipt=dict(receipt)
            return receipt
        except BaseException as exc:
            raise SceneRenderError(type(exc).__name__+': '+str(exc),receipt) from exc

    def snapshot(self):
        if self.last_canvas is None:raise RuntimeError('no acknowledged scene canvas')
        return self.last_canvas,dict(self.last_metadata)

    def close(self):
        self.renderer.point_raster.close()
