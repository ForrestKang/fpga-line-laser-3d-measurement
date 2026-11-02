"""HDMI side of the live scan: runs inside the separate display owner process.

``ScanHDMIOutput`` keeps the three persistent frame stores and park-pointer
flip of :class:`PreviewHDMIOutput` (the path already used on the board by the
rotating preview), but fills the inactive store with a LiveScene canvas.
Messages arrive through the existing owner ``preview`` RPC as
``show_preview(gray, width, height, **metadata)`` with ``metadata["kind"]``:

``setup``    load calibration + turntable, reply with their hashes (gray is 1x1)
``capture``  add one 752x480 frame at a step count, then redraw
``view``     redraw the current scene at a new view angle (gray is 1x1)
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

import numpy as np

from ..p2.hdmi_output import HEIGHT, WIDTH
from ..p2.preview_output import PreviewHDMIOutput
from .pipeline import FRAME_HEIGHT, FRAME_WIDTH


def pack_hdmi_lanes(canvas_bgr: np.ndarray, store: np.ndarray) -> None:
    """Write a (720, 1280, 3) BGR canvas into a G,B,R,X frame store in place."""

    if canvas_bgr.shape != (HEIGHT, WIDTH, 3) or store.shape != (HEIGHT, WIDTH, 4):
        raise ValueError("canvas and frame store must be 1280x720")
    target = np.asarray(store)
    target[:, :, 0] = canvas_bgr[:, :, 1]
    target[:, :, 1] = canvas_bgr[:, :, 0]
    target[:, :, 2] = canvas_bgr[:, :, 2]
    target[:, :, 3] = 0


class ScanHDMIOutput(PreviewHDMIOutput):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.scene = None

    # -- message handlers -----------------------------------------------------------------------
    def _setup(self, metadata: dict[str, Any]) -> dict[str, Any]:
        from .live import LiveScene
        from .pipeline import ScanCalibration
        from .turntable import TurntableModel

        calibration = ScanCalibration.load(metadata["camera"], metadata["plane"])
        turntable = TurntableModel.from_json(metadata["turntable"])
        self.scene = LiveScene(calibration, turntable, elevation_deg=float(metadata.get("elevation_deg", 25.0)),
                               title=str(metadata.get("title", "SCAN3D LIVE")))
        return {"kind": "setup", "rendered": False, "camera_sha256": calibration.camera_sha256,
                "plane_sha256": calibration.plane_sha256, "turntable_fingerprint": turntable.fingerprint}

    def _present(self, canvas: np.ndarray) -> dict[str, Any]:
        if self.quarantined:
            raise RuntimeError("preview buffers quarantined")
        if self.geometry is None:
            self._initialize(FRAME_WIDTH, FRAME_HEIGHT, 0, False)
        self._status()
        active = (int(self.dma.read(0x28)) >> 16) & 31
        desired = int(self.dma.read(0x28)) & 31
        if active != self.current or desired != self.current:
            raise RuntimeError("unexpected HDMI active-store ownership")
        target = (self.current + 1) % 3
        frame = self.buffers[target]
        started = time.monotonic()
        pack_hdmi_lanes(canvas, frame)
        frame.flush()
        filled = time.monotonic()
        self.dma.write(4, 0x1000)
        self.dma.write(0x28, (int(self.dma.read(0x28)) & ~31) | target)
        self._wait_store(target)
        self.current = target
        self.updates += 1
        return {"active_store": target, "update_count": self.updates, "fill_s": filled - started,
                "switch_s": time.monotonic() - filled, "vdma_status": self._status(),
                "hdmi_memory_lane_order": "G,B,R,X"}

    def show_preview(self, gray, width, height, **metadata):  # type: ignore[override]
        kind = metadata.get("kind")
        if kind == "setup":
            return self._setup(metadata)
        if self.scene is None:
            raise RuntimeError("scan display received a frame before setup")
        receipt: dict[str, Any] = {"kind": kind}
        if kind == "capture":
            if (width, height) != (FRAME_WIDTH, FRAME_HEIGHT) or len(gray) != FRAME_WIDTH * FRAME_HEIGHT:
                raise ValueError("scan capture must be a complete 752x480 gray frame")
            receipt.update(self.scene.add_capture(bytes(gray), steps=int(metadata["steps"]),
                                                  position_index=int(metadata["position_index"]),
                                                  request_id=metadata.get("request_id")))
            receipt["raw_sha256"] = hashlib.sha256(bytes(gray)).hexdigest()
        elif kind != "view":
            raise ValueError(f"unknown scan display message kind {kind!r}")
        if "stage" in metadata:
            self.scene.stage = str(metadata["stage"])
        if "progress" in metadata:
            self.scene.progress = str(metadata["progress"])
        if self.scene.last_raw is None:
            receipt["rendered"] = False
            return receipt
        canvas, timing = self.scene.render(yaw_deg=float(metadata.get("yaw_deg", 0.0)))
        receipt.update(timing)
        receipt.update(self._present(canvas))
        receipt.update(rendered=True, points=len(self.scene.cloud), captures=self.scene.captures)
        return receipt


def install_scan_output_factory() -> None:
    """Make the display owner's ``create_output`` return a ScanHDMIOutput (owner process only)."""

    from ..p2 import hdmi_output

    original = hdmi_output.create_output
    if getattr(original, "_scan3d", False):
        return

    def factory(*args: Any, **kwargs: Any) -> ScanHDMIOutput:
        base = original(*args, **kwargs)
        return ScanHDMIOutput(base.clock, base.vtc, base.dma, base.allocator, timeout_s=base.timeout_s)

    factory._scan3d = True  # type: ignore[attr-defined]
    hdmi_output.create_output = factory


__all__ = ["ScanHDMIOutput", "install_scan_output_factory", "pack_hdmi_lanes"]
