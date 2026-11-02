"""Generator-side mesh loading and rigid transforms (millimetres).

No mesh, face labels, or target dimensions belong in an estimator input.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import struct

import numpy as np


@dataclass(frozen=True)
class TriangleMesh:
    triangles: np.ndarray
    source_sha256: str = "procedural"
    source_name: str = "anonymous"

    def __post_init__(self):
        triangles = np.array(self.triangles, dtype=np.float64, copy=True)
        if triangles.ndim != 3 or triangles.shape[1:] != (3, 3):
            raise ValueError("triangles must have shape (N,3,3)")
        if not len(triangles) or not np.isfinite(triangles).all():
            raise ValueError("mesh must contain finite triangles")
        triangles.setflags(write=False)
        object.__setattr__(self, "triangles", triangles)

    @property
    def bounds(self):
        return np.stack((self.triangles.min(axis=(0, 1)), self.triangles.max(axis=(0, 1))))


def load_target(path, *, transform=None, scale=1.0, center_xy=True, ground=True):
    """Read binary/ASCII STL, then explicitly orient, scale, and position it.

    ``transform`` is a generator-side 4x4 affine matrix. By default arbitrary
    held-out meshes retain orientation and are centered over the turntable.
    Source STL files are never modified.
    """
    path = Path(path)
    data = path.read_bytes()
    count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else -1
    if count >= 0 and len(data) == 84 + 50 * count:
        record = np.dtype([("normal", "<f4", 3), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
        triangles = np.frombuffer(data, dtype=record, count=count, offset=84)["vertices"].astype(float)
    else:
        vertices = re.findall(rb"\bvertex\s+([^\r\n]+)", data)
        try:
            triangles = np.asarray([[float(x) for x in row.split()] for row in vertices]).reshape(-1, 3, 3)
        except ValueError as exc:
            raise ValueError("invalid ASCII STL") from exc
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("scale must be finite and positive")
    triangles = triangles * scale
    if transform is not None:
        matrix = np.asarray(transform, dtype=float)
        if matrix.shape != (4, 4) or not np.isfinite(matrix).all() or not np.allclose(matrix[3], [0, 0, 0, 1]):
            raise ValueError("transform must be a finite affine 4x4 matrix")
        triangles = triangles @ matrix[:3, :3].T + matrix[:3, 3]
    if triangles.size:
        low, high = triangles.min(axis=(0, 1)), triangles.max(axis=(0, 1))
        offset = np.array([(low[0] + high[0]) / 2 if center_xy else 0,
                           (low[1] + high[1]) / 2 if center_xy else 0,
                           low[2] if ground else 0])
        triangles -= offset
    return TriangleMesh(triangles, hashlib.sha256(data).hexdigest(), path.name)


def fixture_transform(target_id):
    """Explicit print-to-scan orientation, only used by the image generator."""
    matrix = np.eye(4)
    if target_id in ("t01_depth_steps", "t03_groove_patch_coupon"):
        matrix[:3, :3] = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]
    return matrix


def rotate_triangles(triangles, angle_rad, axis_point, axis_direction):
    axis = np.asarray(axis_direction, dtype=float)
    axis = axis / np.linalg.norm(axis)
    x, y, z = axis
    cross = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    rotation = np.eye(3) + np.sin(angle_rad) * cross + (1 - np.cos(angle_rad)) * cross @ cross
    point = np.asarray(axis_point, dtype=float)
    return (triangles - point) @ rotation.T + point


def box_mesh(bounds):
    low, high = np.asarray(bounds, dtype=float)
    if low.shape != (3,) or high.shape != (3,) or np.any(high <= low):
        raise ValueError("occluder bounds must be increasing (2,3)")
    vertices = np.array([[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])])
    faces = [[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
             [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]]
    return TriangleMesh(vertices[faces], source_name="occluder")
