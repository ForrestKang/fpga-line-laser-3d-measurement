"""Independent triangle/laser intersection image formation, simulation only.

Rendering uses CAD only here; callers must retain forward_metadata exclusively
in their evaluator channel. Estimators receive gray, angle and calibration.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import cv2
import numpy as np

from .mesh_helpers import TriangleMesh, box_mesh, load_target, rotate_triangles


def default_config():
    return json.loads((Path(__file__).resolve().parents[1] / "configs" / "virtual_camera.json").read_text(encoding="utf-8-sig"))


def plane_segments(triangles, plane):
    """Intersect each triangle with a plane; omit coplanar area ambiguity."""
    plane = np.asarray(plane, dtype=float)
    signed = triangles @ plane[:3] + plane[3]
    crossing = (signed.min(axis=1) <= 1e-10) & (signed.max(axis=1) >= -1e-10)
    ids = np.flatnonzero(crossing & (np.ptp(signed, axis=1) > 1e-10))
    segments, face_ids = [], []
    for face in ids:
        vertices, values = triangles[face], signed[face]
        hits = []
        for a, b in ((0, 1), (1, 2), (2, 0)):
            if abs(values[a]) < 1e-10:
                hits.append(vertices[a])
            if values[a] * values[b] < 0:
                hits.append(vertices[a] + values[a] / (values[a] - values[b]) * (vertices[b] - vertices[a]))
        if len(hits) < 2:
            continue
        unique = np.unique(np.round(hits, 11), axis=0)
        if len(unique) >= 2:
            # An edge lying in the plane gives exactly two unique endpoints.
            segments.append(unique[[0, -1]])
            face_ids.append(face)
    return np.asarray(segments, dtype=float).reshape(-1, 2, 3), np.asarray(face_ids, dtype=int)


def visible_from(points, origin, triangles, epsilon_mm=1e-5, batch_size=96):
    """Finite segment/triangle test, two-sided, no image-space z-buffer shortcut.

    Surface self hits at the endpoint do not block. This independently tests
    camera visibility and illumination visibility from the laser emitter.
    """
    points = np.asarray(points, dtype=float).reshape(-1, 3)
    origin = np.asarray(origin, dtype=float)
    visible = np.ones(len(points), dtype=bool)
    a = triangles[:, 0]
    edge1, edge2 = triangles[:, 1] - a, triangles[:, 2] - a
    source_to_a = origin - a
    q = np.cross(source_to_a, edge1)
    numerator = np.einsum("ij,ij->i", edge2, q)
    for begin in range(0, len(points), batch_size):
        directions = points[begin:begin + batch_size] - origin
        distances = np.linalg.norm(directions, axis=1)
        directions /= distances[:, None]
        p = np.cross(directions[:, None], edge2[None])
        det = np.einsum("ti,bti->bt", edge1, p)
        valid = np.abs(det) > 1e-10
        inverse = np.zeros_like(det)
        np.divide(1.0, det, out=inverse, where=valid)
        u = np.einsum("ti,bti->bt", source_to_a, p) * inverse
        v = (directions @ q.T) * inverse
        distance = numerator[None] * inverse
        blocked = valid & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9)
        blocked &= (distance > epsilon_mm) & (distance < distances[:, None] - epsilon_mm)
        visible[begin:begin + len(directions)] = ~np.any(blocked, axis=1)
    return visible


def project_points(points, config):
    points = np.asarray(points, dtype=float)
    camera = points @ np.asarray(config["world_to_camera_rotation"]).T + config["world_to_camera_translation_mm"]
    homogeneous = camera @ np.asarray(config["camera_matrix"]).T
    return homogeneous[:, :2] / homogeneous[:, 2, None], camera


def _sample_segments(segments, config):
    """Perspective-correct samples along the dominant projected segment axis.

    Sampling integer rows avoids a subpixel point-splat centroid bias. World
    interpolation is perspective-correct, using reciprocal camera depth.
    """
    width, height = config["image_size"]
    if not len(segments):
        return np.empty((0, 3)), np.empty((0, 2))
    uv, camera = project_points(segments.reshape(-1, 3), config)
    uv, depth = uv.reshape(-1, 2, 2), camera[:, 2].reshape(-1, 2)
    points, pixels = [], []
    for segment, pixel, z in zip(segments, uv, depth):
        if np.any(z <= 0):
            continue
        dy = pixel[1, 1] - pixel[0, 1]
        dx = pixel[1, 0] - pixel[0, 0]
        if abs(dx) > abs(dy) or abs(dy) < 1e-8:
            # Shallow top-surface stripes need horizontal sampling as well.
            # Half-pixel steps bound the point-spread discretization error.
            count = max(2, int(np.ceil(abs(dx) * 2)) + 1)
            t = np.linspace(0, 1, count)
            ys = pixel[0, 1] + t * dy
        else:
            ys = np.arange(max(0, int(np.ceil(pixel[:, 1].min()))), min(height - 1, int(np.floor(pixel[:, 1].max()))) + 1)
            if not len(ys):
                continue
            t = (ys - pixel[0, 1]) / dy
        xs = pixel[0, 0] + t * (pixel[1, 0] - pixel[0, 0])
        in_frame = (xs >= -4) & (xs < width + 4) & (ys >= -4) & (ys < height + 4)
        t, xs, ys = t[in_frame], xs[in_frame], ys[in_frame]
        alpha = (t / z[1]) / ((1 - t) / z[0] + t / z[1])
        points.append(segment[0] + alpha[:, None] * (segment[1] - segment[0]))
        pixels.append(np.column_stack((xs, ys)))
    if not points:
        return np.empty((0, 3)), np.empty((0, 2))
    points, pixels = np.concatenate(points), np.concatenate(pixels)
    # Shared triangle edges can produce the same sample. Do not double power.
    _, indices = np.unique(np.round(points, 8), axis=0, return_index=True)
    return points[indices], pixels[indices]


def _render_instant(mesh, angle_rad, config, perturbations):
    triangles = rotate_triangles(mesh.triangles, angle_rad,
                                 config["rotation_axis_point_world_mm"],
                                 config["rotation_axis_direction_world"])
    scene = triangles
    if perturbations.get("occluder_bounds_world_mm") is not None:
        scene = np.concatenate((scene, box_mesh(perturbations["occluder_bounds_world_mm"]).triangles))
    segments, faces = plane_segments(triangles, config["laser_plane_world"])
    points, pixels = _sample_segments(segments, config)
    epsilon = config["visibility_epsilon_mm"]
    camera_visible = visible_from(points, config["camera_origin_world_mm"], scene, epsilon)
    laser_visible = visible_from(points, config["laser_origin_world_mm"], scene, epsilon)
    visible = camera_visible & laser_visible
    visible_points, visible_pixels = points[visible], pixels[visible]
    width, height = config["image_size"]
    background = float(perturbations.get("background", config["background"]))
    gray = np.full((height, width), background, dtype=float)
    edge = perturbations.get("background_edge")
    if edge:
        column = int(edge.get("column", width // 2))
        gray[:, max(0, column):] += float(edge.get("amplitude", 20))
    amplitude = float(perturbations.get("stripe_amplitude", config["stripe_amplitude"]))
    sigma = float(perturbations.get("stripe_sigma_px", config["stripe_sigma_px"]))
    if sigma <= 0 or not np.isfinite(sigma):
        raise ValueError("stripe_sigma_px must be finite and positive")
    stripe = np.zeros_like(gray)
    radius = int(np.ceil(4 * sigma))
    for u, v in visible_pixels:
        columns = np.arange(max(0, int(np.floor(u)) - radius), min(width, int(np.floor(u)) + radius + 2))
        rows = np.arange(max(0, int(np.floor(v)) - radius), min(height, int(np.floor(v)) + radius + 2))
        profile_x = np.exp(-0.5 * ((columns - u) / sigma) ** 2)
        for row in rows:
            profile = amplitude * np.exp(-0.5 * ((row - v) / sigma) ** 2) * profile_x
            stripe[row, columns] = np.maximum(stripe[row, columns], profile)
    gray += stripe
    _, visible_camera = project_points(visible_points, config)
    meta = {
        "evidence_type": "SIMULATION_ONLY", "truth_channel": "EVALUATOR_ONLY_DO_NOT_PASS_TO_ESTIMATOR",
        "source_name": mesh.source_name, "source_sha256": mesh.source_sha256,
        "angle_rad": float(angle_rad), "triangle_count": len(triangles),
        "laser_plane_intersection_segments": len(segments),
        "intersected_triangle_fraction": float(len(faces) / len(triangles)),
        "in_frame_intersection_samples": len(points),
        "camera_visible_fraction": float(camera_visible.mean()) if len(points) else 0.0,
        "laser_visible_fraction": float(laser_visible.mean()) if len(points) else 0.0,
        "dual_visible_fraction": float(visible.mean()) if len(points) else 0.0,
        "visible_points_world_mm": visible_points,
        "visible_points_camera_mm": visible_camera,
        "visible_pixels_uv": visible_pixels,
        "occluded_sample_count": int((~visible).sum()),
    }
    return gray, meta


def render_frame(mesh, angle_rad, config=None, perturbations=None, seed=0):
    """Return native uint8 grayscale and evaluator-only forward truth.

    Knobs: stripe_sigma_px, stripe_amplitude, background, noise_std,
    saturation_level, background_edge={column,amplitude},
    occluder_bounds_world_mm=[[xmin,ymin,zmin],[xmax,ymax,zmax]],
    motion_blur_rad and motion_blur_samples. No hidden target-dependent policy.
    """
    config = default_config() if config is None else config
    perturbations = {} if perturbations is None else dict(perturbations)
    if not isinstance(mesh, TriangleMesh):
        raise TypeError("mesh must be TriangleMesh")
    if not np.isfinite(angle_rad):
        raise ValueError("angle must be finite")
    gray, metadata = _render_instant(mesh, angle_rad, config, perturbations)
    blur = float(perturbations.get("motion_blur_rad", 0))
    if blur:
        samples = int(perturbations.get("motion_blur_samples", 5))
        if samples < 2:
            raise ValueError("motion_blur_samples must be at least 2")
        blurred = np.zeros_like(gray)
        for offset in np.linspace(-blur / 2, blur / 2, samples):
            exposure, _ = _render_instant(mesh, angle_rad + offset, config, perturbations)
            blurred += exposure / samples
        gray = blurred
    noise = float(perturbations.get("noise_std", 0))
    if noise < 0:
        raise ValueError("noise_std must be nonnegative")
    if noise:
        gray += np.random.default_rng(seed).normal(0, noise, gray.shape)
    saturation = float(perturbations.get("saturation_level", 255))
    if not 0 < saturation <= 255:
        raise ValueError("saturation_level must lie in (0,255]")
    metadata["perturbations"] = copy.deepcopy(perturbations)
    metadata["truth_exposure"] = "mid_exposure_geometry" if blur else "instantaneous"
    metadata["seed"] = int(seed)
    return np.rint(np.clip(gray, 0, saturation)).astype(np.uint8), metadata


def render_sequence(mesh, angles_rad=None, config=None, perturbations=None, seed=0, frame_count=240):
    """Yield one frame at a time; never accumulate a revolution in memory."""
    if angles_rad is None:
        angles_rad = np.linspace(0, 2 * np.pi, frame_count, endpoint=False)
    for index, angle in enumerate(angles_rad):
        image, metadata = render_frame(mesh, float(angle), config, perturbations, seed + index)
        metadata["frame_id"] = index
        yield image, metadata


