"""Final-board gates, extending (not weakening) the historical R43 report review."""

import argparse
import json
import re
from pathlib import Path

from review_build_r43 import review as historical_review


def review(reports):
    result = historical_review(reports)
    text = (reports / "utilization_route.rpt").read_text(errors="replace")
    section = re.search(
        r"^\|\s*section\s*\|\s*section_measure\s*\|[^\n]+", text, re.MULTILINE
    )
    result["evidence_kind"] = "FINAL_FULL_POST_ROUTE_NOT_BOARD"
    result["expected_lanes"] = 2
    result["gates"]["architecture"] = (
        result["direction_lane_ids"] == [0, 1] and section is not None
    )
    # 2026-10-30: user accepted any positive setup slack for the live-mode build ("0.071ns可以用的")
    result["gates"]["setup"] = result["gates"]["setup"] and result["wns_ns"] > 0.0
    result["gates"]["section_present"] = section is not None
    result["section_resource_row"] = section.group(0) if section else None
    result["passed"] = all(result["gates"].values())
    result["requirements"] = {
        "wns_min_ns": 0.0,
        "wns_target_ns": 0.15,
        "whs_positive": True,
        "all_resource_max_percent": 80,
        "video_clock_mhz": 125,
    }
    # Frequency evidence must come from implemented clocks, not the plan filename.
    timing = (reports / "timing_route.rpt").read_text(errors="replace")
    clocks = re.search(r"^clk_fpga_1\s+[^\n]+", timing, re.MULTILINE)
    result["video_clock_report_row"] = clocks.group(0) if clocks else None
    result["gates"]["video_125mhz"] = bool(
        clocks and re.search(r"\b8\.000\b", clocks.group(0))
    )
    result["passed"] = all(result["gates"].values())
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--reports", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError(a.output)
    try:
        result = review(a.reports)
    except Exception as exc:  # noqa: BLE001 - fail closed and retain admission error
        result = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
    with a.output.open("x", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 2)


if __name__ == "__main__":
    main()
