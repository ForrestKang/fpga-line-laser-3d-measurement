"""Compare two completed replay roots for functional equivalence (not speed).

The comparison is deliberately split by platform:

* same platform (``--same-platform``): measurement and preview XYZ files must be
  byte-identical; each source digest is verified against its own saved bytes
  before comparing source bodies outside explicitly dynamic sequence/cycle fields;
* different platforms: no byte-equality claim is made for XYZ floats. Frame
  identity, independently verified source functionality, point counts and
  feature/association entity semantics are compared explicitly.

No historical file is modified.  The report is written to a NEW path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _run_dirs(root: Path):
    rows = []
    for path in sorted(root.iterdir()):
        if path.is_dir() and ((path / "RESULT.json").is_file()
                              or re.fullmatch(r"T\d{2}_\d+", path.name)):
            rows.append(path)
    return rows


def _frame_fingerprint(result: dict) -> list[dict]:
    fields = ("frame_id", "raw_sha256", "primary_sha256", "pfo3_sha256",
              "valid_XYZ_points", "preview_XYZ_points")
    frames = result.get("stage_frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("missing or empty stage_frames")
    out = []
    for row in frames:
        if not isinstance(row, dict):
            raise ValueError("invalid stage_frames row")
        for key in fields:
            if key not in row:
                raise ValueError("per-frame missing " + key)
        for key in ("raw_sha256", "primary_sha256", "pfo3_sha256"):
            _digest(row[key], "per-frame " + key)
        for key in ("frame_id", "valid_XYZ_points", "preview_XYZ_points"):
            _integer(row[key], "per-frame " + key)
        out.append({key: row[key] for key in fields})
    return out


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _integer(value, label, minimum=0):
    _require(type(value) is int and value >= minimum, label + " must be an integer >= " + str(minimum))


def _digest(value, label):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
             label + " must be a lowercase SHA-256")


def _validate_result(result):
    _require(type(result) is dict, "RESULT must be an object")
    _require(result.get("schema") == "R25_PL_REPLAY_V1", "missing/unsupported RESULT schema")
    _require(type(result.get("target")) is str and bool(result["target"]), "missing RESULT target")
    for key in ("frames", "requested_frames", "cloud_points", "preview_XYZ_points", "valid_XYZ_profiles"):
        _integer(result.get(key), "RESULT " + key, 1)
    _require(result["frames"] == result["requested_frames"], "RESULT incomplete frame count")
    _require(result["valid_XYZ_profiles"] <= result["frames"], "invalid valid_XYZ_profiles")
    _require(result.get("geometry_evidence_type") == "SIMULATION_ONLY", "missing/unsupported geometry evidence")
    frames = _frame_fingerprint(result)
    _require(len(frames) == result["frames"], "stage_frames count differs")
    _require([row["frame_id"] for row in frames] == list(range(result["frames"])),
             "per-frame frame_id must be ordered, unique and complete")
    for stage_key, total_key in (("valid_XYZ_points", "cloud_points"),
                                 ("preview_XYZ_points", "preview_XYZ_points")):
        _require(sum(row[stage_key] for row in frames) == result[total_key], stage_key + " total differs")
    return frames


def _feature_signature(path: Path) -> dict:
    _require(path.is_file(), "missing features.json")
    data = _json(path)
    _require(type(data) is dict, "features.json must be an object")
    for key in ("features", "raw_features"):
        _require(type(data.get(key)) is list and bool(data[key]), "missing/empty/invalid feature list " + key)
        for item in data[key]:
            _require(type(item) is dict and type(item.get("type")) is str,
                     "invalid feature record in " + key)
            _require(type(item.get("region_id")) is str and item.get("status") in ("available", "unavailable")
                     and "reason" in item and type(item.get("support")) is dict,
                     "missing feature identity/status/support in " + key)
            _integer(item["support"].get("point_count"), "feature support point_count")
    _require(type(data.get("association_audit")) is dict, "missing/invalid association_audit")
    _integer(data["association_audit"].get("dropped_count"), "association dropped_count")
    _require(type(data["association_audit"].get("groups")) is list, "missing association groups")
    _require(len(data["raw_features"]) - len(data["features"]) == data["association_audit"]["dropped_count"],
             "association dropped_count does not match candidate counts")
    # Timing/provenance wrappers may legitimately differ between runs.  The
    # extracted candidates, retained raw candidates and association audit are
    # the functional payload.
    signature = {
        "features": data["features"],
        "raw_features": data["raw_features"],
        "association_audit": data["association_audit"],
    }
    # Cross-platform float payloads are not required to be bit-identical.
    # Compare the entity semantics (type/region/status/reason and support
    # counts), and report numeric differences as warnings rather than claiming
    # a byte-equality failure across CPU architectures.
    def semantics(items):
        rows = []
        for item in items or []:
            support = item.get("support") if isinstance(item, dict) else None
            rows.append((
                item.get("type") if isinstance(item, dict) else None,
                item.get("region_id") if isinstance(item, dict) else None,
                item.get("status") if isinstance(item, dict) else None,
                item.get("reason") if isinstance(item, dict) else None,
                None if not isinstance(support, dict) else support.get("point_count"),
            ))
        return rows
    return {"canonical": _canonical(signature),
            "sha256": hashlib.sha256(_canonical(signature).encode("utf-8")).hexdigest(),
            "counts": {
                "features": len(data.get("features") or []),
                "raw_features": len(data.get("raw_features") or []),
            },
            "feature_semantics": semantics(data.get("features")),
            "raw_semantics": semantics(data.get("raw_features")),
            "dropped_count": (data.get("association_audit") or {}).get("dropped_count")}


def _storage_reader():
    # Also support invocation as a script, without importing review-only code.
    for path in (ROOT, ROOT / "structured_light_system_algorithm_v1/src/python",
                 ROOT / "structured_light_system_geometry_v1/src/python"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from candidate.storage_r21 import iter_records
    return iter_records


def _audit_target(path, result):
    """Exhaust the strict disk reader, bind stages and independently reassemble PL.

    Only compact frame identities are retained; raw/source/XYZ buffers are not
    accumulated across the run (the comparator also runs on memory-limited ARM).
    """
    import numpy as np
    reader = _storage_reader()
    from candidate.pfo3_r25 import HEADER_FIELDS, decode, identity
    from candidate.pfo3_stream_r25 import assemble

    count = result["frames"]
    _require(result.get("feature_discovery_included") is True
             and result.get("preview_XYZ_persisted") is True, "missing complete full-chain receipt")
    _require(result.get("actual_PL_execution") is True
             and result.get("actual_PL_frames") == count, "missing complete actual PL receipt")
    for key in ("ARM_pixel_extraction_calls", "ARM_branch_link_calls", "ARM_Raw_quality_calls"):
        _require(type(result.get(key)) is int and result[key] == 0, "missing/nonzero " + key)
    writer = result.get("writer")
    _require(type(writer) is dict, "missing durable writer receipt")
    for key in ("accepted", "written", "pending"):
        _integer(writer.get(key), "writer " + key)
    _require(type(writer) is dict and writer.get("accepted") == count
             and writer.get("written") == count and writer.get("pending") == 0
             and writer.get("durable") is True and writer.get("worker_joined") is True
             and "error" in writer and writer["error"] is None, "missing/incomplete durable writer receipt")
    stages = result["stage_frames"]
    cloud = np.load(path / "cloud.npy", allow_pickle=False, mmap_mode="r")
    _require(cloud.dtype == np.dtype("<f8") and cloud.shape == (result["cloud_points"], 3),
             "cloud.npy dtype/shape differs")
    preview_index = [json.loads(line) for line in (path / "preview_index.jsonl").read_text(encoding="utf-8").splitlines()]
    _require(len(preview_index) == count, "preview index frame count differs")
    details = []
    point_at = preview_at = preview_count = 0
    epoch = None
    with (path / "cloud.float64.bin").open("rb") as xyz, (path / "preview.float64.bin").open("rb") as preview:
        for row in reader(path / "frames"):
            if row["kind"] != "frame":
                continue
            index = len(details)
            _require(index < count, "unexpected extra disk frame")
            stage, ix = stages[index], preview_index[index]
            metadata, profile = row["metadata"], row["profile"]
            _require(metadata["request_id"] == stage["frame_id"] == profile["request_id"],
                     "disk/stage per-frame frame_id differs")
            _integer(metadata["capture_epoch"], "capture_epoch", 1)
            if epoch is None:
                epoch = metadata["capture_epoch"]
            _require(metadata["capture_epoch"] == epoch == profile["capture_epoch"], "capture_epoch differs")
            if "capture_epoch" in stage:
                _require(stage["capture_epoch"] == epoch, "stage capture_epoch differs")
            for key, field in (("raw", "raw_sha256"), ("source_pfo3_primary", "primary_sha256"),
                               ("source_pfo3_payload", "pfo3_sha256")):
                data = row.get(key)
                _require(type(data) is bytes and bool(data), "missing saved source " + key)
                _require(hashlib.sha256(data).hexdigest() == stage[field], "per-frame disk SHA differs: " + field)
            final, primary = row["source_pfo3_payload"], row["source_pfo3_primary"]
            header = dict(zip(HEADER_FIELDS, np.frombuffer(final, dtype="<u4", count=64).tolist()))
            decoded = decode(final, expected_identity=identity(header))
            fingerprint = "".join(header["fingerprint" + str(i)].to_bytes(4, "little").hex() for i in range(8))
            assembled, receipt = assemble(primary, expected_identity=identity(decoded.header),
                                          expected_fingerprint=fingerprint)
            _require(assembled.payload == final, "primary reassembly differs from final source")
            transport = stage.get("transport_receipt")
            _require(type(transport) is dict, "missing per-frame transport_receipt")
            _integer(transport.get("native_sequence"), "transport native_sequence")
            _require(transport.get("actual_PL_execution") is True
                     and transport.get("input_sha256") == stage["raw_sha256"], "transport Raw/PL admission differs")
            for key, expected in (("primary_stream_sha256", stage["primary_sha256"]),
                                  ("assembled_sha256", stage["pfo3_sha256"]),
                                  ("native_sequence", receipt["native_sequence"])):
                _require(key in transport and transport[key] == expected, "transport receipt differs: " + key)
            pm = profile.get("frame_metadata")
            _require(type(pm) is dict and type(pm.get("PL_transport_receipt")) is dict,
                     "missing saved profile transport receipt")
            profile_transport = pm["PL_transport_receipt"]
            for key in ("primary_stream_sha256", "assembled_sha256", "native_sequence"):
                _require(profile_transport.get(key) == transport[key], "profile transport binding differs: " + key)
            validator = _validator_identity(profile_transport)
            _require(validator == _validator_identity(transport), "stage/profile validator identity differs")
            for value in (transport, profile_transport):
                _require(value.get("body_validator_sha256") == validator["library_sha256"],
                         "validator library receipt differs")
            _require(pm.get("pfo3_stream_sha256") == stage["pfo3_sha256"], "profile final SHA differs")
            _digest(transport.get("bit_sha256"), "transport bit SHA")
            _require(pm.get("pfo3_bit_sha256") == transport["bit_sha256"], "stage/profile bit identity differs")
            points = row["points_axis_mm"]
            _require(isinstance(points, np.ndarray) and points.dtype == np.dtype("<f8")
                     and points.shape == (stage["valid_XYZ_points"], 3), "saved measurement XYZ shape/dtype differs")
            piece = points.tobytes()
            _require(xyz.read(len(piece)) == piece, "cloud.float64.bin differs from saved XYZ concatenation")
            _require(cloud[point_at:point_at + len(points)].tobytes() == piece, "cloud.npy differs from saved XYZ")
            point_at += len(points)
            _require(type(ix) is dict and ix.get("frame") == stage["frame_id"]
                     and ix.get("offset") == preview_at, "preview frame/offset differs")
            _integer(ix.get("count"), "preview index count")
            _integer(ix.get("bytes"), "preview index bytes")
            _require(ix["count"] == stage["preview_XYZ_points"] and ix["bytes"] == ix["count"] * 24,
                     "preview index length/count differs")
            _digest(ix.get("sha256"), "preview SHA")
            piece = preview.read(ix["bytes"])
            _require(len(piece) == ix["bytes"] and hashlib.sha256(piece).hexdigest() == ix["sha256"],
                     "preview piece SHA/length differs")
            _require(np.isfinite(np.frombuffer(piece, dtype="<f8")).all(), "nonfinite preview XYZ")
            preview_at += len(piece)
            preview_count += ix["count"]
            details.append({"frame_id": stage["frame_id"], "capture_epoch": epoch})
        # Reaching here exhausts iter_records, including batch commits and EOF.
        _require(len(details) == count and point_at == result["cloud_points"], "disk frame/point totals differ")
        _require(not xyz.read(1), "cloud.float64.bin trailing bytes")
        _require(not preview.read(1) and preview_count == result["preview_XYZ_points"], "preview EOF/total differs")
    return {"full_iter_records_audit": True, "independent_reassembly": True,
            "cloud_concat_exact": True, "preview_index_full_audit": True, "frames": details}


def _validator_identity(receipt):
    value = receipt.get("body_validator_identity")
    _require(type(value) is dict, "missing body validator identity")
    for key in ("manifest_sha256", "library_sha256", "source_sha256", "python_decoder_sha256"):
        _digest(value.get(key), "validator " + key)
    _integer(value.get("abi"), "validator ABI", 1)
    return value


_ALLOWED_PROFILE_PATHS = frozenset({
    "frame_metadata.pfo3_stream_sha256", "frame_metadata.replay_run_tag",
    "frame_metadata.PL_transport_receipt.primary_stream_sha256",
    "frame_metadata.PL_transport_receipt.assembled_sha256",
    "frame_metadata.PL_transport_receipt.native_sequence",
    "frame_metadata.PL_transport_receipt.wall_s",
    "frame_metadata.PL_transport_receipt.assembly_backend",
    "frame_metadata.PL_transport_receipt.body_validator_identity.manifest_sha256",
})


def _differences(first, second, path=""):
    import numpy as np
    if isinstance(first, np.ndarray) or isinstance(second, np.ndarray):
        equal = (isinstance(first, np.ndarray) and isinstance(second, np.ndarray)
                 and first.dtype == second.dtype and first.shape == second.shape
                 and first.tobytes() == second.tobytes())
        return [] if equal else [{"path": path, "kind": "array_bytes"}]
    if type(first) is not type(second):
        return [{"path": path, "kind": "type"}]
    if isinstance(first, dict):
        output = []
        for key in sorted(set(first) | set(second)):
            child = path + "." + key if path else key
            if key not in first or key not in second:
                output.append({"path": child, "kind": "missing"})
            else:
                output.extend(_differences(first[key], second[key], child))
        return output
    if isinstance(first, (list, tuple)):
        if len(first) != len(second):
            return [{"path": path, "kind": "length"}]
        return [d for i, (a, b) in enumerate(zip(first, second))
                for d in _differences(a, b, path + "[%d]" % i)]
    return [] if first == second else [{"path": path, "kind": "scalar", "left": first, "right": second}]


def _compare_sources(left, right, *, same_platform):
    import numpy as np
    from itertools import zip_longest
    from candidate.pfo3_r25 import HEADER_FIELDS
    reader = _storage_reader()
    sides = ((row for row in reader(path / "frames") if row["kind"] == "frame") for path in (left, right))
    errors, changes = [], []
    for a, b in zip_longest(*sides):
        _require(a is not None and b is not None, "disk frame counts differ")
        frame_id = a["metadata"]["request_id"]
        _require(frame_id == b["metadata"]["request_id"], "disk frame identities differ")
        _require(a["metadata"]["capture_epoch"] == b["metadata"]["capture_epoch"], "disk capture_epoch differs")
        _require(a["raw"] == b["raw"], "per-frame Raw bytes differ")
        final_a, final_b = a["source_pfo3_payload"], b["source_pfo3_payload"]
        headers = [dict(zip(HEADER_FIELDS, np.frombuffer(data, dtype="<u4", count=64).tolist()))
                   for data in (final_a, final_b)]
        header_diff = _differences(*headers)
        if any(d["path"] not in {"processing_cycles_lo", "processing_cycles_hi"} for d in header_diff):
            errors.append("per-frame final non-cycle header differs: " + str(frame_id))
        if final_a[256:] != final_b[256:]:
            errors.append("per-frame final candidate/row body differs: " + str(frame_id))
        pa, pb = (np.frombuffer(row["source_pfo3_primary"], dtype="<u4") for row in (a, b))
        primary_diff = []
        if pa.shape != pb.shape:
            errors.append("per-frame primary length differs: " + str(frame_id))
        else:
            allowed = {4, len(pa) - 32 + 14, len(pa) - 32 + 15, len(pa) - 32 + 23}
            primary_diff = [{"word": i, "left": int(pa[i]), "right": int(pb[i]), "allowed_dynamic": i in allowed}
                            for i in np.flatnonzero(pa != pb).tolist()]
            if any(not d["allowed_dynamic"] for d in primary_diff):
                errors.append("per-frame primary non-dynamic words differ: " + str(frame_id))
        ida, idb = (_validator_identity(row["profile"]["frame_metadata"]["PL_transport_receipt"])
                    for row in (a, b))
        for key in ("library_sha256", "source_sha256", "python_decoder_sha256", "abi"):
            _require(ida[key] == idb[key], "validator executable identity differs: " + key)
        profile_diff = _differences(a["profile"], b["profile"])
        allowed_profile = _ALLOWED_PROFILE_PATHS | (frozenset() if same_platform else
                                                   {"points_camera_mm", "points_axis_mm"})
        if any(d["path"] not in allowed_profile for d in profile_diff):
            errors.append("per-frame decoded MBR1 functional payload differs: " + str(frame_id))
        if same_platform:
            for key in ("points_camera_mm", "points_axis_mm"):
                if _differences(a[key], b[key], key):
                    errors.append("per-frame " + key + " bytes differ: " + str(frame_id))
        changes.append({"frame_id": frame_id, "primary_differences": primary_diff,
                        "final_header_differences": header_diff, "profile_differences": profile_diff})
    return errors, changes


def compare_target(left: Path, right: Path, *, same_platform: bool,
                   contract_only: bool = False) -> dict:
    errors = []
    warnings = []
    exact = {}
    results, features, audits = [], [], []
    for label, path in (("left", left), ("right", right)):
        result = feature = audit = None
        try:
            result = _json(path / "RESULT.json")
            _validate_result(result)
            for name, total in (("cloud.float64.bin", result["cloud_points"]),
                                ("preview.float64.bin", result["preview_XYZ_points"])):
                _require((path / name).is_file() and (path / name).stat().st_size == total * 24,
                         name + " missing or XYZ byte count differs")
            _require((path / "frames/payloads.bin").is_file(), "missing frames/payloads.bin")
            feature = _feature_signature(path / "features.json")
            if not contract_only:
                audit = _audit_target(path, result)
        except Exception as exc:  # malformed/unsupported evidence is a reported failure
            errors.append(label + " validation failed: " + type(exc).__name__ + ": " + str(exc))
        results.append(result)
        features.append(feature)
        audits.append(audit)
    left_result, right_result = results
    if not errors:
        for key in ("target", "frames", "requested_frames", "cloud_points", "preview_XYZ_points",
                    "valid_XYZ_profiles", "geometry_evidence_type"):
            if left_result[key] != right_result[key]:
                errors.append("RESULT field differs: " + key)
        # Full source digests belong to each run and can differ due to native
        # sequence/cycles. Contract-only fixtures have no disk source proof.
        fields = ("frame_id", "raw_sha256", "valid_XYZ_points", "preview_XYZ_points")
        for a, b in zip(left_result["stage_frames"], right_result["stage_frames"]):
            if any(a[key] != b[key] for key in fields):
                errors.append("per-frame identity, Raw digest or point counts differ")
        for name in ("cloud.float64.bin", "preview.float64.bin", "frames/payloads.bin"):
            a, b = left / name, right / name
            sa, sb = _sha(a), _sha(b)
            exact[name] = {"left_sha256": sa, "right_sha256": sb,
                           "left_bytes": a.stat().st_size, "right_bytes": b.stat().st_size}
            if name != "frames/payloads.bin" and sa != sb:
                if same_platform:
                    errors.append(name + " bytes differ on the same platform")
                else:
                    warnings.append(name + " differs across platforms; not a byte-equality failure")
        if contract_only:
            warnings.append("CONTRACT_ONLY: saved source integrity and formal equivalence are not proven")
        else:
            try:
                source_errors, source_changes = _compare_sources(left, right, same_platform=same_platform)
                errors.extend(source_errors)
                exact["source_functional_comparison"] = source_changes
            except Exception as exc:  # never turn a failed decode into equivalence
                errors.append("source comparison failed: " + type(exc).__name__ + ": " + str(exc))
        left_features, right_features = features
        if same_platform:
            if left_features["sha256"] != right_features["sha256"]:
                errors.append("feature/association signature differs")
        else:
            if left_features["counts"] != right_features["counts"]:
                errors.append("feature/raw candidate counts differ across platforms")
            if left_features["feature_semantics"] != right_features["feature_semantics"]:
                errors.append("feature entity semantics differ across platforms")
            if left_features["raw_semantics"] != right_features["raw_semantics"]:
                errors.append("raw feature entity semantics differ across platforms")
            if left_features["dropped_count"] != right_features["dropped_count"]:
                errors.append("association dropped_count differs across platforms")
            if left_features["sha256"] != right_features["sha256"]:
                warnings.append("feature numeric payload differs across platforms; "
                                "entity semantics/counts are equal")
        exact["features"] = {
            "left_sha256": left_features["sha256"], "right_sha256": right_features["sha256"],
            "left_counts": left_features["counts"], "right_counts": right_features["counts"],
        }

    return {
        "target": left_result.get("target") if isinstance(left_result, dict) else None,
        "left": str(left),
        "right": str(right),
        "same_platform": same_platform,
        "errors": errors,
        "warnings": warnings,
        "digests": exact,
        "audits": audits,
        "contract_only": contract_only,
        "contract_passed": not errors,
        "source_proven": not contract_only and all(audit is not None for audit in audits),
        "passed": not errors and not contract_only,
        "physical_accuracy_verified": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", type=Path, required=True,
                        help="reference/control replay root, e.g. separate64")
    parser.add_argument("--right", type=Path, required=True,
                        help="candidate replay root")
    parser.add_argument("--same-platform", action="store_true",
                        help="require byte identity of cloud/preview/decoded geometry; use only "
                             "when both roots came from the same board and runtime")
    parser.add_argument("--contract-only", action="store_true",
                        help="validate synthetic comparison fixtures without source proof; "
                             "always reports passed=false and exits nonzero")
    parser.add_argument("--output", type=Path, required=True,
                        help="new comparison JSON; existing files are never overwritten")
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit("refusing to overwrite comparison: " + str(args.output))
    left, right = args.left.resolve(), args.right.resolve()
    left_dirs, right_dirs = _run_dirs(left), _run_dirs(right)
    if not left_dirs or not right_dirs:
        raise SystemExit("both roots must contain at least one completed target directory")
    if [p.name for p in left_dirs] != [p.name for p in right_dirs]:
        raise SystemExit("target directory sets differ: %s vs %s" % (
            [p.name for p in left_dirs], [p.name for p in right_dirs]))
    targets = [compare_target(a, b, same_platform=args.same_platform, contract_only=args.contract_only)
               for a, b in zip(left_dirs, right_dirs)]
    report = {
        "schema": "R26_RUN_COMPARE_V1",
        "left": str(left),
        "right": str(right),
        "same_platform": args.same_platform,
        "contract_only": args.contract_only,
        "targets": targets,
        "passed": all(row["passed"] for row in targets),
        "note": "Speed and physical accuracy are never inferred. Each run's saved sources "
                "are audited before excluding only listed dynamic source fields. Cross-platform "
                "XYZ bytes are not compared; same-platform exactness is mandatory. "
                "Contract-only results never prove saved sources or formal equivalence.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(data)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    print(json.dumps({"output": str(args.output), "passed": report["passed"],
                      "targets": len(targets)}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
