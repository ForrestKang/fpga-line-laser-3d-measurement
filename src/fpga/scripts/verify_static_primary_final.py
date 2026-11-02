#!/usr/bin/env python3
"""Strictly decode archived PL primary streams for an offline static-camera run.

This tool reads frames.json and primary_<frame>.bin files only. It never opens a
device, loads an overlay, or infers physical dimensions from a protocol-valid frame.

Run an archive:
  python scripts/verify_static_primary_final.py --run <static-run> --output <new.json>

Exercise the frozen decoder and damaged-stream rejection with a saved real sample:
  python scripts/verify_static_primary_final.py --self-test --sample-primary <sample.bin> \
      --output <new-selftest.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from candidate.pfo3_r25 import PFO3Error, identity  # noqa: E402
from r33.protocol import HEADER_FIELDS, assemble_reference  # noqa: E402
from r33.protocol_r35 import assemble_r35  # noqa: E402


R33_MAGIC = 0x33544850
R35_MAGIC = 0x35544850
HEADER_WORDS = 64
HEADER_BYTES = HEADER_WORDS * 4


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_header(data: bytes) -> tuple[str, dict[str, int]]:
    if len(data) < HEADER_BYTES or len(data) % 4:
        raise PFO3Error("primary header truncated or stream not word aligned")
    words = struct.unpack_from("<64I", data)
    header = dict(zip(HEADER_FIELDS, words))
    if (words[0], words[1]) == (R35_MAGIC, 35):
        return "R35_PGO1", header
    if (words[0], words[1]) == (R33_MAGIC, 33):
        return "R33_STRICT", header
    raise PFO3Error("unsupported primary magic/version")


def _decode_primary(data: bytes):
    protocol, header = _read_header(data)
    expected_identity = identity(header)
    if protocol == "R35_PGO1":
        # Explicit strict Python reference path: do not require or silently load a
        # board-built native validator when auditing an archive on a workstation.
        frame, receipt = assemble_r35(data, expected_identity=expected_identity,
                                      backend="reference")
    else:
        frame, receipt = assemble_reference(data, expected_identity=expected_identity)
    return protocol, expected_identity, frame, receipt


def _count_geometry(frame) -> dict[str, int] | None:
    geometry = getattr(frame, "geometry_records", None)
    if geometry is None:
        return None
    if getattr(geometry, "ndim", 0) != 2 or geometry.shape[1] != 8:
        raise PFO3Error("decoded PL geometry records have invalid shape")
    flags = geometry[:, 7]
    measurement = int(((flags & 0x101) == 0x101).sum())
    preview = int(((flags & 0x102) == 0x102).sum())
    return {"geometry_records": int(len(geometry)),
            "measurement_xyz": measurement,
            "preview_xyz": preview}


def _external_identity_checks(metadata: dict[str, Any], observed: dict[str, int]) -> dict[str, Any]:
    # live.py binds identity.frame_id to its 1-based stats frame number. It also
    # records the motion identity in each frames.json row. Other PFO3 identity
    # fields are not currently persisted there and are reported as unavailable.
    map_fields = {"frame": "frame_id", "epoch": "epoch", "cfg_version": "cfg_version",
                  "exposure_version": "exposure_version", "motor_id": "motor_id",
                  "motor_epoch": "motor_epoch", "motor_steps": "motor_steps",
                  "FV_ticks": "FV_ticks", "exposure_start": "exposure_start",
                  "exposure_end": "exposure_end", "source": "source"}
    compared: dict[str, dict[str, int | bool]] = {}
    unavailable: list[str] = []
    mismatches: list[str] = []
    for metadata_key, identity_key in map_fields.items():
        if metadata_key not in metadata:
            unavailable.append(identity_key)
            continue
        value = metadata[metadata_key]
        if isinstance(value, bool) or not isinstance(value, int):
            mismatches.append(f"frames.json {metadata_key} is not an integer")
            continue
        equal = int(value) == int(observed[identity_key])
        compared[identity_key] = {"metadata": int(value), "primary": int(observed[identity_key]),
                                  "match": equal}
        if not equal:
            mismatches.append(f"identity mismatch: {identity_key}")
    if "frame" not in metadata:
        mismatches.append("frames.json frame number missing")
    return {"compared": compared, "unavailable_in_frames_json": unavailable,
            "mismatches": mismatches}


def _verify_one(run: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    fid = metadata["frame"]
    name = f"primary_{fid:06d}.bin"
    path = run / name
    result: dict[str, Any] = {"frame": fid, "file": name, "verified": False,
                              "issues": []}
    if path.is_symlink() or not path.is_file():
        result["issues"].append("primary file missing or not a regular file")
        return result
    data = path.read_bytes()
    result.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    expected_bytes = metadata.get("primary_bytes")
    if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes <= 0:
        result["issues"].append("frames.json primary_bytes missing or invalid")
    elif expected_bytes != len(data):
        result["issues"].append("primary byte count differs from frames.json")
    declared_sha = metadata.get("primary_sha256")
    if declared_sha is not None and declared_sha != result["sha256"]:
        result["issues"].append("primary SHA-256 differs from frames.json")

    try:
        protocol, observed_identity, frame, receipt = _decode_primary(data)
        identity_check = _external_identity_checks(metadata, observed_identity)
        result["identity"] = {"protocol": protocol,
                              "primary": observed_identity,
                              "cross_check": identity_check}
        result["issues"].extend(identity_check["mismatches"])
        result["decoded"] = {
            "diagnostic_candidates": int(len(frame.records)),
            "row_summaries": int(len(frame.rows)),
            "measurement_candidate_count": int(frame.header["measurement_count"]),
            "preview_candidate_count": int(frame.header["preview_count"]),
            "accepted_measurement_centres": int(frame.accepted("measurement").sum()),
            "accepted_preview_centres": int(frame.accepted("preview").sum()),
            "geometry": _count_geometry(frame),
            "decoder_receipt": receipt,
        }
        # The live writer records centres as the number of accepted measurement
        # 2-D centres and xyz_measurement as PL geometry points. These are distinct
        # quantities and are checked against the corresponding records only.
        accepted_m = result["decoded"]["accepted_measurement_centres"]
        if "centres" in metadata:
            if not isinstance(metadata["centres"], int) or isinstance(metadata["centres"], bool):
                result["issues"].append("frames.json centres is not an integer")
            elif metadata["centres"] != accepted_m:
                result["issues"].append("measurement centre count differs from primary")

        geometry_counts = result["decoded"]["geometry"]
        if geometry_counts is not None:
            if "xyz_measurement" in metadata:
                count = metadata["xyz_measurement"]
                if isinstance(count, bool) or not isinstance(count, int) or count != geometry_counts["measurement_xyz"]:
                    result["issues"].append("measurement XYZ count differs from primary geometry flags")
            if "xyz" in metadata:
                count = metadata["xyz"]
                if isinstance(count, bool) or not isinstance(count, int):
                    result["issues"].append("frames.json xyz is not an integer")
                elif count not in (geometry_counts["measurement_xyz"], geometry_counts["preview_xyz"]):
                    result["issues"].append("XYZ count differs from both primary measurement and preview geometry")
                else:
                    result["xyz_count_cross_check"] = {
                        "frames_json_xyz": count,
                        "matches_measurement": count == geometry_counts["measurement_xyz"],
                        "matches_preview": count == geometry_counts["preview_xyz"],
                        "note": "frames.json does not persist view_preview; either selected set is accepted",
                    }
        result["verified"] = not result["issues"]
    except Exception as error:
        result["issues"].append(f"strict protocol decode rejected primary: {type(error).__name__}: {error}")
    return result


def verify_run(run: Path) -> dict[str, Any]:
    run = run.resolve()
    frames_path = run / "frames.json"
    report: dict[str, Any] = {
        "schema": "STATIC_PRIMARY_STRICT_READBACK_V1",
        "evidence_class": "ARCHIVED_FILES_OFFLINE_STRICT_PROTOCOL_DECODE",
        "run_directory": str(run),
        "decoder": "frozen runtime R35/R33 strict Python reference decoder",
        "hardware_accessed": False,
        "physical_dimension_accuracy_proven": False,
        "dimension_limit": "Protocol validity and count consistency do not establish optical calibration or physical size accuracy.",
        "complete": False,
        "issues": [],
        "frames": [],
    }
    if not frames_path.is_file() or frames_path.is_symlink():
        report["issues"].append("frames.json missing or not a regular file")
        return report
    raw_json = frames_path.read_bytes()
    report["frames_json"] = {"bytes": len(raw_json),
                              "sha256": hashlib.sha256(raw_json).hexdigest()}
    try:
        metadata_rows = json.loads(raw_json.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        report["issues"].append(f"frames.json parse failed: {type(error).__name__}: {error}")
        return report
    if not isinstance(metadata_rows, list) or not metadata_rows:
        report["issues"].append("frames.json must be a non-empty array")
        return report

    seen: set[int] = set()
    valid_rows: list[dict[str, Any]] = []
    for index, row in enumerate(metadata_rows):
        if not isinstance(row, dict):
            report["issues"].append(f"frames.json row {index} is not an object")
            continue
        fid = row.get("frame")
        if isinstance(fid, bool) or not isinstance(fid, int) or fid < 0:
            report["issues"].append(f"frames.json row {index} has invalid frame number")
            continue
        if fid in seen:
            report["issues"].append(f"repeated frame number {fid}")
            continue
        seen.add(fid)
        valid_rows.append(row)

    expected_names = {f"primary_{row['frame']:06d}.bin" for row in valid_rows}
    actual_names = {path.name for path in run.glob("primary_*.bin")}
    for name in sorted(expected_names - actual_names):
        report["issues"].append(f"primary file absent for frames.json entry: {name}")
    for name in sorted(actual_names - expected_names):
        report["issues"].append(f"unlisted primary file present: {name}")

    report["frames"] = [_verify_one(run, row) for row in valid_rows]
    for item in report["frames"]:
        if not item["verified"]:
            report["issues"].extend(f"frame {item['frame']}: {issue}" for issue in item["issues"])
    report["counts"] = {
        "frames_json": len(metadata_rows),
        "unique_valid_frame_rows": len(valid_rows),
        "primary_files_listed": len(actual_names),
        "strict_decode_passed": sum(bool(item["verified"]) for item in report["frames"]),
        "strict_decode_failed": sum(not bool(item["verified"]) for item in report["frames"]),
    }
    report["identity_coverage"] = {
        "rule": "primary header frame_id must match frames.json frame; any motor/config identity fields present in frames.json are also compared",
        "full_identity_in_frames_json": all(
            all(key in row for key in ("frame", "epoch", "cfg_version", "exposure_version", "motor_id",
                                       "motor_epoch", "motor_steps", "FV_ticks", "exposure_start",
                                       "exposure_end", "source")) for row in valid_rows),
    }
    report["complete"] = bool(valid_rows) and not report["issues"]
    report["limits"] = [
        "This verifies saved PL primary protocol contents and their correspondence to persisted frame metadata; it does not prove an optical scene or sensor frame beyond the archived source records.",
        "frames.json currently omits some PFO3 identity fields; omitted fields are shown as unavailable instead of being inferred.",
        "No measurement of physical dimensions or accuracy is made.",
    ]
    return report


def _self_test(sample_primary: Path) -> dict[str, Any]:
    data = sample_primary.read_bytes()
    protocol, observed_identity, frame, receipt = _decode_primary(data)
    geom = _count_geometry(frame)
    fid = int(observed_identity["frame_id"])
    metadata: dict[str, Any] = {
        "frame": fid,
        "primary_bytes": len(data),
        "centres": int(frame.accepted("measurement").sum()),
        "motor_id": int(observed_identity["motor_id"]),
        "motor_epoch": int(observed_identity["motor_epoch"]),
        "motor_steps": int(observed_identity["motor_steps"]),
    }
    if geom is not None:
        metadata["xyz_measurement"] = geom["measurement_xyz"]
        metadata["xyz"] = geom["preview_xyz"]

    good_result: dict[str, Any]
    damaged_error = None
    with tempfile.TemporaryDirectory(prefix="static_primary_verify_") as temp:
        temp_run = Path(temp)
        (temp_run / "frames.json").write_text(json.dumps([metadata]), encoding="utf-8")
        (temp_run / f"primary_{fid:06d}.bin").write_bytes(data)
        good_result = verify_run(temp_run)
        # Corrupt the first tagged record (after the 64-word header); R35's strict
        # record parser must reject it. This only changes the temporary test copy.
        damaged = bytearray(data)
        if len(damaged) < HEADER_BYTES + 4:
            raise AssertionError("sample primary contains no record word to damage")
        damaged[HEADER_BYTES] = 0xFF
        try:
            _decode_primary(bytes(damaged))
        except PFO3Error as error:
            damaged_error = str(error)
        else:
            raise AssertionError("strict Python decoder accepted a corrupted record tag")
        (temp_run / f"primary_{fid:06d}.bin").write_bytes(damaged)
        damaged_result = verify_run(temp_run)
        damaged_rejected_by_run = not damaged_result["complete"]

    if not good_result.get("complete"):
        raise AssertionError("real primary sample failed end-to-end readback fixture: "
                             + json.dumps(good_result.get("issues", [])))
    if not damaged_rejected_by_run:
        raise AssertionError("run verifier accepted corrupted primary")
    return {
        "schema": "STATIC_PRIMARY_STRICT_READBACK_SELFTEST_V1",
        "evidence_class": "OFFLINE_TEST_USING_SAVED_PRIMARY_SAMPLE",
        "hardware_accessed": False,
        "sample_primary": str(sample_primary.resolve()),
        "sample_bytes": len(data),
        "sample_sha256": hashlib.sha256(data).hexdigest(),
        "protocol": protocol,
        "frame_id": fid,
        "valid_sample_strict_decode": True,
        "valid_sample_fixture_run_complete": bool(good_result["complete"]),
        "decoded_counts": good_result["frames"][0]["decoded"],
        "damaged_primary_rejected": damaged_error is not None and damaged_rejected_by_run,
        "damaged_rejection": damaged_error,
        "physical_dimension_accuracy_proven": False,
        "limits": ["Offline strict protocol validation only; not a physical measurement accuracy result."],
        "decoder_receipt": receipt,
    }


def _write_new_json(path: Path, data: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents replacing any earlier evidence.
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, help="archived static live run containing frames.json")
    parser.add_argument("--output", type=Path, required=True, help="new JSON evidence path; existing file is never overwritten")
    parser.add_argument("--self-test", action="store_true", help="run offline valid/damaged sample checks")
    parser.add_argument("--sample-primary", type=Path, help="saved real R35/R33 primary sample for --self-test")
    args = parser.parse_args(argv)
    if args.self_test:
        if args.run is not None or args.sample_primary is None:
            parser.error("--self-test requires --sample-primary and cannot be combined with --run")
        result = _self_test(args.sample_primary.resolve())
    else:
        if args.run is None or args.sample_primary is not None:
            parser.error("normal readback requires --run and does not accept --sample-primary")
        result = verify_run(args.run)
    try:
        _write_new_json(args.output, result)
    except FileExistsError:
        parser.error(f"refusing to overwrite existing output: {args.output}")
    print(json.dumps({"output": str(args.output.resolve()),
                      "complete": result.get("complete", result.get("valid_sample_fixture_run_complete")),
                      "issues": result.get("issues", []),
                      "physical_dimension_accuracy_proven": False}, ensure_ascii=False))
    return 0 if result.get("complete", result.get("valid_sample_fixture_run_complete", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
