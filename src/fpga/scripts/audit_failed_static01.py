#!/usr/bin/env python3
"""Derived, offline audit of saved packets from a failed live run.

No frames.json is reconstructed. Header identity is self-declared except for
the filename frame number and any fields in the original status.last entry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

from verify_static_primary_final import ROOT, _count_geometry, _decode_primary

sys.path.insert(0, str(ROOT / "runtime/structured_light_system_geometry_v1/src/python"))
import numpy as np
from r33.geometry_golden_r34 import load_calibration
from geometry_reference_r35 import parameters, reconstruct


def sha(data):
    return hashlib.sha256(data).hexdigest()


def audit(run):
    run = run.resolve()
    freeze_path = ROOT / "evidence/FINAL_PACKAGE_FILES.json"
    freeze = json.loads(freeze_path.read_text(encoding="utf-8-sig"))
    sources = ["runtime/r33/protocol.py", "runtime/r33/protocol_r35.py",
               "runtime/candidate/pfo3_r25.py", "runtime/r33/geometry_golden_r34.py",
               "runtime/geometry_reference_r35.py"]
    source_checks = []
    for name in sources:
        digest = sha((ROOT / name).read_bytes())
        source_checks.append(dict(file=name, sha256=digest,
                                  frozen_sha256=freeze[name]["sha256"],
                                  match=digest == freeze[name]["sha256"]))
    if not all(item["match"] for item in source_checks):
        raise ValueError("decoder/reference source differs from frozen package")
    status_path = run / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8-sig"))
    calibration_path = run / "calibration_live.json"
    p = parameters(load_calibration(calibration_path, admit_measured=True), 0.0)
    parameter_sha = sha(struct.pack("<36q", *p))
    files = sorted(run.glob("primary_*.bin"))
    report = dict(schema="FAILED_STATIC_SAVED_PACKET_DERIVED_AUDIT_V1",
                  evidence_class="OFFLINE_DERIVED_AUDIT_OF_SAVED_PACKETS",
                  run_directory=str(run), hardware_accessed=False,
                  original_run_passed=False, run_completed_60s=False,
                  triggering_packet_preserved=False,
                  frames_json_present=(run / "frames.json").exists(),
                  frames_json_created=False, source_checks=source_checks,
                  original_status=dict(sha256=sha(status_path.read_bytes()),
                                       phase=status.get("phase"), frames=status.get("frames"),
                                       last_frame=status.get("last", {}).get("frame"),
                                       elapsed_s=status.get("elapsed_s")),
                  calibration=dict(sha256=sha(calibration_path.read_bytes()),
                                   angle_rad=0.0, parameters_sha256=parameter_sha),
                  limits=["The triggering packet was not saved; this does not reproduce or explain SIGSEGV.",
                          "Without frames.json, identity is cross-checked against filename and status.last only where available.",
                          "Raw SHA is a derived file fingerprint; primary has no independent raw-input hash binding.",
                          "Body CRC32 is recomputed for the derived assembled R33 archive, not a transmitted primary CRC.",
                          "Integer geometry equality does not establish optical or physical measurement accuracy."],
                  frames=[], issues=[])
    ids = []
    for path in files:
        fid = int(path.stem.split("_")[-1])
        ids.append(fid)
        item = dict(frame=fid, file=path.name, passed=False, issues=[])
        report["frames"].append(item)
        try:
            if path.is_symlink():
                raise ValueError("primary symlink refused")
            data = path.read_bytes()
            protocol, observed, frame, receipt = _decode_primary(data)
            item.update(bytes=len(data), sha256=sha(data), protocol=protocol,
                        primary_declared_identity=observed,
                        filename_identity_match=observed["frame_id"] == fid)
            if observed["frame_id"] != fid:
                item["issues"].append("frame_id differs from filename")
            last = status.get("last", {}) if status.get("last", {}).get("frame") == fid else {}
            item["status_last_identity_checks"] = {
                key: dict(status=last[key], primary=value, match=last[key] == value)
                for key, value in observed.items() if key in last}
            if any(not check["match"] for check in item["status_last_identity_checks"].values()):
                item["issues"].append("identity differs from original status.last")
            raw_path = run / f"raw_{fid:06d}.gray8"
            if not raw_path.is_file() or raw_path.is_symlink():
                raise ValueError("regular raw file missing")
            raw = raw_path.read_bytes()
            item["raw"] = dict(file=raw_path.name, bytes=len(raw), sha256=sha(raw),
                               expected_bytes=752*480, length_match=len(raw) == 752*480,
                               status_last_sha_match=(sha(raw) == last["raw_sha256"]
                                                      if "raw_sha256" in last else None))
            if len(raw) != 752*480 or item["raw"]["status_last_sha_match"] is False:
                item["issues"].append("raw size or persisted status.last hash mismatch")
            header = frame.header
            crc = zlib.crc32(frame.payload[256:])
            item["decoded"] = dict(candidates=len(frame.records),
                                   header_candidates=int(header["diagnostic_count"]),
                                   row_summaries=len(frame.rows),
                                   qualified=int(header["qualified_count"]),
                                   measurement=int(header["measurement_count"]),
                                   preview=int(header["preview_count"]),
                                   assembled_bytes=len(frame.payload),
                                   assembled_body_crc32=crc,
                                   header_body_crc32=int(header["body_crc32"]),
                                   crc_match=crc == int(header["body_crc32"]),
                                   geometry=_count_geometry(frame), receipt=receipt)
            if len(frame.records) != header["diagnostic_count"] or crc != header["body_crc32"]:
                item["issues"].append("derived header count/CRC inconsistent")
            geometry = frame.geometry_records
            expected = np.asarray([reconstruct(p, int(frame.records[int(g[0]), 4]),
                                               int(frame.records[int(g[0]), 5]),
                                               int(g[0]), int(g[7]) & 3)
                                   for g in geometry], dtype=np.uint32).reshape(-1, 8)
            mismatched = int(np.any(expected != geometry, axis=1).sum())
            item["integer_geometry"] = dict(records=len(geometry), mismatched_records=mismatched,
                                           all_words_equal=mismatched == 0,
                                           parameter_sha_match=frame.geometry_sha256 == parameter_sha,
                                           epoch=int(frame.geometry_epoch))
            if mismatched or frame.geometry_sha256 != parameter_sha:
                item["issues"].append("geometry differs from calibration-bound integer reference")
            item["passed"] = not item["issues"]
        except Exception as error:
            item["issues"].append(f"{type(error).__name__}: {error}")
    if ids != list(range(1, 40)):
        report["issues"].append("expected saved packet sequence 1..39 differs")
    report["issues"].extend(f"frame {f['frame']}: {issue}" for f in report["frames"] for issue in f["issues"])
    report["counts"] = dict(primary_files=len(files), passed=sum(f["passed"] for f in report["frames"]),
                            failed=sum(not f["passed"] for f in report["frames"]))
    report["all_saved_packets_passed"] = bool(files) and not report["issues"]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=ROOT / "archive/static01")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; original evidence must not be overwritten")
    report = audit(args.run)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"output": str(args.output), "counts": report["counts"],
                      "all_saved_packets_passed": report["all_saved_packets_passed"],
                      "issues": report["issues"]}))
    return 0 if report["all_saved_packets_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
