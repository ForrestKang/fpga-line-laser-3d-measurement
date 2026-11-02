"""Static computational XYZ benchmark using already selected PL-derived rows.

Explicitly frozen historical calibration is an input workload, not current
optical admission. All original range/training bounds remain enforced. There
is no axis, angle synthesis, accumulation, or dimension acceptance here.
"""
from collections import Counter
import time

import numpy as np

from sl_geometry.section_batch import reconstruct_section_batch
from .continuous_processor import ProcessedProfile
from .pipeline import ScanCalibration
from .stream_compact import compact_geometry_quality


def readonly(value):
    array = np.asarray(value).copy()
    array.setflags(write=False)
    return array


class SelectedGeometryBenchmark:
    def __init__(self, camera_path, plane_path, *, vector_geometry=False, cached_context=False):
        self.calibration = ScanCalibration.load(camera_path, plane_path)
        if type(vector_geometry) is not bool: raise TypeError('vector_geometry must be bool')
        self.vector_geometry = vector_geometry
        if type(cached_context) is not bool: raise TypeError('cached_context must be bool')
        self._context = None
        if cached_context:
            from sl_geometry.section_context_r16 import SectionContext
            c=self.calibration
            self._context=SectionContext(c.camera_matrix,c.distortion,c.plane,c.ray_range_mm,c.training_bounds_mm)
        self._last_identity = None

    def __call__(self, frame, stripe, config, selection, transition):
        identity = (frame.epoch, frame.request_id)
        if self._last_identity is not None and (identity[0] != self._last_identity[0]
                                                or identity[1] <= self._last_identity[1]):
            raise ValueError('geometry benchmark requires ordered same-epoch captures')
        if frame.compact_packet.configuration.roi != config.roi:
            raise ValueError('geometry stripe/packet ROI mismatch')
        c = self.calibration
        started = time.perf_counter()
        rows = np.arange(480, dtype=np.int64)
        reconstruct = reconstruct_section_batch
        if self.vector_geometry:
            from sl_geometry.section_vector_r14 import reconstruct_section_vector
            reconstruct = reconstruct_section_vector
        if self._context is not None:
            section=self._context(rows,stripe.center_q12,stripe.accepted)
        else:
            section = reconstruct(rows, stripe.center_q12, stripe.accepted,
                c.camera_matrix, c.distortion, c.plane, c.ray_range_mm, c.training_bounds_mm)
        points = section['points_mm']
        valid = section['valid'] & ~section['outside_training_bounds'] & np.isfinite(points).all(axis=1)
        reasons = list(stripe.reasons)
        for row in np.flatnonzero(stripe.accepted):
            if not section['valid'][row]:
                reasons[row] = section['reasons'][row]
            elif section['outside_training_bounds'][row]:
                reasons[row] = 'outside_training_bounds'
            elif not valid[row]:
                reasons[row] = 'nonfinite_geometry'
        rejection = dict(Counter(reason for reason in reasons if reason not in (None, 'roi_excluded')))
        x0, y0, x1, y1 = config.roi
        quality = compact_geometry_quality(frame.compact_packet, stripe.accepted[y0:y1],
                                           rejection_counts=rejection)
        profile = ProcessedProfile(frame.request_id, frame.epoch, 'static', readonly(rows),
            readonly(stripe.center_q12), readonly(stripe.accepted), readonly(points),
            readonly(np.full_like(points, np.nan)), readonly(valid), tuple(reasons), rejection,
            quality, None, 'unregistered', None, False,
            bool(quality.status == 'good' and valid.sum() >= 3), False, False, 0,
            0, 0, (), 'historical_calibration_not_currently_validated')
        # An observational result must not acquire formal validity from a point count.
        summary = dict(xyz_computed=True, geometry_s=time.perf_counter()-started,
            xyz_valid_point_count=int(valid.sum()), geometry_rejection_counts=rejection,
            geometry_valid_rows=valid.tolist(), geometry_reasons=reasons,
            geometry_source='historical_frozen_calibration_computational_benchmark',
            current_optical_validation_passed=False, measurement_eligible=False,
            axis_validated=False, fused=False, transition=transition,
            camera_sha256=c.camera_sha256, plane_sha256=c.plane_sha256)
        summary['geometry_numeric_policy'] = ('vector_r14_scalar_boundary_fallback_1e-8mm' if self.vector_geometry
                                               else 'original_scalar_dot_order_batch')
        if self._context is not None:
            summary['geometry_numeric_policy']='r16_immutable_context_original_opencv_scalar_boundary_fallback_1e-8mm'
        self._last_identity = identity
        return dict(summary=summary, points_camera_mm=readonly(points[valid]), display_result=profile)
