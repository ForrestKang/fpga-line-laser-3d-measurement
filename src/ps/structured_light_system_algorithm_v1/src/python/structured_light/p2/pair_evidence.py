"""Evidence writer for one same-frame raw/Profile capture.

This module deliberately sits above the transport and below any UI.  It
accepts the immutable :class:`~structured_light.p2.types.CaptureResult`
produced by ``P2Controller.capture_pair`` and writes a self-contained record
which can be inspected on a PC or copied to the PYNQ board.  A failed
comparison is still exportable when a caller has a completed result (for
example, a replay or a diagnostic transport); the writer never changes a
failure into a successful result.
"""

from __future__ import annotations

from dataclasses import asdict
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from ..frame_io import GrayFrame, save_gray8_png
from ..gray_reference import RowResult, reference_rows
from ..profile import Profile, decode_profile
from .types import CaptureResult, DiagnosticRow, DiagnosticsSnapshot


ROW_FIELDS = (
    "row", "s0", "s1", "xc_fixed", "peak", "width", "candidate_count",
    "quality", "valid", "flags", "diagnostics",
)


def _json_default(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8")


def _diag_value(row: DiagnosticRow, field: str) -> Any:
    if field == "diagnostics":
        return row.diagnostics
    return getattr(row, field)


def _ref_value(row: RowResult, field: str) -> Any:
    if field == "diagnostics":
        return int(row.too_narrow) | (int(row.roi_excluded) << 1) | (int(row.line_bad) << 2) | (int(row.overflow) << 3)
    return getattr(row, field)


def _point_map(profile: Profile) -> dict[int, Any]:
    return {point.row: point for point in profile.points}


def _row_error_records(
    rows: tuple[RowResult, ...],
    actual_by_row: dict[int, DiagnosticRow],
) -> list[dict[str, Any]]:
    """Return the row-by-row error data used by both CSV and plot.

    The error is calculated from the actual diagnostic value even for an
    invalid or multi-candidate row.  Those rows are retained with a status
    marker, rather than being silently dropped from the curve.  A missing
    diagnostic row has no error value and is represented as ``None``.
    """
    records: list[dict[str, Any]] = []
    for expected in rows:
        actual = actual_by_row.get(expected.row)
        actual_xc = actual.xc_fixed if actual is not None else None
        signed = (int(actual_xc) - int(expected.xc_fixed)) if actual_xc is not None else None
        if actual is None:
            status = "missing"
        elif not actual.valid:
            status = "invalid"
        elif actual.candidate_count != 1:
            status = "multiple"
        else:
            status = "valid_single"
        records.append({
            "row": expected.row,
            "expected_xc_fixed": expected.xc_fixed,
            "rtl_xc_fixed": actual_xc,
            "signed_error_lsb": signed,
            "error_lsb": abs(signed) if signed is not None else None,
            "valid": actual.valid if actual is not None else None,
            "candidate_count": actual.candidate_count if actual is not None else None,
            "error_known": signed is not None,
            "status": status,
        })
    return records


def _write_row_error_plot(records: list[dict[str, Any]], output: Path) -> None:
    """Draw a dependency-free row-versus-error plot with invalid markers."""
    from PIL import Image, ImageDraw

    width, height = 1280, 720
    canvas = Image.new("RGB", (width, height), (248, 250, 252))
    draw = ImageDraw.Draw(canvas)
    left, top, right, bottom = 90, 78, width - 58, height - 86
    draw.text((left, 24), "ROW VS XC ERROR (LSB)", fill=(18, 28, 42))
    draw.text((left, 48), "Blue: valid single candidate | red X: invalid/multiple | gray: missing", fill=(65, 76, 88))

    known = [int(item["signed_error_lsb"]) for item in records if item["signed_error_lsb"] is not None]
    max_abs = max([abs(value) for value in known] + [1])
    # Keep zero-error curves visible while allowing larger real errors to fit.
    scale = max_abs * 1.15
    row_values = [int(item["row"]) for item in records]
    first_row = min(row_values, default=0)
    last_row = max(row_values, default=1)
    if first_row == last_row:
        last_row = first_row + 1

    def map_x(row: int) -> int:
        return left + round((row - first_row) * (right - left) / (last_row - first_row))

    def map_y(error: float) -> int:
        return top + round((scale - error) * (bottom - top) / (2.0 * scale))

    zero_y = map_y(0)
    draw.rectangle((left, top, right, bottom), outline=(85, 96, 108), width=1)
    draw.line((left, zero_y, right, zero_y), fill=(180, 188, 198), width=1)
    draw.text((12, zero_y - 8), "0", fill=(70, 80, 90))
    draw.text((left - 48, top - 8), f"+{max_abs}", fill=(70, 80, 90))
    draw.text((left - 42, bottom - 8), f"-{max_abs}", fill=(70, 80, 90))
    draw.text((left, bottom + 18), f"row {first_row}", fill=(70, 80, 90))
    draw.text((right - 48, bottom + 18), f"row {last_row}", fill=(70, 80, 90))

    valid_points: list[tuple[int, int]] = []
    for item in records:
        row = int(item["row"])
        error = item["signed_error_lsb"]
        x = map_x(row)
        if error is None:
            draw.ellipse((x - 3, zero_y - 3, x + 3, zero_y + 3), fill=(145, 152, 160))
            continue
        y = map_y(float(error))
        if item["status"] == "valid_single":
            valid_points.append((x, y))
        else:
            # Preserve the error value and make the invalid state obvious.
            draw.line((x - 4, y - 4, x + 4, y + 4), fill=(210, 52, 52), width=2)
            draw.line((x - 4, y + 4, x + 4, y - 4), fill=(210, 52, 52), width=2)
    if len(valid_points) > 1:
        draw.line(valid_points, fill=(35, 112, 214), width=2)
    for x, y in valid_points:
        draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=(35, 112, 214))
    canvas.save(output)


def _overlay_fonts() -> tuple[Any, Any]:
    """Return readable fonts for a 1280x720 board display.

    PYNQ images normally provide DejaVu Sans.  Keep an Arial fallback for
    Windows replay and finally use Pillow's built-in bitmap font so evidence
    export remains usable on a minimal host.  The first two choices are
    intentionally explicit: Pillow's default font is too small for HDMI.
    """
    from PIL import ImageFont

    candidates = (
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"),
    )
    for title_path, body_path in candidates:
        if Path(title_path).is_file() and Path(body_path).is_file():
            return (
                ImageFont.truetype(title_path, 26),
                ImageFont.truetype(body_path, 22),
            )
    default = ImageFont.load_default()
    return default, default


def _display_time_text(result: CaptureResult) -> tuple[str, str | None]:
    """Build unambiguous time labels for the HDMI evidence canvas.

    The SLP2 timestamp is the PL 100 MHz counter captured when the request is
    accepted.  Completion fields may come from a board clock that was known
    to be wrong during bring-up, so they are explicitly labelled instead of
    being presented as exposure or request-start time.
    """
    metadata = result.metadata
    request_utc = (
        metadata.get("request_time_utc")
        or metadata.get("request_utc")
        or metadata.get("started_utc")
    )
    completion_utc = metadata.get("completed_utc") or metadata.get("completion_utc")
    pl_line = f"request time: PL accepted {result.timestamp} ticks @100MHz"
    if request_utc is not None:
        pl_line += f" | request UTC: {request_utc}"
    if completion_utc is not None:
        completion = f"board completion: {completion_utc} (clock unverified)"
    else:
        completion = None
    return pl_line, completion


def _draw_overlay(
    frame: GrayFrame,
    rows: tuple[RowResult, ...],
    actual_rows: tuple[DiagnosticRow, ...],
    output: Path,
    *,
    request_id: int,
    passed: bool,
    max_error_lsb: int | None,
    valid_single_count: int,
    request_time: str,
    label: str,
    completion_time: str | None = None,
) -> bytes:
    """Write an annotated RGB/BGR canvas and return its BGR bytes.

    The raw image is placed 1:1 in a 1280x720 canvas.  Only valid rows with
    exactly one candidate are drawn, so invalid and multi-stripe rows cannot
    create a visually plausible contour.
    """
    from PIL import Image, ImageDraw
    import numpy as np

    title_font, body_font = _overlay_fonts()
    canvas = Image.new("RGB", (1280, 720), (14, 22, 32))
    image = Image.frombytes("L", (frame.width, frame.height), frame.pixels)
    ox, oy = (1280 - frame.width) // 2, (720 - frame.height) // 2
    canvas.paste(image.convert("RGB"), (ox, oy))
    draw = ImageDraw.Draw(canvas)
    point_rows = {row.row: row for row in actual_rows}
    for expected in rows:
        actual = point_rows.get(expected.row)
        if actual is None or not actual.valid or actual.candidate_count != 1:
            continue
        x = actual.xc_fixed / 4096.0
        y = oy + expected.row
        px = ox + x
        # A small cross remains visible at the native 1:1 image scale.
        draw.line((px - 3, y, px + 3, y), fill=(0, 255, 0), width=1)
        draw.line((px, y - 3, px, y + 3), fill=(0, 255, 0), width=1)
    status = "PASS" if passed else "FAILED"
    error = "n/a" if max_error_lsb is None else str(max_error_lsb)
    # The image begins at y=120 for 752x480, so reserve a readable three-line
    # header above it.  Do not place text over the captured pixels.
    draw.rectangle((16, 12, 1264, 112), fill=(0, 0, 0))
    draw.text((28, 18), f"{label} | request {request_id} | Python vs RTL: {status}",
              fill=(255, 255, 255), font=title_font)
    draw.text((28, 54), request_time, fill=(230, 235, 255), font=body_font)
    draw.text((28, 88), f"valid single rows: {valid_single_count} | max xc error: {error} LSB",
              fill=(210, 240, 210), font=body_font)
    # Completion is intentionally placed as a small secondary label in the
    # unused upper-right area.  It is never called a request or exposure time.
    if completion_time:
        draw.text((760, 88), completion_time, fill=(255, 220, 170), font=body_font)
    rgb = np.asarray(canvas, dtype=np.uint8)
    bgr = rgb[:, :, ::-1].tobytes()
    canvas.save(output.with_suffix(".png"))
    return bgr


def export_pair_evidence(
    result: CaptureResult,
    output: str | Path,
    *,
    threshold: int = 30,
    roi: tuple[int, int, int, int] | None = None,
    min_width: int = 1,
    max_width: int = 32,
    min_sum: int = 30,
    min_peak: int = 15,
    label: str = "REAL CAMERA CAPTURE",
) -> dict[str, Any]:
    """Export a complete, reproducible record for one ``CaptureResult``.

    The function recomputes the Python oracle from ``result.raw_frame``.  It
    calculates error metrics from every row, rather than relying on the
    bounded mismatch list stored in ``ReferenceComparison``.  ``comparison``
    is copied as evidence but never overwritten.
    """
    if not isinstance(result, CaptureResult):
        raise TypeError("result must be a CaptureResult")
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    frame = result.raw_frame
    expected = tuple(reference_rows(
        frame, threshold=threshold, roi=roi, min_width=min_width,
        max_width=max_width, min_sum=min_sum, min_peak=min_peak,
    ))
    actual = tuple(result.diagnostics.rows)
    actual_by_row = {row.row: row for row in actual}
    row_error_records = _row_error_records(expected, actual_by_row)
    try:
        profile = result.profile if isinstance(result.profile, Profile) else decode_profile(
            result.profile_payload, frame_width=frame.width, frame_height=frame.height
        )
    except Exception:
        profile = None
    points = _point_map(profile) if profile is not None else {}

    (root / "gray.bin").write_bytes(frame.pixels)
    save_gray8_png(frame, root / "gray.png")
    (root / "profile.slp2.bin").write_bytes(bytes(result.profile_payload))
    _write_json(root / "diagnostics.json", result.diagnostics)

    mismatch_rows: list[dict[str, Any]] = []
    # Start unknown.  A zero result must be obtained from at least one actual
    # row comparison, rather than being a default that could mask missing data.
    max_error: int | None = None
    missing_row = False
    valid_single = 0
    fractional = 0
    for ref in expected:
        diag = actual_by_row.get(ref.row)
        point = points.get(ref.row)
        if diag is not None and diag.valid and diag.candidate_count == 1:
            valid_single += 1
            if diag.xc_fixed % 4096:
                fractional += 1
        row_record: dict[str, Any] = {"row": ref.row}
        for field in ROW_FIELDS[1:]:
            exp = _ref_value(ref, field)
            got = _diag_value(diag, field) if diag is not None else None
            row_record[f"expected_{field}"] = exp
            row_record[f"rtl_{field}"] = got
            row_record[f"diff_{field}"] = None if got is None else int(got) - int(exp)
            if got != exp:
                mismatch_rows.append({"row": ref.row, "field": field, "expected": exp, "actual": got})
        exp_xc = ref.xc_fixed
        got_xc = diag.xc_fixed if diag is not None else None
        if got_xc is not None and max_error is not None:
            max_error = max(max_error, abs(int(got_xc) - int(exp_xc)))
        elif got_xc is not None:
            max_error = abs(int(got_xc) - int(exp_xc))
        else:
            missing_row = True
        row_record["rtl_profile_xc_fixed"] = point.xc_fixed if point is not None else None
        row_record["rtl_profile_valid"] = point.valid if point is not None else None
        if point is None or diag is None:
            missing_row = True
            mismatch_rows.append({"row": ref.row, "field": "missing_row_or_point", "expected": True, "actual": False})
        elif point.xc_fixed != diag.xc_fixed or point.valid != diag.valid or point.flags != diag.flags or point.quality != diag.quality:
            mismatch_rows.append({"row": ref.row, "field": "profile_vs_diagnostics", "expected": asdict(diag), "actual": asdict(point)})

    fieldnames = ["row"] + [name for field in ROW_FIELDS[1:] for name in (f"expected_{field}", f"rtl_{field}", f"diff_{field}")] + ["rtl_profile_xc_fixed", "rtl_profile_valid"]
    with (root / "rows.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        # Rebuild rows from the same loop without retaining a second mutable representation.
        for ref in expected:
            diag, point = actual_by_row.get(ref.row), points.get(ref.row)
            row_record = {"row": ref.row}
            for field in ROW_FIELDS[1:]:
                exp = _ref_value(ref, field)
                got = _diag_value(diag, field) if diag is not None else None
                row_record[f"expected_{field}"] = exp
                row_record[f"rtl_{field}"] = got
                row_record[f"diff_{field}"] = None if got is None else int(got) - int(exp)
            row_record["rtl_profile_xc_fixed"] = point.xc_fixed if point is not None else None
            row_record["rtl_profile_valid"] = point.valid if point is not None else None
            writer.writerow(row_record)
    with (root / "row_error.csv").open("w", newline="", encoding="utf-8") as stream:
        error_fields = [
            "row", "expected_xc_fixed", "rtl_xc_fixed", "signed_error_lsb",
            "error_lsb", "valid", "candidate_count", "error_known", "status",
        ]
        writer = csv.DictWriter(stream, fieldnames=error_fields)
        writer.writeheader()
        writer.writerows(row_error_records)
    _write_row_error_plot(row_error_records, root / "row_error.png")
    _write_json(root / "mismatches.json", {"count": len(mismatch_rows), "items": mismatch_rows})
    if missing_row:
        max_error = None
    passed = bool(result.comparison and result.comparison.passed and not mismatch_rows)
    request_time_value = (
        result.metadata.get("request_time_utc")
        or result.metadata.get("request_utc")
        or result.metadata.get("started_utc")
    )
    request_time = str(request_time_value) if request_time_value is not None else f"PL timestamp {result.timestamp}"
    display_time, completion_time = _display_time_text(result)
    bgr = _draw_overlay(
        frame, expected, actual, root / "overlay", request_id=result.request_id,
        passed=passed, max_error_lsb=max_error, valid_single_count=valid_single,
        request_time=display_time, completion_time=completion_time, label=label,
    )
    (root / "display_bgr.bin").write_bytes(bgr)

    summary = {
        "schema": "same_frame_pair_evidence_v1",
        "request_id": result.request_id,
        "epoch": result.epoch,
        "session_id": result.session_id,
        "frame_width": frame.width,
        "frame_height": frame.height,
        "raw_length": len(frame.pixels),
        "raw_sha256": hashlib.sha256(frame.pixels).hexdigest(),
        "profile_length": len(result.profile_payload),
        "profile_sha256": hashlib.sha256(result.profile_payload).hexdigest(),
        "timestamp": result.timestamp,
        "request_time": request_time,
        "request_time_utc": request_time_value,
        "completion_time_utc": (
            result.metadata.get("completed_utc")
            or result.metadata.get("completion_utc")
        ),
        "angle_raw": result.angle_raw,
        "angle_valid": result.angle_valid,
        "motion_enabled": result.motion_enabled,
        "comparison_passed": passed,
        "comparison_reported": asdict(result.comparison) if result.comparison else None,
        "mismatch_count_all_rows": len(mismatch_rows),
        "max_xc_error_lsb": max_error,
        "valid_single_candidate_rows": valid_single,
        "fractional_center_rows": fractional,
        "fractional_center_present": bool(fractional),
        "row_error_known_count": sum(1 for item in row_error_records if item["error_known"]),
        "row_error_invalid_count": sum(1 for item in row_error_records if item["status"] == "invalid"),
        "row_error_multiple_count": sum(1 for item in row_error_records if item["status"] == "multiple"),
        "python_parameters": {"threshold": threshold, "roi": roi, "min_width": min_width, "max_width": max_width, "min_sum": min_sum, "min_peak": min_peak},
        # Same-frame provenance is required for this evidence.  Preserve an
        # explicit transport claim; an adapter that omits it remains unknown
        # instead of being promoted to true by the exporter.
        "same_frame": (None if "same_frame" not in result.metadata
                        else bool(result.metadata["same_frame"])),
        "evidence_files": [p.name for p in sorted(root.iterdir()) if p.is_file()] + ["summary.json"],
    }
    _write_json(root / "summary.json", summary)
    return summary


__all__ = ["ROW_FIELDS", "export_pair_evidence"]
