"""Bounded R33 UV markers through the existing display-only IPC contract.

This adapter never decodes a PFO3 archive. R33 records remain the sole source
of two-dimensional coordinates and PL decisions. Integer marker coordinates
are display rasterization only; geometry and persisted Q12 values are untouched.
"""
import os
import time
from dataclasses import dataclass
from types import MappingProxyType
import numpy as np
from candidate.display_pfo3_r25 import PLDisplayAnnotations, PFO3RendererR25
from .protocol import Frame

MAX_MARKERS = 512
DEFAULT_MARKERS = 384
# Match the publisher's default 8 MiB pool and active + two pending slots.
# Each slot must also fit Raw, the retained XYZ delta plus worker merge, and
# its descriptor. PL accepts at most 16 observations per each of 480 rows.
DISPLAY_RAW_BYTES = 752*480
DISPLAY_DESCRIPTOR_BYTES = 4096
MAX_FRAME_XYZ_BYTES = 480*16*3*8
MAX_DEFERRED_BYTES = (8*1024**2//3 - DISPLAY_RAW_BYTES
                      - 2*MAX_FRAME_XYZ_BYTES - DISPLAY_DESCRIPTOR_BYTES)


@dataclass(frozen=True)
class DeferredR33Annotations:
    """Frozen display source; sample only after publisher queue coalescing.

    Header scalars and ndarray descriptors are privately frozen. Observation
    bytes remain shared with the validated source, with no record-array copy.
    Data-dependent marker failures are reported by the publisher worker, which
    retains the failed snapshot through its existing failure/close contract.
    """
    frame: object
    actual_PL_execution: bool = False
    geometry_evidence_type: str = 'SIMULATION_ONLY'
    max_points: int = DEFAULT_MARKERS
    marker_backend: object = None

    def __post_init__(self):
        backend = _validate_source(self.frame, self.actual_PL_execution,
                                   self.max_points, self.marker_backend)
        if (type(self.geometry_evidence_type) is not str or
                not self.geometry_evidence_type.isascii() or len(self.geometry_evidence_type) > 96):
            raise ValueError('bounded ASCII annotation label required')
        # A read-only flag on a caller-owned writable allocation is insufficient:
        # the caller could re-enable it while a queued frame is materializing.
        base = self.frame.records
        while isinstance(base, np.ndarray):
            base = base.base
        if base is not self.frame.payload or len(self.frame.records) > 180480:
            raise ValueError('bounded bytes-backed R33 observation storage required')
        header = dict(self.frame.header)
        if any(type(key) is not str or type(value) is not int for key, value in header.items()):
            raise ValueError('immutable scalar R33 header required')
        for name in ('frame_id', 'qualified_count', 'measurement_count', 'preview_count',
                     'processing_cycles_lo', 'processing_cycles_hi'):
            if type(header.get(name)) is not int or header[name] < 0:
                raise ValueError('nonnegative display metadata required')
        records = self.frame.records.view()
        rows = np.frombuffer(b'', '<u4').reshape(0, 16)
        object.__setattr__(self, 'frame', Frame(MappingProxyType(header), records, rows, self.frame.payload))
        object.__setattr__(self, 'marker_backend', backend)

    @property
    def frame_id(self): return self.frame.header['frame_id']

    @property
    def point_count(self): return min(self.max_points, len(self.frame.records))

    @property
    def retained_bytes(self): return len(self.frame.payload)

    def materialize_compact(self):
        return annotations_from_frame(self.frame, actual_pl_execution=self.actual_PL_execution,
            geometry_evidence_type=self.geometry_evidence_type, max_points=self.max_points,
            marker_backend=self.marker_backend, compact=True)


def deferred_annotations_from_frame(frame, *, actual_pl_execution=False,
                                    geometry_evidence_type='SIMULATION_ONLY',
                                    max_points=DEFAULT_MARKERS, marker_backend=None):
    """Defer ordinary frames while keeping maximum diagnostic frames legal.

    Rare large archives use the existing eager compact sampler rather than
    keeping their full payload alive in the default 8 MiB display publisher.
    Three lazy slots (active plus two pending) fit that same pool including
    Raw, each frame's maximum XYZ delta and worker merge, and descriptors.
    Further coalesced deltas still obey the publisher's bounded backpressure.
    """
    source = DeferredR33Annotations(frame, actual_pl_execution, geometry_evidence_type,
                                    max_points, marker_backend)
    if source.retained_bytes+source.point_count*128+DISPLAY_DESCRIPTOR_BYTES > MAX_DEFERRED_BYTES:
        return source.materialize_compact()
    return source


def _quotas(sizes, limit):
    quotas = [0, 0, 0]
    remaining = min(limit, sum(sizes))
    while remaining:
        active = [i for i in range(3) if quotas[i] < sizes[i]]
        rounds = min(remaining//len(active), min(sizes[i]-quotas[i] for i in active))
        if rounds:
            for i in active: quotas[i] += rounds
            remaining -= rounds*len(active)
        else:
            for i in active[:remaining]: quotas[i] += 1
            remaining = 0
    return quotas


def _validate_source(frame, actual_pl_execution, max_points, marker_backend):
    if type(frame) is not Frame:
        raise TypeError('validated R33 Frame required')
    if type(actual_pl_execution) is not bool:
        raise TypeError('explicit hardware execution flag required')
    if type(max_points) is not int or not 1 <= max_points <= MAX_MARKERS:
        raise ValueError('R33 display marker limit must be 1..512')
    marker_backend = os.environ.get('SCAN3D_R34_MARKERS_BACKEND', 'auto') if marker_backend is None else marker_backend
    if marker_backend not in ('auto', 'numpy', 'native'):
        raise ValueError('R34 marker backend must be auto, numpy or native')
    h = frame.header; a = frame.records
    if (h.get('algorithm_revision'), h.get('width'), h.get('height')) != (33, 752, 480):
        raise ValueError('R33 752x480 observations required')
    if (not isinstance(a, np.ndarray) or a.dtype != np.dtype('<u4') or
            a.ndim != 2 or a.shape[1] != 16 or a.flags.writeable or type(frame.payload) is not bytes):
        raise ValueError('immutable R33 observation storage required')
    if actual_pl_execution and h.get('source') not in (1, 2):
        raise ValueError('fixture cannot claim actual PL execution')
    decisions = h.get('frame_decisions')
    if type(decisions) is not int or not 0 <= decisions <= 0xffffffff:
        raise ValueError('R33 frame decisions required')
    return marker_backend


def annotations_from_frame(frame, *, actual_pl_execution=False,
                           geometry_evidence_type='SIMULATION_ONLY',
                           max_points=DEFAULT_MARKERS, compact=False,
                           marker_backend=None):
    """Sample validated R33 final observations, preserving frame-use gates.

    Returned PLDisplayAnnotations is an IPC *marker* envelope, never an old
    observation packet. Both u and v are rounded to nearest display pixel
    (positive ties upward). Rejected candidates without a valid center use a
    cross at the actual run start/anchor row, never an invented center.
    ``compact=True`` retains those exact groups in immutable numeric arrays for
    bounded binary display IPC, avoiding per-marker Python tuple validation.
    The default tuple envelope remains available for legacy consumers.
    """
    if type(compact) is not bool:
        raise TypeError('explicit compact marker flag required')
    marker_backend = _validate_source(frame, actual_pl_execution, max_points, marker_backend)
    h = frame.header; a = frame.records; decisions = h['frame_decisions']
    measure_usable = bool(decisions & 1); preview_usable = bool(decisions & 2)
    groups = None
    if marker_backend != 'numpy':
        from .markers_native_r34 import sample
        groups = sample(a, decisions, max_points, required=marker_backend == 'native')
    if groups is None:
        groups = _numpy_groups(a, decisions, max_points, compact)
    elif not compact:
        groups = [tuple(map(tuple, group.tolist())) for group in groups]
    backend = ('ACTUAL_PL_R33_CAMERA' if h.get('source') == 1 else 'ACTUAL_PL_R33_DDR_REPLAY') if actual_pl_execution else 'R33_PACKET_EXECUTION_UNCONFIRMED'
    metadata = PLDisplayAnnotations(
        int(h['frame_id']), backend, geometry_evidence_type, actual_pl_execution,
        *(((), (), ()) if compact else groups), len(a), int(h['qualified_count']), int(h['measurement_count']), int(h['preview_count']),
        measure_usable, preview_usable,
        'USABLE' if measure_usable else 'FRAME_UNUSABLE',
        'USABLE' if preview_usable else 'FRAME_UNUSABLE', int(decisions >> 8),
        int(h['processing_cycles_lo'])+(int(h['processing_cycles_hi']) << 32))
    if compact:
        from candidate.display_compact_r14 import CompactPLAnnotations
        return CompactPLAnnotations(metadata, tuple(groups))
    return metadata


def _numpy_groups(a, decisions, max_points, compact):
    """Frozen numeric sampler retained for library-missing fallback and A/B."""
    measure_usable = bool(decisions & 1); preview_usable = bool(decisions & 2)
    # The three decisions share one record word. Decode it once, rather than
    # traversing the strided record column independently for each mode.
    flags = (a[:, 13] >> 24).astype(np.uint8)
    valid = (flags & 1) != 0
    measurement = ((flags & 2) != 0) & measure_usable
    preview = ((flags & 4) != 0) & preview_usable & ~measurement
    if np.any((measurement | preview) & ~valid):
        raise ValueError('accepted R33 marker has no center')
    indices = [np.flatnonzero(mask) for mask in
               (measurement, preview, ~(measurement | preview))]
    quotas = _quotas([len(ids) for ids in indices], max_points)
    chosen = []
    for ids, quota in zip(indices, quotas):
        if quota != len(ids): ids = ids[np.arange(quota)*(len(ids)-1)//max(1, quota-1)]
        chosen.append(ids)
    groups = [np.empty((0, 3), np.int64) for _ in range(3)] if compact else [(), (), ()]
    if sum(quotas):
        # Bound all coordinate work by the display sample cap and perform the
        # same rounding/range checks once for the combined three-class sample.
        ids = np.concatenate(chosen)
        center = valid[ids]
        # uint32 -> int64 before rounding, so malformed arithmetic cannot wrap.
        u = np.where(center, (a[ids, 4].astype(np.int64)+2048)//4096, a[ids, 2])
        v = np.where(center, (a[ids, 5].astype(np.int64)+2048)//4096, a[ids, 1])
        if np.any(u < 0) or np.any(u > 752) or np.any(v < 0) or np.any(v > 480):
            raise ValueError('R33 marker outside display domain')
        # A valid center immediately inside the far edge can round to width/
        # height. Clipping belongs only to the bounded display rasterization.
        packed = np.column_stack((np.minimum(u, 751)*4096, np.minimum(v, 479), (~center).astype(int)))
        offset = 0
        for group, quota in enumerate(quotas):
            selected = packed[offset:offset+quota]
            groups[group] = selected if compact else tuple(map(tuple, selected.tolist()))
            offset += quota
    return groups


class R33Renderer(PFO3RendererR25):
    """Reuse native Raw/XYZ raster and marker IPC, label their R33 semantics.

    R40: with SCAN3D_R40_PL_VIEW=1 the FPGA draws the point view into the model
    window over the HDMI stream; the ARM then projects no points and leaves that
    window as a plain background (the PL's colour 0 is transparent).
    """
    def __init__(self, config):
        super().__init__(config)
        self.pl_view = os.environ.get('SCAN3D_R40_PL_VIEW') == '1'
        if self.pl_view:
            import cv2
            empty = self.base_renderer._empty_model.copy()
            empty[:] = (37, 25, 15)
            cv2.putText(empty, 'FPGA PL VIEW / ALL POINTS / 60 Hz', (10, 470),
                        cv2.FONT_HERSHEY_SIMPLEX, .42, (177, 192, 205), 1, cv2.LINE_AA)
            empty.setflags(write=False)
            self.base_renderer._empty_model = empty

    def render(self, gray, points, *, status, cloud_revision=None, annotations=None):
        import cv2
        started = time.perf_counter()
        if self.pl_view:
            points = np.asarray(points)[:0]
        from candidate.display_compact_r14 import CompactPLAnnotations
        if annotations is not None and (type(annotations) not in (PLDisplayAnnotations, CompactPLAnnotations) or
                not annotations.backend.startswith(('ACTUAL_PL_R33_', 'R33_PACKET_'))):
            raise ValueError('R33 display marker envelope required')
        canvas = super().render(gray, points, status=status,
                                cloud_revision=cloud_revision, annotations=annotations)
        cv2.rectangle(canvas, (0, 0), (1279, 23), self._background, -1)
        self._draw_line(canvas, 'R33 / '+self.target, (12, 17), scale=.48, cache=True)
        if annotations is None:
            cv2.rectangle(canvas, (240, 24), (1279, 82), self._background, -1)
            self._draw_line(canvas, 'R33 / NO OBSERVATION SNAPSHOT FOR THIS DISPLAY FRAME', (250, 49), cache=True)
            self._draw_line(canvas, 'Execution and geometry evidence remain in the run receipt.', (250, 73), cache=True)
            cv2.rectangle(canvas, (16, 636), (1264, 716), self._background, -1)
            self._draw_line(canvas, 'R33 Raw and XYZ view; observation marks are not available for this frame.', (24, 650), cache=True)
        else:
            cv2.rectangle(canvas, (510, 636), (1264, 655), self._background, -1)
            self._draw_line(canvas, 'UV Q12 rounded only for display; x marks an uncomputed run start.', (510, 650), scale=.40, cache=True)
            cv2.rectangle(canvas, (16, 689), (1264, 719), self._background, -1)
            self._draw_line(canvas, 'R33 Raw + primary + UV observations + measurement/preview XYZ saved independently.', (24, 704), scale=.43, cache=True)
        if self.pl_view:
            cv2.rectangle(canvas, (800, 98), (1264, 121), self._background, -1)
            self._draw_line(canvas, f'PL VIEW (FPGA) / FULL OBSERVED {self.full_cloud_points:,}',
                            (810, 113), (170, 153, 132), scale=.43, cache=False)
        if status.source and ' | computed ' in status.source:
            self._draw_pipeline_status(canvas, status.source)
        if self._status_canvas is not None: np.copyto(self._status_canvas, canvas)
        self.timings.update(renderer='R33_UV_OBSERVATIONS_NATIVE_RAW_XYZ', pl_view=self.pl_view,
                            marker_coordinate_contract='R33_UV_Q12_DISPLAY_ROUND_ONLY',
                            render_with_annotations_ms=(time.perf_counter()-started)*1000.)
        return canvas


def renderer_factory(config):
    return R33Renderer(config)
