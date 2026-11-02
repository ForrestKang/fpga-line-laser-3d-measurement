"""Select an R26 fixed-data A/B winner from executed AB_FINAL.json receipts.

This is a reporting tool only.  It refuses to invent speed: if either limit's
correctness comparison failed, or the candidate has no measured improvement, it
returns a non-zero exit and no recommendation.  It never starts a run.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

CANDIDATE = "shared_all"
CONFIG_FLAGS = {
    "geometry_backend": "prepared_serial",
    "display_min_interval_ms": 180.0,
    "feature_workers": 2,
    "display_backend": "process",
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_one(root: Path) -> dict:
    final = _load(root / "AB_FINAL.json")
    comparisons = final.get("comparisons") or []
    if not comparisons or not all(row.get("passed") is True for row in comparisons):
        raise ValueError("AB correctness comparison missing or failed at " + str(root))
    by_name = {row["name"]: row for row in final.get("runs") or []}
    if CANDIDATE not in by_name or "control" not in by_name:
        raise ValueError("control/candidate missing at " + str(root))
    return {"root": str(root), "limit": final.get("limit"),
            "control": by_name["control"], "candidate": by_name[CANDIDATE],
            "configs": final.get("configs"), "comparisons": comparisons}


def _target_metric(run: dict, target: str, path: list[str]):
    summary = (run.get("summary") or {}).get(target) or {}
    value = summary
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _rate(values):
    values = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not values:
        return None
    return sum(values) / len(values)


def _improvement(control, candidate):
    if control in (None, 0) or candidate is None:
        return None
    return (float(control) - float(candidate)) / float(control)


def _rate_improvement(control, candidate):
    if control in (None, 0) or candidate is None:
        return None
    return (float(candidate) - float(control)) / float(control)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ab8", type=Path, required=True)
    parser.add_argument("--ab64", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit("refusing to overwrite recommendation: " + str(args.output))
    ab8 = _check_one(args.ab8)
    ab64 = _check_one(args.ab64)
    rows = [ab8, ab64]
    targets = sorted({name for name in (ab64["control"].get("summary") or {})})
    if not targets:
        raise ValueError("AB64 control has no target summary; cannot select")
    metrics = {}
    accepted = True
    reasons = []
    for target in targets:
        def measure(row, name, path):
            return _target_metric(row[name], target, path)
        # The 64-frame run is the selection run.  Eight frames are recorded as a
        # startup-sensitive diagnostic; feature-pool startup can dominate there.
        control64, candidate64 = ab64["control"], ab64["candidate"]
        ca = measure(ab64, "control", ["performance", "active_average_hz", "producer_times_s"])
        da = measure(ab64, "candidate", ["performance", "active_average_hz", "producer_times_s"])
        cw = measure(ab64, "control", ["performance", "minimum_5s_hz", "producer_times_s"])
        dw = measure(ab64, "candidate", ["performance", "minimum_5s_hz", "producer_times_s"])
        cf = measure(ab64, "control", ["feature_discovery_s"])
        df = measure(ab64, "candidate", ["feature_discovery_s"])
        cpu_c = measure(ab64, "control",
                        ["performance", "active_cpu_conservative_upper_pct"])
        cpu_d = measure(ab64, "candidate",
                        ["performance", "active_cpu_conservative_upper_pct"])
        entry = {
            "selection_limit": 64,
            "control_active_avg_hz": ca,
            "candidate_active_avg_hz": da,
            "active_avg_improvement": _rate_improvement(ca, da),
            "control_worst_5s_xyz_hz": cw,
            "candidate_worst_5s_xyz_hz": dw,
            "control_feature_s": cf,
            "candidate_feature_s": df,
            "feature_improvement": _improvement(cf, df),
            "control_active_cpu_upper_pct": cpu_c,
            "candidate_active_cpu_upper_pct": cpu_d,
            "diagnostic_8": {
                "control_active_avg_hz": measure(ab8, "control",
                    ["performance", "active_average_hz", "producer_times_s"]),
                "candidate_active_avg_hz": measure(ab8, "candidate",
                    ["performance", "active_average_hz", "producer_times_s"]),
            },
        }
        metrics[target] = entry
        if entry["active_avg_improvement"] is None or entry["active_avg_improvement"] <= 0.02:
            accepted = False
            reasons.append(target + ": 64-frame active average did not improve by >2%")
        if entry["feature_improvement"] is None or entry["feature_improvement"] <= 0.05:
            accepted = False
            reasons.append(target + ": 64-frame feature time did not improve by >5%")
        if entry["candidate_worst_5s_xyz_hz"] < cw:
            accepted = False
            reasons.append(target + ": 64-frame worst 5 s XYZ regressed")
    report = {
        "schema": "R26_AB_SELECTION_V1",
        "selected": CANDIDATE if accepted else None,
        "recommended_flags": CONFIG_FLAGS if accepted else None,
        "accepted": accepted,
        "reasons": reasons,
        "metrics": metrics,
        "limits": [row["limit"] for row in rows],
        "note": "Uses only executed AB_FINAL.json content. Host timings are not board evidence.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        import os
        os.fsync(stream.fileno())
    print(json.dumps({"output": str(args.output), "accepted": accepted,
                      "selected": report["selected"], "reasons": reasons}))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
