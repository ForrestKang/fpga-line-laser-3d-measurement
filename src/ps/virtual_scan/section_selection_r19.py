"""Select one actually observed section by measured boundary support only.

No target identity, expected count, dimension or truth is accepted. Sections
are never concatenated. Result indices refer to their original full cloud.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import numpy as np


def select_observed_section(discover, cloud, profiles, *, calibration_id):
    """Choose one complete supported profile, with deterministic provenance."""
    cloud = np.asarray(cloud, float)
    if cloud.ndim != 2 or cloud.shape[1] != 3 or not np.isfinite(cloud).all():
        raise ValueError("finite Nx3 observed cloud required")
    counts = [int(p["valid_point_count"]) for p in profiles]
    if any(n < 0 for n in counts) or sum(counts) != len(cloud):
        raise ValueError("profile counts must partition the original cloud")
    best = None
    start = 0
    for index, (profile, count) in enumerate(zip(profiles, counts)):
        offset = start
        start += count
        if not count or not profile.get("valid", True):
            continue
        angle = float(profile["angle_rad"])
        if not math.isfinite(angle):
            raise ValueError("finite profile angle required")
        c, s = math.cos(angle), math.sin(angle)
        inverse = np.array([[c, s, 0.], [-s, c, 0.], [0., 0., 1.]])
        observed = cloud[offset:offset+count] @ inverse
        discovered = discover(observed, calibration_id=calibration_id,
                              reference_basis="SIMULATION_ONLY shared calibrated axis XYZ mm",
                              scan_angles_rad=[0.])
        accepted, gaps = [], []
        for feature in discovered:
            if feature["status"] != "available":
                continue
            f = copy.deepcopy(feature)
            local_ids = np.asarray(f["support"]["point_indices"], int)
            if (len(local_ids) == 0 or local_ids.min() < 0 or local_ids.max() >= count):
                raise ValueError("feature indices must stay within their observed section")
            if f["type"] == "groove_width":
                z = observed[local_ids, 2]
                brackets = []
                for edge in f["edges_mm"]:
                    below, above = z[z <= edge], z[z >= edge]
                    if not len(below) or not len(above):
                        break
                    brackets.append([float(below.max()), float(above.min())])
                if len(brackets) != 2:
                    continue
                # Surface changes can legitimately reject several image rows;
                # a plateau's internal sampling spacing is not a transition
                # uncertainty limit. Require disjoint edge brackets with a
                # measured floor between them, and localization finer than
                # that observed floor extent. This is relative support only.
                floor_span = brackets[1][0]-brackets[0][1]
                if floor_span <= 0. or any(b-a >= floor_span for a, b in brackets):
                    continue
                f["observed_edge_brackets_mm"] = brackets
                f["boundary_sampling_half_width_sum_mm"] = sum((b-a)/2 for a, b in brackets)
                f["boundary_sampling_uncertainty_kind"] = "observed sampling interval, not calibrated confidence"
                gaps.extend(b-a for a, b in brackets)
            accepted.append(f)
        grooves = [f for f in accepted if f["type"] == "groove_width"]
        # A larger count is more actually supported recesses, never a requested
        # level count. Ties prefer finer measured edge brackets, lower residual,
        # more observed support, then the earliest original profile.
        score = (len(grooves), -max(gaps, default=math.inf),
                 -sum(float(f.get("residual_mm") or 0.) for f in grooves),
                 sum(f["support"]["point_count"] for f in grooves), -index)
        if best is None or score > best[0]:
            best = (score, index, offset, count, angle, inverse, accepted, profile)
    if best is None:
        return []
    _, index, offset, count, angle, inverse, result, profile = best
    for feature in result:
        ids = np.asarray(feature["support"]["point_indices"], int)+offset
        observed = cloud[ids]
        feature["source_kind"] = "single_observed_section"
        feature["view_angle_rad"] = angle
        feature["region_id"] = "view_"+feature["region_id"]
        feature["direction"] = (inverse @ np.asarray(feature["direction"])).tolist()
        feature["support"]["point_indices"] = ids.tolist()
        feature["support"]["minimum_mm"] = observed.min(axis=0).tolist()
        feature["support"]["maximum_mm"] = observed.max(axis=0).tolist()
        feature["section_source"] = dict(
            profile_index=index, frame_id=profile.get("frame_id"),
            request_id=profile.get("request_id"), cloud_start_index=offset,
            observed_point_count=count, angle_rad=angle,
            selection_method="single_profile_supported_recesses_then_edge_brackets",
            no_cross_profile_point_combination=True)
    return result


def section_features(discover, reconstruction, dataset):
    """Read only saved XYZ/profile metadata using the existing tool contract."""
    reconstruction, dataset = Path(reconstruction), Path(dataset)
    profiles = [json.loads(line) for line in (reconstruction/"profiles.jsonl").read_text().splitlines()]
    with np.load(reconstruction/"cloud.npz") as saved:
        cloud = saved["points"]
    config = json.loads((dataset.parent/"calibration.json").read_text())
    return select_observed_section(discover, cloud, profiles, calibration_id=config["calibration_id"])
