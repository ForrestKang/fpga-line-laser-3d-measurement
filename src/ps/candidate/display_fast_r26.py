"""Display-only cadence, bounded sampling and cached-line helpers for R26.

Everything here is presentation policy only.  It never reads measurement
admission, never touches the full point cloud on disk, and never changes the
submit/ACK accounting owned by :mod:`candidate.display_process_r25`.

The helpers are pure Python/NumPy (the line cache imports OpenCV lazily) so
they can be exercised on a host without a board, a shared-memory child or an
HDMI owner.
"""
from collections import OrderedDict
import math

import numpy as np


MAX_MIN_INTERVAL_S = 10.0
DEFAULT_SAMPLE_CAP = 16000
MAX_SAMPLE_CAP = 24000


def validate_cadence(value, *, name='display_min_interval_s'):
    """Return a finite nonnegative seconds cadence in ``0..10``.

    ``None`` and the historical default ``0.0`` both mean "render every
    pending image as soon as possible".  A positive value means the child
    waits until ``last_render_start + value`` while newer submissions are
    allowed to replace the pending slot.
    """
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name + ' must be a finite number of seconds')
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= MAX_MIN_INTERVAL_S:
        raise ValueError(name + ' must be finite and within 0..10 seconds')
    return value


def validate_sample_cap(value, visible_points):
    """Bound the model-panel sample independently of the shared cloud size."""
    if type(visible_points) is not int or not 1 <= visible_points <= MAX_SAMPLE_CAP:
        raise ValueError('preview scratch budget exceeded')
    if value is None:
        value = min(DEFAULT_SAMPLE_CAP, visible_points)
    if isinstance(value, bool) or type(value) is not int:
        raise ValueError('display_sample_cap must be an integer')
    if not 1 <= value <= visible_points:
        raise ValueError('display_sample_cap must be within 1..visible_points')
    return value


def sampling_plan(count, cap):
    """Return ``(stride, sampled_points)`` for an even bounded sample.

    ``cloud[:count:stride]`` keeps the historical display sampling order and
    never materialises the full history.  ``sampled_points <= cap`` is checked
    explicitly so a caller cannot quietly exceed the preview scratch budget.
    """
    if type(count) is not int or count < 0:
        raise ValueError('nonnegative integer point count required')
    if type(cap) is not int or not 1 <= cap <= MAX_SAMPLE_CAP:
        raise ValueError('sample cap must be 1..24000')
    stride = max(1, (count + cap - 1) // cap)
    sampled = 0 if count == 0 else (count + stride - 1) // stride
    if sampled > cap:
        raise ValueError('bounded sampling plan exceeded its cap')
    return stride, sampled


def sample_points_bounded(cloud, count, cap):
    """Copy only the bounded strided display sample, never the full cloud."""
    array = np.asarray(cloud)
    if array.ndim != 2 or array.shape[1] != 3:
        raise ValueError('Nx3 shared cloud required')
    if type(count) is not int or not 0 <= count <= len(array):
        raise ValueError('point count outside shared cloud')
    stride, sampled = sampling_plan(count, cap)
    if not sampled:
        return np.empty((0, 3), dtype=array.dtype), stride
    sample = array[:count:stride].copy()
    if len(sample) != sampled:
        raise ValueError('bounded sample size mismatch')
    return sample, stride


def _finite_float(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(float(value)))


def percentile(values, fraction):
    """Linear-interpolation percentile used only for display evidence."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if not 0.0 <= fraction <= 1.0:
        raise ValueError('fraction must be 0..1')
    position = fraction * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def performance_summary(records):
    """Per-ACK render/owner/age distribution; never an ACK/submit ratio.

    ``coalesced_total`` is the child's count of submissions skipped between
    rendered frames.  It is independent evidence for the parent's ``dropped``
    counter and is not used as a frame rate.
    """
    rows = [row for row in records or () if isinstance(row, dict)]
    render = [float(row['render_s']) for row in rows if _finite_float(row.get('render_s'))]
    render_cpu = [float(row['render_thread_cpu_s']) for row in rows
                  if _finite_float(row.get('render_thread_cpu_s'))]
    owner = [float(row['owner_s']) for row in rows if _finite_float(row.get('owner_s'))]
    age = [float(row['submit_to_ack_s']) for row in rows
           if _finite_float(row.get('submit_to_ack_s'))]
    sampled = [int(row['sampled_points']) for row in rows
               if type(row.get('sampled_points')) is int and row['sampled_points'] >= 0]
    sampled_bytes = [int(row['sampled_nbytes']) for row in rows
                     if type(row.get('sampled_nbytes')) is int and row['sampled_nbytes'] >= 0]
    strides = [int(row['sampling_stride']) for row in rows
               if type(row.get('sampling_stride')) is int and row['sampling_stride'] >= 1]
    coalesced = [int(row['coalesced']) for row in rows
                 if type(row.get('coalesced')) is int and row['coalesced'] >= 0]
    cadence = [float(row['cadence_wait_s']) for row in rows
               if _finite_float(row.get('cadence_wait_s'))]
    return dict(
        frames=len(rows),
        render_s_p50=percentile(render, .5), render_s_p95=percentile(render, .95),
        render_s_max=max(render) if render else None,
        render_thread_cpu_s_p50=percentile(render_cpu, .5),
        render_thread_cpu_s_p95=percentile(render_cpu, .95),
        owner_s_p50=percentile(owner, .5), owner_s_p95=percentile(owner, .95),
        submit_to_ack_p50_s=percentile(age, .5), submit_to_ack_p95_s=percentile(age, .95),
        sampled_points_max=max(sampled) if sampled else None,
        sampled_nbytes_max=max(sampled_bytes) if sampled_bytes else None,
        sampling_stride_max=max(strides) if strides else None,
        coalesced_total=sum(coalesced) if coalesced else 0,
        cadence_wait_s_total=sum(cadence) if cadence else 0.,
        scope='PER_ACK_RENDER_OWNER_AND_AGE_NOT_ACK_RATIO')


class TextLineCache:
    """Bounded exact-pixel cache for text drawn over a restored static strip.

    A cached entry owns the complete post-draw BGR tile (including its static
    background), so replaying it cannot leave a longer previous string behind.
    Callers must restore the strip from their static template before drawing;
    the process renderer does that for every dynamic band on every frame.

    ``cache=False`` draws without an entry, which is used for per-frame strings
    (frame ids, counters) so the bounded cache never grows with run length.
    """

    def __init__(self, max_entries=48):
        if type(max_entries) is not int or not 1 <= max_entries <= 512:
            raise ValueError('text cache size must be 1..512')
        self.max_entries = max_entries
        self._entries = OrderedDict()
        self.hits = 0
        self.misses = 0

    def draw(self, canvas, text, origin, *, font, scale, color, thickness=1,
             line_type=None, cache=True):
        import cv2
        if not isinstance(canvas, np.ndarray) or canvas.dtype != np.uint8 or canvas.ndim != 3:
            raise ValueError('writable uint8 BGR canvas required')
        if not canvas.flags.writeable or not canvas.flags.c_contiguous:
            raise ValueError('writable contiguous canvas required')
        if not isinstance(text, str) or not text:
            return False
        if line_type is None:
            line_type = cv2.LINE_AA
        x, y = int(origin[0]), int(origin[1])
        bgr = tuple(int(value) for value in color)
        height, width = canvas.shape[:2]
        if cache:
            key = (text, x, y, float(scale), bgr, int(thickness), int(line_type), int(font))
            entry = self._entries.get(key)
            if entry is not None:
                x0, y0, tile = entry
                canvas[y0:y0 + tile.shape[0], x0:x0 + tile.shape[1]] = tile
                self._entries.move_to_end(key)
                self.hits += 1
                return True
            (text_width, text_height), baseline = cv2.getTextSize(
                text, font, float(scale), int(thickness))
            x0 = max(0, x - 6)
            y0 = max(0, y - int(text_height) - 8)
            x1 = min(width, x + int(text_width) + 6)
            y1 = min(height, y + int(baseline) + 8)
            if x0 >= x1 or y0 >= y1:
                return False
        cv2.putText(canvas, text, (x, y), int(font), float(scale), bgr,
                    int(thickness), int(line_type))
        if cache:
            self._entries[key] = (x0, y0, canvas[y0:y1, x0:x1].copy())
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            self.misses += 1
        return False

    def clear(self):
        self._entries.clear()

    def summary(self):
        return dict(entries=len(self._entries), max_entries=self.max_entries,
                    hits=self.hits, misses=self.misses)


__all__ = ['DEFAULT_SAMPLE_CAP', 'MAX_MIN_INTERVAL_S', 'MAX_SAMPLE_CAP',
           'TextLineCache', 'percentile', 'performance_summary',
           'sample_points_bounded', 'sampling_plan', 'validate_cadence',
           'validate_sample_cap']
