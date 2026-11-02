"""Live scene: per-capture update and per-tick render, independent of any hardware.

The HDMI owner process holds one LiveScene. Each capture adds one section to
the point cloud and only that section's quads to the mesh; each render reuses
the cached mesh. The motion process never runs this code, so rendering time
cannot delay guard refreshes or motor tasks.
"""

from __future__ import annotations

from collections import deque
import time
from typing import Any, Sequence

import numpy as np

from .cloud import PointCloud
from .pipeline import FrameSection, ScanCalibration, process_frame
from .render import compose_left, compose_right
from .surface import IncrementalSurface
from .turntable import TurntableModel


class LiveScene:
    def __init__(self, calibration: ScanCalibration, turntable: TurntableModel, *, elevation_deg: float = 25.0,
                 title: str = "SCAN3D LIVE") -> None:
        self.calibration = calibration
        self.turntable = turntable
        self.elevation_deg = float(elevation_deg)
        self.title = title
        self.cloud = PointCloud()
        self.surface = IncrementalSurface(turntable)
        self.last_raw: bytes | None = None
        self.last_section: FrameSection | None = None
        self._left: np.ndarray | None = None  # camera half of the canvas, rebuilt per capture
        self.stage = "WAITING FOR FIRST FRAME"
        self.progress = ""
        self.timings: deque[dict[str, float]] = deque(maxlen=64)
        self.captures = 0
        self.renders = 0

    def add_capture(self, raw: bytes, *, steps: int, position_index: int, request_id: int | None) -> dict[str, Any]:
        started = time.perf_counter()
        section = process_frame(raw, steps=steps, position_index=position_index, calibration=self.calibration,
                                turntable=self.turntable, request_id=request_id)
        processed = time.perf_counter()
        self.cloud.add(section)
        self.surface.add(section.steps, section.rows[section.valid], section.points_object_mm[section.valid])
        meshed = time.perf_counter()
        self.last_raw = bytes(raw)
        self.last_section = section
        self._left = None
        self.captures += 1
        timing = {"process_s": processed - started, "mesh_s": meshed - processed}
        self.timings.append(timing)
        return {**section.summary(), **timing}

    def status_lines(self) -> list[str]:
        mesh = self.surface.mesh()
        return [f"{self.title} | {self.stage}",
                f"{self.progress} | sections {len(mesh.angles_deg)} | points {len(self.cloud)}",
                f"turntable {self.turntable.state} | v3 stripe selection | updates after every capture",
                "no millimetre accuracy claim | missing rows and unseen faces are not filled"]

    def render(self, *, yaw_deg: float,
               status_lines: Sequence[str] | None = None) -> tuple[np.ndarray, dict[str, float]]:
        if self.last_raw is None:
            raise RuntimeError("no capture has been added yet")
        started = time.perf_counter()
        mesh = self.surface.mesh()
        if self._left is None:
            self._left = compose_left(self.last_raw, self.last_section)
        canvas = compose_right(self._left, self.cloud, self.turntable, yaw_rad=yaw_deg * 3.141592653589793 / 180.0,
                               status_lines=list(status_lines or self.status_lines()),
                               elevation_deg=self.elevation_deg, mesh=mesh)
        self.renders += 1
        timing = {"render_s": time.perf_counter() - started, "quads": float(len(mesh.quads))}
        return canvas, timing


__all__ = ["LiveScene"]
