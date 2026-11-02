"""R33 UV-Q12 observations -> shared geometry -> independent mode clouds.

Only virtual calibration is currently admitted. This is replay evidence, not
physical metrology. No image processing, CAD or expected shape is consumed.
"""
from dataclasses import dataclass
from pathlib import Path
from collections import Counter
import hashlib
import json
import math
import numbers
import time

import numpy as np
from sl_geometry.geometry import pixel_rays


def _array(value, shape, name):
    a = np.asarray(value, dtype=np.float64)
    if a.shape != shape or not np.isfinite(a).all():
        raise ValueError(name + ' has invalid shape or nonfinite values')
    return np.frombuffer(a.tobytes(), dtype=np.float64).reshape(shape)


@dataclass(frozen=True)
class Calibration:
    camera_matrix: np.ndarray
    distortion: np.ndarray
    plane: np.ndarray
    axis_point_mm: np.ndarray
    axis_direction: np.ndarray
    basis_camera: np.ndarray
    rotation_angle_sign: int
    calibration_id: str
    source_sha256: str
    ray_range_mm: object = None
    training_bounds_mm: object = None
    evidence_type: str = 'SIMULATION_ONLY'


def load_calibration(path, *, admit_measured=False):
    data = Path(path).read_bytes()
    return parse_calibration(json.loads(data), source_sha256=hashlib.sha256(data).hexdigest(),
                             admit_measured=admit_measured)


def parse_calibration(d, *, source_sha256='', admit_measured=False):
    """Admit explicit simulation calibration; never promote it to measured.

    R41 live: with admit_measured=True a 'line_laser_camera_live_v1' file carrying
    MEASURED_CANDIDATE camera/laser values is admitted too; its own evidence label stays.
    """
    simulated = (isinstance(d, dict) and d.get('schema') == 'virtual_line_laser_camera_v1'
                 and d.get('evidence_type') == 'SIMULATION_ONLY' and d.get('simulation_only') is True)
    measured = (admit_measured and isinstance(d, dict) and d.get('schema') == 'line_laser_camera_live_v1'
                and d.get('evidence_type') == 'MEASURED_CANDIDATE' and d.get('simulation_only') is False)
    if not (simulated or measured) or d.get('units') != 'mm' or d.get('image_size') != [752, 480]:
        raise ValueError('explicit SIMULATION_ONLY virtual calibration required')
    required = {'camera_matrix', 'distortion', 'laser_plane_camera', 'axis_point_camera_mm',
                'axis_direction_camera', 'radial_reference_camera', 'rotation_angle_sign'}
    if not required.issubset(d):
        raise ValueError('missing calibration fields: ' + ','.join(sorted(required-set(d))))
    cid = d.get('calibration_id')
    if not isinstance(cid, str) or not cid.strip():
        raise ValueError('calibration_id required')
    k = _array(d['camera_matrix'], (3, 3), 'camera_matrix')
    if (k[0, 0] <= 0 or k[1, 1] <= 0 or np.linalg.matrix_rank(k) != 3
            or not np.allclose(k[2], [0, 0, 1], rtol=0, atol=1e-12)):
        raise ValueError('invalid camera matrix')
    distortion = np.asarray(d['distortion'], dtype=np.float64).reshape(-1)
    if distortion.size not in (4, 5, 8, 12, 14) or not np.isfinite(distortion).all():
        raise ValueError('invalid distortion coefficients')
    if 'distortion_coefficients' in d and not np.array_equal(distortion, d['distortion_coefficients']):
        raise ValueError('distortion aliases disagree')
    distortion = _array(distortion, distortion.shape, 'distortion')
    plane = _array(d['laser_plane_camera'], (4,), 'laser_plane_camera')
    if np.max(abs(plane[:3])) == 0:
        raise ValueError('plane normal must be nonzero')
    with np.errstate(over='ignore', invalid='ignore'):
        scale = np.max(abs(plane[:3]))
        offset = (plane[3] / scale) / np.linalg.norm(plane[:3] / scale)
    if not math.isfinite(float(offset)):
        raise ValueError('plane offset outside finite numeric range')
    point = _array(d['axis_point_camera_mm'], (3,), 'axis_point_camera_mm')
    axis = _array(d['axis_direction_camera'], (3,), 'axis_direction_camera')
    if not np.isclose(np.linalg.norm(axis), 1., rtol=0, atol=1e-6):
        raise ValueError('axis direction must be unit length')
    axis = _array(axis / np.linalg.norm(axis), (3,), 'axis')
    for key, expected in [('rotation_axis_point_camera_mm', point), ('rotation_axis_direction_camera', axis)]:
        if key in d and not np.allclose(_array(d[key], (3,), key), expected, rtol=0, atol=1e-9):
            raise ValueError('axis aliases disagree')
    radial = _array(d['radial_reference_camera'], (3,), 'radial_reference_camera')
    radial = radial - axis * (radial @ axis)
    if np.linalg.norm(radial) <= 1e-9:
        raise ValueError('radial reference parallel to axis')
    radial /= np.linalg.norm(radial)
    basis = _array(np.column_stack((radial, np.cross(axis, radial), axis)), (3, 3), 'basis')
    sign = d.get('rotation_angle_sign')
    if type(sign) is not int or sign not in (-1, 1):
        raise ValueError('rotation angle sign must be -1 or +1')
    limits = d.get('ray_range_mm')
    if limits is not None:
        limits = _array(limits, (2,), 'ray_range_mm')
        if limits[0] < 0 or limits[1] < limits[0]:
            raise ValueError('invalid ray range')
    bounds = d.get('training_bounds_mm')
    if bounds is not None:
        bounds = _array(bounds, (2, 3), 'training_bounds_mm')
        if np.any(bounds[0] > bounds[1]):
            raise ValueError('invalid training bounds')
    if source_sha256 and (len(source_sha256) != 64 or any(c not in '0123456789abcdef' for c in source_sha256)):
        raise ValueError('invalid source SHA256')
    return Calibration(k, distortion, plane, point, axis, basis, sign, cid,
                       source_sha256, limits, bounds)


@dataclass(frozen=True)
class GeometryResult:
    measurement_camera_mm: np.ndarray
    preview_camera_mm: np.ndarray
    measurement_axis_mm: np.ndarray
    preview_axis_mm: np.ndarray
    measurement_ids: np.ndarray
    preview_ids: np.ndarray
    rejection_counts: dict
    timing_ms: dict
    calibration_id: str
    calibration_sha256: str
    evidence_type: str = 'SIMULATION_ONLY'


def reconstruct_pair(frame, calibration, *, angle_rad=0.):
    """Compute union geometry once using both fractional pixel coordinates.

    Axis points are inverse-rotated into the common zero-angle reference and
    expressed in its radial-X, axis-cross-X Y, axis-Z right-handed basis.
    Each returned mode owns separate arrays, even when its point set matches.
    """
    start = time.perf_counter()
    if not isinstance(calibration, Calibration) or calibration.evidence_type != 'SIMULATION_ONLY':
        raise ValueError('validated simulation Calibration required')
    if (isinstance(angle_rad, (bool, np.bool_)) or not isinstance(angle_rad, numbers.Real)
            or not math.isfinite(float(angle_rad))):
        raise ValueError('angle_rad must be finite')
    a = np.asarray(frame.records)
    if (a.ndim != 2 or a.shape[1] != 16 or a.dtype.kind != 'u' or a.dtype.itemsize != 4
            or frame.header['algorithm_revision'] != 33):
        raise ValueError('R33 final observation frame required')
    m = np.asarray(frame.accepted('measurement'), bool)
    p = np.asarray(frame.accepted('preview'), bool)
    valid_center = ((a[:, 13] >> 24) & 1).astype(bool)
    if np.any(m & ~p) or np.any((m | p) & (~valid_center | ((a[:, 13] & 255) != 0))):
        raise ValueError('accepted observation lacks valid PL center')
    decisions = frame.header.get('frame_decisions')
    if type(decisions) is not int or not 0 <= decisions <= 0xffffffff:
        raise ValueError('valid R33 frame decisions required')
    # Local acceptance cannot override the final whole-frame quality gate.
    frame_rejects = {}
    for name, mask, bit in [('measurement', m, 1), ('preview', p, 2)]:
        count = int(np.count_nonzero(mask)) if not decisions & bit else 0
        frame_rejects[name] = {'frame_quality': count} if count else {}
        if not decisions & bit:
            mask[:] = False
    ids = np.flatnonzero(m | p)
    uv = a[ids, 4:6].astype(np.float64) / 4096.
    if (not np.isfinite(uv).all() or np.any(uv < 0)
            or np.any(uv[:, 0] >= 752) or np.any(uv[:, 1] >= 480)):
        raise ValueError('R33 UV center outside image')
    prepared = time.perf_counter()
    c = calibration
    if not np.any(c.distortion):
        # Exact zero-distortion pinhole path. Avoid an OpenCV import and call
        # for the frozen virtual camera, while preserving both UV fractions.
        rays = np.column_stack(((uv[:, 0]-c.camera_matrix[0, 2])/c.camera_matrix[0, 0],
                                (uv[:, 1]-c.camera_matrix[1, 2])/c.camera_matrix[1, 1],
                                np.ones(len(uv))))
        if c.camera_matrix[0, 1] != 0 or c.camera_matrix[1, 0] != 0:
            rays = pixel_rays(uv, c.camera_matrix, c.distortion)
    else:
        rays = pixel_rays(uv, c.camera_matrix, c.distortion)
    if not np.isfinite(rays).all():
        raise ValueError('nonfinite undistorted rays')
    directions = rays / np.linalg.norm(rays, axis=1)[:, None]
    normal = c.plane[:3] / np.max(abs(c.plane[:3]))
    normal_length = np.linalg.norm(normal)
    offset = (c.plane[3] / np.max(abs(c.plane[:3]))) / normal_length
    normal /= normal_length
    cosine = directions @ normal
    reasons = np.full(len(ids), '', dtype=object)
    parallel = abs(cosine) <= max(1e-6, 10 * np.finfo(float).eps)
    with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
        distance = -offset / cosine
    behind = (~np.isfinite(distance)) | (distance <= 0) | (directions[:, 2] <= 0)
    reasons[behind] = 'behind'
    reasons[parallel & (directions[:, 2] > 0)] = 'parallel'
    good = ~parallel & ~behind
    if c.ray_range_mm is not None:
        outside = good & ((distance < c.ray_range_mm[0]) | (distance > c.ray_range_mm[1]))
        reasons[outside] = 'out_of_range'; good[outside] = False
    camera = np.full((len(ids), 3), np.nan)
    camera[good] = distance[good, None] * directions[good]
    if c.training_bounds_mm is not None:
        outside = good & np.any((camera < c.training_bounds_mm[0]) | (camera > c.training_bounds_mm[1]), axis=1)
        reasons[outside] = 'outside_training_bounds'; good[outside] = False
    section_end = time.perf_counter()
    delta = camera - c.axis_point_mm
    theta = -float(angle_rad) * c.rotation_angle_sign
    co, si = math.cos(theta), math.sin(theta)
    rotated = delta * co + np.cross(c.axis_direction, delta) * si + np.outer(delta @ c.axis_direction, c.axis_direction) * (1-co)
    axis_points = rotated @ c.basis_camera
    axis_end = time.perf_counter()
    selections = {name: mask[ids] for name, mask in [('measurement', m), ('preview', p)]}
    rejects = {name: dict(Counter(reasons[mask & ~good])) for name, mask in selections.items()}
    for name in rejects:
        rejects[name].update(frame_rejects[name])
    mi = selections['measurement'] & good; pi = selections['preview'] & good
    outputs = (camera[mi].copy(), camera[pi].copy(), axis_points[mi].copy(),
               axis_points[pi].copy(), a[ids[mi], 0].copy(), a[ids[pi], 0].copy())
    finish = time.perf_counter()
    return GeometryResult(*outputs,
                          rejects, {'prepare': (prepared-start)*1000, 'section': (section_end-prepared)*1000,
                                    'axis': (axis_end-section_end)*1000, 'select': (finish-axis_end)*1000,
                                    'total': (finish-start)*1000},
                          c.calibration_id, c.source_sha256)
