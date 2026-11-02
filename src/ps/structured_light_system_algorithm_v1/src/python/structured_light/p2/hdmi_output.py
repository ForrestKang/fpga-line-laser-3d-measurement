"""Bounded 720p output driver; no PYNQ native unbounded clock/DMA waits.

Clock register encoding follows pinned PYNQ v3.1 video_display.c/h.
VTC programming follows AMD embeddedsw xilinx_v2025.2 xvtc.c/h.

The evidence canvas and all exported image bytes use canonical BGR order.
The combined overlay has ``C_S_AXIS_VIDEO_FORMAT=2`` (RGB): the AXI4-Stream
pixel is encoded as ``{R, B, G}`` (bits 23:16, 15:8, 7:0).  The PYNQ-Z2
Digilent ``rgb2dvi`` IP then maps ``vid_pData[23:16]`` to red,
``vid_pData[7:0]`` to green and ``vid_pData[15:8]`` to blue.  Since the
existing RTL removes only the pad byte and otherwise preserves the low 24
bits, the DDR lane contract for this combined output is ``G, B, R, X``.
``bgr_to_bgrx`` remains the canonical BGRX helper for compatibility;
``bgr_to_hdmi_memory`` applies this display-only lane swizzle.

Frames remain allocated until MM2S halt or reset has been observed.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import time

WIDTH, HEIGHT, STRIDE = 1280, 720, 1280 * 4
PIXEL_HZ = 100000000 * 52 / 7 / 2 / 5
REFRESH_HZ = PIXEL_HZ / 1650 / 750
# Official integer search chooses DIVCLK=7, FBMULT=52, CLKOUT0_DIV=2;
# BUFR_DIVIDE=5 supplies the pixel clock from the serial clock.
CLOCK_REGISTERS = {0x08: 0x41, 0x0c: 0x69a, 0x10: 0,
                   0x14: 0x20c4, 0x18: 0xcfafa401, 0x1c: 0xa300ff}
VTC_REGISTERS = {
    0x60: (720 << 16) | 1280, 0x68: 0, 0x6c: 0x7f,
    0x70: 1650, 0x74: (750 << 16) | 750,
    0x78: (1430 << 16) | 1390,
    0x7c: (1280 << 16) | 1280,
    # AMD VTC uses vertical sync start=active+front-1, end=start+width.
    0x80: (729 << 16) | 724, 0x84: (1390 << 16) | 1390,
    0x88: (1280 << 16) | 1280, 0x8c: (729 << 16) | 724,
    0x90: (1390 << 16) | 1390, 0x94: 720 << 16,
}


# PYNQ ip_dict keys vary with the Vivado handoff: the current combined HWH
# exposes the flattened ``hdmi_out_*`` INSTANCE names, while an older raw-HDMI
# handoff used the explicit ``hdmi_out/frontend/*`` hierarchy.  Keep this a
# finite allow-list and still require the exact physical address below.
HDMI_IP_ALIASES = {
    "hdmi_vdma": (
        "hdmi_vdma",
        "/hdmi_vdma",
    ),
    "hdmi_vtc": (
        "hdmi_out_vtc_out",
        "/hdmi_out/vtc_out",
        "hdmi_out/vtc_out",
        "hdmi_out/frontend/vtc_out",
        "/hdmi_out/frontend/vtc_out",
    ),
    "hdmi_dynclk": (
        "hdmi_out_axi_dynclk",
        "/hdmi_out/axi_dynclk",
        "hdmi_out/axi_dynclk",
        "hdmi_out/frontend/axi_dynclk",
        "/hdmi_out/frontend/axi_dynclk",
    ),
}


def _ip_dict_module(ip_dict, aliases):
    """Return one explicitly named PYNQ IP entry and its key."""

    keys = []
    for alias in aliases:
        normalized = str(alias).strip("/")
        keys.extend((alias, normalized, "/" + normalized, normalized.replace("/", "_")))
    seen = set()
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        module = ip_dict.get(key)
        if isinstance(module, dict):
            return module, key
    return None, None


def _module_phys_addr(module):
    value = module.get("phys_addr")
    if value is None:
        return None
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None


def poll_until(predicate, *, timeout_s=.5, name='hardware operation'):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.001)
    raise TimeoutError(name + ' deadline')


def bgr_to_bgrx(raw: bytes) -> bytes:
    """Pack canonical BGR canvas bytes as canonical little-endian BGRX.

    This helper is retained for callers that need a conventional BGRX
    buffer.  The combined PYNQ-Z2 HDMI path uses
    :func:`bgr_to_hdmi_memory`, because its direct RGB video path has the
    ``G,B,R,X`` memory lane contract documented in the module docstring.
    """
    import numpy as np
    if len(raw) != WIDTH * HEIGHT * 3:
        raise ValueError('HDMI canvas must contain exactly 1280x720 BGR bytes')
    frame = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    frame[:, :, :3] = np.frombuffer(raw, np.uint8).reshape(HEIGHT, WIDTH, 3)
    return frame.tobytes()


def bgr_to_hdmi_memory(raw: bytes) -> bytes:
    """Pack a canonical BGR canvas for the combined HDMI overlay.

    The returned bytes are little-endian ``G,B,R,X`` per pixel.  This makes
    the 24-bit AXI4-Stream word ``{R,B,G}``, which is the format-2 encoding
    expected by ``v_axi4s_vid_out`` and the channel mapping used by the
    Digilent ``rgb2dvi`` IP.  The input and all evidence files remain BGR.
    """
    import numpy as np
    if len(raw) != WIDTH * HEIGHT * 3:
        raise ValueError('HDMI canvas must contain exactly 1280x720 BGR bytes')
    bgr = np.frombuffer(raw, np.uint8).reshape(HEIGHT, WIDTH, 3)
    frame = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    frame[:, :, 0] = bgr[:, :, 1]  # low byte: green
    frame[:, :, 1] = bgr[:, :, 0]  # middle byte: blue
    frame[:, :, 2] = bgr[:, :, 2]  # high byte: red
    return frame.tobytes()


def colorbar_bgr() -> bytes:
    """Synthetic output test only; never labeled as a camera capture."""
    from PIL import Image, ImageDraw
    import numpy as np
    image = Image.new('RGB', (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    colors = ['white', 'yellow', 'cyan', 'green', 'magenta', 'red', 'blue', 'black']
    for i, color in enumerate(colors):
        draw.rectangle((i*160, 0, (i+1)*160-1, HEIGHT-1), fill=color)
    draw.rectangle((0, 0, WIDTH-1, 60), fill='black')
    draw.text((24, 24), 'HDMI OUTPUT TEST PATTERN | NOT CAMERA DATA', fill='white')
    return np.asarray(image)[:, :, ::-1].tobytes()


class BoundedHDMIOutput:
    def __init__(self, clock, vtc, dma, allocator, *, timeout_s=.5):
        self.clock, self.vtc, self.dma, self.allocator = clock, vtc, dma, allocator
        self.timeout_s, self.buffer = timeout_s, None
        self.started = False
        self.quarantined = False
        self.snapshots = []

    def configure(self):
        if self.quarantined:
            raise RuntimeError('VDMA buffer quarantined; reconfiguration required')
        self.clock.write(0, 0)
        poll_until(lambda: not self.clock.read(4) & 1, timeout_s=self.timeout_s,
                   name='video clock stop')
        for offset, value in CLOCK_REGISTERS.items():
            self.clock.write(offset, value)
        self.clock.write(0, 1)
        poll_until(lambda: self.clock.read(4) & 1, timeout_s=self.timeout_s,
                   name='video clock lock')
        self.vtc.write(0, 0x03fdef02)  # sources=generator; register update
        self.vtc.write(0x0c, 0)  # no IRQ required by this bounded static display
        for offset, value in VTC_REGISTERS.items():
            self.vtc.write(offset, value)

    def _halt_dma(self):
        self.dma.write(0, self.dma.read(0) & ~1)
        try:
            poll_until(lambda: self.dma.read(4) & 1, timeout_s=self.timeout_s,
                       name='VDMA MM2S halt')
        except TimeoutError:
            self.dma.write(0, 4)
            poll_until(lambda: not self.dma.read(0) & 4 and self.dma.read(4) & 1,
                       timeout_s=self.timeout_s, name='VDMA MM2S reset')

    def show(self, raw_bgr, *, evidence: bool = True):
        if self.quarantined:
            raise RuntimeError('VDMA buffer quarantined; new display refused')
        import numpy as np
        import cv2
        if type(evidence) is not bool:raise ValueError('evidence must be explicit bool')
        if isinstance(raw_bgr,np.ndarray):
            if raw_bgr.shape!=(HEIGHT,WIDTH,3) or raw_bgr.dtype!=np.uint8 or not raw_bgr.flags.c_contiguous:
                raise ValueError('HDMI array must be contiguous 1280x720 BGR uint8')
            bgr=raw_bgr
        else:
            if len(raw_bgr)!=WIDTH*HEIGHT*3:raise ValueError('HDMI canvas must contain exactly 1280x720 BGR bytes')
            bgr=np.frombuffer(raw_bgr,np.uint8).reshape(HEIGHT,WIDTH,3)
        # The canvas remains canonical BGR; only the hardware buffer follows
        # the combined overlay's G,B,R,X lane contract.
        self._halt_dma()
        if self.buffer is not None:
            self.buffer.freebuffer()
            self.buffer = None
        frame = self.allocator(shape=(HEIGHT, WIDTH, 4), dtype=np.uint8)
        self.buffer = frame  # retain ownership even if subsequent setup fails
        # One C loop writes the final hardware lanes directly into owned CMA
        # memory. No intermediate multi-megabyte BGRX byte string is needed.
        frame[:,:,3]=0
        cv2.mixChannels([bgr],[np.asarray(frame)],[0,1,1,0,2,2])
        frame.flush()
        status = int(self.dma.read(4))
        if status & 0xff0:
            raise RuntimeError(f'VDMA pre-existing error, status={status:#x}')
        self.dma.write(0x54, STRIDE)
        self.dma.write(0x58, STRIDE)
        for i in range(3):
            self.dma.write(0x5c + i*4, int(frame.physical_address))
        self.dma.write(0x28, int(self.dma.read(0x28)) & ~0x1f)  # park frame 0
        self.dma.write(4, 0x1000)  # acknowledge only frame-count event
        self.dma.write(0, 0x00011001)  # run, park, frame-count threshold=1
        self.dma.write(0x50, HEIGHT)  # VSIZE commits the transfer
        self.vtc.write(0, 0x03fdef06)  # generator enabled, update enabled
        poll_until(lambda: not self.dma.read(4) & 1, timeout_s=self.timeout_s,
                   name='VDMA MM2S running')
        poll_until(lambda: bool(self.dma.read(4) & 0x1000) or bool(self.dma.read(4) & 0xff0),
                   timeout_s=self.timeout_s, name='VDMA first displayed frame event')
        status = int(self.dma.read(4))
        canvas_sha=hashlib.sha256(memoryview(bgr)).hexdigest() if evidence else None
        memory_sha=hashlib.sha256(memoryview(np.asarray(frame))).hexdigest() if evidence else None
        snapshot = {'vdma_status': status, 'vtc_status': int(self.vtc.read(4)),
                    'vtc_timing_status': int(self.vtc.read(0x64)),
                    'clock_running': int(self.clock.read(4)),
                    'canvas_sha256': canvas_sha,
                    'evidence_hashes_computed':evidence,
                    # Keep the historical key for consumers of earlier
                    # snapshots while explicitly recording its actual lane
                    # order and a descriptive alias.
                    'bgrx_sha256': memory_sha,
                    'hdmi_memory_sha256': memory_sha,
                    'hdmi_memory_lane_order': 'G,B,R,X'}
        self.snapshots.append(snapshot)
        if status & 0xff0:
            raise RuntimeError(f'VDMA output error, status={status:#x}')
        self.started = True
        return snapshot

    def close(self):
        try:
            self._halt_dma()
        except Exception:
            self.quarantined = self.buffer is not None
            raise
        self.vtc.write(0, self.vtc.read(0) & ~4)
        self.started = False
        if self.buffer is not None:
            self.buffer.freebuffer()
            self.buffer = None
        self.quarantined = False


def create_output(overlay, manifest_path, *, background_owner=False, owner_log=None, preview_only=False):
    if background_owner:
        from .hdmi_owner import RemoteHDMIOutput
        return RemoteHDMIOutput(manifest_path, owner_log=owner_log,preview_only=preview_only)
    from pynq import MMIO, allocate
    manifest = json.loads(Path(manifest_path).read_text())
    mode = manifest.get('overlay_mode', manifest.get('overlay'))
    if mode not in {'camera_raw_hdmi', 'structured_light_profile_hdmi'}:
        raise ValueError('camera/HDMI combined manifest required')
    addresses = manifest['addresses']
    expected = {'hdmi_vdma': 0x40420000 if mode == 'structured_light_profile_hdmi' else 0x40410000, 'hdmi_vtc': 0x43c20000, 'hdmi_dynclk': 0x43c30000}
    # Addresses are checked against HWH module metadata before MMIO use.
    for name, value in expected.items():
        declared = addresses.get(name)
        if isinstance(declared, dict): declared = declared.get('base', declared.get('address'))
        actual = int(declared, 0) if isinstance(declared, str) else declared
        if actual != value: raise ValueError(f'HDMI address mismatch: {name}')
        module, instance = _ip_dict_module(overlay.ip_dict, HDMI_IP_ALIASES[name])
        if module is None or _module_phys_addr(module) != value:
            raise ValueError(f'HWH lacks {name} at its exact module address')
    return BoundedHDMIOutput(MMIO(expected['hdmi_dynclk'], 0x10000),
                             MMIO(expected['hdmi_vtc'], 0x10000),
                             MMIO(expected['hdmi_vdma'], 0x10000), allocate)
