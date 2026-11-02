"""Shaded surface view of the scan, meshed only between measured neighbours.

A quad joins rows r and r+1 of two angularly adjacent sections. It is kept
only when all four corners were measured, the two sections are close enough in
angle, and no corner jumps in radius (a step edge or a wrong stripe). Missing
rows, unseen horizontal faces and wide angle gaps stay open; nothing is
interpolated or taken from CAD. Points that belong to no quad are still drawn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np

from .cloud import PointCloud
from .turntable import TurntableModel


BASE_RGB = (150, 196, 166)
SHADE_LEVELS = 32
HEADER_PX = 64


@dataclass(frozen=True)
class SurfaceMesh:
    vertices: np.ndarray        # (N, 3) display frame: x, y across the axis, z up the axis (mm)
    quads: np.ndarray           # (M, 4) vertex indices
    loose: np.ndarray           # (K,) vertex indices not used by any quad
    angles_deg: tuple[float, ...]
    skipped: dict[str, int]
    strips: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))  # (M,) contiguous-run id per quad

    @property
    def meshed_fraction(self) -> float:
        return 0.0 if len(self.vertices) == 0 else 1.0 - len(self.loose) / len(self.vertices)


def display_frame(turntable: TurntableModel) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Origin and x/y/z unit vectors: z along the axis pointing up the image, x toward the camera."""

    up = np.asarray(turntable.axis_direction, float)
    if up[1] > 0:  # camera +Y points down the image
        up = -up
    toward_camera = -np.asarray(turntable.axis_point_mm, float)
    x = toward_camera - up * float(up @ toward_camera)
    x /= np.linalg.norm(x)
    y = np.cross(up, x)
    return np.asarray(turntable.axis_point_mm, float), x, y, up


def _local(turntable: TurntableModel, xyz: np.ndarray) -> np.ndarray:
    origin, ex, ey, ez = display_frame(turntable)
    rel = np.asarray(xyz, float).reshape(-1, 3) - origin
    return np.column_stack((rel @ ex, rel @ ey, rel @ ez))


def _pair_quads(rows_a: np.ndarray, base_a: int, rows_b: np.ndarray, base_b: int, radius: np.ndarray,
                max_radial_jump_mm: float) -> tuple[np.ndarray, int, np.ndarray]:
    """Quads between rows r, r+1 of two sections whose four corners were all measured.

    Also returns a run label per kept quad: consecutive rows form one run, so a
    run is a gap-free piece of the strip between the two sections.
    """

    candidates = rows_a[np.isin(rows_a + 1, rows_a) & np.isin(rows_a, rows_b) & np.isin(rows_a + 1, rows_b)]
    if len(candidates) == 0:
        return np.zeros((0, 4), np.int64), 0, np.zeros(0, np.int64)
    ia0 = base_a + np.searchsorted(rows_a, candidates)
    ia1 = base_a + np.searchsorted(rows_a, candidates + 1)
    ib0 = base_b + np.searchsorted(rows_b, candidates)
    ib1 = base_b + np.searchsorted(rows_b, candidates + 1)
    quads = np.column_stack((ia0, ia1, ib1, ib0)).astype(np.int64)
    r = radius[quads]
    keep = (r.max(axis=1) - r.min(axis=1)) <= max_radial_jump_mm
    kept_rows = candidates[keep]
    runs = np.concatenate(([0], np.cumsum(np.diff(kept_rows) != 1))) if len(kept_rows) else np.zeros(0, np.int64)
    return quads[keep], int(np.count_nonzero(~keep)), runs.astype(np.int64)


class IncrementalSurface:
    """Mesh that grows one section at a time; equivalent to :func:`surface_mesh`.

    A section whose angle is not past the last one (a revisit, or a scan that
    reverses) triggers one full rebuild; a normal scan only meshes the new
    section against its predecessor and refreshes the wrap-around seam.
    """

    def __init__(self, turntable: TurntableModel, *, max_angle_gap_deg: float = 20.0,
                 max_radial_jump_mm: float = 1.0, duplicate_angle_deg: float = 0.5) -> None:
        self.turntable = turntable
        self.max_angle_gap_deg = float(max_angle_gap_deg)
        self.max_radial_jump_mm = float(max_radial_jump_mm)
        self.duplicate_angle_deg = float(duplicate_angle_deg)
        self._raw: list[tuple[int, np.ndarray, np.ndarray]] = []
        self._reset()

    def _reset(self) -> None:
        self._angles: list[float] = []
        self._rows: list[np.ndarray] = []
        self._bases: list[int] = []
        self._vertices: list[np.ndarray] = []
        self._radius: list[np.ndarray] = []
        self._quads: list[np.ndarray] = []
        self._strips: list[np.ndarray] = []
        self._next_strip = 0
        self._wrap = np.zeros((0, 4), np.int64)
        self._wrap_strips = np.zeros(0, np.int64)
        self._count = 0
        self.skipped = {"angle_gap": 0, "radial_jump": 0}
        self._wrap_skipped = {"angle_gap": 0, "radial_jump": 0}
        self._cache: SurfaceMesh | None = None

    def _radius_array(self) -> np.ndarray:
        return np.concatenate(self._radius) if self._radius else np.zeros(0)

    def _append(self, angle: float, rows: np.ndarray, xyz: np.ndarray) -> None:
        order = np.argsort(rows, kind="stable")
        rows = np.asarray(rows, np.int64)[order]
        local = _local(self.turntable, np.asarray(xyz, float)[order])
        base = self._count
        self._angles.append(angle)
        self._rows.append(rows)
        self._bases.append(base)
        self._vertices.append(local)
        self._radius.append(np.hypot(local[:, 0], local[:, 1]))
        self._count += len(rows)
        if len(self._angles) >= 2:
            gap = self._angles[-1] - self._angles[-2]
            if gap > self.max_angle_gap_deg:
                self.skipped["angle_gap"] += 1
            else:
                quads, rejected, runs = _pair_quads(self._rows[-2], self._bases[-2], rows, base,
                                                    self._radius_array(), self.max_radial_jump_mm)
                self._quads.append(quads)
                self._strips.append(runs + self._next_strip)
                self._next_strip += int(runs.max()) + 1 if len(runs) else 0
                self.skipped["radial_jump"] += rejected
        self._update_wrap()

    def _update_wrap(self) -> None:
        self._wrap = np.zeros((0, 4), np.int64)
        self._wrap_strips = np.zeros(0, np.int64)
        self._wrap_skipped = {"angle_gap": 0, "radial_jump": 0}
        if len(self._angles) < 3:
            return
        gap = self._angles[0] + 360.0 - self._angles[-1]
        if gap > self.max_angle_gap_deg:
            self._wrap_skipped["angle_gap"] = 1
            return
        self._wrap, rejected, runs = _pair_quads(self._rows[-1], self._bases[-1], self._rows[0], self._bases[0],
                                                 self._radius_array(), self.max_radial_jump_mm)
        self._wrap_strips = runs + self._next_strip
        self._wrap_skipped["radial_jump"] = rejected

    def add(self, steps: int, rows: np.ndarray, xyz: np.ndarray) -> None:
        self._raw.append((int(steps), np.asarray(rows).copy(), np.asarray(xyz, float).copy()))
        self._cache = None
        angle = math.degrees(self.turntable.angle_rad(int(steps))) % 360.0
        if not self._angles:
            self._append(angle, rows, xyz)
            return
        duplicate = any(min(abs(angle - a), 360.0 - abs(angle - a)) < self.duplicate_angle_deg
                        for a in (self._angles[0], self._angles[-1]))
        if duplicate:
            return
        if angle > self._angles[-1]:
            self._append(angle, rows, xyz)
            return
        self._rebuild()

    def _rebuild(self) -> None:
        raw = sorted(((math.degrees(self.turntable.angle_rad(s)) % 360.0, r, x) for s, r, x in self._raw),
                     key=lambda item: item[0])
        self._reset()
        for angle, rows, xyz in raw:
            if self._angles and (angle - self._angles[-1] < self.duplicate_angle_deg
                                 or self._angles[0] + 360.0 - angle < self.duplicate_angle_deg):
                continue
            self._append(angle, rows, xyz)

    def mesh(self) -> SurfaceMesh:
        if self._cache is None:
            vertices = np.concatenate(self._vertices) if self._vertices else np.zeros((0, 3))
            parts = [q for q in self._quads if len(q)] + ([self._wrap] if len(self._wrap) else [])
            quads = np.concatenate(parts) if parts else np.zeros((0, 4), np.int64)
            strip_parts = [s for s in self._strips if len(s)] + ([self._wrap_strips] if len(self._wrap_strips) else [])
            strips = np.concatenate(strip_parts) if strip_parts else np.zeros(0, np.int64)
            used = np.zeros(len(vertices), bool)
            used[quads.ravel()] = True
            skipped = {k: self.skipped[k] + self._wrap_skipped[k] for k in self.skipped}
            self._cache = SurfaceMesh(vertices, quads, np.flatnonzero(~used),
                                      tuple(round(a, 3) for a in self._angles), skipped, strips)
        return self._cache


def surface_mesh(cloud: PointCloud, turntable: TurntableModel, *, max_angle_gap_deg: float = 20.0,
                 max_radial_jump_mm: float = 1.0, duplicate_angle_deg: float = 0.5) -> SurfaceMesh:
    """Build the whole mesh at once (reference path; sorts sections by angle first)."""

    surface = IncrementalSurface(turntable, max_angle_gap_deg=max_angle_gap_deg,
                                 max_radial_jump_mm=max_radial_jump_mm,
                                 duplicate_angle_deg=duplicate_angle_deg)
    surface._raw = [(int(s), np.asarray(r).copy(), np.asarray(x, float).copy()) for s, r, x in cloud.frames]
    surface._rebuild()
    return surface.mesh()


def _view(yaw_deg: float, elevation_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    yaw, elev = math.radians(yaw_deg), math.radians(elevation_deg)
    toward_viewer = np.array([math.cos(elev) * math.cos(yaw), math.cos(elev) * math.sin(yaw), math.sin(elev)])
    right = np.array([-math.sin(yaw), math.cos(yaw), 0.0])
    up = np.cross(toward_viewer, right)
    return right, up, toward_viewer


def draw_surface_panel(mesh: SurfaceMesh, width: int, height: int, *, yaw_deg: float,
                       elevation_deg: float = 25.0, background=(10, 14, 20)) -> tuple[np.ndarray, list]:
    """Render the mesh into a (height, width, 3) BGR panel; returns (panel, text labels)."""

    import cv2

    panel = np.empty((height, width, 3), np.uint8)
    panel[:] = background
    labels: list[tuple[str, int, int, tuple[int, int, int]]] = []
    if len(mesh.vertices) == 0:
        labels.append(("no measured points yet", 20, height // 2, (200, 200, 200)))
        cv2.putText(panel, labels[0][0], (20, height // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1,
                    cv2.LINE_AA)
        return panel, labels

    v = mesh.vertices
    z0 = float(v[:, 2].min())
    pts = v - np.array([0.0, 0.0, z0])
    reach = max(5.0, math.ceil(float(np.hypot(pts[:, 0], pts[:, 1]).max()) / 5.0) * 5.0)
    top = max(10.0, math.ceil(float(pts[:, 2].max()) / 10.0) * 10.0)
    right, up, toward = _view(yaw_deg, elevation_deg)
    # Fit a yaw-independent cylinder (grid corners and axis labels included) so a turning
    # view keeps one zoom; the top HEADER_PX rows of the panel stay free for status text.
    bound = reach * 1.45
    elev = math.radians(elevation_deg)
    span_h = top * math.cos(elev) + 2.0 * bound * math.sin(elev)
    scale = min((width - 40) / (2.0 * bound), (height - HEADER_PX - 30) / span_h)
    ox = width / 2.0
    oy = (HEADER_PX + height - 10) / 2.0 + scale * top * math.cos(elev) / 2.0

    def project(p: np.ndarray) -> np.ndarray:
        p = np.atleast_2d(p)
        return np.column_stack((ox + scale * (p @ right), oy - scale * (p @ up)))

    shift = 4
    fixed = float(1 << shift)
    grid_color = (70, 70, 70)
    step = 5.0 if reach <= 25 else 10.0
    ticks = np.arange(-reach, reach + 1e-6, step)
    for t in ticks:
        for a, b in (([t, -reach, 0], [t, reach, 0]), ([-reach, t, 0], [reach, t, 0])):
            p = project(np.array([a, b], float))
            cv2.line(panel, tuple((p[0] * fixed).astype(int)), tuple((p[1] * fixed).astype(int)),
                     grid_color, 1, cv2.LINE_AA, shift)
    # Tick labels go on the grid edges nearest the viewer; the Z axis stands on the leftmost corner.
    sx = 1.0 if toward[0] >= 0 else -1.0
    sy = 1.0 if toward[1] >= 0 else -1.0
    for t in ticks:
        px, py = project(np.array([t, sy * reach * 1.12, 0.0]))[0]
        labels.append((f"{t:g}", int(px) - 8, int(py), (170, 170, 170)))
        px, py = project(np.array([sx * reach * 1.12, t, 0.0]))[0]
        labels.append((f"{t:g}", int(px) - 8, int(py), (170, 170, 170)))
    floor = [np.array([fx * reach, fy * reach, 0.0]) for fx in (-1, 1) for fy in (-1, 1)]
    zbase = min(floor, key=lambda corner: float(corner @ right))
    p = project(np.array([zbase, zbase + [0, 0, top]]))
    cv2.line(panel, tuple((p[0] * fixed).astype(int)), tuple((p[1] * fixed).astype(int)), (120, 120, 120), 1,
             cv2.LINE_AA, shift)
    for z in np.arange(0, top + 1e-6, 10.0):
        px, py = project(zbase + [0, 0, z])[0]
        cv2.line(panel, (int(px) - 4, int(py)), (int(px) + 4, int(py)), (120, 120, 120), 1)
        labels.append((f"{z:g}", int(px) - 30, int(py) - 6, (170, 170, 170)))
    for text, point in (("X / mm", [0, sy * reach * 1.4, 0]), ("Y / mm", [sx * reach * 1.4, 0, 0]),
                        ("Z / mm", list(zbase + [0, 0, top * 1.08]))):
        px, py = project(np.asarray(point, float))[0]
        labels.append((text, int(px) - 20, int(py), (210, 210, 210)))

    screen = project(pts)
    depth = pts @ toward
    light = np.array([0.45, -0.35, 0.82])
    light = light / np.linalg.norm(light)
    view_light = light[0] * right + light[1] * up + light[2] * toward
    base = np.array(BASE_RGB[::-1], float)
    palette = np.clip(base[None, :] * np.linspace(0.30, 1.0, SHADE_LEVELS)[:, None], 0, 255).astype(np.uint8)

    q = mesh.quads
    loose = np.asarray(mesh.loose, np.int64)
    strips = mesh.strips if len(mesh.strips) == len(q) else np.arange(len(q))
    if len(q):
        a, b, c, d = (pts[q[:, k]] for k in range(4))
        normals = np.cross(c - a, d - b)
        normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-9)
        shade = np.clip(0.30 + 0.75 * np.abs(normals @ view_light), 0.30, 1.0)
        quad_level = np.round((shade - 0.30) / 0.70 * (SHADE_LEVELS - 1)).astype(np.int64)
        _, strip_index = np.unique(strips, return_inverse=True)
        strip_index = strip_index.ravel()
        strip_depth = (np.bincount(strip_index, weights=depth[q].mean(axis=1))
                       / np.bincount(strip_index))[strip_index]
    else:
        quad_level = strip_index = np.zeros(0, np.int64)
        strip_depth = np.zeros(0)
    # Painter's order far to near over whole strips (gap-free runs between two sections,
    # which do not overlap themselves on screen) and loose points (level -1). One fill call
    # per (strip, shade) group.
    item_depth = np.concatenate((strip_depth, depth[loose]))
    item_unit = np.concatenate((strip_index, -1 - np.arange(len(loose))))
    item_level = np.concatenate((quad_level, np.full(len(loose), -1)))
    order = np.lexsort((item_level, item_unit, item_depth))
    unit, level = item_unit[order], item_level[order]
    cuts = np.flatnonzero((np.diff(unit) != 0) | (np.diff(level) != 0)) + 1
    polygons = np.round(screen[q] * fixed).astype(np.int32) if len(q) else np.zeros((0, 4, 2), np.int32)
    quad_count = len(q)
    for group in np.split(order, cuts) if len(order) else ():
        first = int(group[0])
        if first >= quad_count:
            x, y = screen[loose[first - quad_count]]
            cv2.circle(panel, (int(x), int(y)), 1, (150, 150, 110), -1)
        else:
            color = tuple(int(v) for v in palette[quad_level[first]])
            cv2.fillPoly(panel, list(polygons[group]), color, cv2.LINE_AA, shift)
    for text, x, y, color in labels:
        cv2.putText(panel, text, (x, y + 10), cv2.FONT_HERSHEY_SIMPLEX, 0.38, color[::-1], 1, cv2.LINE_AA)
    return panel, labels


__all__ = ["SurfaceMesh", "display_frame", "draw_surface_panel", "surface_mesh"]
