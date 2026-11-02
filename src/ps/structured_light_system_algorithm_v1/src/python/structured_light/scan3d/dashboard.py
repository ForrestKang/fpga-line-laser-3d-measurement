"""Native 1280x720 scan dashboard for the HDMI display.

The camera panel displays the complete supplied 752x480 grayscale frame
without gain, contrast stretch, threshold, or color map. The 3D panel draws
only supplied points or a supplied measured-neighbour mesh. A bundled Noto CJK
font is picked up automatically when available; otherwise labels use explicit
English text.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
import time
from typing import Sequence

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .surface import SurfaceMesh, draw_surface_panel


CANVAS_W, CANVAS_H = 1280, 720
CAMERA_IMAGE = (24, 128, 752, 480)  # Full native 752x480 frame, copied without resampling.
MODEL_VIEW = (808, 128, 448, 480)
METRIC_CARDS = tuple((24 + i * 308, 644, 296, 66) for i in range(4))

_BG_TOP = (7, 17, 28)
_BG_BOTTOM = (11, 23, 36)
_CARD = (15, 29, 43)
_CARD_ALT = (18, 34, 49)
_EDGE = (38, 57, 74)
_TEXT = (226, 235, 243)
_MUTED = (132, 153, 170)
_TEAL = (40, 210, 184)
_AMBER = (255, 190, 96)
_RED = (255, 107, 124)
_GREEN = (93, 220, 157)


@dataclass(frozen=True)
class DashboardStatus:
    """Operator-facing facts for one displayed frame and model revision."""

    mode: str = "SCAN"
    source: str = "live"
    progress_pct: float | None = None
    angle_deg: float | None = None
    quality: str = "unvalidated"
    camera_fps: float | None = None
    model_fps: float | None = None
    axis_calibrated: bool = False
    timing_calibrated: bool = False
    volume_calibrated: bool = False
    laser_available: bool | None = None
    debug_details: tuple[str, ...] = ()
    frame_number: int | None = None
    display_fps: float | None = None

    def __post_init__(self) -> None:
        for name in ("mode", "source", "quality"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be text")
        for name in ("progress_pct", "angle_deg", "camera_fps", "model_fps", "display_fps"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value)):
                raise ValueError(f"{name} must be a finite number or None")
        if (self.frame_number is not None
                and (isinstance(self.frame_number, bool) or not isinstance(self.frame_number, int)
                     or self.frame_number < 0)):
            raise ValueError("frame_number must be a nonnegative integer or None")
        if self.progress_pct is not None and not 0 <= self.progress_pct <= 100:
            raise ValueError("progress_pct must be in 0..100")
        if any(type(getattr(self, name)) is not bool for name in
               ("axis_calibrated", "timing_calibrated", "volume_calibrated")):
            raise ValueError("calibration states must be bool")
        if self.laser_available is not None and type(self.laser_available) is not bool:
            raise ValueError("laser_available must be bool or None")
        if not isinstance(self.debug_details, tuple) or any(not isinstance(item, str) for item in self.debug_details):
            raise ValueError("debug_details must be a tuple of strings")


@dataclass(frozen=True)
class MeasurementCard:
    """A dimension and its explicit comparison inputs; absent facts stay absent."""

    name: str
    value_mm: float | None = None
    tolerance_mm: float | None = None
    truth_mm: float | None = None
    truth_reference: str | None = None
    result: str | None = None
    valid: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("measurement name is required")
        if type(self.valid) is not bool:
            raise ValueError("measurement valid must be bool")
        for name in ("value_mm", "tolerance_mm", "truth_mm"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value)):
                raise ValueError(f"{name} must be finite or None")
        if self.tolerance_mm is not None and self.tolerance_mm < 0:
            raise ValueError("tolerance_mm must be nonnegative")
        if self.result is not None and not isinstance(self.result, str):
            raise ValueError("result must be text or None")
        if self.truth_reference is not None and not isinstance(self.truth_reference, str):
            raise ValueError("truth_reference must be text or None")


def _source_label(source: str, *, chinese: bool) -> str:
    normalized = source.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"live", "camera", "device"}:
        return "实时" if chinese else "LIVE"
    if normalized in {"replay", "offline", "recorded", "stored", "stored_sample_frame"}:
        return "回放" if chinese else "REPLAY"
    safe = source.strip().upper()
    if not safe.isascii():
        return "数据源" if chinese else "SOURCE UNKNOWN"
    return ("数据源: " if chinese else "SOURCE: ") + (safe[:18] or "UNKNOWN")


def _mode_label(mode: str, source: str, *, chinese: bool) -> str:
    value = mode.strip() or "SCAN"
    normalized_source = source.strip().lower().replace("-", "_").replace(" ", "_")
    is_replay = normalized_source in {"replay", "offline", "recorded", "stored", "stored_sample_frame"}
    if is_replay and ("LIVE" in value.upper() or "实时" in value):
        return "回放扫描" if chinese else "REPLAY SCAN"
    if not value.isascii() and not chinese:
        return "SCAN MODE"
    return value[:22]


def _camera_frame(raw: bytes | bytearray | memoryview | np.ndarray) -> np.ndarray:
    if isinstance(raw, (bytes, bytearray, memoryview)):
        if len(raw) != 752 * 480:
            raise ValueError("raw camera frame must contain exactly 752x480 gray8 pixels")
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(480, 752)
    else:
        frame = np.asarray(raw)
        if frame.shape != (480, 752) or frame.dtype != np.uint8:
            raise ValueError("raw camera frame must be a 752x480 uint8 grayscale array")
    return frame


def _normalise_points(points: object | None) -> np.ndarray:
    if points is None:
        return np.zeros((0, 3), dtype=np.float64)
    try:
        array = np.asarray(points, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError("points must be an Nx3 array") from exc
    if array.ndim != 2 or array.shape[1] != 3:
        raise ValueError("points must have shape Nx3")
    if not len(array):
        return array
    # Most cloud buffers contain only finite points. Avoid allocating a filtered
    # copy on every display refresh; retain the established NaN/Inf rejection.
    finite = np.isfinite(array)
    if finite.all():
        return array
    return array[finite.all(axis=1)]


@lru_cache(maxsize=4)
def _point_panel_background(width: int, height: int, with_grid: bool) -> np.ndarray:
    """Immutable bounded templates; avoid repeated RGB broadcast on ARM."""
    panel = np.empty((height, width, 3), dtype=np.uint8)
    panel[:] = (37, 25, 15)  # BGR for a deep blue RGB panel.
    if with_grid:
        grid = (65, 52, 39)
        for x in range(38, width, 48):
            cv2.line(panel, (x, 32), (x, height - 34), grid, 1, cv2.LINE_AA)
        for y in range(32, height - 24, 48):
            cv2.line(panel, (26, y), (width - 26, y), grid, 1, cv2.LINE_AA)
    panel.setflags(write=False)
    return panel


def _draw_point_panel(points: np.ndarray, width: int, height: int, yaw_deg: float, *,
                      raster=None, raster_receipt=None) -> tuple[np.ndarray, tuple[str, ...]]:
    """Fast isometric point view with a fixed XYZ triad and equal XYZ scale."""
    panel = _point_panel_background(width, height, bool(len(points))).copy()
    labels = ("X / mm", "Y / mm", "Z / mm")
    if raster_receipt is not None:
        raster_receipt.update(point_raster_requested='native' if raster is not None else 'python',
                              point_raster_backend='python',point_raster_fallback=None)
    if not len(points):
        cv2.putText(panel, "Waiting for valid points", (width // 2 - 114, height // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.52, (177, 192, 205), 1, cv2.LINE_AA)
        return panel, labels

    yaw = math.radians(yaw_deg)
    elevation = math.radians(25.0)
    toward = np.array([math.cos(elevation) * math.cos(yaw), math.cos(elevation) * math.sin(yaw),
                       math.sin(elevation)])
    right = np.array([-math.sin(yaw), math.cos(yaw), 0.0])
    up = np.cross(toward, right)

    low, high = points.min(axis=0), points.max(axis=0)
    centered = points - (low + high) * 0.5
    projected_x, projected_y = centered @ right, centered @ up
    span_x = max(float(np.ptp(projected_x)), 1.0)
    span_y = max(float(np.ptp(projected_y)), 1.0)
    scale = min((width - 112) / span_x, (height - 106) / span_y)
    native_used = False
    if raster is not None:
        z = points[:, 2]
        outcome = raster.try_raster(points, projected_x, projected_y, panel, scale,
            float(z.min()), max(float(z.max() - z.min()), 1e-9))
        native_used = outcome['used']
        if raster_receipt is not None:
            raster_receipt.update(point_raster_backend='native' if native_used else 'python',
                                  point_raster_fallback=outcome['reason'])
    if not native_used:
        screen_x = np.rint(width * 0.52 + projected_x * scale).astype(np.int32)
        screen_y = np.rint(height * 0.48 - projected_y * scale).astype(np.int32)

        z = points[:, 2]
        z_span = max(float(z.max() - z.min()), 1e-9)
        ratio = np.clip((z - z.min()) / z_span, 0.0, 1.0)
        colors = np.column_stack((
            210 - 110 * ratio,
            150 + 75 * ratio,
            105 + 115 * ratio,
        )).astype(np.uint8)
        inside = ((screen_x >= 3) & (screen_x < width - 3) & (screen_y >= 3) & (screen_y < height - 3))
        xs, ys, colors = screen_x[inside], screen_y[inside], colors[inside]
        # Two vertical pixels per sample keep thin surfaces visible on a 720p panel.
        for dy in (0, 1):
            panel[ys + dy, xs] = colors

    origin = (52, height - 54)
    axis_colors = ((94, 127, 255), (113, 225, 135), (255, 177, 78))
    for index, unit in enumerate(np.eye(3)):
        direction = np.array([unit @ right, -(unit @ up)])
        direction /= max(float(np.linalg.norm(direction)), 1e-9)
        end = (int(origin[0] + direction[0] * 38), int(origin[1] + direction[1] * 38))
        cv2.arrowedLine(panel, origin, end, axis_colors[index], 2, cv2.LINE_AA, tipLength=0.22)
        cv2.putText(panel, "XYZ"[index], (end[0] + 3, end[1] - 3), cv2.FONT_HERSHEY_SIMPLEX,
                    0.42, axis_colors[index], 1, cv2.LINE_AA)
    return panel, labels


class DashboardRenderer:
    """Cached 1280x720 HDMI UI renderer.

    The static layout is rasterized once. Each render copies that BGR template,
    copies the full gray camera frame directly into its native-size panel, then
    draws compact dynamic UI with OpenCV and cached CJK glyph masks. A nonempty
    model panel is cached only when the caller supplies ``cloud_revision``;
    callers must advance that revision whenever points or mesh data changes.
    """

    def __init__(self, *, font_path: str | Path | None = None, debug: bool = False,
                 camera_cache_size: int = 2, model_cache_size: int = 3,
                 point_raster_manifest: str | Path | None = None) -> None:
        if type(debug) is not bool:
            raise ValueError("debug must be bool")
        if type(camera_cache_size) is not int or camera_cache_size < 1:
            raise ValueError("camera_cache_size must be positive")
        if type(model_cache_size) is not int or model_cache_size < 1:
            raise ValueError("model_cache_size must be positive")
        self.debug = debug
        self.point_raster = None
        if point_raster_manifest is not None:
            from .point_raster import NativePointRaster
            self.point_raster = NativePointRaster(point_raster_manifest)
        self._point_raster_receipt = {}
        # Retain the old argument for source compatibility. Camera pixels are
        # now copied directly each frame, so a frame-hash cache is unnecessary.
        self._camera_cache_size = camera_cache_size
        self._model_cache_size = model_cache_size
        self._model_cache: OrderedDict[tuple[object, ...], np.ndarray] = OrderedDict()
        self._text_cache: OrderedDict[tuple[str, int], np.ndarray] = OrderedDict()
        self._text_cache_size = 192
        self._text_cache_hits = 0
        self._overlay_cache = OrderedDict()
        self._overlay_cache_size = 128
        self._overlay_cache_hits = 0
        self._resolved_font = self._resolve_font(font_path)
        self.chinese = self._resolved_font is not None
        self._fonts: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
        self._base_bgr = cv2.cvtColor(self._build_background(), cv2.COLOR_RGB2BGR)
        self._empty_model = self._empty_model_panel(MODEL_VIEW[2], MODEL_VIEW[3])
        self.timings: dict[str, float | int | bool] = {}

    @staticmethod
    def _resolve_font(font_path: str | Path | None) -> Path | None:
        if font_path is not None:
            path = Path(font_path).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"CJK font not found: {path}")
            return path
        filename = Path("assets") / "fonts" / "NotoSansCJKsc-Regular.otf"
        for parent in Path(__file__).resolve().parents:
            candidate = parent / filename
            if candidate.is_file():
                return candidate
        return None

    def _font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        if size not in self._fonts:
            if self._resolved_font is not None:
                self._fonts[size] = ImageFont.truetype(str(self._resolved_font), size=size)
            else:
                try:self._fonts[size] = ImageFont.load_default(size=size)
                except TypeError:
                    # Older Pillow's bitmap default cannot render punctuation.
                    # Use the board's existing scalable Latin font; English mode
                    # stays explicit because this is not a CJK font.
                    fallback = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
                    if fallback.is_file():
                        self._fonts[size] = ImageFont.truetype(str(fallback), size=size)
                    else:
                        self._fonts[size] = ImageFont.load_default()
        return self._fonts[size]

    def _label(self, key: str) -> str:
        labels = {
            "brand": ("结构光三维扫描", "STRUCTURED LIGHT 3D SCANNER"),
            "subtitle": ("扫描工作台", "SCAN WORKSPACE"),
            "camera": ("相机画面", "CAMERA FRAME"),
            "gray": ("原始灰度 · 752 × 480", "RAW GRAYSCALE | 752 x 480"),
            "model": ("三维点云模型", "3D POINT CLOUD"),
            "dimensions": ("尺寸与公差", "DIMENSIONS & TOLERANCE"),
            "progress": ("扫描进度", "SCAN PROGRESS"),
            "quality": ("质量", "QUALITY"),
            "camera_rate": ("相机", "CAMERA"),
            "model_rate": ("模型", "MODEL"),
            "axis": ("转轴", "AXIS"),
            "timing": ("时序", "TIMING"),
            "volume": ("体积标定", "VOLUME"),
            "wait_points": ("等待有效点云", "WAITING FOR VALID POINTS"),
            "no_dimensions": ("暂无尺寸结果", "NO DIMENSION RESULTS"),
            "awaiting_data": ("等待有效测量", "AWAITING VALID DATA"),
            "not_validated": ("未验证", "NOT VALIDATED"),
            "unavailable": ("不可用", "UNAVAILABLE"),
            "available": ("可用", "AVAILABLE"),
            "check_device": ("检查器件通路", "CHECK DEVICE PATH"),
            "laser_ok": ("激光器件已连接", "LASER DEVICE READY"),
            "laser_unknown": ("激光器件状态未知", "LASER DEVICE UNKNOWN"),
            "angle": ("角度", "ANGLE"),
            "points": ("点", "POINTS"),
            "fps": ("帧/秒", "FPS"),
            "tolerance": ("公差", "TOL"),
            "truth": ("标准", "TRUTH"),
            "debug": ("调试", "DEBUG"),
            "live": ("实时", "LIVE"),
            "replay": ("回放", "REPLAY"),
            "calibration": ("标定", "CALIBRATION"),
            "display_rate": ("界面", "DISPLAY"),
        }
        zh, en = labels[key]
        return zh if self.chinese else en

    def _build_background(self) -> np.ndarray:
        ratio = np.linspace(0.0, 1.0, CANVAS_H, dtype=np.float32)[:, None, None]
        top, bottom = np.asarray(_BG_TOP, dtype=np.float32), np.asarray(_BG_BOTTOM, dtype=np.float32)
        rows = np.round(top * (1.0 - ratio) + bottom * ratio).astype(np.uint8)
        rgb = np.broadcast_to(rows, (CANVAS_H, CANVAS_W, 3)).copy()
        image = Image.fromarray(rgb)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, CANVAS_W, 82), fill=(10, 24, 37))
        draw.rectangle((0, 80, CANVAS_W, 82), fill=_EDGE)
        draw.rounded_rectangle((16, 94, 784, 621), radius=12, fill=_CARD, outline=_EDGE, width=1)
        draw.rounded_rectangle((800, 94, 1264, 621), radius=12, fill=_CARD, outline=_EDGE, width=1)
        for rect in METRIC_CARDS:
            x, y, w, h = rect
            draw.rounded_rectangle((x, y, x + w, y + h), radius=9, fill=_CARD_ALT, outline=_EDGE, width=1)
        x, y, w, h = CAMERA_IMAGE
        draw.rounded_rectangle((x - 1, y - 1, x + w, y + h), radius=4, fill=(5, 11, 18), outline=(58, 78, 95), width=1)
        x, y, w, h = MODEL_VIEW
        draw.rounded_rectangle((x - 1, y - 1, x + w, y + h), radius=4, fill=(15, 25, 37), outline=(58, 78, 95), width=1)
        # Static progress track, status row and card separators are part of the
        # cached template; only the filled portion changes per frame.
        draw.rounded_rectangle((432, 60, 690, 67), radius=3, fill=(34, 51, 66))
        draw.line((24, 618, 1256, 618), fill=_EDGE, width=1)
        self._draw_static_text(draw)
        return np.asarray(image, dtype=np.uint8)

    def _draw_static_text(self, draw: ImageDraw.ImageDraw) -> None:
        draw.rounded_rectangle((24, 22, 54, 52), radius=8, fill=(21, 63, 76))
        draw.ellipse((33, 31, 45, 43), fill=_TEAL)
        draw.text((68, 18), self._label("brand"), font=self._font(21), fill=_TEXT)
        draw.text((69, 48), self._label("subtitle"), font=self._font(11), fill=_MUTED)
        draw.text((34, 99), self._label("camera"), font=self._font(13), fill=_TEXT)
        draw.text((615, 101), self._label("gray"), font=self._font(10), fill=_MUTED)
        draw.text((818, 99), self._label("model"), font=self._font(13), fill=_TEXT)
        draw.text((35, 620), self._label("dimensions"), font=self._font(11), fill=_MUTED)

    def _text_mask(self, text: str, size: int) -> np.ndarray:
        key = (text, size)
        cached = self._text_cache.get(key)
        if cached is not None:
            self._text_cache.move_to_end(key)
            self._text_cache_hits += 1
            return cached
        font = self._font(size)
        temp = Image.new("L", (1, 1), 0)
        try:
            box = ImageDraw.Draw(temp).textbbox((0, 0), text, font=font)
        except ValueError:
            # Board Pillow 8 uses a bitmap fallback when CJK fonts are absent;
            # its ImageDraw.textbbox only accepts FreeType. Preserve the same
            # actual glyph metrics without requiring a new font installation.
            width, height = font.getsize(text)
            box = (0, 0, width, height)
        width, height = max(1, box[2] - box[0]), max(1, box[3] - box[1])
        glyph = Image.new("L", (width, height), 0)
        ImageDraw.Draw(glyph).text((-box[0], -box[1]), text, font=font, fill=255)
        mask = np.asarray(glyph, dtype=np.uint8).copy()
        self._text_cache[key] = mask
        while len(self._text_cache) > self._text_cache_size:
            self._text_cache.popitem(last=False)
        return mask

    def _model_tile(self, points: np.ndarray, mesh: SurfaceMesh | None, cloud_revision: object,
                    yaw_deg: float) -> tuple[np.ndarray, bool]:
        if not len(points) and (mesh is None or not len(mesh.vertices)):
            return self._empty_model, True
        # A content hash allocated and scanned the full cloud on every frame.
        # Revisions are the caller's explicit invalidation token; without one,
        # rendering is uncached so changed data can never be hidden.
        if cloud_revision is None:
            return self._render_model(points, mesh, float(yaw_deg)), False
        key = (repr(cloud_revision), float(yaw_deg), id(points) if len(points) else None,
               id(mesh) if mesh is not None and len(mesh.vertices) else None)
        if key in self._model_cache:
            self._model_cache.move_to_end(key)
            return self._model_cache[key], True
        panel = self._render_model(points, mesh, float(yaw_deg))
        self._model_cache[key] = panel
        self._model_cache.move_to_end(key)
        while len(self._model_cache) > self._model_cache_size:
            self._model_cache.popitem(last=False)
        return panel, False

    def _render_model(self, points: np.ndarray, mesh: SurfaceMesh | None, yaw_deg: float) -> np.ndarray:
        _, _, width, height = MODEL_VIEW
        if mesh is not None and len(mesh.vertices):
            self._point_raster_receipt['point_raster_backend'] = 'mesh'
            panel = draw_surface_panel(mesh, width, height, yaw_deg=yaw_deg,
                                       elevation_deg=25.0, background=(37, 25, 15))[0]
        elif len(points):
            panel = _draw_point_panel(points, width, height, yaw_deg,
                raster=self.point_raster,raster_receipt=self._point_raster_receipt)[0]
        else:
            return self._empty_model
        return panel

    def _empty_model_panel(self, width: int, height: int) -> np.ndarray:
        panel = np.empty((height, width, 3), dtype=np.uint8)
        panel[:] = (37, 25, 15)
        cv2.line(panel, (width // 2 - 34, height // 2 - 12), (width // 2 + 34, height // 2 - 12),
                 (91, 75, 52), 1, cv2.LINE_AA)
        if self.chinese:
            label = self._label("wait_points")
            box = self._text_mask(label, 14)
            self._put_cached_mask(panel, box, (width - box.shape[1]) // 2,
                                  (height - box.shape[0]) // 2 + 8, (177, 192, 205))
        else:
            cv2.putText(panel, self._label("wait_points"), (width // 2 - 114, height // 2 + 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.52, (177, 192, 205), 1, cv2.LINE_AA)
        return panel

    @staticmethod
    def _put_cached_mask(canvas: np.ndarray, mask: np.ndarray, x: int, y: int,
                         color_rgb: tuple[int, int, int]) -> None:
        height, width = mask.shape
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(canvas.shape[1], x + width), min(canvas.shape[0], y + height)
        if x0 >= x1 or y0 >= y1:
            return
        alpha = mask[y0 - y:y1 - y, x0 - x:x1 - x].astype(np.uint16)[..., None]
        target = canvas[y0:y1, x0:x1]
        inverse = 255 - alpha
        color_bgr = np.asarray(color_rgb[::-1], dtype=np.uint16)
        target[:] = ((target.astype(np.uint16) * inverse + color_bgr * alpha + 127) // 255).astype(np.uint8)

    def _text_size(self, text: str, size: int) -> tuple[int, int]:
        if not text.isascii():
            mask = self._text_mask(text, size)
            return mask.shape[1], mask.shape[0]
        scale = max(0.38, size / 22.0)
        (width, height), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        return width, height

    def _put_text(self, canvas: np.ndarray, text: str, x: int, y: int, *, size: int = 12,
                  color: tuple[int, int, int] = _TEXT) -> None:
        if not text:
            return
        bounds = self._text_bounds(text, x, y, size)
        self._cached_overlay(canvas, ('text', text, x, y, size, color), bounds,
            lambda: self._paint_text(canvas, text, x, y, size=size, color=color))

    def _text_bounds(self, text, x, y, size):
        if text.isascii():
            scale = max(0.38, size / 22.0)
            (width, height), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX,
                                                      scale, 1 if size < 18 else 2)
            bottom = y + max(size - 2, 1)
            return (x-3, bottom-height-3, x+width+4, bottom+baseline+4)
        else:
            height, width = self._text_mask(text, size).shape
            return (x, y, x+width, y+height)

    def _cached_overlay(self, canvas, key, bounds, paint):
        """Reuse a small overlay only over pixel-identical underlay.

        Cache entries own both before/after pixels. This preserves antialiasing
        and overlapping labels across status changes without caching a whole
        camera canvas or retaining caller-owned image buffers.
        """
        x0, y0, x1, y1 = bounds
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(canvas.shape[1], x1), min(canvas.shape[0], y1)
        if x0 >= x1 or y0 >= y1:
            paint()
            return
        target = canvas[y0:y1, x0:x1]
        cached = self._overlay_cache.get(key)
        if cached is not None and np.array_equal(target, cached[0]):
            target[:] = cached[1]
            self._overlay_cache.move_to_end(key)
            self._overlay_cache_hits += 1
            return
        before = target.copy()
        paint()
        self._overlay_cache[key] = (before, target.copy())
        self._overlay_cache.move_to_end(key)
        while len(self._overlay_cache) > self._overlay_cache_size:
            self._overlay_cache.popitem(last=False)

    def _paint_text(self, canvas, text, x, y, *, size, color):
        if text.isascii():
            scale = max(0.38, size / 22.0)
            thickness = 1 if size < 18 else 2
            cv2.putText(canvas, text, (x, y + max(size - 2, 1)), cv2.FONT_HERSHEY_SIMPLEX,
                        scale, tuple(int(v) for v in color[::-1]), thickness, cv2.LINE_AA)
        else:
            self._put_cached_mask(canvas, self._text_mask(text, size), x, y, color)

    @staticmethod
    def _rounded_rect(canvas: np.ndarray, box: tuple[int, int, int, int], color_rgb: tuple[int, int, int],
                      radius: int = 8) -> None:
        left, top, right, bottom = box
        color_bgr = tuple(int(v) for v in color_rgb[::-1])
        radius = max(0, min(radius, (right - left) // 2, (bottom - top) // 2))
        if radius == 0:
            cv2.rectangle(canvas, (left, top), (right, bottom), color_bgr, -1)
            return
        cv2.rectangle(canvas, (left + radius, top), (right - radius, bottom), color_bgr, -1)
        cv2.rectangle(canvas, (left, top + radius), (right, bottom - radius), color_bgr, -1)
        for cx, cy in ((left + radius, top + radius), (right - radius, top + radius),
                       (left + radius, bottom - radius), (right - radius, bottom - radius)):
            cv2.circle(canvas, (cx, cy), radius, color_bgr, -1, cv2.LINE_AA)

    def _pill(self, canvas: np.ndarray, box: tuple[int, int, int, int], text: str,
              fill_rgb: tuple[int, int, int], foreground_rgb: tuple[int, int, int], size: int = 10) -> None:
        left, top, right, bottom = box
        width, height = self._text_size(text, size)
        tx = left + max(2, (right-left-width)//2)
        ty = top + max(0, (bottom-top-height)//2)
        x0, y0, x1, y1 = self._text_bounds(text, tx, ty, size)
        self._cached_overlay(canvas, ('pill', box, text, fill_rgb, foreground_rgb, size),
            (min(left-2, x0), min(top-2, y0), max(right+3, x1), max(bottom+3, y1)),
            lambda: self._paint_pill(canvas, box, text, fill_rgb, foreground_rgb, size))

    def _paint_pill(self, canvas, box, text, fill_rgb, foreground_rgb, size):
        self._rounded_rect(canvas, box, fill_rgb, radius=min(9, (box[3] - box[1]) // 2))
        left, top, right, bottom = box
        width, height = self._text_size(text, size)
        self._put_text(canvas, text, left + max(2, (right - left - width) // 2),
                       top + max(0, (bottom - top - height) // 2), size=size, color=foreground_rgb)

    @staticmethod
    def _metric_presentation(card: MeasurementCard, status: DashboardStatus) -> tuple[str, str, str, str]:
        """Return display name/value/tolerance/result while enforcing validity gates."""
        calibrated = (status.axis_calibrated and status.timing_calibrated and status.volume_calibrated
                      and status.laser_available is not False)
        value_valid = (card.valid and calibrated and card.value_mm is not None
                       and math.isfinite(float(card.value_mm)))
        value = f"{float(card.value_mm):.2f} mm" if value_valid else "-"
        tolerance = f"+/- {float(card.tolerance_mm):.2f} mm" if card.tolerance_mm is not None else "-"
        result = (card.result or "").strip().upper()
        if status.laser_available is False:
            decision = "CHECK DEVICE PATH"
        elif not calibrated:
            decision = "NOT VALIDATED"
        elif not value_valid:
            decision = "UNAVAILABLE"
        elif result in {"PASS", "FAIL"}:
            has_comparison_basis = (card.truth_mm is not None and card.tolerance_mm is not None
                                    and isinstance(card.truth_reference, str) and card.truth_reference.strip())
            if has_comparison_basis:
                error = abs(float(card.value_mm) - float(card.truth_mm))
                epsilon = 8 * np.spacing(max(abs(float(card.truth_mm)), abs(float(card.value_mm)), 1.0))
                decision = "PASS" if error <= float(card.tolerance_mm) + epsilon else "FAIL"
            else:
                decision = "NOT VALIDATED"
        elif result and result.isascii():
            decision = result[:20]
        else:
            decision = "AVAILABLE"
        name = card.name.strip()
        return name[:24], value, tolerance, decision

    def _localized_metric(self, text: str) -> str:
        if self.chinese:
            return {
                "NOT VALIDATED": "未验证", "UNAVAILABLE": "不可用", "AVAILABLE": "可用",
                "CHECK DEVICE PATH": "检查器件通路", "PASS": "通过", "FAIL": "未通过",
            }.get(text, text)
        return text

    def render(self, raw: bytes | bytearray | memoryview | np.ndarray, points: object | None,
               *, status: DashboardStatus = DashboardStatus(),
               measurements: Sequence[MeasurementCard] = (), cloud_revision: object = None,
               mesh: SurfaceMesh | None = None, yaw_deg: float = -32.0) -> np.ndarray:
        """Render one raw grayscale camera frame and the current measured cloud."""
        started = time.perf_counter()
        if not isinstance(status, DashboardStatus):
            raise TypeError("status must be DashboardStatus")
        if isinstance(yaw_deg, bool) or not isinstance(yaw_deg, (int, float)) or not math.isfinite(yaw_deg):
            raise ValueError("yaw_deg must be finite")
        if mesh is not None and not isinstance(mesh, SurfaceMesh):
            raise TypeError("mesh must be SurfaceMesh or None")
        cards = tuple(measurements)
        if any(not isinstance(card, MeasurementCard) for card in cards):
            raise TypeError("measurements must contain MeasurementCard values")
        frame = _camera_frame(raw)
        points_array = _normalise_points(points)

        stage = time.perf_counter()
        canvas = np.empty_like(self._base_bgr)
        cx, cy, cw, ch = CAMERA_IMAGE
        mx, my, mw, mh = MODEL_VIEW
        # Both panels fill their complete rectangles below. Copy only their
        # static complement; every returned canvas still owns fresh storage.
        canvas[:cy] = self._base_bgr[:cy]
        canvas[cy + ch:] = self._base_bgr[cy + ch:]
        canvas[cy:cy + ch, :cx] = self._base_bgr[cy:cy + ch, :cx]
        canvas[cy:cy + ch, cx + cw:mx] = self._base_bgr[cy:cy + ch, cx + cw:mx]
        canvas[cy:cy + ch, mx + mw:] = self._base_bgr[cy:cy + ch, mx + mw:]
        background_copy_ms = (time.perf_counter() - stage) * 1000.0

        stage = time.perf_counter()
        camera_view = canvas[cy:cy + ch, cx:cx + cw]
        cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR, dst=camera_view)
        camera_copy_ms = (time.perf_counter() - stage) * 1000.0

        stage = time.perf_counter()
        self._point_raster_receipt = dict(point_raster_requested=
            'native' if self.point_raster is not None else 'python',
            point_raster_backend='model_cache',point_raster_fallback=None)
        model, model_hit = self._model_tile(points_array, mesh, cloud_revision, float(yaw_deg))
        canvas[my:my + mh, mx:mx + mw] = model
        model_ms = (time.perf_counter() - stage) * 1000.0

        text_hits_before = self._text_cache_hits
        overlay_hits_before = self._overlay_cache_hits
        stage = time.perf_counter()

        mode = _mode_label(status.mode, status.source, chinese=self.chinese)
        self._put_text(canvas, mode[:22], 270, 27, size=13, color=_TEAL)
        if status.frame_number is not None:
            self._put_text(canvas, f"F{status.frame_number}", 355, 29, size=10, color=_MUTED)
        source = _source_label(status.source, chinese=self.chinese)
        self._pill(canvas, (1111, 23, 1255, 54), source, (24, 53, 68), _TEAL, 10)

        self._put_text(canvas, self._label("progress"), 432, 10, size=10, color=_MUTED)
        angle = f"{status.angle_deg:.1f} deg" if status.angle_deg is not None else "-"
        angle_label = self._label("angle")
        self._put_text(canvas, angle_label, 432, 32, size=12, color=_TEXT)
        self._put_text(canvas, angle, 478, 32, size=12, color=_TEXT)
        progress = status.progress_pct
        progress_text = f"{progress:.0f}%" if progress is not None else "-"
        self._put_text(canvas, progress_text, 653, 32, size=12, color=_TEXT)
        if progress is not None and progress > 0:
            end_x = 432 + int(258 * progress / 100.0)
            cv2.rectangle(canvas, (432, 60), (end_x, 67), tuple(v for v in _TEAL[::-1]), -1)

        quality_key = status.quality.strip().lower()
        quality_map = {
            "good": (("良好", "GOOD"), _GREEN),
            "weak": (("偏弱", "WEAK"), _AMBER),
            "incomplete": (("不完整", "INCOMPLETE"), _AMBER),
            "ambiguous": (("有歧义", "AMBIGUOUS"), _AMBER),
            "saturated": (("已饱和", "SATURATED"), _RED),
            "unvalidated": (("待验证", "UNVALIDATED"), _MUTED),
        }
        (quality_zh, quality_en), quality_color = quality_map.get(
            quality_key, (("未知", "UNKNOWN"), _MUTED))
        self._put_text(canvas, self._label("quality"), 727, 10, size=10, color=_MUTED)
        self._pill(canvas, (727, 33, 875, 62), quality_zh if self.chinese else quality_en,
                   tuple(int(v * 0.22) for v in quality_color), quality_color, 10)

        camera_label = self._label("camera_rate")
        camera_label_x = 405
        camera_label_width, _ = self._text_size(camera_label, 9)
        self._put_text(canvas, camera_label, camera_label_x, 101, size=9, color=_MUTED)
        camera_fps = f"{status.camera_fps:.1f} FPS" if status.camera_fps is not None else "- FPS"
        self._put_text(canvas, camera_fps, camera_label_x + camera_label_width + 5, 101, size=9, color=_MUTED)

        display_label = self._label("display_rate")
        display_label_x = 525
        display_label_width, _ = self._text_size(display_label, 9)
        self._put_text(canvas, display_label, display_label_x, 101, size=9, color=_MUTED)
        display_fps = f"{status.display_fps:.1f} FPS" if status.display_fps is not None else "- FPS"
        self._put_text(canvas, display_fps, display_label_x + display_label_width + 5, 101, size=9, color=_MUTED)

        point_count = len(mesh.vertices) if mesh is not None else len(points_array)
        point_count_text = f"{point_count:,}"
        point_count_width, _ = self._text_size(point_count_text, 9)
        self._put_text(canvas, point_count_text, 1080 - point_count_width, 101, size=9, color=_MUTED)
        self._put_text(canvas, self._label("points"), 1084, 101, size=9, color=_MUTED)
        has_model = point_count > 0
        if has_model:
            model_label = self._label("model_rate")
            self._put_text(canvas, model_label, 1110, 101, size=9, color=_MUTED)
            if status.model_fps is not None:
                self._put_text(canvas, f"{status.model_fps:.1f} FPS", 1150, 101, size=9, color=_MUTED)

        state_items = (("AXIS", status.axis_calibrated), ("TIME", status.timing_calibrated),
                       ("VOLUME", status.volume_calibrated))
        self._put_text(canvas, self._label("calibration"), 240, 621, size=9, color=_MUTED)
        for index, (label, valid) in enumerate(state_items):
            if self.chinese:
                localized = {"AXIS": "轴", "TIME": "时序", "VOLUME": "体积"}[label]
                value = f"{localized}✓" if valid else f"{localized}—"
            else:
                value = f"{label} OK" if valid else f"{label} —"
            left = 323 + index * 91
            self._pill(canvas, (left, 619, left + 84, 640), value,
                       (24, 60, 57) if valid else (48, 47, 43), _GREEN if valid else _AMBER, 8)

        if status.laser_available is False:
            warning, warning_color = self._label("check_device"), _RED
            warning_fill = (55, 31, 39)
        elif status.laser_available is True:
            warning, warning_color = self._label("laser_ok"), _GREEN
            warning_fill = (22, 48, 44)
        else:
            warning, warning_color = self._label("laser_unknown"), _MUTED
            warning_fill = (31, 42, 51)
        self._pill(canvas, (918, 619, 1255, 640), warning, warning_fill, warning_color, 9)

        if self.debug and status.debug_details:
            detail = " | ".join(item for item in status.debug_details if item.isascii())[:100]
            if detail:
                self._put_text(canvas, f"DEBUG: {detail}", 35, 609, size=8, color=_AMBER)

        for index, rect in enumerate(METRIC_CARDS):
            x, y, _, _ = rect
            if index < len(cards):
                name, value, tolerance, decision = self._metric_presentation(cards[index], status)
                if not self.chinese and not name.isascii():
                    name = "DIMENSION"
                decision = self._localized_metric(decision)
                if self.chinese:
                    value = "—" if value == "-" else value
                    tolerance = "—" if tolerance == "-" else tolerance
                self._put_text(canvas, name, x + 12, y + 6, size=10, color=_MUTED)
                self._put_text(canvas, value, x + 12, y + 25, size=18,
                               color=_TEXT if value not in {"-", "—"} else _MUTED)
                if tolerance not in {"-", "—"}:
                    tolerance_label = self._label("tolerance")
                    tolerance_label_width, _ = self._text_size(tolerance_label, 9)
                    self._put_text(canvas, tolerance_label, x + 12, y + 49, size=9, color=_MUTED)
                    self._put_text(canvas, tolerance, x + 16 + tolerance_label_width, y + 49,
                                   size=9, color=_MUTED)
                if decision in {"PASS", "通过"}:
                    color = _GREEN
                elif decision in {"FAIL", "未通过", "CHECK DEVICE PATH", "检查器件通路"}:
                    color = _RED
                elif decision in {"NOT VALIDATED", "未验证"}:
                    color = _AMBER
                else:
                    color = _MUTED
                self._pill(canvas, (x + 187, y + 21, x + 282, y + 45), decision,
                           (26, 43, 54), color, 8)
            elif not cards and index == 0:
                self._put_text(canvas, self._label("no_dimensions"), x + 12, y + 10, size=11, color=_TEXT)
                self._put_text(canvas, self._label("awaiting_data"), x + 12, y + 39, size=9, color=_MUTED)
            else:
                self._put_text(canvas, self._label("awaiting_data"), x + 12, y + 23, size=9, color=_MUTED)

        dynamic_ui_ms = (time.perf_counter() - stage) * 1000.0
        total_ms = (time.perf_counter() - started) * 1000.0
        self.timings = {
            "camera_cache_hit": False,
            "model_cache_hit": model_hit,
            "cache_hits": int(model_hit),
            "text_cache_hits": self._text_cache_hits - text_hits_before,
            "overlay_cache_hits": self._overlay_cache_hits - overlay_hits_before,
            "background_copy_ms": background_copy_ms,
            "camera_copy_ms": camera_copy_ms,
            "model_ms": model_ms,
            "dynamic_ui_ms": dynamic_ui_ms,
            "compose_ms": background_copy_ms + camera_copy_ms + model_ms + dynamic_ui_ms,
            "render_ms": total_ms,
            **self._point_raster_receipt,
        }
        return canvas

__all__ = ["CANVAS_H", "CANVAS_W", "CAMERA_IMAGE", "MODEL_VIEW", "METRIC_CARDS",
           "DashboardRenderer", "DashboardStatus", "MeasurementCard"]
