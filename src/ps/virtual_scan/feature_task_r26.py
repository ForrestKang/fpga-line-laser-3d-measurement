"""Versioned production final-analysis boundary; frozen V4 is the default."""
from virtual_scan.features_v4 import (discover_features_bounded as _discover,
                                     FeaturePolicy,FeaturePolicyV5)
from virtual_scan.feature_parallel_r26 import FeatureOperationError,run_bounded
from pathlib import Path
import numpy as np


def _discover_persisted(cloud_path, version, **options):
    cloud = np.load(cloud_path, mmap_mode='r', allow_pickle=False)
    if cloud.ndim != 2 or cloud.shape[1] != 3 or cloud.dtype.kind != 'f':
        raise ValueError('feature cloud must be floating Nx3')
    if version == 'v6':
        from virtual_scan.features_v6 import discover_features_with_audit
    else:
        from virtual_scan.features_v4 import discover_features_with_audit
    return discover_features_with_audit(cloud, **options)


def discover_features_bounded(points_axis_mm, *, calibration_id,
                              reference_basis=None, scan_angles_rad=None,
                              policy=None, association_policy=None,
                              workers=2, legacy=False, deadline_s=120.0,
                              cancelled=None, result_directory=None,
                              minimum_available_bytes=128 * 1024**2,
                              version='v4'):
    if version not in ('v4','v5','v6'):
        raise ValueError('feature version must be v4, v5 or v6')
    if version!='v4' and legacy:
        raise ValueError('legacy reference requires v4 version')
    if version=='v6':
        from virtual_scan.features_v6 import FeaturePolicyV6,discover_features_with_audit
        selected_policy=policy if policy is not None else FeaturePolicyV6()
        if not isinstance(selected_policy,FeaturePolicyV6):
            raise ValueError('v6 requires FeaturePolicyV6 or policy=None')
    elif version=='v5':
        selected_policy=policy if policy is not None else FeaturePolicyV5()
        if not isinstance(selected_policy,FeaturePolicyV5):
            raise ValueError('v5 requires FeaturePolicyV5 or policy=None')
    else:
        selected_policy=policy if policy is not None else FeaturePolicy()
        if type(selected_policy) is not FeaturePolicy:
            raise ValueError('v4 requires frozen FeaturePolicy or policy=None')
    options=dict(calibration_id=calibration_id,reference_basis=reference_basis,
                 scan_angles_rad=scan_angles_rad,workers=workers,legacy=legacy,
                 association_policy=association_policy,policy=selected_policy)
    if isinstance(points_axis_mm, (str, Path)):
        return run_bounded(_discover_persisted,
                           (str(Path(points_axis_mm).resolve()), version), options,
                           deadline_s=deadline_s, cancelled=cancelled,
                           minimum_available_bytes=minimum_available_bytes,
                           result_directory=result_directory)
    if version=='v6':
        return run_bounded(discover_features_with_audit,(points_axis_mm,),options,
                           deadline_s=deadline_s,cancelled=cancelled,
                           minimum_available_bytes=minimum_available_bytes,
                           result_directory=result_directory)
    return _discover(points_axis_mm, deadline_s=deadline_s, cancelled=cancelled,
                     minimum_available_bytes=minimum_available_bytes,
                     result_directory=result_directory, **options)
