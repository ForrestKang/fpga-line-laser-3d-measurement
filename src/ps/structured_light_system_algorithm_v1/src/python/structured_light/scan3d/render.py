"""1280x720 BGR HDMI canvas: camera frame on the left, shaded surface on the right.

The left side (camera frame, stripe marks, frame facts) only changes when a
capture arrives, so it is built once by ``compose_left`` and reused by every
view redraw in ``compose_right``. OpenCV draws all pixels and text.
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

from .cloud import PointCloud
from .pipeline import FRAME_HEIGHT, FRAME_WIDTH, FrameSection, as_gray
from .turntable import TurntableModel


CANVAS_W, CANVAS_H = 1280, 720
LEFT = (16, 104, 640, 408)      # x, y, w, h of the camera panel
RIGHT = (672, 104, 592, 600)    # x, y, w, h of the surface panel
BACKGROUND = (36, 24, 16)       # BGR
PANEL = (20, 14, 10)


def _text(canvas: np.ndarray, text: str, x: int, y: int, rgb: tuple[int, int, int], scale: float = 0.42) -> None:
    import cv2

    cv2.putText(canvas, text, (x, y + 12), cv2.FONT_HERSHEY_SIMPLEX, scale, rgb[::-1], 1, cv2.LINE_AA)


def compose_left(raw: bytes | np.ndarray, section: FrameSection | None) -> np.ndarray:
    """Canvas with background, camera panel, stripe marks and frame facts; right panel blank."""

    import cv2

    canvas = np.empty((CANVAS_H, CANVAS_W, 3), np.uint8)
    canvas[:] = BACKGROUND
    lx, ly, lw, lh = LEFT
    rx, ry, rw, rh = RIGHT
    canvas[ry:ry + rh, rx:rx + rw] = PANEL
    canvas[ly:ly + lh, lx:lx + lw] = cv2.resize(as_gray(raw), (lw, lh), interpolation=cv2.INTER_AREA)[:, :, None]
    _text(canvas, "green = v3 accepted stripe   red = rejected by v3 gates", lx, ly + lh + 8, (200, 200, 200))
    if section is None:
        return canvas
    for rows, centers, color in ((section.rows[section.stripe_accepted],
                                  section.center_q12[section.stripe_accepted], (0, 255, 0)),
                                 (section.rejected_rows, section.rejected_center_q12, (0, 0, 255))):
        xs = (lx + centers * (lw / (FRAME_WIDTH * 4096.0))).astype(np.intp)
        ys = (ly + rows * (lh / FRAME_HEIGHT)).astype(np.intp)
        for dy in (0, 1):
            for dx in (0, 1):
                canvas[np.minimum(ys + dy, ly + lh - 1), np.minimum(xs + dx, lx + lw - 1)] = color
    info = section.summary()
    _text(canvas, f"position {info['position_index']}  steps {info['steps']}  angle {info['angle_deg']:.2f} deg"
                  f"  request {info['request_id']}", lx, ly + lh + 30, (255, 224, 128))
    _text(canvas, f"stripe rows {info['stripe_rows']}  xyz {info['xyz_points']}  "
                  f"extrapolated {info['outside_training_bounds']}", lx, ly + lh + 52, (255, 224, 128))
    return canvas


def compose_right(left: np.ndarray, cloud: PointCloud, turntable: TurntableModel, *, yaw_rad: float,
                  status_lines: Sequence[str], elevation_deg: float = 25.0, mesh=None) -> np.ndarray:
    """Copy of ``left`` with the shaded surface panel and status lines drawn in."""

    from .surface import draw_surface_panel, surface_mesh

    canvas = left.copy()
    rx, ry, rw, rh = RIGHT
    if mesh is None:
        mesh = surface_mesh(cloud, turntable)
    canvas[ry:ry + rh, rx:rx + rw] = draw_surface_panel(mesh, rw, rh, yaw_deg=math.degrees(yaw_rad) - 60.0,
                                                        elevation_deg=elevation_deg, background=PANEL)[0]
    for index, line in enumerate(list(status_lines)[:4]):
        _text(canvas, line, 24, 8 + 21 * index, (255, 255, 255) if index else (120, 255, 160), 0.48)
    if not turntable.calibrated:
        _text(canvas, "ASSUMED AXIS / NOMINAL ANGLE - NOT A MEASUREMENT", rx + 12, ry + 6, (255, 80, 80))
    _text(canvas, f"points {len(cloud)}  quads {len(mesh.quads)}  meshed {100 * mesh.meshed_fraction:.0f}%  "
                  f"extrapolated {int(cloud.outside_training.sum())}", rx + 12, ry + 24, (200, 200, 200))
    _text(canvas, "measured neighbours only; gaps and unseen faces stay open", rx + 12, ry + 42, (200, 200, 200))
    return canvas


def compose_canvas(raw: bytes | np.ndarray, section: FrameSection | None, cloud: PointCloud,
                   turntable: TurntableModel, *, yaw_rad: float, status_lines: Sequence[str],
                   elevation_deg: float = 25.0, mesh=None) -> np.ndarray:
    return compose_right(compose_left(raw, section), cloud, turntable, yaw_rad=yaw_rad,
                         status_lines=status_lines, elevation_deg=elevation_deg, mesh=mesh)


def build_canvas(raw: bytes | np.ndarray, section: FrameSection | None, cloud: PointCloud,
                 turntable: TurntableModel, *, yaw_rad: float, status_lines: Sequence[str],
                 elevation_deg: float = 25.0, mesh=None) -> bytes:
    """Canonical BGR bytes for the bounded HDMI ``show`` path."""

    return compose_canvas(raw, section, cloud, turntable, yaw_rad=yaw_rad, status_lines=status_lines,
                          elevation_deg=elevation_deg, mesh=mesh).tobytes()


__all__ = ["CANVAS_H", "CANVAS_W", "build_canvas", "compose_canvas", "compose_left", "compose_right"]
