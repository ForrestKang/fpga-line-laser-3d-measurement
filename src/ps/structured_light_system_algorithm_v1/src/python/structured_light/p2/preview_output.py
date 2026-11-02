"""Persistent HDMI frame stores, switched only after a bounded park ACK.

Uses the MM2S PARK_PTR current (bits 20:16) and desired (bits 4:0)
fields also used by the official PYNQ v3.1 video driver. CPU never modifies
the active store. All stores stay owned until VDMA halt is confirmed.
"""
import time
from .hdmi_output import BoundedHDMIOutput, WIDTH, HEIGHT, STRIDE, poll_until

_BLIT_READY=False
_CV2=None


def prepare_preview_backend():
    global _BLIT_READY,_CV2
    if not _BLIT_READY:
        try:
            import cv2
        except ImportError:
            _CV2=None
        else:
            cv2.setNumThreads(1)
            _CV2=cv2
        _BLIT_READY=True
    return 'opencv' if _CV2 is not None else 'numpy_out'


class PreviewHDMIOutput(BoundedHDMIOutput):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.buffers = []
        self.geometry = None
        self.current = None
        self.label_at = 0.
        self.label_pixels = None
        self.labels = None
        self.updates = 0
        self.blit_backend = prepare_preview_backend()

    def _status(self):
        status = int(self.dma.read(4))
        if status & 0xff0:
            raise RuntimeError(f'preview VDMA error: {status:#x}')
        return status

    def _wait_store(self, index):
        def acknowledged():
            status = self._status()
            active = (int(self.dma.read(0x28)) >> 16) & 31
            return not status & 1 and active == index and bool(status & 0x1000)
        poll_until(acknowledged, timeout_s=self.timeout_s, name='preview VDMA park acknowledge')

    def _initialize(self, width, height, rotation, mirror):
        import numpy as np
        from .live_preview import preview_canvas
        from .hdmi_output import bgr_to_hdmi_memory
        self._halt_dma()
        self._status()
        static = preview_canvas(bytes(width*height), width, height,
                                request_id=0, fps=0, rotation=rotation, mirror=mirror)
        packed = np.frombuffer(bgr_to_hdmi_memory(static), np.uint8).reshape(HEIGHT, WIDTH, 4)
        for _ in range(3):
            frame = self.allocator(shape=(HEIGHT, WIDTH, 4), dtype=np.uint8)
            self.buffers.append(frame)  # retain even if a subsequent setup fails
            self.buffer = self.buffers[0]  # ownership/quarantine sentinel
            frame[:] = packed
            frame.flush()
        self.dma.write(0x54, STRIDE)
        self.dma.write(0x58, STRIDE)
        for index, frame in enumerate(self.buffers):
            self.dma.write(0x5c+4*index, int(frame.physical_address))
        self.dma.write(0x28, int(self.dma.read(0x28)) & ~31)
        self.dma.write(4, 0x1000)
        self.dma.write(0, 0x11001)
        self.dma.write(0x50, HEIGHT)
        self.vtc.write(0, 0x03fdef06)
        self._wait_store(0)
        self.current = 0
        self.geometry = (width, height, rotation, mirror)
        self.started = True

    def show_preview(self, gray, width, height, *, request_id, fps, rotation=0, mirror=False):
        import math
        import numpy as np
        from PIL import Image, ImageDraw
        if self.quarantined:
            raise RuntimeError('preview buffers quarantined')
        if (width <= 0 or height <= 0 or len(gray) != width*height or
                rotation not in (0,90,180,270) or not math.isfinite(fps) or fps < 0):
            raise ValueError('invalid complete preview frame')
        display_width, display_height = (height,width) if rotation in (90,270) else (width,height)
        if display_width > 1200 or display_height > 570:
            raise ValueError('preview geometry exceeds native display area')
        t0 = time.monotonic()
        geometry = (width,height,rotation,mirror)
        if self.geometry is None:
            self._initialize(*geometry)
        elif geometry != self.geometry:
            raise ValueError('stop preview before changing display geometry')
        self._status()
        active = (int(self.dma.read(0x28)) >> 16) & 31
        desired = int(self.dma.read(0x28)) & 31
        if active != self.current or desired != self.current:
            raise RuntimeError('unexpected VDMA ownership/park change')
        target = (self.current+1) % 3
        frame = self.buffers[target]
        pixels = np.frombuffer(gray, np.uint8).reshape(height,width)
        if mirror: pixels = pixels[:,::-1]
        if rotation: pixels = np.rot90(pixels, -(rotation//90))
        dh,dw = pixels.shape
        x,y = (WIDTH-dw)//2,(HEIGHT-dh)//2
        # Gray is identical in G/B/R lanes; one 32-bit write per pixel.
        words = np.asarray(frame).view(np.uint32).reshape(HEIGHT,WIDTH)
        if _CV2 is not None:
            roi=np.asarray(frame)[y:y+dh,x:x+dw]
            converted=_CV2.cvtColor(pixels,_CV2.COLOR_GRAY2RGBA,dst=roi)
            roi[:,:,3]=0  # X padding follows the existing G,B,R,X contract.
            if converted.ctypes.data != roi.ctypes.data:
                raise RuntimeError('native gray conversion did not use the owned display store')
        else:
            np.multiply(pixels,0x010101,out=words[y:y+dh,x:x+dw],dtype=np.uint32)
        cx,cy = x+dw//2,y+dh//2
        words[cy,cx-12:cx+13] = 0xff30b0
        words[cy-12:cy+13,cx] = 0xff30b0
        # Only a small header is rendered, at most once per second.
        if self.label_pixels is None or time.monotonic()-self.label_at >= 1:
            image = Image.new('RGB',(WIDTH,64),'#101822')
            draw = ImageDraw.Draw(image)
            draw.text((24,15),f'LIVE CAMERA VIDEO | {width}x{height} | request {request_id} | {fps:.2f} fps',fill='white')
            draw.text((24,38),f'rotation={rotation} CW mirror={mirror} | native raw bytes unchanged | PREVIEW ONLY',fill='#ffd060')
            rgb = np.asarray(image)
            header = np.zeros((64,WIDTH,4),np.uint8)
            header[:,:,0],header[:,:,1],header[:,:,2] = rgb[:,:,1],rgb[:,:,2],rgb[:,:,0]
            self.label_pixels = header
            self.label_at = time.monotonic()
        frame[:64] = self.label_pixels
        # Contiguous whole rows preserve PynqBuffer's address/offset and let
        # cache maintenance cover the changed ROI plus the small header.
        frame[y:y+dh].flush()
        frame[:64].flush()
        filled = time.monotonic()
        # Acknowledge the new park index before the old store can be reused.
        self.dma.write(4,0x1000)
        self.dma.write(0x28,(int(self.dma.read(0x28)) & ~31)|target)
        self._wait_store(target)
        self.current = target
        self.updates += 1
        result = {'vdma_status':self._status(),'active_store':target,
                  'persistent_stores':3,'update_count':self.updates,
                  'fill_s':filled-t0,'switch_s':time.monotonic()-filled,
                  'gray_blit_backend':self.blit_backend,
                  'hdmi_memory_lane_order':'G,B,R,X'}
        self.snapshots.append(result)
        del self.snapshots[:-8]
        return result

    def show(self, raw_bgr):
        raise RuntimeError('persistent preview accepts only gray frames')

    def close(self):
        try:
            self._halt_dma()
        except Exception:
            self.quarantined = bool(self.buffers)
            raise
        self.vtc.write(0,self.vtc.read(0) & ~4)
        for frame in self.buffers:
            frame.freebuffer()
        self.buffers.clear()
        self.buffer = None
        self.geometry = self.current = None
        self.started = self.quarantined = False
