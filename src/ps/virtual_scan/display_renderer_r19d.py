"""Reuse static canvas while keeping native Raw/model pixels identical.

R26 additions are display-only:

* the 448x480 point-panel grid/axes chrome is rasterised once and copied,
  instead of redrawing 18 anti-aliased grid lines and three arrowed axes per
  ACK;
* static text bands are restorable from the template, so the PFO3 renderer can
  skip the core dashboard's dynamic text it would immediately overwrite;
* every stage keeps a separate wall/CPU receipt for the board A/B.

The point raster still uses the existing manifest-bound native kernel when it
is available.  Its pure-NumPy fallback is retained byte-for-byte.
"""
import math
import time

import numpy as np

from .display_r19 import ReplayDashboardRenderer


def _fallback_panel_background(width, height, with_grid):
    """Copy of the dashboard's immutable panel template (fallback only)."""
    import cv2
    panel = np.empty((height, width, 3), dtype=np.uint8)
    panel[:] = (37, 25, 15)
    if with_grid:
        grid = (65, 52, 39)
        for x in range(38, width, 48):
            cv2.line(panel, (x, 32), (x, height - 34), grid, 1, cv2.LINE_AA)
        for y in range(32, height - 24, 48):
            cv2.line(panel, (26, y), (width - 26, y), grid, 1, cv2.LINE_AA)
    panel.setflags(write=False)
    return panel


def _panel_background(width, height, with_grid):
    try:
        from structured_light.scan3d.dashboard import _point_panel_background
        return _point_panel_background(width, height, with_grid)
    except (ImportError, AttributeError):  # older dashboard without the cache
        return _fallback_panel_background(width, height, with_grid)


def _draw_axes(panel, width, height, yaw_deg):
    """Exactly the dashboard's fixed XYZ triad drawing."""
    import cv2
    yaw = math.radians(yaw_deg)
    elevation = math.radians(25.0)
    toward = np.array([math.cos(elevation) * math.cos(yaw),
                       math.cos(elevation) * math.sin(yaw), math.sin(elevation)])
    right = np.array([-math.sin(yaw), math.cos(yaw), 0.0])
    up = np.cross(toward, right)
    origin = (52, height - 54)
    axis_colors = ((94, 127, 255), (113, 225, 135), (255, 177, 78))
    for index, unit in enumerate(np.eye(3)):
        direction = np.array([unit @ right, -(unit @ up)])
        direction /= max(float(np.linalg.norm(direction)), 1e-9)
        end = (int(origin[0] + direction[0] * 38), int(origin[1] + direction[1] * 38))
        cv2.arrowedLine(panel, origin, end, axis_colors[index], 2, cv2.LINE_AA, tipLength=0.22)
        cv2.putText(panel, "XYZ"[index], (end[0] + 3, end[1] - 3), cv2.FONT_HERSHEY_SIMPLEX,
                    0.42, axis_colors[index], 1, cv2.LINE_AA)


class ReusedCanvasRenderer(ReplayDashboardRenderer):
    def __init__(self, **kwargs):
        projection_manifest=kwargs.pop('projection_raster_manifest',None)
        super().__init__(**kwargs)
        from candidate.display_projection_r14 import optional_projection
        self._projected_raster=optional_projection(projection_manifest)
        self.canvas = self.template.copy()
        self._model_yaw_deg = -32.0
        self._build_model_chrome()
        # Regions overwritten by the derived renderer are restored from a
        # read-only template before drawing, so no previous frame can ghost.
        # The historical r19d regions are the default and remain compatible
        # with display_r21.MultiBranchRenderer.
        self._static_regions = ((slice(0, 24), slice(None)),
                                (slice(24, 83), slice(240, None)),
                                (slice(96, 121), slice(400, None)))
        self.static_source = self.template
        self.draw_core_dynamic_text = True
        self._model_receipt = {}
        self.native_finite_validation_only = False

    def _build_model_chrome(self):
        """Rasterise grid+axes once; keep the axes overlay for draw order."""
        from structured_light.scan3d.dashboard import MODEL_VIEW
        _, _, width, height = MODEL_VIEW
        chrome = _panel_background(width, height, True).copy()
        axes = chrome.copy()
        _draw_axes(axes, width, height, self._model_yaw_deg)
        changed = np.any(axes != chrome, axis=2)
        ys, xs = np.nonzero(changed)
        if len(xs):
            x0, x1 = int(xs.min()), int(xs.max()) + 1
            y0, y1 = int(ys.min()), int(ys.max()) + 1
            bbox = (x0, y0, x1, y1)
            tile = axes[y0:y1, x0:x1].copy()
            mask = changed[y0:y1, x0:x1].copy()
        else:  # pragma: no cover - the triad is always nonempty
            bbox = None
            tile = None
            mask = None
        chrome.setflags(write=False)
        if tile is not None:
            tile.setflags(write=False)
            mask.setflags(write=False)
        self._model_chrome = chrome
        self._axes_bbox = bbox
        self._axes_tile = tile
        self._axes_mask = mask
        # Only these sparse pixels change when the cached triad is composited.
        # Precompute the gather once; a boolean gather on both sides allocates
        # fresh index/colour arrays for every HDMI update.
        if mask is not None:
            self._axes_indices = np.nonzero(mask)
            self._axes_colors = tile[self._axes_indices]
            self._axes_colors.setflags(write=False)
            self._axes_count = len(self._axes_indices[0])
        else:
            self._axes_indices = None
            self._axes_colors = None
            self._axes_count = 0
        self._cache_model_projection()

    def _cache_model_projection(self):
        """Keep the original projection math, but not its per-frame work."""
        yaw = math.radians(self._model_yaw_deg)
        elevation = math.radians(25.0)
        toward = np.array([math.cos(elevation) * math.cos(yaw),
                           math.cos(elevation) * math.sin(yaw), math.sin(elevation)])
        self._projection_right = np.array([-math.sin(yaw), math.cos(yaw), 0.0])
        self._projection_up = np.cross(toward, self._projection_right)
        self._projection_yaw_deg = self._model_yaw_deg

    def _model_tile_fast(self, points, cloud_revision, *, destination=None):
        """Bounded point panel with cached chrome and the existing native raster.

        Ownership/order: points are painted over the grid, then the cached
        triad strokes are composited on top, matching the dashboard's
        draw order.  A point pixel that coincides with an anti-aliased triad
        edge is blended against the cached background instead of the point;
        this is a bounded display-only difference outside marker/camera pixels
        and is documented in tests/r26/b_display/README.md.
        """
        from structured_light.scan3d.dashboard import MODEL_VIEW
        _, _, width, height = MODEL_VIEW
        started = time.perf_counter()
        receipt = {}
        array = np.asarray(points)
        if array.ndim != 2 or array.shape[1:] != (3,):
            raise ValueError('points must be an Nx3 array')
        if array.dtype != np.float64:
            array = np.asarray(array, dtype=np.float64)
        defer_finite = self.native_finite_validation_only and self._projected_raster is not None
        if len(array) and not defer_finite and not np.isfinite(array).all():
            array = array[np.isfinite(array).all(axis=1)]
        if not len(array):
            empty = self.base_renderer._empty_model
            receipt.update(model_points=0, model_chrome_cache_hit=True,
                           model_axes_overlay_pixels=0,
                           model_copy_ms=0., model_project_ms=0.,
                           model_raster_ms=0., model_axes_ms=0.,
                           model_tile_ms=(time.perf_counter() - started) * 1000.,
                           point_raster_requested='empty',
                           point_raster_backend='empty_model',
                           point_raster_fallback=None)
            self._model_receipt = receipt
            self.base_renderer._point_raster_receipt = dict(receipt)
            return empty, True

        if self._projection_yaw_deg != self._model_yaw_deg:
            self._cache_model_projection()
        right = self._projection_right
        up = self._projection_up

        if destination is None:
            panel = self._model_chrome.copy()
        else:
            if (destination.shape != self._model_chrome.shape or destination.dtype != np.uint8
                    or not destination.flags.writeable or destination.strides[1:] != (3,1)
                    or destination.strides[0] < width*3):
                raise ValueError('writable packed BGR model destination required')
            panel = destination
            np.copyto(panel,self._model_chrome)
        copy_ms = (time.perf_counter() - started) * 1000.0

        stage = time.perf_counter()
        projection_used=False
        projection_reason=None
        if self._projected_raster is not None:
            outcome=self._projected_raster.try_project_raster(array,right,up,panel)
            projection_used=bool(outcome["used"])
            projection_reason=outcome["reason"]
        projection_attempt_ms=(time.perf_counter()-stage)*1000.
        if projection_used:
            project_ms=projection_attempt_ms
            raster_ms=0.  # Fused call already included in project_ms.
            native_used=True
            native_reason=None
            raster=self._projected_raster
        else:
            stage = time.perf_counter()
            # A native decline does not paint pixels. Restore the original
            # finite filtering before NumPy math, including an all-invalid
            # cloud. Never infer finite admission from a declined call.
            if defer_finite and not np.isfinite(array).all():
                array = array[np.isfinite(array).all(axis=1)]
                if not len(array):
                    empty, hit = self._model_tile_fast(array, cloud_revision)
                    self._model_receipt.update(projection_backend='numpy',
                        projection_fallback=projection_reason,
                        finite_validation_backend='numpy_after_native_decline')
                    self.base_renderer._point_raster_receipt=dict(self._model_receipt)
                    return empty, hit
            low, high = array.min(axis=0), array.max(axis=0)
            centered = array - (low + high) * 0.5
            projected_x, projected_y = centered @ right, centered @ up
            span_x = max(float(np.ptp(projected_x)), 1.0)
            span_y = max(float(np.ptp(projected_y)), 1.0)
            scale = min((width - 112) / span_x, (height - 106) / span_y)
            project_ms = projection_attempt_ms+(time.perf_counter() - stage) * 1000.0

            stage = time.perf_counter()
            native_used = False
            native_reason = None
            raster = getattr(self.base_renderer, 'point_raster', None)
            z = array[:, 2]
            # The XYZ bounds above already traversed every Z sample. Reuse the
            # exact extrema instead of reducing this strided column three times.
            z_min = float(low[2])
            z_span = max(float(high[2] - low[2]), 1e-9)
            if raster is not None:
                outcome = raster.try_raster(array, projected_x, projected_y, panel, scale,
                                            z_min, z_span)
                native_used = bool(outcome['used'])
                native_reason = outcome['reason']
            if not native_used:
                screen_x = np.rint(width * 0.52 + projected_x * scale).astype(np.int32)
                screen_y = np.rint(height * 0.48 - projected_y * scale).astype(np.int32)
                ratio = np.clip((z - z_min) / z_span, 0.0, 1.0)
                colors = np.column_stack((210 - 110 * ratio,
                                          150 + 75 * ratio,
                                          105 + 115 * ratio)).astype(np.uint8)
                inside = ((screen_x >= 3) & (screen_x < width - 3)
                          & (screen_y >= 3) & (screen_y < height - 3))
                xs, ys, sampled_colors = screen_x[inside], screen_y[inside], colors[inside]
                # Two vertical pixels per sample keep thin surfaces visible.
                for dy in (0, 1):
                    panel[ys + dy, xs] = sampled_colors
            raster_ms = (time.perf_counter() - stage) * 1000.0

        stage = time.perf_counter()
        overlay_pixels = 0
        if self._axes_mask is not None:
            x0, y0, x1, y1 = self._axes_bbox
            region = panel[y0:y1, x0:x1]
            region[self._axes_indices] = self._axes_colors
            overlay_pixels = self._axes_count
        axes_ms = (time.perf_counter() - stage) * 1000.0

        receipt.update(model_points=len(array), model_chrome_cache_hit=True,
                       model_axes_overlay_pixels=overlay_pixels,
                       model_copy_ms=copy_ms, model_project_ms=project_ms,
                       model_raster_ms=raster_ms, model_axes_ms=axes_ms,
                       model_tile_ms=(time.perf_counter() - started) * 1000.0,
                       point_raster_requested='native' if raster is not None else 'python',
                       point_raster_backend=('native_project_raster_r14' if projection_used else ('native' if native_used else 'python')),
                       point_raster_fallback=native_reason,
                       projection_backend=('native_fused_r14' if projection_used else 'numpy'),
                       projection_attempt_ms=projection_attempt_ms,
                       finite_validation_backend=('native_fused' if defer_finite and projection_used
                           else ('numpy_after_native_decline' if defer_finite else 'numpy_before_projection')),
                       finite_numpy_scan_elided=bool(defer_finite and projection_used),
                       projection_fallback=projection_reason)
        self._model_receipt = receipt
        self.base_renderer._point_raster_receipt = dict(receipt)
        return panel, False

    def render(self, gray, points, *, status, cloud_revision=None):
        import cv2
        from structured_light.scan3d.dashboard import CAMERA_IMAGE, MODEL_VIEW
        started = time.perf_counter()
        if gray.shape != (480, 752) or gray.dtype != np.uint8:
            raise ValueError('native gray8 frame required')
        canvas = self.canvas
        # Restore every band the derived renderer may have written, including
        # clipped long strings, from the read-only template.  This is the
        # ghost-free contract; no per-frame canvas allocation is needed.
        source = self.static_source
        for rows, columns in self._static_regions:
            canvas[rows, columns] = source[rows, columns]
        static_s = time.perf_counter() - started
        t = time.perf_counter()
        cx, cy, cw, ch = CAMERA_IMAGE
        mx, my, mw, mh = MODEL_VIEW
        # LINE_8 run-start crosses can extend two pixels beyond the Raw panel.
        # Restore that narrow static perimeter too, otherwise a later frame
        # without annotations leaves old marks outside the cvtColor destination.
        canvas[cy - 2:cy, cx - 2:cx + cw + 2] = source[cy - 2:cy, cx - 2:cx + cw + 2]
        canvas[cy + ch:cy + ch + 2, cx - 2:cx + cw + 2] = source[cy + ch:cy + ch + 2, cx - 2:cx + cw + 2]
        canvas[cy:cy + ch, cx - 2:cx] = source[cy:cy + ch, cx - 2:cx]
        canvas[cy:cy + ch, cx + cw:cx + cw + 2] = source[cy:cy + ch, cx + cw:cx + cw + 2]
        cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR, dst=canvas[cy:cy + ch, cx:cx + cw])
        camera_s = time.perf_counter() - t
        t = time.perf_counter()
        destination = canvas[my:my + mh, mx:mx + mw]
        model, hit = self._model_tile_fast(points, cloud_revision, destination=destination)
        if model is not destination:
            destination[:] = model
        model_s = time.perf_counter() - t
        t = time.perf_counter()
        core_dynamic = 'DRAWN'
        if getattr(self, 'draw_core_dynamic_text', True):
            text = (f'FRAME {status.frame_number}   ANGLE {status.angle_deg:.2f} deg'
                    f'   PROGRESS {status.progress_pct:.1f}%')
            cv2.putText(canvas, text, (250, 49), cv2.FONT_HERSHEY_SIMPLEX, .48,
                        (243, 235, 226), 1, cv2.LINE_AA)
            cv2.putText(canvas, 'SIMULATION / AXIS & TIME & VOLUME: PHYSICALLY UNVALIDATED',
                        (250, 75), cv2.FONT_HERSHEY_SIMPLEX, .40, (96, 190, 255), 1, cv2.LINE_AA)
            cv2.putText(canvas, f'VISIBLE {len(points):,} / FULL OBSERVED {self.full_cloud_points:,}',
                        (810, 113), cv2.FONT_HERSHEY_SIMPLEX, .43, (170, 153, 132), 1, cv2.LINE_AA)
        else:
            core_dynamic = 'SKIPPED_STATIC_BANDS_RESTORED_BY_DERIVED_RENDERER'
        self.timings = dict(renderer='r19d_reused_canvas', static_copy_ms=static_s * 1000,
                            camera_copy_ms=camera_s * 1000, model_ms=model_s * 1000,
                            dynamic_ui_ms=(time.perf_counter() - t) * 1000,
                            render_ms=(time.perf_counter() - started) * 1000,
                            model_cache_hit=hit, core_dynamic_text=core_dynamic,
                            **self._model_receipt)
        return canvas


def reused_canvas_renderer(config):
    return ReusedCanvasRenderer(point_raster_manifest=config.get('raster_manifest'))
