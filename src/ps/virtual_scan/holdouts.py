"""Evaluator/generator-only held-out geometry; never imported by estimator."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .mesh_helpers import TriangleMesh, fixture_transform, load_target

ROOT = Path(__file__).resolve().parents[1]
SPEC = Path(r'D:\NUECSDC-FPGA\structured_light_system_algorithm_v1\report\printed_test_targets_spec_20261016.json')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def frozen_parameters():
    """Chosen only after ALGORITHM_FREEZE_01; write/hash before any rendering."""
    return [
        dict(id='h01_changed_steps', source='t01_depth_steps', scale=[.88, 1.17, .91], angle=.31, translation=[1.2, -1.4, .4]),
        dict(id='h02_four_level_cylinder', source='t02_stepped_cylinder', diameters=[36., 27., 18., 11.], heights=[10., 12., 9., 11.], angle=.42, translation=[1.4, -1.8, .5], segments=128),
        dict(id='h03_changed_grooves', source='t03_groove_patch_coupon', scale=[.93, 1.08, 1.04], angle=-.23, translation=[-1., 1., .6]),
        dict(id='h04_changed_posts', source='t04_asymmetric_twin_posts', base=[38., 36., 4.5], posts=[dict(center=[-8., -1.], size=[11., 15., 33.]), dict(center=[9., 4.], size=[8., 9., 27.])], angle=-.37, translation=[-1.5, 1.2, .3]),
    ]


def pose(case):
    c, s = math.cos(case.get('angle', 0.)), math.sin(case.get('angle', 0.))
    return np.array([[c, -s, 0.], [s, c, 0.], [0., 0., 1.]]), np.asarray(case.get('translation', [0., 0., 0.]))


def _box_union(boxes):
    """Exact exposed rectilinear union faces, avoiding internal occluding faces."""
    axes = [np.unique([v[i] for box in boxes for v in box]) for i in range(3)]
    shape = tuple(len(a)-1 for a in axes)
    occupied = np.zeros(shape, bool)
    for index in np.ndindex(shape):
        middle = np.array([(axes[i][j]+axes[i][j+1])/2 for i, j in enumerate(index)])
        occupied[index] = any(np.all(middle > lo) and np.all(middle < hi) for lo, hi in boxes)
    triangles = []
    for index in zip(*np.nonzero(occupied)):
        for axis in range(3):
            others = [i for i in range(3) if i != axis]
            for sign in (-1, 1):
                neighbor = list(index); neighbor[axis] += sign
                if 0 <= neighbor[axis] < shape[axis] and occupied[tuple(neighbor)]:
                    continue
                vertices = []
                for u, v in ((0, 0), (1, 0), (1, 1), (0, 1)):
                    p = [0., 0., 0.]
                    p[axis] = axes[axis][index[axis]+(sign > 0)]
                    p[others[0]] = axes[others[0]][index[others[0]]+u]
                    p[others[1]] = axes[others[1]][index[others[1]]+v]
                    vertices.append(p)
                triangles.extend([[vertices[0], vertices[1], vertices[2]], [vertices[0], vertices[2], vertices[3]]])
    return np.asarray(triangles)


def _stepped_cylinder(diameters, heights, segments):
    theta = np.arange(segments)*math.tau/segments
    circle = np.column_stack((np.cos(theta), np.sin(theta)))
    triangles = []; z = 0.
    for level, (diameter, height) in enumerate(zip(diameters, heights)):
        lower = np.column_stack((circle*diameter/2, np.full(segments, z)))
        upper = lower.copy(); upper[:, 2] += height
        for i in range(segments):
            j = (i+1) % segments
            triangles.extend([[lower[i], lower[j], upper[j]], [lower[i], upper[j], upper[i]]])
            if level == 0:
                triangles.append([[0., 0., z], lower[j], lower[i]])
            if level == len(diameters)-1:
                triangles.append([[0., 0., z+height], upper[i], upper[j]])
            else:
                inner_i = np.r_[circle[i]*diameters[level+1]/2, z+height]
                inner_j = np.r_[circle[j]*diameters[level+1]/2, z+height]
                triangles.extend([[upper[i], upper[j], inner_j], [upper[i], inner_j, inner_i]])
        z += height
    return np.asarray(triangles)


def build_case(case):
    """Return actual transformed mesh and separate feature truth."""
    source = case['source']; rotation, translation = pose(case)
    original = next(p for p in json.loads(SPEC.read_text(encoding='utf8'))['test_parts'] if p['id'] == source)['geometry']
    scale = np.asarray(case.get('scale', [1., 1., 1.]))
    solids = []; expected = []
    def feature(kind, value, region, direction, **extra):
        expected.append(dict(type=kind, value_mm=float(value), region=region, direction=(rotation@direction).tolist(), **extra))
    def solid(region, center, sizes, lo, hi, cylinder=False, required=True):
        center_world = rotation@np.r_[center, 0.]+translation
        solids.append(dict(id=region, center_xy_mm=center_world[:2].tolist(), z_range_mm=[lo+translation[2], hi+translation[2]]))
        if cylinder:
            feature('diameter', sizes[0], region, [1., 0., 0.], direction_invariant=True)
            feature('segment_height', hi-lo, region, [0., 0., 1.])
        else:
            order = np.argsort(sizes)
            for kind, axis in zip(('prism_width', 'prism_depth'), order):
                direction = np.eye(3)[axis]
                feature(kind, sizes[axis], region, direction)
                if abs(sizes[0]-sizes[1]) < 1e-10:
                    expected[-1]['direction_alternatives'] = [(rotation@np.eye(3)[i]).tolist() for i in range(2)]
            feature('prism_height', hi-lo, region, [0., 0., 1.])
        feature('center_x', center_world[0], region, [1., 0., 0.], direction_world=True)
        feature('center_y', center_world[1], region, [0., 1., 0.], direction_world=True)
        for item in expected:
            if item['region'] == region:
                item['required'] = required
            if item.get('direction_world'):
                item['direction'] = [1., 0., 0.] if item['type'] == 'center_x' else [0., 1., 0.]
    if source == 't02_stepped_cylinder':
        diameters = case.get('diameters', original['diameters_mm'])
        heights = case.get('heights', [original['band_height_mm']]*len(diameters))
        if 'diameters' in case:
            triangles = _stepped_cylinder(diameters, heights, case['segments'])
        else:
            triangles = load_target(ROOT/'fixtures/printed_targets_v1'/f'{source}.stl').triangles
        z = 0.
        for i, (d, h) in enumerate(zip(diameters, heights)):
            solid(f'cylinder_{i}', [0., 0.], [d], z, z+h, True); z += h
    elif source == 't04_asymmetric_twin_posts':
        base = case.get('base', original['base_mm'])
        posts = case.get('posts', [dict(center=p['center_xy_mm'], size=p['size_xyz_mm']) for p in original['posts']])
        boxes = [(np.array([-base[0]/2, -base[1]/2, 0.]), np.array([base[0]/2, base[1]/2, base[2]]))]
        solid('base', [0., 0.], base[:2], 0., base[2], required=False)
        for i, post in enumerate(posts):
            center, size = np.asarray(post['center']), post['size']
            boxes.append((np.r_[center-np.asarray(size[:2])/2, base[2]], np.r_[center+np.asarray(size[:2])/2, base[2]+size[2]]))
            solid(f'post_{i}', center, size[:2], base[2], base[2]+size[2])
        triangles = _box_union(boxes) if 'posts' in case else load_target(ROOT/'fixtures/printed_targets_v1'/f'{source}.stl').triangles
    else:
        triangles = load_target(ROOT/'fixtures/printed_targets_v1'/f'{source}.stl', transform=fixture_transform(source)).triangles*scale
        if source == 't01_depth_steps':
            half = max(original['front_z_mm'])/2
            for i, ((lo, hi), depth) in enumerate(zip(original['bands_y_mm'], original['front_z_mm'])):
                solid(f'step_band_{i}', [0., (half-depth/2)*scale[1]], [original['base_mm'][0]*scale[0], depth*scale[1]], lo*scale[2], hi*scale[2], required=False)
        elif source == 't03_groove_patch_coupon':
            ends = [0.] + [v for interval in original['grooves_y_mm'] for v in interval] + [original['base_mm'][1]]
            for i in range(0, len(ends)-1, 2):
                lo, hi = ends[i:i+2]
                solid(f'ungrooved_band_{i//2}', [0., 0.], [original['base_mm'][0]*scale[0], original['base_mm'][2]*scale[1]], lo*scale[2], hi*scale[2], required=False)
    triangles = triangles@rotation.T+translation
    # Group actual vertical triangle planes by unit normal and offset, so extra
    # emitted plane results can be checked against real surfaces independently.
    planes = []
    for tri in triangles:
        normal = np.cross(tri[1]-tri[0], tri[2]-tri[0]); length = np.linalg.norm(normal)
        if length < 1e-10 or abs(normal[2]/length) > 1e-6:
            continue
        normal /= length
        if normal[np.argmax(abs(normal))] < 0:
            normal = -normal
        offset = float(tri[0]@normal)
        group = next((g for g in planes if np.dot(g['normal'], normal) > .999999 and abs(g['offset']-offset) < 1e-5), None)
        if group is None:
            group = dict(id=f'face_{len(planes)}', normal=normal.tolist(), offset=offset, vertices=[]); planes.append(group)
        group['vertices'].extend(tri.tolist())
    for plane in planes:
        vertices = np.asarray(plane.pop('vertices'))
        plane['bounds_mm'] = [vertices.min(axis=0).tolist(), vertices.max(axis=0).tolist()]
    def plane_id(local_y):
        normal = rotation@np.array([0., 1., 0.]); point = rotation@np.array([0., local_y, 0.])+translation
        return next(p['id'] for p in planes if abs(abs(np.dot(normal, p['normal']))-1) < 1e-6 and abs(point@p['normal']-p['offset']) < 1e-5)
    if source == 't01_depth_steps':
        fronts = original['front_z_mm']; half = max(fronts)/2
        for i in range(len(fronts)-1):
            feature('parallel_plane_spacing', (fronts[i+1]-fronts[i])*scale[1], f'step_{i}', [0., 1., 0.], planes=[plane_id((half-fronts[i])*scale[1]), plane_id((half-fronts[i+1])*scale[1])])
    if source == 't03_groove_patch_coupon':
        half = original['base_mm'][2]/2
        for i, ((lo, hi), depth) in enumerate(zip(original['grooves_y_mm'], original['groove_depths_mm'])):
            feature('parallel_plane_spacing', depth*scale[1], f'groove_depth_{i}', [0., 1., 0.], planes=[plane_id(-half*scale[1]), plane_id((-half+depth)*scale[1])])
            feature('groove_width', (hi-lo)*scale[2], f'groove_width_{i}', [0., 0., 1.], edges_mm=[lo*scale[2]+translation[2], hi*scale[2]+translation[2]])
    return TriangleMesh(triangles, source_name=case['id']), dict(case_id=case['id'], source=source, pose=case, solids=solids, planes=planes, expected_features=expected, evidence_type='SIMULATION_ONLY', physical_validation=False)
