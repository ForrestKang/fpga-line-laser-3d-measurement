"""Controlled scene comparisons; image statistics never replace user observation."""
from __future__ import annotations

import itertools
import numpy as np


def scene_statistics(groups: dict[str, list[np.ndarray]]) -> dict:
    if set(groups) != {'a', 'b', 'aprime'} or any(len(v) != 3 for v in groups.values()):
        raise ValueError('exactly three frames per A/B/A-prime group required')
    shapes = {x.shape for v in groups.values() for x in v}
    if len(shapes) != 1 or any(x.dtype != np.uint8 or x.ndim != 2 for v in groups.values() for x in v):
        raise ValueError('same-size raw uint8 grayscale frames required')
    def compare(a, b, threshold):
        delta = b.astype(np.float64) - a.astype(np.float64)
        shift = float(np.median(delta))
        residual = np.abs(delta - shift)
        return dict(mean_abs_dn=float(np.abs(delta).mean()),
                    median_brightness_shift_dn=shift,
                    residual_p95_dn=float(np.percentile(residual, 95)),
                    raw_change_ratio_20dn=float((np.abs(delta) > 20).mean()),
                    residual_change_ratio_20dn=float((residual > 20).mean()),
                    spatial_change_ratio=float((residual > threshold).mean()),
                    threshold_dn=float(threshold))
    medians = {name: np.median(np.stack(frames), axis=0) for name, frames in groups.items()}
    within = {}
    noise = []
    for name, frames in groups.items():
        samples = []
        for a, b in itertools.combinations(frames, 2):
            delta = b.astype(float) - a.astype(float)
            noise.append(float(np.percentile(np.abs(delta - np.median(delta)), 99)))
            samples.append(compare(a, b, 8))
        within[name] = samples
    threshold = max(8., max(noise) * 3)
    comparisons = {f'{a}_to_{b}': compare(medians[a], medians[b], threshold)
                   for a, b in [('a', 'b'), ('b', 'aprime'), ('a', 'aprime')]}
    return {'within_scene': within, 'between_scene': comparisons,
            'noise_threshold_dn': threshold,
            'noise_separation_supported': threshold < 255,
            'scene_change_verified': False,
            'requires_user_confirmation': True,
            'interpretation': 'Brightness shift separated from spatial changes; statistics alone do not prove paper placement.'}
