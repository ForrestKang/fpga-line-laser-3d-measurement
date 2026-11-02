"""Truth-labelled, stateless 3D canvases for the existing HDMI canvas sink.

No hardware imports, capture, motor control, calibration fitting or meshing.
Each call replaces every pixel, including when geometry is empty.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import json

import cv2
import numpy as np


CAMERA_RECT = (24, 128, 752, 480)
MODEL_RECT = (808, 128, 448, 480)
MAX_POINTS = 250_000
MAX_FACES = 500_000


@dataclass(frozen=True)
class ViewSource:
    source: str
    data_id: str
    calibration: str
    units: str = 'unknown'
    calibration_reference: str | None = None

    def __post_init__(self):
        if self.source not in {'saved_r8', 'live_static', 'current_snapshot'}:
            raise ValueError('source must explicitly be saved_r8, live_static or current_snapshot')
        if (not isinstance(self.data_id, str) or not self.data_id.strip()
                or len(self.data_id) > 160 or not self.data_id.isascii()
                or any(ord(c) < 32 for c in self.data_id)):
            raise ValueError('data_id must be nonempty printable ASCII, at most 160 chars')
        if self.calibration not in {'reference_assisted', 'pending', 'independent', 'unknown'}:
            raise ValueError('explicit supported calibration state required')
        if self.units not in {'mm', 'nominal_mm_unverified', 'unknown'}:
            raise ValueError('units must be mm, nominal_mm_unverified or unknown')
        if self.source == 'saved_r8' and (self.calibration != 'reference_assisted'
                                         or self.units != 'nominal_mm_unverified'):
            raise ValueError('saved R8 is reference assisted with unverified nominal scale')
        if self.source != 'saved_r8' and self.calibration == 'reference_assisted':
            raise ValueError('saved R8 geometry cannot be relabelled current static')
        if self.calibration == 'independent':
            if (self.units != 'mm' or not isinstance(self.calibration_reference, str)
                    or not self.calibration_reference.strip()):
                raise ValueError('independent calibration needs explicit reference and mm units')
        if self.calibration != 'independent' and self.units == 'mm':
            raise ValueError('unvalidated calibration cannot claim mm units')

    @property
    def title(self):
        if self.source == 'saved_r8':
            return 'SAVED DATA / REFERENCE ASSISTED'
        current = 'CURRENT SNAPSHOT' if self.source == 'current_snapshot' else 'CURRENT STATIC'
        if self.calibration == 'independent':
            return current+' / CALIBRATION DECLARED'
        return current+' / CALIBRATION PENDING'


def _geometry(points, triangles):
    try:
        raw = np.asarray(points if points is not None else np.empty((0, 3)))
        if raw.dtype.kind not in 'fiu' or raw.ndim != 2 or raw.shape[1] != 3:
            raise ValueError('points must be a numeric Nx3 array')
        if len(raw) > MAX_POINTS:
            raise ValueError('point bound exceeded')
        vertices = raw.astype(np.float64, copy=False)
        if not np.isfinite(vertices).all() or np.any(np.abs(vertices) > 1e12):
            raise ValueError('points must be finite and bounded to 1e12 units')
        faces = np.asarray(triangles if triangles is not None else np.empty((0, 3), dtype=int))
        if faces.ndim != 2 or faces.shape[1] != 3 or faces.dtype.kind not in 'iu':
            raise ValueError('triangles must be an integer Mx3 array')
        if len(faces) > MAX_FACES:
            raise ValueError('face bound exceeded')
        if len(faces) and (faces.min() < 0 or faces.max() >= len(vertices)):
            raise ValueError('triangle index outside supplied vertices')
        return vertices, faces.astype(np.int64, copy=False)
    except (TypeError, OverflowError) as exc:
        raise ValueError('invalid geometry') from exc


def project_z_up(points, *, width=448, height=480, yaw_deg=-55., elevation_deg=23.):
    """Equal-scale orthographic projection; depth increases toward the viewer."""
    if (not math.isfinite(yaw_deg) or not math.isfinite(elevation_deg)
            or not 1 <= elevation_deg <= 80):
        raise ValueError('finite yaw and elevation in 1..80 degrees required')
    yaw, elevation = math.radians(yaw_deg), math.radians(elevation_deg)
    toward = np.array([math.cos(elevation)*math.cos(yaw),
                       math.cos(elevation)*math.sin(yaw), math.sin(elevation)])
    right = np.array([-math.sin(yaw), math.cos(yaw), 0.])
    up = np.cross(toward, right)
    if len(points):
        center = (points.min(axis=0) + points.max(axis=0))*.5
        view = np.column_stack(((points-center)@right, (points-center)@up,
                                (points-center)@toward))
        span = np.maximum(np.ptp(view[:, :2], axis=0), 1e-9)
        scale = min((width-64)/span[0], (height-100)/span[1])
        screen = np.column_stack((width*.5 + view[:, 0]*scale,
                                  height*.46 - view[:, 1]*scale, view[:, 2]))
    else:
        screen = np.empty((0, 3))
    return screen, np.column_stack((right, -up, toward))


def _raster_mesh(panel, screen, faces, colors):
    """Barycentric per-pixel z-buffer. Only supplied triangles are rasterized."""
    height, width = panel.shape[:2]
    depth_buffer = np.full((height, width), -np.inf)
    for face, color in zip(faces, colors):
        xyz = screen[face]
        a, b, c = xyz[:, :2]
        denominator = (b[1]-c[1])*(a[0]-c[0]) + (c[0]-b[0])*(a[1]-c[1])
        if abs(denominator) < 1e-9:
            continue
        lo = np.maximum(np.floor(xyz[:, :2].min(axis=0)).astype(int), 0)
        hi = np.minimum(np.ceil(xyz[:, :2].max(axis=0)).astype(int), [width-1, height-1])
        if np.any(lo > hi):
            continue
        yy, xx = np.mgrid[lo[1]:hi[1]+1, lo[0]:hi[0]+1]
        w0 = ((b[1]-c[1])*(xx-c[0]) + (c[0]-b[0])*(yy-c[1]))/denominator
        w1 = ((c[1]-a[1])*(xx-c[0]) + (a[0]-c[0])*(yy-c[1]))/denominator
        w2 = 1-w0-w1
        depth = w0*xyz[0, 2] + w1*xyz[1, 2] + w2*xyz[2, 2]
        stored = depth_buffer[lo[1]:hi[1]+1, lo[0]:hi[0]+1]
        mask = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9) & (depth > stored)
        stored[mask] = depth[mask]
        panel[lo[1]:hi[1]+1, lo[0]:hi[0]+1][mask] = color
    return depth_buffer


def _text(image, text, x, y, scale=.52, color=(215, 225, 235)):
    cv2.putText(image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                scale, color, 1, cv2.LINE_AA)


def render_3d_canvas(raw, *, source: ViewSource, points=None, triangles=None,
                     yaw_deg=-55., elevation_deg=23.):
    """Return a new contiguous 720p BGR canvas and a truthful software receipt.

    ``points`` may be a cumulative cloud; ownership and admission stay with the
    caller. No geometry from a previous call is retained or inferred.
    Pending current data must be empty until calibration provides current XYZ.
    """
    if not isinstance(source, ViewSource):
        raise ValueError('explicit ViewSource required')
    if isinstance(raw, (bytes, bytearray, memoryview)):
        if len(raw) != 752*480:
            raise ValueError('camera frame must contain 752x480 gray8 pixels')
        gray = np.frombuffer(raw, dtype=np.uint8).reshape(480, 752)
    else:
        gray = np.asarray(raw)
        if gray.shape != (480, 752) or gray.dtype != np.uint8:
            raise ValueError('camera frame must be 752x480 gray8')
    vertices, faces = _geometry(points, triangles)
    if source.source != 'saved_r8' and source.calibration != 'independent' and len(vertices):
        raise ValueError('current calibration pending: supply empty geometry, never old R8 points')
    screen, basis = project_z_up(vertices, yaw_deg=yaw_deg, elevation_deg=elevation_deg)
    canvas = np.full((720, 1280, 3), (28, 17, 7), dtype=np.uint8)
    panel = np.full((480, 448, 3), (40, 28, 17), dtype=np.uint8)
    if len(vertices):
        heights = (vertices[:, 2]-vertices[:, 2].min())/max(float(np.ptp(vertices[:, 2])), 1e-9)
        if len(faces):
            polygons = vertices[faces]
            normals = np.cross(polygons[:, 1]-polygons[:, 0], polygons[:, 2]-polygons[:, 0])
            normals /= np.maximum(np.linalg.norm(normals, axis=1)[:, None], 1e-12)
            shade = .40 + .60*np.abs(normals@np.array([.3, -.4, .8660254]))
            level = heights[faces].mean(axis=1)
            colors = np.column_stack((175+50*level, 125+90*level, 65+70*level))*shade[:, None]
            _raster_mesh(panel, screen, faces, colors.astype(np.uint8))
        else:
            for index in np.argsort(screen[:, 2], kind='stable'):
                x, y = np.rint(screen[index, :2]).astype(int)
                if 0 <= x < 448 and 0 <= y < 480:
                    panel[y, x] = (220, int(130+95*heights[index]), 90)
    else:
        _text(panel, 'NO CURRENT XYZ' if source.source != 'saved_r8' else 'NO POINTS', 96, 225)
        _text(panel, 'CALIBRATION PENDING' if source.source != 'saved_r8' else 'EMPTY DATA', 65, 255)
    origin = np.array([55., 420.])
    for index, color in enumerate(((90, 120, 255), (100, 230, 120), (255, 180, 80))):
        direction = basis[index, :2]
        direction /= max(float(np.linalg.norm(direction)), 1e-12)
        endpoint = np.rint(origin+direction*36).astype(int)
        cv2.arrowedLine(panel, tuple(origin.astype(int)), tuple(endpoint), color, 2, cv2.LINE_AA)
        _text(panel, 'XYZ'[index], int(endpoint[0])+3, int(endpoint[1])-3, .4, color)
    _text(panel, 'Z UP / ORTHOGRAPHIC / DEPTH TESTED', 17, 22, .40)
    _text(canvas, source.title, 24, 40, .83, (100, 205, 255))
    _text(canvas, 'DATA ID: '+source.data_id[:100], 24, 72, .5)
    camera_label = {'saved_r8': 'SAVED CAMERA FRAME', 'live_static': 'CURRENT CAMERA FRAME',
                    'current_snapshot': 'CURRENT SNAPSHOT / FILE INPUT'}[source.source]
    _text(canvas, camera_label+
          ' / RAW 752 x 480', 24, 110, .55)
    _text(canvas, 'SUPPLIED XYZ / FACES ONLY', 808, 110, .52)
    canvas[128:608, 24:776] = gray[:, :, None]
    canvas[128:608, 808:1256] = panel
    unit_label = {'mm': 'mm (DECLARED CALIBRATION)', 'nominal_mm_unverified': 'NOMINAL SCALE / UNVERIFIED',
                  'unknown': 'UNITS UNKNOWN'}[source.units]
    _text(canvas, f'POINTS {len(vertices)} / SUPPLIED FACES {len(faces)} / {unit_label}', 24, 643, .55)
    _text(canvas, 'DISPLAY ONLY / PHYSICAL ACCURACY NOT VERIFIED / NO DIMENSION ACCEPTANCE', 24, 675, .53,
          (100, 190, 255))
    _text(canvas, 'Missing surfaces remain missing. No geometry generated by this renderer.', 24, 704, .48)
    receipt = dict(source=source.source, data_id=source.data_id, title=source.title,
                   calibration_state=source.calibration, calibration_reference=source.calibration_reference,
                   units=source.units, vertex_count=len(vertices), supplied_triangle_count=len(faces),
                   generated_triangle_count=0, z_up=True, projection='equal_scale_orthographic',
                   depth_test='per_pixel_barycentric_z_buffer' if len(faces) else 'point_depth_order',
                   raw_camera_resampled=False, measurement_eligible=False,
                   physical_accuracy_verified=False, hdmi_acknowledged=False)
    return canvas, receipt


def load_saved_r8(path):
    """Require the exported R8 contract; preserve its uncalibrated semantics."""
    path = Path(path)
    data = json.loads(path.read_text(encoding='utf-8'))
    if (data.get('schema') != 'scan3d.reference_assisted_preview.v1'
            or data.get('units') != 'nominal_mm_unverified'
            or data.get('measurement_eligible') is not False
            or data.get('status') != 'REFERENCE_ASSISTED_UNCALIBRATED_PREVIEW'):
        raise ValueError('not the explicitly uncalibrated R8 saved-data contract')
    vertices, faces = _geometry(data['vertices'], data['triangles'])
    return ViewSource('saved_r8', path.name, 'reference_assisted', data['units']), vertices, faces
