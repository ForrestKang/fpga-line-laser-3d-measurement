"""Run the finite PYNQ acceptance for algorithm-v1 without changing the bitstream.

The algorithm stage is deliberately CPU-only. Hardware stages attach to the
already signed Overlay (or perform one explicitly authorized load), hold the
shared Stage-B owner lock for their complete run, keep the motor disabled, and
write each stage into a new evidence directory.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import traceback
import uuid
from typing import Any, Iterator, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "python"
SCRIPTS = ROOT / "board" / "scripts"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

APPROVAL_SCHEMA_VERSION = 1
STAGES = ("preflight", "first-frame", "stability", "algorithm", "recovery", "hdmi", "audit")
FRAME_BYTES = 752 * 480
BOARD_STATIC_COUNT = 1000
PAIR_SMOKE_COUNT = 10
RECOVERY_COUNT = 32
ALGORITHM_MODES = ("fixed", "local31", "local63")
DEFAULT_ROI = (0, 120, 752, 300)
DEFAULT_INSTALLATION_ID = "stage_b_candidate"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: str | Path, value: Any, *, replace: bool = False) -> Path:
    """Write explicit UTF-8 JSON, using atomic replace only for live status."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
    if replace:
        with temp.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    else:
        with target.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    return target


def update_status(output: Path, status: Mapping[str, Any]) -> None:
    write_json(output / "status.json", dict(status), replace=True)


def _read_json_object(path: str | Path, label: str) -> dict[str, Any]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label} JSON {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _artifact_hashes(artifacts: Mapping[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name in ("bit", "hwh", "manifest"):
        value = artifacts.get(name)
        digest = value.get("sha256") if isinstance(value, Mapping) else value
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"artifact {name} is missing a SHA-256")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError(f"artifact {name} SHA-256 is malformed") from exc
        normalized[name] = digest.lower()
    return normalized


def validate_roi_threshold(roi: Sequence[int], threshold: int) -> tuple[tuple[int, int, int, int], int]:
    if not isinstance(roi, (list, tuple)) or len(roi) != 4 or any(type(item) is not int for item in roi):
        raise ValueError("ROI must contain exactly four integer coordinates")
    x0, y0, x1, y1 = (int(item) for item in roi)
    if not (0 <= x0 < x1 <= 752 and 0 <= y0 < y1 <= 480):
        raise ValueError("ROI must be a non-empty region inside the 752x480 frame")
    if type(threshold) is not int or not 0 <= threshold <= 255:
        raise ValueError("threshold must be an integer in 0..255")
    return (x0, y0, x1, y1), threshold


def validate_approved_config(
    path: str | Path,
    *,
    artifacts: Mapping[str, Any],
    roi: Sequence[int],
    threshold: int,
    boot_id: str | None = None,
    installation_id: str = DEFAULT_INSTALLATION_ID,
    source_first_frame: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Check explicit frame/ROI/threshold approval against current inputs."""
    value = _read_json_object(path, "approved-config")
    expected_roi, expected_threshold = validate_roi_threshold(roi, threshold)
    if expected_threshold not in {30, 60, 90, 120}:
        raise ValueError("approved threshold must be one of 30, 60, 90, or 120")
    expected_artifacts = _artifact_hashes(artifacts)
    expected_keys = {"schema_version", "installation_id", "source_first_frame", "roi", "threshold",
                     "artifacts", "boot_id", "operator_confirmation"}
    if set(value) != expected_keys or type(value.get("schema_version")) is not int or value["schema_version"] != APPROVAL_SCHEMA_VERSION:
        raise ValueError("approved-config must use schema_version 1 with the exact approved fields")
    if type(value.get("installation_id")) is not str or value["installation_id"] != installation_id:
        raise ValueError("approved-config installation_id differs from the current installation")
    if _artifact_hashes(value.get("artifacts", {})) != expected_artifacts:
        raise ValueError("approved-config artifact hashes differ from current signed artifacts")
    try:
        approved_roi, approved_threshold = validate_roi_threshold(value.get("roi"), value.get("threshold"))
    except ValueError as exc:
        raise ValueError(f"approved-config ROI/threshold is invalid: {exc}") from exc
    if approved_roi != expected_roi:
        raise ValueError("approved-config ROI differs from the requested ROI")
    if approved_threshold != expected_threshold:
        raise ValueError("approved-config threshold differs from the requested threshold")
    expected_boot = value.get("boot_id")
    if type(expected_boot) is not str or not expected_boot.strip():
        raise ValueError("approved-config must bind the current boot_id")
    if boot_id is not None and expected_boot != boot_id:
        raise ValueError("approved-config boot_id differs from the current board boot")

    source = value.get("source_first_frame")
    if not isinstance(source, dict) or set(source) != {"session_id", "epoch", "request_id", "raw_sha256"}:
        raise ValueError("approved-config source_first_frame identity has an invalid key set")
    if not isinstance(source.get("session_id"), str) or not source["session_id"].strip():
        raise ValueError("approved-config first frame has no session_id")
    if type(source.get("epoch")) is not int or not 0 <= source["epoch"] <= 0xFFFF:
        raise ValueError("approved-config first-frame epoch must be an integer in 0..65535")
    if type(source.get("request_id")) is not int or not 0 <= source["request_id"] <= 0xFFFF_FFFF:
        raise ValueError("approved-config first-frame request_id must be an integer in 0..2^32-1")
    source_sha = source.get("raw_sha256")
    if not isinstance(source_sha, str) or len(source_sha) != 64:
        raise ValueError("approved-config first-frame raw_sha256 is missing or malformed")
    try:
        int(source_sha, 16)
    except ValueError as exc:
        raise ValueError("approved-config first-frame raw_sha256 is malformed") from exc
    normalized_source = {
        "session_id": source["session_id"], "epoch": source["epoch"],
        "request_id": source["request_id"], "raw_sha256": source_sha.lower(),
    }
    if source_first_frame is not None:
        expected_source = {
            "session_id": source_first_frame.get("session_id"),
            "epoch": source_first_frame.get("epoch"),
            "request_id": source_first_frame.get("request_id"),
            "raw_sha256": str(source_first_frame.get("raw_sha256", "")).lower(),
        }
        if normalized_source != expected_source:
            raise ValueError("approved-config source first-frame identity or raw SHA differs from captured evidence")

    confirmation = value.get("operator_confirmation")
    if not isinstance(confirmation, dict) or set(confirmation) != {"confirmed", "statement", "host_utc"}:
        raise ValueError("operator confirmation must use the exact confirmed/statement/host_utc fields")
    if confirmation.get("confirmed") is not True:
        raise ValueError("approved-config requires explicit operator confirmation")
    if not isinstance(confirmation.get("statement"), str) or not confirmation["statement"].strip():
        raise ValueError("operator confirmation requires a non-empty statement")
    host_utc = confirmation.get("host_utc")
    if not isinstance(host_utc, str) or not host_utc.strip():
        raise ValueError("operator confirmation requires host_utc")
    try:
        parsed_utc = datetime.fromisoformat(host_utc.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("operator confirmation host_utc must be an ISO timestamp") from exc
    if parsed_utc.tzinfo is None or parsed_utc.utcoffset() != timedelta(0):
        raise ValueError("operator confirmation host_utc must explicitly use UTC")
    return {**value, "roi": list(expected_roi), "threshold": expected_threshold,
            "artifacts": expected_artifacts, "source_first_frame": normalized_source}


def validate_host_count(count: int) -> int:
    """Bound small offline test runs; real board acquisition always uses 1000."""
    if type(count) is not int or not 1 <= count <= 10:
        raise ValueError("--count is a host-only algorithm test limit in 1..10; board stability always uses 1000")
    return count


def require_pair_smoke(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    identities = {(item.get("epoch"), item.get("request_id")) for item in records}
    complete = (
        len(records) == PAIR_SMOKE_COUNT
        and len(identities) == PAIR_SMOKE_COUNT
        and all(item.get("comparison_passed") is True and item.get("raw_bytes") == FRAME_BYTES for item in records)
    )
    result = {"passed": complete, "count": len(records), "distinct_ids": len(identities),
              "required": PAIR_SMOKE_COUNT, "raw_bytes_each": FRAME_BYTES}
    if not complete:
        raise RuntimeError("10-pair smoke gate requires ten distinct successful raw/Profile comparisons")
    return result


def validate_loaded_receipt(path: str | Path, artifacts: Mapping[str, Any]) -> dict[str, Any]:
    record = _read_json_object(path, "loaded-overlay receipt")
    if record.get("loaded") is not True and record.get("overlay_loaded") is not True:
        raise ValueError("loaded-overlay receipt does not confirm an active Overlay")
    if not isinstance(record.get("boot_identity"), (dict, str)) or not record.get("boot_identity"):
        raise ValueError("loaded-overlay receipt must bind the current boot identity")
    if not isinstance(record.get("capture_checkpoint"), dict):
        raise ValueError("attach requires the latest clean capture handoff checkpoint")
    hashes = _artifact_hashes(artifacts)
    for name in ("bit", "hwh", "manifest"):
        if str(record.get(f"{name}_sha256", "")).lower() != hashes[name]:
            raise ValueError(f"loaded-overlay receipt {name} hash differs from signed artifacts")
    return record


def validate_power_off_proof(path: str | Path) -> dict[str, Any]:
    record = _read_json_object(path, "physical power-off proof")
    if not any(record.get(field) is True for field in (
        "physical_power_off_confirmed", "power_off_confirmed", "power_off_confirmed_for_load"
    )):
        raise ValueError("one explicit physical power-off proof is required before an Overlay load")
    return record


def current_boot_id() -> str:
    source = Path("/proc/sys/kernel/random/boot_id")
    try:
        value = source.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("current Linux boot_id is unavailable on the PYNQ target") from exc
    if not value:
        raise RuntimeError("current Linux boot_id is empty")
    return value


@contextmanager
def _bounded_overlay_downloads(*, allow_one: bool) -> Iterator[dict[str, int]]:
    """Prevent any attach-time download and cap an authorized load at one."""
    from pynq import Overlay  # type: ignore

    original = Overlay.download
    state = {"downloads": 0}

    def bounded(instance: Any, *args: Any, **kwargs: Any) -> Any:
        state["downloads"] += 1
        if not allow_one or state["downloads"] > 1:
            raise RuntimeError("algorithm-v1 acceptance refused an unapproved or repeated Overlay download")
        return original(instance, *args, **kwargs)

    Overlay.download = bounded
    try:
        yield state
    finally:
        Overlay.download = original


def _guard_is_idle(runtime: Any) -> dict[str, Any]:
    status = dict(runtime.guard.status())
    if status.get("active") or status.get("step_pin") or not status.get("enable_pin"):
        raise RuntimeError("motor guard is not in direct-wire disabled idle")
    return status


def _create_runtime(args: argparse.Namespace, output: Path, *, hdmi: bool) -> tuple[Any, int]:
    from structured_light.motion.pynq_runtime import create_stage_b_runtime

    receipt = getattr(args, "receipt", None)
    power_proof = getattr(args, "power_off_proof", None)
    if (receipt is None) == (power_proof is None):
        raise ValueError("provide exactly one of --receipt (attach) or --power-off-proof (single load)")
    admission: dict[str, Any]
    if receipt is not None:
        admission = {"attach": True, "loaded_overlay_receipt": receipt}
        allow_one_download = False
    else:
        admission = {"load": True, "power_off_confirmation": power_proof}
        allow_one_download = True

    roi = tuple(args.roi)
    with _bounded_overlay_downloads(allow_one=allow_one_download) as downloads:
        runtime = create_stage_b_runtime(
            args.manifest,
            stage="static",
            output=output / "runtime",
            installation=args.installation_id,
            roi=roi,
            settle_roi=roi,
            stripe_roi=roi,
            threshold=args.threshold,
            no_display=not hdmi,
            **admission,
        )
    if (allow_one_download and downloads["downloads"] != 1) or (not allow_one_download and downloads["downloads"] != 0):
        try:
            runtime.close()
        finally:
            raise RuntimeError("Overlay load/attach count differs from the requested admission")
    return runtime, downloads["downloads"]


def _artifact_inventory(manifest: str | Path) -> dict[str, Any]:
    from structured_light.motion.pynq_runtime import verify_stage_b_artifacts

    checked = verify_stage_b_artifacts(manifest)
    return {
        "manifest": str(checked["manifest"]),
        "bit": str(checked["bit"]),
        "hwh": str(checked["hwh"]),
        "contract": str(checked["contract"]),
        "files": checked["files"],
        "contract_sha256": checked["contract_sha256"],
        "source_sha256": checked["source_sha256"],
        "overlay_mode": checked["overlay_mode"],
        "part": checked["part"],
        "preboard_passed": checked["preboard_passed"],
    }


def _offline_artifact_inventory(manifest_path: str | Path) -> dict[str, Any]:
    """Verify the bound file hashes for CPU analysis without importing runtime/PYNQ code."""
    manifest = Path(manifest_path).resolve(strict=True)
    record = _read_json_object(manifest, "Overlay manifest")
    if record.get("preboard_passed") is not True or record.get("overlay_mode") != "structured_light_profile_hdmi":
        raise ValueError("CPU analysis requires the accepted signed profile+HDMI manifest")
    if record.get("part") != "xc7z020clg400-1" or record.get("carrier_mode") != "direct_wire":
        raise ValueError("CPU analysis manifest target/carrier differs from PYNQ-Z2 direct-wire artifacts")
    if record.get("frame_width") != 752 or record.get("frame_height") != 480:
        raise ValueError("CPU analysis manifest frame size differs from 752x480")
    files = record.get("files")
    if not isinstance(files, dict) or not isinstance(files.get("bit"), dict) or not isinstance(files.get("hwh"), dict):
        raise ValueError("Overlay manifest is missing bit/HWH records")
    actual_files: dict[str, dict[str, Any]] = {}
    for name in ("bit", "hwh"):
        item = files[name]
        rel = item.get("path")
        if not isinstance(rel, str) or not rel:
            raise ValueError(f"Overlay manifest {name} path is missing")
        artifact = Path(rel)
        if not artifact.is_absolute():
            artifact = manifest.parent / artifact
        artifact = artifact.resolve(strict=True)
        digest = sha256_file(artifact)
        if digest.lower() != str(item.get("sha256", "")).lower():
            raise ValueError(f"Overlay manifest {name} SHA-256 differs from file bytes")
        if type(item.get("size")) is not int or item["size"] != artifact.stat().st_size:
            raise ValueError(f"Overlay manifest {name} size differs from file bytes")
        actual_files[name] = {"path": str(artifact), "sha256": digest, "size": artifact.stat().st_size}
    contract_rel = record.get("contract_file")
    if not isinstance(contract_rel, str) or not contract_rel:
        raise ValueError("Overlay manifest contract_file is missing")
    contract = (manifest.parent / contract_rel).resolve(strict=True)
    contract_sha = sha256_file(contract)
    if contract_sha.lower() != str(record.get("contract_sha256", "")).lower():
        raise ValueError("Overlay manifest contract SHA-256 differs from file bytes")
    actual_files["manifest"] = {"path": str(manifest), "sha256": sha256_file(manifest), "size": manifest.stat().st_size}
    return {
        "manifest": str(manifest), "bit": actual_files["bit"]["path"], "hwh": actual_files["hwh"]["path"],
        "contract": str(contract), "files": actual_files, "contract_sha256": contract_sha,
        "source_sha256": record.get("source_sha256"), "overlay_mode": record["overlay_mode"],
        "part": record["part"], "preboard_passed": True,
    }


def _record_board_context(output: Path, runtime: Any, artifacts: Mapping[str, Any], *, downloads: int) -> dict[str, Any]:
    boot_id = current_boot_id()
    context = {
        "schema": "algorithm_v1_board_session_v1",
        "session_id": str(runtime.controller.session_id),
        "installation_id": str(getattr(runtime.session, "installation", DEFAULT_INSTALLATION_ID)),
        "boot_id": boot_id,
        "artifacts": _artifact_hashes(artifacts["files"]),
        "artifact_catalog": dict(artifacts["files"]),
        "manifest": artifacts["manifest"],
        "overlay_mode": artifacts["overlay_mode"],
        "overlay_downloads": downloads,
        "motion_commands": 0,
        "created_utc": utc_now(),
    }
    write_json(output / "board_session.json", context)
    return context


def _signal_gate(runtime: Any) -> dict[str, Any]:
    signals = runtime.signals()
    if signals.get("passed") is not True or signals.get("signals", {}).get("camera_signal_verified") is not True:
        raise RuntimeError("camera ID and 16-frame FV/LV/PCLK signal gate failed")
    return signals


def load_first_frame_binding(path: str | Path, *, artifacts: Mapping[str, Any],
                            boot_id: str, installation_id: str) -> dict[str, Any]:
    binding_path = Path(path)
    if binding_path.is_dir():
        binding_path = binding_path / "first_frame_binding.json"
    binding_path = binding_path.resolve(strict=True)
    value = _read_json_object(binding_path, "first-frame binding")
    prepared_roi = (0, 0, 752, 480)
    if value.get("stage") != "first-frame" or value.get("passed") is not True:
        raise ValueError("first-frame binding is not a passing one-pair capture")
    if value.get("boot_id") != boot_id or value.get("installation_id") != installation_id:
        raise ValueError("first-frame boot/install identity differs from the current board run")
    if value.get("roi") != list(prepared_roi) or value.get("threshold") != 60:
        raise ValueError("first-frame preparation must use full-frame ROI and threshold 60")
    if _artifact_hashes(value.get("artifacts", {})) != _artifact_hashes(artifacts):
        raise ValueError("first-frame artifact hashes differ from current signed artifacts")
    source = value.get("source_first_frame")
    if not isinstance(source, dict) or set(source) != {"session_id", "epoch", "request_id", "raw_sha256"}:
        raise ValueError("first-frame binding lacks the strict source identity")
    if (type(source.get("epoch")) is not int or not 0 <= source["epoch"] <= 0xFFFF
            or type(source.get("request_id")) is not int or not 0 <= source["request_id"] <= 0xFFFF_FFFF):
        raise ValueError("first-frame source epoch/request_id are outside the hardware identity range")
    if not isinstance(source.get("raw_sha256"), str) or len(source["raw_sha256"]) != 64:
        raise ValueError("first-frame raw SHA-256 is missing or malformed")
    raw_value = value.get("frame_path")
    if not isinstance(raw_value, str) or not raw_value:
        raise ValueError("first-frame binding has no raw frame path")
    raw_path = Path(raw_value)
    if not raw_path.is_absolute():
        raw_path = binding_path.parent / raw_path
    raw_path = raw_path.resolve(strict=True)
    raw_sha = sha256_file(raw_path)
    if raw_path.stat().st_size != FRAME_BYTES or raw_sha != source.get("raw_sha256"):
        raise ValueError("first-frame raw bytes do not match the recorded SHA-256")
    if source.get("session_id") != value.get("session_id"):
        raise ValueError("first-frame source session differs from its board session")
    capture = value.get("capture", {})
    if (capture.get("request_id") != source.get("request_id") or capture.get("epoch") != source.get("epoch")
            or capture.get("raw_sha256") != source.get("raw_sha256")
            or capture.get("raw_bytes") != FRAME_BYTES or capture.get("comparison_passed") is not True):
        raise ValueError("first-frame pair identity differs from the source binding")
    return {"session_id": source["session_id"], "epoch": source["epoch"],
            "request_id": source["request_id"], "raw_sha256": raw_sha}


def load_approved_stability_binding(
    evidence_root: str | Path,
    approved_config: str | Path,
    *,
    artifacts: Mapping[str, Any],
    roi: Sequence[int],
    threshold: int,
    boot_id: str,
    installation_id: str,
) -> dict[str, Any]:
    status = _read_json_object(Path(evidence_root) / "stability" / "status.json", "stability status")
    if status.get("passed") is not True or status.get("completed") != BOARD_STATIC_COUNT:
        raise ValueError("recovery/HDMI requires a completed approved stability batch")
    if status.get("boot_id") != boot_id or status.get("installation_id") != installation_id:
        raise ValueError("current board boot/installation differs from the approved stability batch")
    if _artifact_hashes(status.get("artifact_hashes", {})) != _artifact_hashes(artifacts):
        raise ValueError("stability artifact hashes differ from current signed artifacts")
    source = status.get("source_first_frame")
    if not isinstance(source, dict):
        raise ValueError("stability status lacks the approved first-frame identity")
    return validate_approved_config(
        approved_config, artifacts=artifacts, roi=roi, threshold=threshold,
        boot_id=boot_id, installation_id=installation_id, source_first_frame=source,
    )


def analyze_recovery_posts(
    recovery_root: str | Path,
    *,
    roi: Sequence[int],
    threshold: int,
    timeout_s: float = 60,
) -> dict[str, Any]:
    """Analyze the 32 bounded recovery post-capture frames after PYNQ close."""
    root = Path(recovery_root).resolve(strict=True)
    records_path = root / "recovery_records.jsonl"
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(records_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or value.get("passed") is not True:
            raise ValueError(f"recovery record line {line_no} failed")
        records.append(value)
    if len(records) != RECOVERY_COUNT:
        raise ValueError(f"recovery sidecar analysis requires exactly {RECOVERY_COUNT} post-capture records")
    from structured_light.stripe_v1 import board_evidence
    from structured_light.profile import decode_profile

    evidence_root = root.parent.parent.resolve(strict=True)
    out_dir = root.parent / "algorithm" / "fixed"
    out_dir.mkdir(parents=True)
    refs_path = root / "analysis_records.jsonl"
    completed = 0
    capture_session_ids: set[str] = set()
    with refs_path.open("x", encoding="utf-8", newline="\n") as stream:
        with board_evidence.BoundedCPUWorker() as worker:
            for index, record in enumerate(records):
                post = record.get("post_recovery")
                if not isinstance(post, dict) or post.get("comparison_passed") is not True:
                    raise ValueError(f"recovery record {index} has no passing post-capture pair")
                relative_result = post.get("session_record")
                if not isinstance(relative_result, str) or not relative_result:
                    raise ValueError(f"recovery record {index} has no session result path")
                result_path = (root / relative_result).resolve(strict=True)
                if not result_path.is_relative_to(root):
                    raise ValueError("recovery session result path escapes the recovery evidence root")
                capture = _read_json_object(result_path, "recovery session capture")
                if capture.get("comparison", {}).get("passed") is not True:
                    raise ValueError(f"recovery session capture {index} same-frame comparison did not pass")
                if capture.get("request_id") != post.get("request_id") or capture.get("epoch") != post.get("epoch"):
                    raise ValueError(f"recovery session capture {index} identity differs from its recovery record")
                raw_name = capture.get("raw_file")
                if not isinstance(raw_name, str) or not raw_name:
                    raise ValueError(f"recovery session capture {index} has no raw file")
                raw_path = (result_path.parent / raw_name).resolve(strict=True)
                raw_bytes = raw_path.read_bytes()
                raw_hash = sha256_bytes(raw_bytes)
                if len(raw_bytes) != FRAME_BYTES or raw_hash != capture.get("raw_sha256") or raw_hash != post.get("raw_sha256"):
                    raise ValueError(f"recovery raw frame {index} byte count or SHA-256 differs")
                profile_name, diag_name = capture.get("profile_file"), capture.get("diagnostics_file")
                if not isinstance(profile_name, str) or not isinstance(diag_name, str):
                    raise ValueError(f"recovery capture {index} lacks its Profile/diagnostic links")
                profile_path = (result_path.parent / profile_name).resolve(strict=True)
                diag_path = (result_path.parent / diag_name).resolve(strict=True)
                if not profile_path.is_relative_to(root) or not diag_path.is_relative_to(root):
                    raise ValueError("recovery Profile or diagnostic path escapes its evidence root")
                profile_hash = sha256_file(profile_path)
                if profile_hash != capture.get("profile_sha256") or profile_path.stat().st_size != capture.get("profile_length"):
                    raise ValueError(f"recovery Profile payload {index} differs from its session receipt")
                decoded_profile = decode_profile(profile_path.read_bytes(), frame_width=752, frame_height=480)
                if decoded_profile.format_version != 2 or decoded_profile.profile_id != int(capture["request_id"]):
                    raise ValueError(f"recovery Profile payload {index} ID/version differs from request")
                diagnostics = _read_json_object(diag_path, "recovery diagnostics")
                if (diagnostics.get("request_id") != capture.get("request_id")
                        or diagnostics.get("epoch") != capture.get("epoch")
                        or diagnostics.get("row_count") != 480
                        or any(row.get("profile_id") != capture.get("request_id") for row in diagnostics.get("rows", ()))):
                    raise ValueError(f"recovery diagnostics {index} are not bound to the post-capture pair")
                session_id = capture.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    raise ValueError(f"recovery session capture {index} has no session_id")
                capture_session_ids.add(session_id)
                identity = {
                    "session_id": session_id,
                    "epoch": int(capture["epoch"]),
                    "request_id": int(capture["request_id"]),
                    "expected_raw_sha256": raw_hash,
                    "capture_index": index,
                }
                analysis = worker.analyze_frame(
                    raw_path, mode="fixed", roi=tuple(roi), threshold=threshold,
                    identity=identity, timeout_s=timeout_s,
                )
                if not isinstance(analysis, dict) or analysis.get("canonical_payload", {}).get("identity") != identity:
                    raise ValueError(f"recovery CPU analysis {index} canonical identity differs")
                sidecar = out_dir / f"frame_{index:04d}.json"
                write_json(sidecar, analysis)
                ref = {"stage": "recovery", "index": index, "mode": "fixed",
                       "session_id": session_id, "epoch": identity["epoch"], "request_id": identity["request_id"],
                       "profile_id": decoded_profile.profile_id,
                       "raw_path": raw_path.relative_to(evidence_root).as_posix(), "raw_sha256": raw_hash,
                       "identity": identity,
                       "sidecar_path": sidecar.relative_to(evidence_root).as_posix(),
                       "canonical_sha256": analysis.get("canonical_sha256")}
                stream.write(json.dumps(ref, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                completed += 1
                del raw_bytes
    return {"passed": completed == RECOVERY_COUNT, "requested": RECOVERY_COUNT,
            "completed": completed, "sample_count": completed, "mode": "fixed",
            "capture_session_ids": sorted(capture_session_ids),
            "records_path": refs_path.relative_to(evidence_root).as_posix(),
            "sidecar_root": out_dir.relative_to(evidence_root).as_posix()}


_FONT_5X7 = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    " ": ("00000",) * 7,
}


def _draw_fixed_text(canvas_rgb: Any, text_value: str, *, x: int, y: int,
                     color: tuple[int, int, int], scale: int = 2) -> None:
    for character in text_value:
        glyph = _FONT_5X7.get(character)
        if glyph is None:
            raise ValueError(f"fixed HDMI bitmap font has no glyph for {character!r}")
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit == "1":
                    canvas_rgb[y + row * scale:y + (row + 1) * scale,
                               x + column * scale:x + (column + 1) * scale, :] = color
        x += 6 * scale


def build_a0_fixed_canvas(pair: Any, analysis: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """Render A0 cyan and new fixed green centers using a fixed bitmap font."""
    import numpy as np

    raw = bytes(pair.raw_bytes)
    if len(raw) != FRAME_BYTES:
        raise ValueError("HDMI comparison requires one complete paired 752x480 raw frame")
    payload = analysis.get("canonical_payload")
    selected = payload.get("selected_profile") if isinstance(payload, Mapping) else None
    selected_rows = selected.get("rows") if isinstance(selected, Mapping) else None
    if not isinstance(selected_rows, list) or len(selected_rows) != 480:
        raise ValueError("fixed analysis sidecar does not contain all 480 selected-profile rows")

    width, height = 1280, 720
    x0, y0 = (width - 752) // 2, (height - 480) // 2
    gray = np.frombuffer(raw, dtype=np.uint8).reshape(480, 752)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    canvas[:, :] = (36, 24, 16)  # RGB view of canonical BGR background (16,24,36)
    canvas[y0:y0 + 480, x0:x0 + 752, :] = gray[:, :, None]
    a0: dict[int, int] = {}
    for item in pair.diagnostics.rows:
        if bool(item.valid) and int(item.candidate_count) == 1:
            a0[int(item.row)] = int(item.xc_fixed)
    new: dict[int, int] = {}
    rejected: dict[str, int] = {}
    for index, row in enumerate(selected_rows):
        if not isinstance(row, dict) or row.get("row") != index:
            raise ValueError("fixed selected-profile rows are not in source order")
        candidate = row.get("candidate")
        if candidate is not None:
            if not isinstance(candidate, dict) or type(candidate.get("center_q12")) is not int:
                raise ValueError(f"fixed selected-profile row {index} has a malformed center")
            new[index] = int(candidate["center_q12"])
        else:
            reason = str(row.get("reason") or "unspecified")
            rejected[reason] = rejected.get(reason, 0) + 1

    def cross(cx: int, cy: int, color: tuple[int, int, int]) -> None:
        if 0 <= cx < width and 0 <= cy < height:
            canvas[cy, max(0, cx - 2):min(width, cx + 3), :] = color
            canvas[max(0, cy - 2):min(height, cy + 3), cx, :] = color

    for row, xc in a0.items():
        x, y = x0 + int(round(xc / 4096)), y0 + row - 3
        cross(x, y, (0, 255, 255))
    for row, xc in new.items():
        x, y = x0 + int(round(xc / 4096)), y0 + row + 3
        cross(x, y, (0, 255, 0))

    comparison_passed = bool(getattr(getattr(pair, "comparison", None), "passed", False))
    request_id, epoch = int(pair.request_id), int(pair.epoch)
    labels = (
        "A0 CYAN V1 GREEN",
        f"REQ {request_id} EPOCH {epoch}",
        f"A0 {len(a0)} NEW {len(new)} CMP {'PASS' if comparison_passed else 'FAIL'}",
        f"REJECT {sum(rejected.values())}",
    )
    for line, value in enumerate(labels):
        _draw_fixed_text(canvas, value, x=24, y=10 + line * 16, color=(255, 255, 255), scale=2)
    common = sorted(a0.keys() & new.keys())
    differences = [abs(a0[row] - new[row]) / 4096.0 for row in common]
    bgr = canvas[:, :, ::-1].copy().tobytes()
    metrics = {
        "canvas_width": width, "canvas_height": height,
        "a0_color_rgb": [0, 255, 255], "new_fixed_color_rgb": [0, 255, 0],
        "visual_row_offsets_px": {"a0": -3, "new_fixed": 3},
        "a0_valid_rows": len(a0), "new_fixed_rows": len(new), "common_rows": len(common),
        "comparison_passed": comparison_passed, "request_id": request_id, "epoch": epoch,
        "rejected_rows": sum(rejected.values()), "rejection_reason_counts": rejected,
        "mean_absolute_center_difference_px": sum(differences) / len(differences) if differences else None,
        "max_absolute_center_difference_px": max(differences) if differences else None,
        "metric_scope": "unlabeled pixel-center comparison only; not accuracy or improvement",
        "raw_sha256": sha256_bytes(raw), "analysis_canonical_sha256": analysis.get("canonical_sha256"),
        "text_font": "fixed 5x7 bitmap glyphs, scale=2",
    }
    return bgr, metrics


def _export_pair(pair: Any, output: Path, index: int) -> dict[str, Any]:
    from b_finish_static import export_result

    record = export_result(pair, output, index)
    raw_path = output / f"frame_{index:03d}" / "raw_gray8.bin"
    raw = bytes(pair.raw_bytes)
    if len(raw) != FRAME_BYTES:
        raise RuntimeError("paired capture raw frame is not exactly 752x480 gray8")
    with raw_path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    record["raw_path"] = str(raw_path)
    record["raw_sha256"] = sha256_bytes(raw)
    return record


def _validate_capture_admission(args: argparse.Namespace, artifacts: Mapping[str, Any]) -> None:
    if args.receipt is not None:
        validate_loaded_receipt(args.receipt, artifacts["files"])
    else:
        validate_power_off_proof(args.power_off_proof)


def run_preflight(args: argparse.Namespace, output: Path) -> dict[str, Any]:
    artifacts = _artifact_inventory(args.manifest)
    _validate_capture_admission(args, artifacts)
    roi, threshold = validate_roi_threshold(args.roi, args.threshold)
    return {
        "schema": "algorithm_v1_board_preflight_v1", "stage": "preflight", "passed": True,
        "artifacts": artifacts, "artifact_hashes": _artifact_hashes(artifacts["files"]),
        "admission": "attach" if args.receipt is not None else "single_load_with_power_off_proof",
        "boot_id": _read_json_object(args.receipt, "loaded-overlay receipt").get("boot_identity") if args.receipt is not None else None,
        "roi": list(roi), "threshold": threshold,
        "motion_commands": 0, "overlay_downloads": 0, "hardware_touched": False,
    }


def _board_stage(args: argparse.Namespace, output: Path, *, stage: str) -> dict[str, Any]:
    artifacts = _artifact_inventory(args.manifest)
    _validate_capture_admission(args, artifacts)
    validate_roi_threshold(args.roi, args.threshold)
    runtime = None
    result: dict[str, Any] = {
        "schema": "algorithm_v1_board_stage_v1", "stage": stage, "passed": False,
        "motion_commands": 0, "overlay_downloads": 0, "hidden_retry": False,
        "artifact_hashes": _artifact_hashes(artifacts["files"]),
        "requested_roi": list(args.roi), "requested_threshold": args.threshold,
    }
    capture_roi = (0, 0, 752, 480) if stage == "first-frame" else tuple(args.roi)
    capture_threshold = 60 if stage == "first-frame" else args.threshold
    runtime_args = argparse.Namespace(**vars(args))
    runtime_args.roi = capture_roi
    runtime_args.threshold = capture_threshold
    result["capture_roi"] = list(capture_roi)
    result["capture_threshold"] = capture_threshold
    status = {"schema": "algorithm_v1_board_status_v1", "stage": stage, "phase": "starting",
              "passed": False, "requested": None, "completed": 0, "updated_utc": utc_now()}
    update_status(output, status)
    try:
        runtime, downloads = _create_runtime(runtime_args, output, hdmi=(stage == "hdmi"))
        result["overlay_downloads"] = downloads
        context = _record_board_context(output, runtime, artifacts, downloads=downloads)
        result["session_id"] = context["session_id"]
        result["installation_id"] = context["installation_id"]
        result["boot_id"] = context["boot_id"]
        guard_before = _guard_is_idle(runtime)
        signals = _signal_gate(runtime)
        result["guard_before"] = guard_before
        result["signals"] = signals
        status.update({"phase": "running", "session_id": context["session_id"], "updated_utc": utc_now()})
        update_status(output, status)

        if stage in {"recovery", "hdmi"}:
            approved = load_approved_stability_binding(
                output.parent, args.approved_config, artifacts=artifacts["files"],
                roi=args.roi, threshold=args.threshold, boot_id=context["boot_id"],
                installation_id=args.installation_id,
            )
            result["approved_config"] = {
                "schema_version": approved["schema_version"],
                "source_first_frame": approved["source_first_frame"],
                "boot_id": approved["boot_id"], "installation_id": approved["installation_id"],
                "roi": approved["roi"], "threshold": approved["threshold"],
                "artifacts": approved["artifacts"],
            }

        if stage == "first-frame":
            status.update({"requested": 1, "phase": "first-frame", "updated_utc": utc_now()})
            update_status(output, status)
            pair = runtime.first_frame()  # exactly one raw/Profile pair
            capture = _export_pair(pair, output, 0)
            if capture.get("comparison_passed") is not True:
                raise RuntimeError("first paired frame did not pass same-frame comparison")
            frame_binding = {
                "schema": "algorithm_v1_first_frame_v1", "stage": "first-frame", "passed": True,
                "session_id": context["session_id"], "capture": capture,
                "boot_id": context["boot_id"], "installation_id": context["installation_id"],
                "source_first_frame": {
                    "session_id": context["session_id"], "epoch": int(pair.epoch),
                    "request_id": int(pair.request_id), "raw_sha256": capture["raw_sha256"],
                },
                "frame_path": Path(capture["raw_path"]).resolve().relative_to(output.resolve()).as_posix(),
                "frame_sha256": capture["raw_sha256"],
                "roi": list(capture_roi), "threshold": capture_threshold,
                "artifact_hashes": context["artifacts"], "artifacts": context["artifact_catalog"],
            }
            write_json(output / "first_frame_binding.json", frame_binding)
            result.update({"requested": 1, "completed": 1, "first_frame": frame_binding})

        elif stage == "stability":
            first_frame_root = args.first_frame_root or (Path(args.approved_config).resolve().parent / "first-frame")
            first_frame = load_first_frame_binding(
                first_frame_root, artifacts=artifacts["files"],
                boot_id=context["boot_id"], installation_id=args.installation_id,
            )
            approved = validate_approved_config(
                args.approved_config, artifacts=artifacts["files"], roi=args.roi, threshold=args.threshold,
                boot_id=context["boot_id"], installation_id=args.installation_id,
                source_first_frame=first_frame,
            )
            result["approved_config"] = {k: approved[k] for k in (
                "schema_version", "source_first_frame", "boot_id", "installation_id",
                "roi", "threshold", "artifacts", "operator_confirmation"
            )}
            result["source_first_frame"] = first_frame
            smoke_dir = output / "pair_smoke"
            smoke_dir.mkdir()
            smoke_records: list[dict[str, Any]] = []
            status.update({"requested": PAIR_SMOKE_COUNT, "phase": "pair-smoke", "updated_utc": utc_now()})
            update_status(output, status)
            for index in range(PAIR_SMOKE_COUNT):
                pair = runtime.session.run_static(raw=False)
                record = _export_pair(pair, smoke_dir, index)
                smoke_records.append(record)
                status.update({"completed": index + 1, "updated_utc": utc_now()})
                update_status(output, status)
            result["pair_smoke"] = require_pair_smoke(smoke_records)
            write_json(output / "pair_smoke_records.json", smoke_records)

            result["requested"] = BOARD_STATIC_COUNT
            status.update({"requested": BOARD_STATIC_COUNT, "completed": 0, "phase": "raw-1000", "updated_utc": utc_now()})
            update_status(output, status)
            result["raw"] = runtime.static(count=BOARD_STATIC_COUNT, mode="raw")
            result["raw_session_id"] = context["session_id"]
            _guard_is_idle(runtime)
            status.update({"completed": BOARD_STATIC_COUNT, "phase": "profile-1000", "updated_utc": utc_now()})
            update_status(output, status)
            result["profile"] = runtime.static(count=BOARD_STATIC_COUNT, mode="profile")
            _guard_is_idle(runtime)
            result["completed"] = BOARD_STATIC_COUNT

        elif stage == "recovery":
            status.update({"requested": RECOVERY_COUNT, "phase": "recovery-32", "updated_utc": utc_now()})
            update_status(output, status)
            import p2_closeout_board as recovery

            closeout_config = {
                "width": 752, "height": 480, "overlay_mode": "structured_light_profile_hdmi",
                "closeout_threshold": args.threshold, "closeout_roi": list(args.roi),
                "min_width": 1, "max_width": 32, "min_sum": 30, "min_peak": 15,
                "normal_timeout_cycles": 100_000_000, "watchdog_timeout_cycles": 500_000,
            }
            recovery_root = output / "recovery"
            recovery_root.mkdir()
            recovery_config_path = recovery_root / "closeout_config.json"
            write_json(recovery_config_path, closeout_config)
            result["recovery_config_path"] = recovery_config_path.relative_to(output.parent.resolve()).as_posix()
            signal = signals["signals"]
            original_prepare = recovery._prepare_board

            def attached_setup(config: dict[str, Any], config_path: Path, target: Path, *, mode: str) -> dict[str, Any]:
                if mode != "structured_light_profile_hdmi":
                    raise RuntimeError("recovery attempted to switch away from the attached signed Overlay")
                target.mkdir(parents=True, exist_ok=True)
                return {"transport": runtime.transport, "signal": signal, "overlay": runtime.overlay}

            recovery._prepare_board = attached_setup
            try:
                result["recovery"] = recovery.run_recovery_stage(
                    closeout_config, recovery_config_path, recovery_root,
                    RECOVERY_COUNT, include_host_timeout=True,
                )
            finally:
                recovery._prepare_board = original_prepare
            if result["recovery"].get("count_completed") != RECOVERY_COUNT or result["recovery"].get("passed") is not True:
                raise RuntimeError("recovery stage did not complete all 32 bounded trials")
            result.update({"requested": RECOVERY_COUNT, "completed": RECOVERY_COUNT})

        elif stage == "hdmi":
            import numpy as np
            from PIL import Image
            from structured_light.stripe_v1 import board_evidence
            result["requested"] = 10
            status.update({"requested": 10, "phase": "hdmi-captures", "updated_utc": utc_now()})
            update_status(output, status)
            runtime.display.display.configure()
            runtime.display.configured = True
            comparison_dir = output / "comparison"
            comparison_dir.mkdir()
            analysis_dir = output / "analysis" / "fixed"
            analysis_dir.mkdir(parents=True)
            refs_path = output / "hdmi_pairs.jsonl"
            captures: list[dict[str, Any]] = []
            pair_refs: list[dict[str, Any]] = []
            context_root = output.parent.resolve(strict=True)
            with refs_path.open("x", encoding="utf-8", newline="\n") as stream, board_evidence.BoundedCPUWorker() as worker:
                for index in range(10):
                    pair = runtime.session.run_static(raw=False)
                    record = _export_pair(pair, output / "hdmi_frames", index)
                    session_id = str(runtime.controller.session_id)
                    identity = {
                        "session_id": session_id,
                        "epoch": int(pair.epoch),
                        "request_id": int(pair.request_id),
                        "expected_raw_sha256": record["raw_sha256"],
                        "capture_index": index,
                    }
                    analysis = worker.analyze_frame(
                        Path(record["raw_path"]), mode="fixed", roi=tuple(args.roi),
                        threshold=args.threshold, identity=identity, timeout_s=60,
                    )
                    if not isinstance(analysis, dict) or analysis.get("canonical_payload", {}).get("identity") != identity:
                        raise ValueError(f"HDMI fixed analysis {index} identity differs from its paired capture")
                    sidecar = analysis_dir / f"frame_{index:04d}.json"
                    write_json(sidecar, analysis)
                    canvas, pixel_metrics = build_a0_fixed_canvas(pair, analysis)
                    canvas_bgr_sha = sha256_bytes(canvas)
                    canvas_path = comparison_dir / f"frame_{index:04d}.png"
                    rgb = np.frombuffer(canvas, dtype=np.uint8).reshape(720, 1280, 3)[:, :, ::-1]
                    Image.fromarray(rgb).save(canvas_path)
                    readback_rgb = np.asarray(Image.open(canvas_path).convert("RGB"), dtype=np.uint8)
                    readback_bgr = readback_rgb[:, :, ::-1].copy().tobytes()
                    canvas_png_sha = sha256_file(canvas_path)
                    if sha256_bytes(readback_bgr) != canvas_bgr_sha:
                        raise RuntimeError(f"HDMI comparison canvas {index} PNG readback differs from source bytes")
                    display_receipt = runtime.display.display.show(canvas)
                    if not isinstance(display_receipt, Mapping) or display_receipt.get("canvas_sha256") != canvas_bgr_sha:
                        raise RuntimeError(f"HDMI display readback {index} does not match the saved comparison canvas")
                    from structured_light.p2.hdmi_output import bgr_to_hdmi_memory
                    expected_memory_sha = sha256_bytes(bgr_to_hdmi_memory(canvas))
                    if (display_receipt.get("hdmi_memory_sha256") != expected_memory_sha
                            or display_receipt.get("hdmi_memory_lane_order") != "G,B,R,X"):
                        raise RuntimeError(f"HDMI buffer hash/lane readback {index} differs from the expected memory image")

                    result_path = output / "runtime" / "sessions" / session_id / (
                        f"capture_e{int(pair.epoch):08x}_r{int(pair.request_id):08x}.result.json"
                    )
                    capture_evidence = _read_json_object(result_path, "HDMI paired session result")
                    if (capture_evidence.get("session_id") != session_id
                            or capture_evidence.get("epoch") != pair.epoch
                            or capture_evidence.get("request_id") != pair.request_id
                            or capture_evidence.get("raw_sha256") != record["raw_sha256"]
                            or capture_evidence.get("comparison", {}).get("passed") is not True):
                        raise RuntimeError(f"HDMI pair {index} SessionRecorder identity/comparison differs")
                    session_raw_path = result_path.parent / str(capture_evidence["raw_file"])
                    if (session_raw_path.stat().st_size != FRAME_BYTES
                            or sha256_file(session_raw_path) != record["raw_sha256"]):
                        raise RuntimeError(f"HDMI SessionRecorder raw file {index} differs from the saved pair")
                    profile_path = result_path.parent / str(capture_evidence["profile_file"])
                    profile_sha = sha256_file(profile_path)
                    if profile_sha != capture_evidence.get("profile_sha256"):
                        raise RuntimeError(f"HDMI Profile payload {index} hash differs from SessionRecorder")
                    if int(pair.profile.profile_id) != int(pair.request_id):
                        raise RuntimeError(f"HDMI Profile ID {index} differs from paired request ID")
                    ref = {
                        "stage": "hdmi", "index": index, "session_id": session_id,
                        "epoch": int(pair.epoch), "request_id": int(pair.request_id),
                        "profile_id": int(pair.profile.profile_id),
                        "raw_path": session_raw_path.resolve().relative_to(context_root).as_posix(),
                        "raw_sha256": record["raw_sha256"],
                        "profile_path": profile_path.resolve().relative_to(context_root).as_posix(),
                        "profile_sha256": profile_sha,
                        "session_record": result_path.resolve().relative_to(context_root).as_posix(),
                        "comparison_passed": True, "identity": identity,
                        "analysis_sidecar_path": sidecar.relative_to(context_root).as_posix(),
                        "analysis_canonical_sha256": analysis.get("canonical_sha256"),
                        "canvas_path": canvas_path.relative_to(context_root).as_posix(),
                        "canvas_png_sha256": canvas_png_sha,
                        "canvas_bgr_sha256": canvas_bgr_sha,
                        "canvas_source": {
                            "raw_path": session_raw_path.resolve().relative_to(context_root).as_posix(),
                            "session_record": result_path.resolve().relative_to(context_root).as_posix(),
                            "analysis_sidecar_path": sidecar.relative_to(context_root).as_posix(),
                            "a0_color_rgb": pixel_metrics["a0_color_rgb"],
                            "new_fixed_color_rgb": pixel_metrics["new_fixed_color_rgb"],
                            "visual_row_offsets_px": pixel_metrics["visual_row_offsets_px"],
                            "canvas_png_sha256": canvas_png_sha,
                            "canvas_bgr_sha256": canvas_bgr_sha,
                        },
                        "pixel_center_metrics": pixel_metrics,
                        "display_receipt": dict(display_receipt),
                    }
                    stream.write(json.dumps(ref, ensure_ascii=False, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    record.update({"display_receipt": dict(display_receipt),
                                   "canvas_png_sha256": canvas_png_sha,
                                   "canvas_bgr_sha256": canvas_bgr_sha,
                                   "pixel_center_metrics": pixel_metrics})
                    captures.append(record)
                    pair_refs.append(ref)
                    status.update({"completed": index + 1, "updated_utc": utc_now()})
                    update_status(output, status)
                    del analysis, canvas, readback_bgr, readback_rgb, rgb
            result["capture_gate"] = require_pair_smoke(captures)
            result["capture_count"] = len(captures)
            result["pairs_path"] = refs_path.relative_to(context_root).as_posix()
            result["pair_references"] = pair_refs
            final_binding = {
                "request_id": pair_refs[-1]["request_id"],
                "final_canvas_png_sha256": pair_refs[-1]["canvas_png_sha256"],
                "final_canvas_bgr_sha256": pair_refs[-1]["canvas_bgr_sha256"],
            }
            reply_binding = {
                "final_request_id": final_binding["request_id"],
                "final_canvas_png_sha256": final_binding["final_canvas_png_sha256"],
                "final_canvas_bgr_sha256": final_binding["final_canvas_bgr_sha256"],
            }
            result.update({
                "final_request_id": final_binding["request_id"],
                "last_canvas_png_sha256": final_binding["final_canvas_png_sha256"],
                "last_canvas_bgr_sha256": final_binding["final_canvas_bgr_sha256"],
                "completed": 10,
            })
            nonce = uuid.uuid4().hex
            result["visual_confirmation_path"] = (output / "hdmi_accepted.json").relative_to(context_root).as_posix()
            result["operator_nonce"] = nonce
            waiting = {"phase": "hdmi", "nonce": nonce, "waiting": True,
                       "timeout_s": args.confirm_timeout_s,
                       "final_request_id": final_binding["request_id"],
                       "final_canvas_png_sha256": final_binding["final_canvas_png_sha256"],
                       "final_canvas_bgr_sha256": final_binding["final_canvas_bgr_sha256"],
                       "statement_required": "Confirm the A0 cyan and algorithm-v1 fixed green centers on the current camera image are visibly correct."}
            write_json(output / "live_status.json", waiting, replace=True)
            print("WAIT " + json.dumps(waiting, ensure_ascii=False), flush=True)
            result["visual_confirmation"] = wait_reply(output, phase="hdmi", nonce=nonce,
                                                        timeout_s=args.confirm_timeout_s,
                                                        expected_binding=reply_binding)
            result["hdmi_static_verified"] = True

        guard_after = _guard_is_idle(runtime)
        result["guard_after"] = guard_after
        result["passed"] = True
        return result
    except BaseException as exc:
        result.update({"passed": False, "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        if runtime is not None:
            if "guard_after" not in result:
                try:
                    result["guard_after"] = _guard_is_idle(runtime)
                except BaseException as exc:
                    try:
                        result["guard_after"] = dict(runtime.guard.status())
                    except BaseException:
                        result["guard_after"] = None
                    result["passed"] = False
                    result["guard_error"] = f"{type(exc).__name__}: {exc}"
            try:
                cleanup = runtime.close()
                result["cleanup"] = cleanup
                if cleanup.get("closed") is not True or not cleanup.get("capture_handoff"):
                    result["passed"] = False
                    result["cleanup_error"] = "runtime did not close with a fresh capture handoff"
            except BaseException as exc:
                result["passed"] = False
                result["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        if stage == "recovery" and result.get("passed") is True and result.get("cleanup", {}).get("closed") is True:
            try:
                result["post_close_analysis"] = analyze_recovery_posts(
                    output / "recovery", roi=args.roi, threshold=args.threshold, timeout_s=60
                )
                if result["post_close_analysis"].get("passed") is not True:
                    result["passed"] = False
            except BaseException as exc:
                result["passed"] = False
                result["post_close_analysis_error"] = f"{type(exc).__name__}: {exc}"
        result["completed_utc"] = utc_now()
        final_status = {
            "schema": "algorithm_v1_board_status_v1", "stage": stage,
            "phase": "complete" if result.get("passed") else "failed",
            "passed": bool(result.get("passed")),
            "requested": result.get("requested"), "completed": result.get("completed", 0),
            "session_id": result.get("session_id"), "artifact_hashes": result.get("artifact_hashes"),
            "installation_id": result.get("installation_id"), "boot_id": result.get("boot_id"),
            "source_first_frame": result.get("source_first_frame"),
            "motion_commands": 0, "overlay_downloads": result.get("overlay_downloads", 0),
            "hidden_retry": False, "updated_utc": utc_now(),
        }
        if stage == "recovery" and result.get("post_close_analysis"):
            final_status["post_close_analysis"] = result["post_close_analysis"]
        if stage == "recovery":
            final_status["recovery_config_path"] = result.get("recovery_config_path")
        cleanup = result.get("cleanup", {})
        final_status["cleanup_closed"] = cleanup.get("closed") is True
        final_status["capture_handoff"] = cleanup.get("capture_handoff")
        final_status["guard_after"] = result.get("guard_after")
        final_status["final_request_id"] = result.get("final_request_id")
        final_status["last_canvas_png_sha256"] = result.get("last_canvas_png_sha256")
        final_status["last_canvas_bgr_sha256"] = result.get("last_canvas_bgr_sha256")
        final_status["operator_nonce"] = result.get("operator_nonce")
        final_status["visual_confirmation_path"] = result.get("visual_confirmation_path")
        stage_error = (result.get("error") or result.get("guard_error") or result.get("cleanup_error")
                       or result.get("post_close_analysis_error"))
        if stage_error:
            final_status["error"] = stage_error
        update_status(output, final_status)
        if not (output / "summary.json").exists():
            write_json(output / "summary.json", result)


def wait_reply(output: Path, *, phase: str, nonce: str, timeout_s: float,
               expected_binding: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(timeout_s, (int, float)) or not 0 < float(timeout_s) <= 1800:
        raise ValueError("visual confirmation timeout must be in (0, 1800] seconds")
    reply_path = output / f"{phase}_reply.json"
    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        if reply_path.exists():
            reply = _read_json_object(reply_path, "operator visual confirmation")
            if (reply.get("phase") != phase or reply.get("nonce") != nonce
                    or reply.get("confirmed") is not True
                    or not isinstance(reply.get("statement"), str)
                    or not reply["statement"].strip()):
                raise ValueError("visual confirmation must match the live nonce and include an explicit statement")
            if expected_binding is not None:
                for key, expected in expected_binding.items():
                    if reply.get(key) != expected:
                        raise ValueError(f"visual confirmation {key} does not match the final displayed canvas")
            write_json(output / f"{phase}_accepted.json", reply)
            return reply
        time.sleep(0.25)
    raise TimeoutError(f"operator visual confirmation deadline expired for {phase}")


def _load_stability_capture_records(root: Path, count: int) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    status_path = root / "status.json"
    status = _read_json_object(status_path, "stability status")
    expected_count = BOARD_STATIC_COUNT if count == BOARD_STATIC_COUNT else count
    if status.get("stage") != "stability" or status.get("passed") is not True or status.get("completed") != expected_count:
        raise ValueError(f"stability root is not a completed {expected_count}-frame Profile-paired run")
    session_id = status.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("stability status has no board capture session_id")
    paths = list(root.rglob("profile_static_records.jsonl"))
    if len(paths) != 1:
        raise ValueError("stability root must contain exactly one profile_static_records.jsonl")
    records_path = paths[0]
    profile_records: list[dict[str, Any]] = []
    for line_no, line in enumerate(records_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"raw capture record line {line_no} is invalid JSON") from exc
        if not isinstance(value, dict) or value.get("passed") is not True or value.get("mode") != "profile":
            raise ValueError(f"profile capture record line {line_no} is not a successful paired raw/Profile capture")
        profile_records.append(value)
    if len(profile_records) != count:
        raise ValueError(f"stability paired Profile records count {len(profile_records)} differs from required {count}")

    session_root = records_path.parent / "sessions" / session_id
    if not session_root.is_dir():
        raise ValueError("stability Profile session directory is missing")
    session_meta = _read_json_object(session_root / "metadata.json", "Profile session metadata")
    if session_meta.get("session_id") != session_id:
        raise ValueError("Profile session metadata session_id differs from stability status")
    records: list[dict[str, Any]] = []
    ids: set[tuple[int, int]] = set()
    for index, value in enumerate(profile_records):
        if value.get("index") != index:
            raise ValueError("Profile static records are not in exact acquisition order")
        request_id, epoch = value.get("request_id"), value.get("epoch")
        if type(request_id) is not int or type(epoch) is not int:
            raise ValueError(f"paired Profile record {index} lacks request/epoch identity")
        identity = (epoch, request_id)
        if identity in ids:
            raise ValueError("paired Profile records contain a duplicate (epoch,request_id)")
        ids.add(identity)
        result_name = f"capture_e{epoch:08x}_r{request_id:08x}.result.json"
        result_path = session_root / result_name
        capture = _read_json_object(result_path, "paired Profile session result")
        if (capture.get("session_id") != session_id or capture.get("request_id") != request_id
                or capture.get("epoch") != epoch):
            raise ValueError(f"paired Profile session result {index} has a mismatched session/request identity")
        comparison = capture.get("comparison")
        if not isinstance(comparison, dict) or comparison.get("passed") is not True:
            raise ValueError(f"paired Profile session result {index} lacks a passing raw/Profile comparison")
        raw_name, profile_name, diag_name = (capture.get(key) for key in ("raw_file", "profile_file", "diagnostics_file"))
        if not all(isinstance(name, str) and name for name in (raw_name, profile_name, diag_name)):
            raise ValueError(f"paired Profile session result {index} has incomplete raw/Profile/diagnostic file links")
        raw_path, profile_path, diag_path = (session_root / name for name in (raw_name, profile_name, diag_name))
        raw_path, profile_path, diag_path = (path.resolve(strict=True) for path in (raw_path, profile_path, diag_path))
        if any(not path.is_relative_to(session_root.resolve(strict=True)) for path in (raw_path, profile_path, diag_path)):
            raise ValueError("paired session file path escapes its capture session")
        raw_digest, profile_digest = sha256_file(raw_path), sha256_file(profile_path)
        if (raw_path.stat().st_size != FRAME_BYTES or capture.get("raw_length") != FRAME_BYTES
                or raw_digest != capture.get("raw_sha256") or raw_digest != value.get("raw_sha256")):
            raise ValueError(f"paired Profile raw frame {index} differs from both capture receipts")
        if (profile_digest != capture.get("profile_sha256") or profile_digest != value.get("profile_sha256")
                or capture.get("profile_length") != profile_path.stat().st_size):
            raise ValueError(f"paired Profile payload {index} differs from both capture receipts")
        diagnostics = _read_json_object(diag_path, "paired capture diagnostics")
        if diagnostics.get("request_id") != request_id or diagnostics.get("epoch") != epoch:
            raise ValueError(f"paired capture diagnostics {index} identity differs from request/epoch")
        if diagnostics.get("row_count") != 480 or len(diagnostics.get("rows", ())) != 480:
            raise ValueError(f"paired capture diagnostics {index} do not cover all 480 rows")
        if any(row.get("profile_id") != request_id for row in diagnostics["rows"]):
            raise ValueError(f"paired capture diagnostics {index} contain a mixed Profile ID")
        from structured_light.profile import decode_profile
        decoded_profile = decode_profile(profile_path.read_bytes(), frame_width=752, frame_height=480)
        if decoded_profile.format_version != 2 or decoded_profile.profile_id != request_id:
            raise ValueError(f"paired capture Profile payload {index} ID/version differs from request")
        records.append({**value, "profile_id": decoded_profile.profile_id,
                        "raw_file": str(raw_path), "raw_sha256": raw_digest,
                        "profile_file": str(profile_path), "profile_sha256": profile_digest,
                        "diagnostics_file": str(diag_path), "session_id": session_id})
    return records, session_id, status


def _resolve_raw_record(root: Path, record: Mapping[str, Any]) -> tuple[Path, str]:
    raw_value = record.get("raw_file")
    if not isinstance(raw_value, str) or not raw_value:
        raise ValueError("raw capture record is missing raw_file")
    raw_path = Path(raw_value)
    if not raw_path.is_absolute():
        raw_path = root / raw_path
    raw_path = raw_path.resolve(strict=True)
    if not raw_path.is_relative_to(root.resolve(strict=True)):
        raise ValueError("raw capture path escapes the selected stability root")
    digest = sha256_file(raw_path)
    expected = record.get("raw_sha256")
    if raw_path.stat().st_size != FRAME_BYTES or not isinstance(expected, str) or digest.lower() != expected.lower():
        raise ValueError("raw capture byte count or SHA-256 differs from its board receipt")
    if type(record.get("request_id")) is not int or type(record.get("epoch")) is not int:
        raise ValueError("raw capture record is missing request/epoch identity")
    return raw_path, digest


def run_algorithm_stage(
    *,
    stability_root: str | Path,
    output: str | Path,
    approved_config: str | Path,
    artifacts: Mapping[str, Any],
    roi: Sequence[int],
    threshold: int,
    count: int = BOARD_STATIC_COUNT,
    boot_id: str | None = None,
    installation_id: str = DEFAULT_INSTALLATION_ID,
) -> dict[str, Any]:
    """Analyze previously captured gray8 files with no PYNQ/hardware import."""
    if count != BOARD_STATIC_COUNT:
        validate_host_count(count)
    roi_value, threshold_value = validate_roi_threshold(roi, threshold)
    root = Path(stability_root).resolve(strict=True)
    out = Path(output).resolve()
    evidence_root = out.parent.resolve(strict=True)
    if not root.is_relative_to(evidence_root):
        raise ValueError("stability root must be a stage directory below the shared evidence root")
    out.mkdir(parents=True, exist_ok=False)
    status: dict[str, Any] = {
        "schema": "algorithm_v1_cpu_status_v1", "stage": "algorithm", "phase": "starting",
        "passed": False, "requested": None, "completed": 0, "hidden_retry": False,
        "updated_utc": utc_now(),
    }
    update_status(out, status)
    completed = 0
    result: dict[str, Any] = {"schema": "algorithm_v1_cpu_summary_v1", "stage": "algorithm",
                              "passed": False, "count": count, "roi": list(roi_value),
                              "threshold": threshold_value, "modes": list(ALGORITHM_MODES),
                              "hidden_retry": False, "warmups_excluded_from_sample_count": True,
                              "sample_counts": {"fixed": 0, "local31": 0, "local63": 0},
                              "warmup_counts": {"fixed": 0, "local31": 0, "local63": 0},
                              "requested_profile_pair_count": count, "measured_sample_count": 0}
    try:
        stability_status = _read_json_object(root / "status.json", "stability status")
        current_boot = boot_id or stability_status.get("boot_id")
        current_installation = str(stability_status.get("installation_id", installation_id))
        if not isinstance(current_boot, str) or not current_boot:
            raise ValueError("stability status has no current board boot_id")
        if current_installation != installation_id:
            raise ValueError("stability installation_id differs from the requested installation")
        if stability_status.get("artifact_hashes") is None or _artifact_hashes(stability_status["artifact_hashes"]) != _artifact_hashes(artifacts):
            raise ValueError("stability status artifact hashes differ from current signed artifacts")
        records, session_id, stability_status = _load_stability_capture_records(root, count)
        source_first_frame = stability_status.get("source_first_frame")
        if not isinstance(source_first_frame, dict):
            raise ValueError("stability status lacks the first-frame approval identity")
        approved = validate_approved_config(
            approved_config, artifacts=artifacts, roi=roi_value, threshold=threshold_value,
            boot_id=current_boot, installation_id=installation_id,
            source_first_frame=source_first_frame,
        )
        raw_inputs = [_resolve_raw_record(root, record) for record in records]
        result["session_id"] = session_id
        result["boot_id"] = current_boot
        result["installation_id"] = current_installation
        result["artifact_hashes"] = _artifact_hashes(artifacts)
        result["approved_first_frame"] = approved["source_first_frame"]
        approved_copy = out / "approved_config.json"
        write_json(approved_copy, approved)
        context = {
            "schema": "algorithm_v1_cpu_context_v1", "stage": "algorithm",
            "session_id": session_id, "artifact_hashes": result["artifact_hashes"],
            "artifact_catalog": dict(artifacts), "stability_root": root.relative_to(evidence_root).as_posix(),
            "evidence_root": str(evidence_root),
            "boot_id": current_boot, "installation_id": current_installation,
            "approved_config": approved_copy.relative_to(evidence_root).as_posix(),
            "approved_source_first_frame": approved["source_first_frame"],
            "roi": list(roi_value), "threshold": threshold_value,
            "count": count, "modes": list(ALGORITHM_MODES), "created_utc": utc_now(),
        }
        write_json(out / "algorithm_context.json", context)

        from structured_light.stripe_v1 import board_evidence

        selection = board_evidence.choose_indices(count=count)
        expected_selection = {
            "fixed": list(range(count)),
            "local31": list(range(0, count, 10)),
            "local63": list(range(0, count, 10)),
        }
        if selection != expected_selection:
            raise ValueError("CPU helper index selection differs from the fixed/local board acceptance contract")
        status.update({"requested": sum(len(selection[k]) for k in selection), "phase": "analyzing", "updated_utc": utc_now()})
        update_status(out, status)
        records_out = out / "algorithm_records.jsonl"
        warmups_out = out / "algorithm_warmups.jsonl"
        result["index_selection"] = selection
        requested_sample_counts = {key: len(selection[key]) for key in ALGORITHM_MODES}
        result["requested_sample_counts"] = requested_sample_counts
        result["sample_counts"] = {key: 0 for key in ALGORITHM_MODES}
        result["requested_profile_pair_count"] = count
        result["measured_sample_count"] = sum(result["sample_counts"].values())
        result["warmup_counts"] = {key: 0 for key in ALGORITHM_MODES}
        with records_out.open("x", encoding="utf-8", newline="\n") as stream, \
                warmups_out.open("x", encoding="utf-8", newline="\n") as warmup_stream:
            for mode in ALGORITHM_MODES:
                selected_indices = selection[mode]
                if not selected_indices:
                    continue
                first_index = selected_indices[0]
                raw_path, raw_hash = raw_inputs[first_index]
                first_receipt = records[first_index]
                first_identity = {
                    "session_id": session_id, "epoch": int(first_receipt["epoch"]),
                    "request_id": int(first_receipt["request_id"]),
                    "expected_raw_sha256": raw_hash, "capture_index": first_index,
                }
                with board_evidence.BoundedCPUWorker() as worker:
                    for warmup_index in range(3):
                        analysis = worker.analyze_frame(
                            raw_path, mode=mode, roi=roi_value, threshold=threshold_value,
                            identity=first_identity, timeout_s=60,
                        )
                        if not isinstance(analysis, dict) or analysis.get("canonical_payload", {}).get("identity") != first_identity:
                            raise ValueError("CPU helper warmup canonical identity differs from the first paired frame")
                        sidecar = out / "warmups" / mode / f"warmup_{warmup_index:02d}.json"
                        write_json(sidecar, analysis)
                        warmup_ref = {
                            "stage": "algorithm", "mode": mode, "warmup_index": warmup_index,
                            "index": first_index,
                            "raw_path": raw_path.relative_to(evidence_root).as_posix(), "raw_sha256": raw_hash,
                            "identity": first_identity,
                            "sidecar_path": sidecar.relative_to(evidence_root).as_posix(),
                            "canonical_sha256": analysis.get("canonical_sha256"),
                        }
                        warmup_stream.write(json.dumps(warmup_ref, ensure_ascii=False, sort_keys=True) + "\n")
                        warmup_stream.flush()
                        os.fsync(warmup_stream.fileno())
                        result["warmup_counts"][mode] += 1
                        del analysis
                    for index in selected_indices:
                        raw_path, raw_hash = raw_inputs[index]
                        source_receipt = records[index]
                        identity = {
                            "session_id": session_id,
                            "epoch": int(source_receipt["epoch"]),
                            "request_id": int(source_receipt["request_id"]),
                            "expected_raw_sha256": raw_hash,
                            "capture_index": index,
                        }
                        analysis = worker.analyze_frame(
                            raw_path, mode=mode, roi=roi_value, threshold=threshold_value,
                            identity=identity, timeout_s=60,
                        )
                        if not isinstance(analysis, dict):
                            raise ValueError("CPU helper must return its canonical analysis as a JSON object")
                        if analysis.get("canonical_payload", {}).get("identity") != identity:
                            raise ValueError("CPU helper canonical identity differs from the board capture receipt")
                        sidecar = out / mode / f"frame_{index:04d}.json"
                        write_json(sidecar, analysis)
                        record_out = {
                            "stage": "algorithm", "index": index, "mode": mode,
                            "raw_path": raw_path.relative_to(evidence_root).as_posix(), "raw_sha256": raw_hash,
                            "profile_id": int(source_receipt.get("profile_id", source_receipt["request_id"])),
                            "identity": identity,
                            "sidecar_path": sidecar.relative_to(evidence_root).as_posix(),
                            "canonical_sha256": analysis.get("canonical_sha256"),
                        }
                        stream.write(json.dumps(record_out, ensure_ascii=False, sort_keys=True) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                        completed += 1
                        result["sample_counts"][mode] += 1
                        status.update({"completed": completed, "updated_utc": utc_now()})
                        update_status(out, status)
                        del analysis
        if completed != sum(requested_sample_counts.values()) or result["sample_counts"] != requested_sample_counts:
            raise RuntimeError("measured CPU output count differs from the declared index selection")
        result.update({
            "passed": True, "completed": completed, "measured_sample_count": completed,
            "sample_count": completed,
            "warmups_excluded_from_sample_count": True,
            "records_path": records_out.relative_to(evidence_root).as_posix(),
            "warmups_path": warmups_out.relative_to(evidence_root).as_posix(),
            "context_path": (out / "algorithm_context.json").relative_to(evidence_root).as_posix(),
            "approved_config_path": approved_copy.relative_to(evidence_root).as_posix(),
        })
        return result
    except BaseException as exc:
        result.update({"passed": False, "completed": completed, "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        status.update({"phase": "complete" if result.get("passed") else "failed",
                       "passed": bool(result.get("passed")), "completed": completed,
                       "session_id": result.get("session_id"), "boot_id": result.get("boot_id"),
                       "installation_id": result.get("installation_id"),
                       "artifact_hashes": result.get("artifact_hashes"),
                       "error": result.get("error"), "updated_utc": utc_now()})
        update_status(out, status)
        if not (out / "summary.json").exists():
            write_json(out / "summary.json", result)


def _run_algorithm(args: argparse.Namespace, output: Path) -> dict[str, Any]:
    artifacts = _offline_artifact_inventory(args.manifest)
    stability_status = _read_json_object(Path(args.stability_root) / "status.json", "stability status")
    validate_approved_config(
        args.approved_config, artifacts=artifacts["files"], roi=args.roi, threshold=args.threshold,
        boot_id=stability_status.get("boot_id"), installation_id=args.installation_id,
        source_first_frame=stability_status.get("source_first_frame"),
    )
    return run_algorithm_stage(
        stability_root=args.stability_root,
        output=output,
        approved_config=args.approved_config,
        artifacts=artifacts["files"],
        roi=args.roi,
        threshold=args.threshold,
        count=args.count if args.count is not None else BOARD_STATIC_COUNT,
        boot_id=stability_status.get("boot_id"),
        installation_id=args.installation_id,
    )


def _hdmi_functional_pending_visual(root: Path, status: Mapping[str, Any]) -> bool:
    if ((root / "hdmi_accepted.json").is_file()
            or status.get("stage") != "hdmi"
            or status.get("requested") != 10 or status.get("completed") != 10
            or status.get("cleanup_closed") is not True or not status.get("capture_handoff")
            or status.get("motion_commands") != 0 or status.get("hidden_retry") is not False):
        return False
    guard = status.get("guard_after")
    if (not isinstance(guard, dict) or guard.get("active") is not False
            or guard.get("step_pin") is not False or guard.get("enable_pin") is not True):
        return False
    live_path = root / "live_status.json"
    refs_path = root / "hdmi_pairs.jsonl"
    if not live_path.is_file() or not refs_path.is_file():
        return False
    try:
        live = _read_json_object(live_path, "HDMI nonce request")
        refs = [json.loads(line) for line in refs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (ValueError, json.JSONDecodeError):
        return False
    if live.get("phase") != "hdmi" or live.get("waiting") is not True or len(refs) != 10:
        return False
    final = refs[-1]
    return (
        final.get("request_id") == live.get("final_request_id") == status.get("final_request_id")
        and final.get("canvas_png_sha256") == live.get("final_canvas_png_sha256") == status.get("last_canvas_png_sha256")
        and final.get("canvas_bgr_sha256") == live.get("final_canvas_bgr_sha256") == status.get("last_canvas_bgr_sha256")
        and bool(final.get("display_receipt", {}).get("hdmi_memory_sha256"))
    )


def _load_board_auditor() -> Any:
    """Load the independent evidence auditor without importing board runtime code."""
    path = ROOT / "build" / "scripts" / "audit_algorithm_v1_board.py"
    if not path.is_file():
        raise FileNotFoundError(f"independent board auditor is missing: {path}")
    name = "algorithm_v1_board_auditor"
    existing = sys.modules.get(name)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path.resolve():
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load independent board auditor: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run_audit(args: argparse.Namespace, output: Path) -> dict[str, Any]:
    evidence_root_value = args.evidence_root or args.stability_root
    if evidence_root_value is None:
        raise ValueError("audit requires --evidence-root")
    root = Path(evidence_root_value).resolve(strict=True)
    artifacts = _offline_artifact_inventory(args.manifest)
    hashes = _artifact_hashes(artifacts["files"])
    required = ("preflight", "first-frame", "stability", "algorithm", "recovery", "hdmi")
    stage_rows = []
    stage_statuses: dict[str, dict[str, Any]] = {}
    stage_roots: dict[str, Path] = {}
    pending_visual = False
    for stage in required:
        status_path = root / stage / "status.json"
        if not status_path.is_file():
            raise ValueError(f"audit is missing the stage status at {status_path}")
        status = _read_json_object(status_path, f"{stage} status")
        if status.get("passed") is not True:
            if stage == "hdmi" and _hdmi_functional_pending_visual(root / "hdmi", status):
                pending_visual = True
            else:
                raise ValueError(f"audit found {stage} stage incomplete or failed")
        stage_hashes = status.get("artifact_hashes")
        if stage_hashes is not None and _artifact_hashes(stage_hashes) != hashes:
            raise ValueError(f"audit {stage} artifact hashes differ from the current signed artifacts")
        if stage in {"first-frame", "stability", "recovery", "hdmi"}:
            if status.get("motion_commands") != 0 or status.get("hidden_retry") is not False:
                raise ValueError(f"audit {stage} status reports a motor command or hidden retry")
            if status.get("cleanup_closed") is not True or not status.get("capture_handoff"):
                raise ValueError(f"audit {stage} did not close cleanly with a fresh capture handoff")
            guard = status.get("guard_after")
            if (not isinstance(guard, dict) or guard.get("active") is not False
                    or guard.get("step_pin") is not False or guard.get("enable_pin") is not True):
                raise ValueError(f"audit {stage} did not finish with the direct-wire guard idle")
        stage_rows.append({"stage": stage, "status_path": str(status_path),
                           "passed": status.get("passed") is True,
                           "status": "functional_pending_visual" if stage == "hdmi" and pending_visual else status.get("phase"),
                           "artifact_hashes": stage_hashes})
        stage_statuses[stage] = status
        stage_roots[stage] = status_path.parent
    board_stages = ("first-frame", "stability", "recovery", "hdmi")
    sessions = []
    for stage in board_stages:
        session = _read_json_object(stage_roots[stage] / "board_session.json", f"{stage} board session")
        if session.get("session_id") != stage_statuses[stage].get("session_id"):
            raise ValueError(f"audit {stage} board_session ID differs from its stage status")
        if _artifact_hashes(session.get("artifact_catalog", {})) != hashes:
            raise ValueError(f"audit {stage} board_session artifact catalog differs from the current artifacts")
        if _artifact_hashes(session.get("artifacts", {})) != hashes:
            raise ValueError(f"audit {stage} board_session artifact hashes differ from the current artifacts")
        if session.get("motion_commands") != 0:
            raise ValueError(f"audit {stage} board_session reports a motor command")
        sessions.append(session)
    session_ids = {item.get("session_id") for item in sessions if item.get("session_id")}
    if len(session_ids) != len(board_stages):
        raise ValueError("audit board stages do not have distinct SessionRecorder/controller identities")
    boot_ids = {item.get("boot_id") for item in sessions if item.get("boot_id")}
    if len(boot_ids) != 1:
        raise ValueError("audit board sessions do not share one current Linux boot_id")
    installations = {item.get("installation_id") for item in sessions}
    if len(installations) != 1 or not all(isinstance(item, str) and item for item in installations):
        raise ValueError("audit board sessions do not share one explicit installation_id")
    downloads = sum(int(item.get("overlay_downloads", 0)) for item in sessions)
    if downloads > 1:
        raise ValueError("audit found more than one explicit Overlay download across acceptance stages")
    for stage in ("first-frame", "stability", "recovery", "hdmi"):
        if stage_statuses[stage].get("boot_id") not in boot_ids:
            raise ValueError(f"audit {stage} board status boot_id differs from the shared session boot")

    stability_summary = _read_json_object(stage_roots["stability"] / "summary.json", "stability summary")
    if stability_summary.get("pair_smoke", {}).get("passed") is not True:
        raise ValueError("audit stability summary does not contain the required 10-pair smoke pass")
    if (stability_summary.get("raw", {}).get("count_completed") != BOARD_STATIC_COUNT
            or stability_summary.get("profile", {}).get("count_completed") != BOARD_STATIC_COUNT):
        raise ValueError("audit stability summary does not contain raw1000 and Profile1000")

    auditor = _load_board_auditor()

    algorithm_root = stage_roots["algorithm"]
    algorithm_summary = _read_json_object(algorithm_root / "summary.json", "algorithm summary")
    stability_status = stage_statuses["stability"]
    approved_config_path = root / str(algorithm_summary["approved_config_path"])
    validate_approved_config(
        approved_config_path,
        artifacts=artifacts["files"], roi=algorithm_summary["roi"],
        threshold=algorithm_summary["threshold"],
        boot_id=stability_status.get("boot_id"),
        installation_id=stability_status.get("installation_id", DEFAULT_INSTALLATION_ID),
        source_first_frame=stability_status.get("source_first_frame"),
    )
    stability_root = stage_roots["stability"]
    stability_runtime = stability_root / "runtime"
    profile_records_path = stability_runtime / "profile_static_records.jsonl"
    capture_config_path = stability_runtime / "capture_config.json"
    approval_bindings = {
        stage: auditor.audit_capture_config_against_approval(
            stage_roots[stage] / "runtime" / "capture_config.json",
            approved_config_path, stage=stage,
        )
        for stage in ("stability", "recovery", "hdmi")
    }
    session_dir = stability_runtime / "sessions" / str(stability_status["session_id"])
    first_frame_root = stage_roots["first-frame"]
    first_frame_capture_config = first_frame_root / "runtime" / "capture_config.json"
    first_frame_audit = auditor.audit_first_frame(first_frame_root, first_frame_capture_config)
    expected_first = stability_status.get("source_first_frame")
    if (first_frame_audit.get("identity", {}).get("session_id") != expected_first.get("session_id")
            or first_frame_audit.get("identity", {}).get("epoch") != expected_first.get("epoch")
            or first_frame_audit.get("identity", {}).get("request_id") != expected_first.get("request_id")
            or first_frame_audit.get("raw_sha256") != expected_first.get("raw_sha256")):
        raise ValueError("audited first-frame SessionRecorder capture differs from the approved source identity")
    raw_audit = auditor.audit_raw_batch(
        stability_runtime / "raw_static_records.jsonl", root,
        expected_count=BOARD_STATIC_COUNT,
    )
    if raw_audit.get("passed") is not True or raw_audit.get("captures_checked") != BOARD_STATIC_COUNT:
        raise ValueError("independent raw-only batch audit did not verify all 1000 frames")
    profile_audit = auditor.audit_profile_session(
        session_dir,
        profile_records_path,
        capture_config_path,
        expected_profile_count=BOARD_STATIC_COUNT,
        raw_captures=raw_audit.get("captures"),
        expected_raw_count=BOARD_STATIC_COUNT,
        expected_smoke_count=PAIR_SMOKE_COUNT,
        smoke_records_path=stability_root / "pair_smoke_records.json",
        first_frame_identity={**first_frame_audit["identity"], "raw_sha256": first_frame_audit["raw_sha256"]},
    )
    if profile_audit.get("passed") is not True or profile_audit.get("session_id") != stability_status.get("session_id"):
        raise ValueError("independent Profile session audit differs from the accepted stability session")
    paired_records = profile_audit.get("profile_captures")
    if not isinstance(paired_records, list) or len(paired_records) != BOARD_STATIC_COUNT:
        raise ValueError("independent Profile audit did not return the exact 1000 paired identities")
    algorithm_audit = auditor.audit_algorithm_records(
        root / str(algorithm_summary["records_path"]), root,
        expected_count=BOARD_STATIC_COUNT,
        warmups_path=root / str(algorithm_summary["warmups_path"]),
        expected_roi=tuple(algorithm_summary["roi"]),
        expected_threshold=int(algorithm_summary["threshold"]),
        summary_path=algorithm_root / "summary.json",
        paired_captures=paired_records,
    )
    recovery_root = stage_roots["recovery"] / "recovery"
    previous_profile = max(paired_records, key=lambda item: item.get("capture_index", -1))
    previous_identity = {
        "request_id": previous_profile["identity"]["request_id"],
        "epoch": previous_profile["identity"]["epoch"],
    }
    recovery_audit = auditor.audit_recovery_stage(
        recovery_root, stage_roots["recovery"] / "runtime" / "capture_config.json", root,
        previous_identity=previous_identity,
    )
    hdmi_root = stage_roots["hdmi"]
    hdmi_capture_config_path = hdmi_root / "runtime" / "capture_config.json"
    hdmi_audit = auditor.audit_hdmi_stage(
        hdmi_root, hdmi_capture_config_path, root,
        expected_count=10,
        expected_roi=tuple(approval_bindings["hdmi"]["approved_roi"]),
        expected_threshold=approval_bindings["hdmi"]["approved_threshold"],
    )
    result = {
        "schema": "algorithm_v1_board_audit_v1", "stage": "audit",
        "passed": False, "status": "functional_pending_visual" if pending_visual else "complete",
        "artifact_hashes": hashes, "stages": stage_rows,
        "board_session_ids": sorted(session_ids), "board_session_count": len(sessions),
        "boot_id": next(iter(boot_ids)), "motion_commands": 0, "overlay_downloads": downloads,
        "first_frame_audit": first_frame_audit, "raw_audit": raw_audit, "profile_audit": profile_audit,
        "algorithm_audit": algorithm_audit, "recovery_audit": recovery_audit, "hdmi_audit": hdmi_audit,
        "approval_bindings": approval_bindings,
        "hardware_algorithm_deployment": False,
        "new_algorithm_execution_target": "PYNQ PS CPU; PL capture/Profile bitstream unchanged",
        "scope": "static acceptance of the existing signed capture path; CPU algorithm evidence only",
        "limitations": ["does not prove improved reflection handling", "does not validate calibration or metric accuracy"],
    }
    component_pass = all(
        isinstance(audit, Mapping) and audit.get("passed") is True
        for audit in (first_frame_audit, raw_audit, profile_audit, algorithm_audit, recovery_audit, hdmi_audit)
    )
    result["passed"] = component_pass and not pending_visual
    if (pending_visual or (isinstance(hdmi_audit, Mapping)
                           and hdmi_audit.get("status") == "functional_pending_visual")):
        result["status"] = "functional_pending_visual"
    elif not result["passed"]:
        result["status"] = "audit_failed"
    write_json(output / "audit_report.json", result)
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage", required=True, choices=STAGES)
    p.add_argument("--manifest", type=Path, required=True)
    admission = p.add_mutually_exclusive_group()
    admission.add_argument("--receipt", type=Path)
    admission.add_argument("--power-off-proof", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--threshold", type=int, default=60)
    p.add_argument("--roi", nargs=4, type=int, default=DEFAULT_ROI)
    p.add_argument("--approved-config", type=Path)
    p.add_argument("--first-frame-root", type=Path)
    p.add_argument("--stability-root", type=Path)
    p.add_argument("--evidence-root", type=Path)
    p.add_argument("--installation-id", default=DEFAULT_INSTALLATION_ID)
    p.add_argument("--count", type=int, help="host-only CPU algorithm test count (1..10); real board runs stay at 1000")
    p.add_argument("--confirm-timeout-s", type=float, default=600.0)
    return p


def _validate_args(args: argparse.Namespace, command: argparse.ArgumentParser) -> None:
    try:
        validate_roi_threshold(args.roi, args.threshold)
        if args.stage in {"preflight", "first-frame", "stability", "recovery", "hdmi"}:
            if (args.receipt is None) == (args.power_off_proof is None):
                raise ValueError("board phases require exactly one of --receipt or --power-off-proof")
        if args.stage in {"stability", "recovery", "hdmi"} and args.approved_config is None:
            raise ValueError(f"{args.stage} requires --approved-config bound to the reviewed first frame")
        if args.stage == "algorithm":
            if args.approved_config is None or args.stability_root is None:
                raise ValueError("algorithm requires --approved-config and --stability-root")
            if args.count is not None:
                validate_host_count(args.count)
        elif args.count is not None:
            raise ValueError("--count is available only for bounded CPU host tests; real board stages use fixed counts")
        if args.stage == "audit" and args.evidence_root is None and args.stability_root is None:
            raise ValueError("audit requires --evidence-root")
        if not 0 < args.confirm_timeout_s <= 1800:
            raise ValueError("--confirm-timeout-s must be in (0, 1800]")
    except (TypeError, ValueError) as exc:
        command.error(str(exc))


def main(argv: Sequence[str] | None = None) -> int:
    command = parser()
    args = command.parse_args(argv)
    _validate_args(args, command)
    output = args.output.resolve()
    if args.stage != "algorithm":
        output.mkdir(parents=True, exist_ok=False)
    started = utc_now()
    stage_name = args.stage
    result: dict[str, Any] = {"stage": stage_name, "passed": False, "started_utc": started,
                              "motion_commands": 0, "hidden_retry": False}
    if stage_name != "algorithm":
        update_status(output, {"stage": stage_name, "phase": "starting", "passed": False,
                               "completed": 0, "updated_utc": started})
    try:
        if stage_name == "preflight":
            result.update(run_preflight(args, output))
            write_json(output / "preflight.json", result)
        elif stage_name in {"first-frame", "stability", "recovery", "hdmi"}:
            result.update(_board_stage(args, output, stage=stage_name))
        elif stage_name == "algorithm":
            result.update(_run_algorithm(args, output))
        else:
            result.update(_run_audit(args, output))
        result["passed"] = result.get("passed") is True
    except BaseException as exc:
        result.update({"passed": False, "error": f"{type(exc).__name__}: {exc}",
                       "traceback": traceback.format_exc()})
        print(result["traceback"], flush=True)
    finally:
        result["completed_utc"] = utc_now()
        if stage_name == "algorithm" and not (output / "status.json").exists():
            output.mkdir(parents=True, exist_ok=False)
        previous_status = {}
        if (output / "status.json").exists():
            try:
                previous_status = _read_json_object(output / "status.json", "existing stage status")
            except ValueError:
                previous_status = {}
        status = {**previous_status, "schema": "algorithm_v1_board_status_v1", "stage": stage_name,
                  "phase": "complete" if result.get("passed") else "failed",
                  "passed": bool(result.get("passed")),
                  "requested": result.get("requested", previous_status.get("requested")),
                  "completed": result.get("completed", previous_status.get("completed", 0)),
                  "session_id": result.get("session_id", previous_status.get("session_id")),
                  "artifact_hashes": result.get("artifact_hashes", previous_status.get("artifact_hashes")),
                  "motion_commands": result.get("motion_commands", previous_status.get("motion_commands", 0)),
                  "overlay_downloads": result.get("overlay_downloads", previous_status.get("overlay_downloads", 0)),
                  "hidden_retry": False, "updated_utc": utc_now()}
        if result.get("status"):
            status["status"] = result["status"]
        if result.get("error"):
            status["error"] = result["error"]
        if stage_name != "algorithm" or not (output / "status.json").exists():
            update_status(output, status)
        if not (output / "summary.json").exists():
            write_json(output / "summary.json", result)
    # Keep console JSON portable to Windows CP950; saved evidence stays UTF-8.
    print(json.dumps(result, ensure_ascii=True, indent=2, default=str), flush=True)
    return 0 if result.get("passed") else 1


if __name__ == "__main__":  # pragma: no cover - exercised on a board or CLI smoke
    raise SystemExit(main())
