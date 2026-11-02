"""Independent, evaluation-only geometry-region matching and metric errors.

Feature values never select correspondences. Matching uses type, declared
direction, spatial support and plane identities, with fixed global thresholds.
"""
from __future__ import annotations

import numpy as np

POLICY = dict(solid_center_gate_mm=1., minimum_height_overlap_fraction=.45,
              plane_support_distance_gate_mm=.35, direction_absolute_dot_min=.99,
              groove_edge_gate_mm=.75, dimension_absolute_error_limit_mm=.6,
              center_absolute_error_limit_mm=.6, surface_p95_limit_mm=.25,
              ambiguous_regions='unmatched; never select using measured dimension',
              section_depth_step='retained unscored projection; never a normal depth or false normal-depth claim',
              duplicates='first geometric match retained, all further matches counted as duplicate extras')


def _direction_ok(feature, expected):
    if expected.get('direction_invariant'):
        return True
    if expected.get('direction_alternatives'):
        return any(_direction_ok(feature, dict(direction=d)) for d in expected['direction_alternatives'])
    a, b = np.asarray(feature.get('direction', [0., 0., 0.])), np.asarray(expected['direction'])
    return bool(np.linalg.norm(a) and np.linalg.norm(b) and abs(a@b)/(np.linalg.norm(a)*np.linalg.norm(b)) >= POLICY['direction_absolute_dot_min'])


def evaluate_features(features, cloud, truth):
    """Evaluate all available/missing/extra features; preserve originals on disk."""
    cloud = np.asarray(cloud, float)
    plane_map = {}; solid_map = {}; details = []
    for index, f in enumerate(features):
        ids = np.asarray(f.get('support', {}).get('point_indices', []), int)
        if len(ids) and (ids.min() < 0 or ids.max() >= len(cloud)):
            raise ValueError('feature support index outside saved observed cloud')
        points = cloud[ids]
        if f['type'] == 'plane_offset' and len(points):
            candidates = []
            for p in truth['planes']:
                if not _direction_ok(f, dict(direction=p['normal'])):
                    continue
                residual = abs(points@np.asarray(p['normal'])-p['offset'])
                if np.percentile(residual, 90) <= POLICY['plane_support_distance_gate_mm']:
                    candidates.append(p['id'])
            if len(candidates) == 1:
                plane_map[f['region_id']] = candidates[0]
        if 'center_xy_mm' in f and len(points):
            candidates = []
            for s in truth['solids']:
                if np.linalg.norm(np.asarray(f['center_xy_mm'])-s['center_xy_mm']) > POLICY['solid_center_gate_mm']:
                    continue
                low, high = s['z_range_mm']; observed_low, observed_high = points[:, 2].min(), points[:, 2].max()
                overlap = max(0., min(high, observed_high)-max(low, observed_low))
                if overlap/max(1e-9, high-low) >= POLICY['minimum_height_overlap_fraction']:
                    candidates.append(s['id'])
            if len(candidates) == 1:
                solid_map[f['region_id']] = candidates[0]
    matched = {}; extras = []; unavailable_outputs = []; unscored = []
    for index, f in enumerate(features):
        if f.get('status') != 'available' or f.get('value_mm') is None:
            unavailable_outputs.append(dict(index=index, type=f['type'], region_id=f.get('region_id'), reason=f.get('reason')))
            continue
        if f['type'] == 'section_depth_step':
            unscored.append(dict(index=index, type=f['type'], region_id=f.get('region_id'), value_mm=f['value_mm'], reason='declared section projection is outside requested normal-distance metrics; not promoted to normal depth'))
            continue
        candidates = []
        for j, expected in enumerate(truth['expected_features']):
            if f['type'] != expected['type'] or not _direction_ok(f, expected):
                continue
            if 'planes' in expected:
                mapped = [plane_map.get(r) for r in f.get('reference_regions', [])]
                ok = len(mapped) == 2 and set(mapped) == set(expected['planes'])
            elif 'edges_mm' in expected:
                ok = 'edges_mm' in f and np.max(abs(np.asarray(f['edges_mm'])-expected['edges_mm'])) <= POLICY['groove_edge_gate_mm']
            else:
                ok = solid_map.get(f.get('region_id')) == expected['region']
            if ok:
                candidates.append(j)
        if len(candidates) == 1:
            j = candidates[0]
            if j not in matched:
                matched[j] = index; continue
            reason = 'duplicate_geometric_region'
        elif len(candidates) > 1:
            reason = 'ambiguous_geometric_region'
        elif f['type'] == 'plane_offset' and f.get('region_id') in plane_map:
            details.append(dict(index=index, type=f['type'], status='supported_supplementary_surface', truth_plane=plane_map[f['region_id']]))
            continue
        elif f['type'] == 'parallel_plane_spacing' and all(r in plane_map for r in f.get('reference_regions', [])) and len(f.get('reference_regions', [])) == 2:
            # A valid spacing between other real faces is retained as an extra
            # measurement, not promoted to a requested step/groove depth.
            mapped = [plane_map[r] for r in f['reference_regions']]
            planes = [next(p for p in truth['planes'] if p['id'] == r) for r in mapped]
            normal = np.asarray(f['direction']); normal /= np.linalg.norm(normal)
            projected = [p['offset']*np.sign(normal@p['normal']) for p in planes]
            error = abs(float(f['value_mm'])-abs(projected[1]-projected[0]))
            details.append(dict(index=index, type=f['type'], status='supported_supplementary_spacing' if error <= .6 else 'incorrect_supplementary_spacing', truth_planes=mapped, absolute_error_mm=error))
            if error <= .6:
                continue
            reason = 'incorrect_supplementary_spacing'
        else:
            reason = 'no_type_direction_and_geometry_match'
        extras.append(dict(index=index, type=f['type'], region_id=f.get('region_id'), value_mm=f['value_mm'], reason=reason))
    for j, expected in enumerate(truth['expected_features']):
        record = dict(expected)
        if j not in matched:
            record.update(status='unavailable', reason='no_unique_observed_type_direction_region_match', measured_mm=None, error_mm=None, absolute_error_mm=None)
        else:
            index = matched[j]; value = float(features[index]['value_mm']); error = value-expected['value_mm']
            limit = POLICY['center_absolute_error_limit_mm'] if expected['type'].startswith('center_') else POLICY['dimension_absolute_error_limit_mm']
            record.update(status='pass' if abs(error) <= limit else 'fail', feature_index=index, measured_mm=value, error_mm=error, absolute_error_mm=abs(error), limit_mm=limit)
        details.append(record)
    required = [r for r in details if 'measured_mm' in r and r.get('required', True)]
    supplemental = [r for r in details if 'measured_mm' in r and not r.get('required', True)]
    return dict(policy=POLICY, expected_count=len(required), pass_count=sum(r['status']=='pass' for r in required), fail_count=sum(r['status']=='fail' for r in required), unavailable_count=sum(r['status']=='unavailable' for r in required), supplementary_pass_count=sum(r['status']=='pass' for r in supplemental), supplementary_fail_count=sum(r['status']=='fail' for r in supplemental), supplementary_unavailable_count=sum(r['status']=='unavailable' for r in supplemental), false_or_duplicate_feature_count=len(extras), extras=extras, unscored_projection_outputs=unscored, unavailable_estimator_outputs=unavailable_outputs, features=details, all_required_features_pass=bool(required) and all(r['status']=='pass' for r in required) and not extras, physical_validation=False)


def evaluate_case(features, cloud, mesh, truth):
    from tools.review_virtual_geometry import point_mesh_metrics
    result = evaluate_features(features, cloud, truth)
    result['surface'] = point_mesh_metrics(cloud, mesh.triangles)
    result['surface_p95_pass'] = bool(result['surface']['available'] and result['surface']['p95_mm'] <= POLICY['surface_p95_limit_mm'])
    result['passed'] = result['all_required_features_pass'] and result['surface_p95_pass']
    result['evidence_type'] = 'SIMULATION_ONLY'
    result['boundary'] = 'Observed-point surface residual is one-way; no filled CAD surfaces, watertightness, physical metrology or board throughput established.'
    return result
