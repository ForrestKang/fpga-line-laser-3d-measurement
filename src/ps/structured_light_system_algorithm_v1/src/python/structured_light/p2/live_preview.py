"""Continuous gray preview without per-frame reference/export work.

This uses successive complete-frame requests, not a new streaming RTL mode.
The real transport still drains both streams and validates PL completion.
"""
from collections import deque
import math
import time


def preview_canvas(pixels, width, height, *, request_id, fps, rotation=0, mirror=False):
    import numpy as np
    from PIL import Image, ImageDraw
    if width <= 0 or height <= 0 or len(pixels) != width * height:
        raise ValueError('preview needs one complete gray8 frame')
    if rotation not in (0, 90, 180, 270) or not math.isfinite(fps) or fps < 0:
        raise ValueError('invalid preview orientation or rate')
    gray = Image.frombytes('L', (width, height), pixels)
    if mirror:
        gray = gray.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if rotation:
        gray = gray.rotate(-rotation, expand=True)  # clockwise, display only
    scale = min(1., 1200 / gray.width, 570 / gray.height)
    if scale < 1:
        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)))
    image = Image.new('RGB', (1280, 720), '#101822')
    x, y = (1280-gray.width)//2, (720-gray.height)//2
    image.paste(gray.convert('RGB'), (x, y))
    draw = ImageDraw.Draw(image)
    draw.text((24, 15), f'LIVE CAMERA PREVIEW | {width}x{height} | request {request_id} | capture/update {fps:.2f} fps', fill='white')
    draw.text((24, 38), f'Display rotation={rotation} deg CW, mirror={mirror} | raw bytes unchanged | NO LASER MEASUREMENT', fill='#ffd060')
    draw.rectangle((x-1, y-1, x+gray.width, y+gray.height), outline='#00d080')
    draw.text((x+gray.width//2-36, y-19), 'IMAGE TOP', fill='#00ff90')
    draw.text((x+gray.width//2-44, y+gray.height+8), 'IMAGE BOTTOM', fill='#00ff90')
    draw.text((max(4,x-80),y+gray.height//2),'LEFT',fill='#00ff90')
    draw.text((x+gray.width+8,y+gray.height//2),'RIGHT',fill='#00ff90')
    # Centre reticle helps aiming. It is display decoration, never raw data.
    cx, cy = x+gray.width//2, y+gray.height//2
    draw.line((cx-12,cy,cx+12,cy),fill='#ffb030')
    draw.line((cx,cy-12,cx,cy+12),fill='#ffb030')
    draw.text((24, 676),'Move one object: observe LEFT/RIGHT and TOP/BOTTOM. Stop preview before measurement capture.',fill='white')
    return np.asarray(image)[:,:,::-1].tobytes()


class LivePreview:
    """One foreground hardware owner, bounded history, no hidden retries."""
    def __init__(self, controller, display, *, rotation=0, mirror=False, clock=time.monotonic):
        if rotation not in (0,90,180,270):
            raise ValueError('rotation must be 0/90/180/270')
        self.controller, self.display = controller, display
        self.rotation, self.mirror, self.clock = rotation, mirror, clock
        self.started = clock()
        self.times = deque(maxlen=31)
        self.count, self.latest, self.closed = 0, None, False
        self.pending = None

    def _capture(self):
        start = self.clock()
        frame = self.controller.capture_raw()
        return (frame,self.controller.transport.mmio.request_id,self.clock()-start)

    def _submit(self,captured):
        frame,request_id,capture_s=captured
        before=self.clock()
        self.display.submit_preview(frame.pixels,frame.width,frame.height,
                                    request_id=request_id,fps=self.rate(),
                                    rotation=self.rotation,mirror=self.mirror)
        submitted=self.clock()
        self.pending=(*captured,submitted,submitted-before)

    def rate(self):
        elapsed=self.times[-1]-self.times[0] if len(self.times)>1 else 0
        return (len(self.times)-1)/elapsed if elapsed>0 else 0.

    def step(self):
        if self.closed:
            raise RuntimeError('preview is closed')
        if hasattr(self.display,'submit_preview') and hasattr(self.display,'finish_preview'):
            # One queued HDMI update at most. Camera remains owned exclusively
            # by this foreground thread while the display process renders.
            if self.pending is None:
                self._submit(self._capture())
            following=self._capture()
            frame,request_id,capture_s,submitted,queue_s=self.pending
            snapshot=self.display.finish_preview()
            acknowledged=self.clock()
            self.pending=None
            self.times.append(acknowledged)
            self.count+=1;self.latest=frame
            self._submit(following)
            return {'frames':self.count,'request_id':request_id,'width':frame.width,'height':frame.height,
                    'raw_sha256':frame.sha256,'fps':self.rate(),'capture_s':capture_s,
                    'queue_s':queue_s,
                    'update_s':acknowledged-submitted,'hdmi':snapshot,'rotation':self.rotation,
                    'mirror':self.mirror,'preview_only':True,'no_hidden_retry':True,
                    'capture_display_overlap':True,'queued_display_frames':1}
        start = self.clock()
        frame = self.controller.capture_raw()
        request_id = self.controller.transport.mmio.request_id
        completed = self.clock()
        self.times.append(completed)
        elapsed = self.times[-1]-self.times[0] if len(self.times)>1 else 0
        fps = (len(self.times)-1)/elapsed if elapsed>0 else 0.
        snapshot = self.display.show_preview(frame.pixels, frame.width, frame.height,
                          request_id=request_id, fps=fps, rotation=self.rotation, mirror=self.mirror)
        self.count += 1
        self.latest = frame
        return {'frames':self.count,'request_id':request_id,'width':frame.width,'height':frame.height,
                'raw_sha256':frame.sha256,'fps':fps,'capture_s':completed-start,
                'update_s':self.clock()-completed,'hdmi':snapshot,'rotation':self.rotation,
                'mirror':self.mirror,'preview_only':True,'no_hidden_retry':True}

    def close(self):
        if self.closed:return
        if self.pending is not None:
            self.display.finish_preview()
            self.pending=None
        self.display.close()  # no free before actual VDMA halt
        self.controller.close()
        recorder = getattr(self.controller,'recorder',None)
        if recorder is not None: recorder.close()
        self.closed=True
