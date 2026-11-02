"""Capture one fixed five-frame laser contrast phase on an attached Stage B runtime.

The script performs no sensor setting writes, camera reset, signal release, or
motor command. Each invocation captures exactly five ON or OFF samples; the
operator supplies a phase-bound proof and the existing clean attach receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Callable, Mapping


FRAME_WIDTH = 752
FRAME_HEIGHT = 480
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT
CAPTURES_PER_PHASE = 5
STRIPE_ROI = (620, 210, 710, 430)
THRESHOLD = 60
PHASES = {"on_a": True, "off": False, "on_b": True}
SENSOR_REGISTERS = (0x00, 0x07, 0x0F, 0xAF, 0x35, 0x0B, 0xD5)


class LaserContrastCaptureError(RuntimeError):
    """Raised when the fixed capture phase cannot continue safely."""


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--source-root", type=Path, required=True)
    result.add_argument("--manifest", type=Path, required=True)
    result.add_argument("--receipt", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--exposure-module", type=Path, required=True)
    result.add_argument("--module-sha256", required=True)
    result.add_argument("--phase", choices=tuple(PHASES), required=True)
    result.add_argument("--operator-proof", type=Path, required=True)
    return result


def validate_operator_proof(value: object, *, phase: str) -> dict[str, Any]:
    """Validate one explicit human confirmation for this phase and case."""

    if phase not in PHASES:
        raise ValueError("phase must be on_a, off, or on_b")
    if not isinstance(value, Mapping):
        raise ValueError("operator proof must be a JSON object")
    if value.get("phase") != phase:
        raise ValueError("operator proof phase does not match this capture")
    if value.get("confirmed") is not True:
        raise ValueError("operator proof requires explicit confirmed=true")
    statement = value.get("statement")
    case_id = value.get("case_id")
    if not isinstance(statement, str) or not statement.strip():
        raise ValueError("operator proof requires a non-empty statement")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("operator proof requires a non-empty case_id string")
    if value.get("laser_on") is not PHASES[phase]:
        raise ValueError("operator proof laser_on state does not match the requested phase")
    return dict(value)


def validate_sensor_settings(settings: object) -> dict[int, int]:
    """Require the fixed MT9V034 context-A linear exposure snapshot."""

    if not isinstance(settings, Mapping):
        raise ValueError("sensor settings must be a register mapping")
    values: dict[int, int] = {}
    for register in SENSOR_REGISTERS:
        value = settings.get(register)
        if type(value) is not int or not 0 <= value <= 0xFFFF:
            raise ValueError(f"sensor register {register:#04x} is missing or malformed")
        values[register] = value
    if values[0x00] != 0x1324:
        raise ValueError("MT9V034 chip ID differs from 0x1324")
    if values[0x07] & 0x8000:
        raise ValueError("sensor is not in context A")
    if values[0x0F] & 0x0001:
        raise ValueError("sensor HDR mode must remain off")
    for register, expected in ((0xAF, 0), (0x35, 16), (0x0B, 60), (0xD5, 0)):
        if values[register] != expected:
            raise ValueError(f"sensor register {register:#04x} differs from fixed value {expected}")
    return values


def verify_module_digest(module_path: str | Path, expected_sha256: str) -> tuple[Path, str]:
    """Bind the dynamically loaded pure SCCB helper to the supplied digest."""

    if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None:
        raise ValueError("module-sha256 must be 64 hexadecimal characters")
    path = Path(module_path).resolve(strict=True)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest.lower() != expected_sha256.lower():
        raise ValueError("exposure helper SHA-256 differs from --module-sha256")
    return path, digest


def load_frozen_sccb_access(module_path: str | Path, expected_sha256: str) -> tuple[type, str]:
    """Load only SCCBAccess from the hash-bound exposure helper file."""

    path, digest = verify_module_digest(module_path, expected_sha256)
    name = f"_frozen_camera_exposure_{digest}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load frozen exposure helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    access_class = getattr(module, "SCCBAccess", None)
    if not isinstance(access_class, type):
        raise ImportError("frozen exposure helper does not define SCCBAccess")
    return access_class, digest


def read_sensor_registers(access: Any) -> dict[int, int]:
    """Issue bounded SCCB reads only; no camera-control writes are made."""

    read = getattr(access, "read", None)
    if not callable(read):
        raise TypeError("sensor access object must expose read(register)")
    return {register: read(register) for register in SENSOR_REGISTERS}


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        _fsync_directory(path.parent)
    finally:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass


def _write_bytes_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)


def _write_json_atomic(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str).encode("utf-8")
    _write_bytes_atomic(path, payload + b"\n")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _sensor_json(settings: Mapping[int, int]) -> dict[str, int]:
    return {f"0x{register:02x}": value for register, value in settings.items()}


def _source_root(path: str | Path) -> Path:
    root = Path(path).resolve(strict=True)
    if not (root / "src" / "python" / "structured_light" / "motion" / "pynq_runtime.py").is_file():
        raise FileNotFoundError("source-root lacks the Stage B PYNQ runtime")
    if not (root / "board" / "scripts" / "b_finish_static.py").is_file():
        raise FileNotFoundError("source-root lacks b_finish_static.export_result")
    return root


def _guard_status(runtime: Any) -> dict[str, Any]:
    guard = getattr(runtime, "guard", None)
    status = guard.status() if guard is not None and callable(getattr(guard, "status", None)) else None
    if not isinstance(status, Mapping):
        raise LaserContrastCaptureError("guard status is unavailable")
    if status.get("active") is not False or status.get("step_pin") is not False or status.get("enable_pin") is not True:
        raise LaserContrastCaptureError("guard must remain idle with STEP low and EN high")
    return dict(status)


def _capture_configuration(runtime: Any) -> dict[str, Any]:
    configuration = getattr(runtime, "capture_configuration", None)
    if not isinstance(configuration, Mapping):
        raise LaserContrastCaptureError("attached runtime lacks the frozen capture configuration")
    stripe = configuration.get("stripe_roi")
    if not isinstance(stripe, Mapping):
        raise LaserContrastCaptureError("runtime stripe ROI readback is unavailable")
    if tuple(stripe.get("requested", ())) != STRIPE_ROI or tuple(stripe.get("readback", ())) != STRIPE_ROI:
        raise LaserContrastCaptureError("runtime stripe ROI differs from the fixed contrast ROI")
    if configuration.get("threshold_requested") != THRESHOLD:
        raise LaserContrastCaptureError("runtime stripe threshold differs from 60")
    return dict(configuration)


def _session_capture_identity(pair: Any) -> dict[str, Any]:
    request_id = getattr(pair, "request_id", None)
    epoch = getattr(pair, "epoch", None)
    session_id = getattr(pair, "session_id", None)
    if type(request_id) is not int or request_id < 0:
        raise LaserContrastCaptureError("capture request_id is unavailable or malformed")
    if type(epoch) is not int or epoch < 0:
        raise LaserContrastCaptureError("capture epoch is unavailable or malformed")
    if not isinstance(session_id, str) or not session_id.strip():
        raise LaserContrastCaptureError("capture session_id is unavailable or malformed")
    return {"request_id": request_id, "epoch": epoch, "session_id": session_id}


def _runtime_recorder_files(runtime: Any, pair: Any, runtime_output: Path,
                            output: Path) -> dict[str, Any]:
    controller = getattr(runtime, "controller", None)
    recorder = getattr(controller, "recorder", None)
    recorder_path = Path(getattr(recorder, "path", "")).resolve(strict=True)
    expected_parent = (runtime_output / "sessions").resolve(strict=True)
    try:
        recorder_path.relative_to(expected_parent)
    except ValueError as exc:
        raise LaserContrastCaptureError("capture recorder path escapes the current runtime output") from exc
    if getattr(recorder, "session_id", None) != pair.session_id or recorder_path.name != pair.session_id:
        raise LaserContrastCaptureError("capture recorder session_id differs from the capture result")
    stem = f"capture_e{pair.epoch:08x}_r{pair.request_id:08x}"
    files = {
        "raw": recorder_path / f"{stem}.raw.bin",
        "profile": recorder_path / f"{stem}.profile.bin",
        "diagnostics": recorder_path / f"{stem}.diagnostics.json",
        "result": recorder_path / f"{stem}.result.json",
    }
    raw = bytes(getattr(pair, "raw_bytes", b""))
    profile = bytes(getattr(pair, "profile_payload", b""))
    if files["raw"].read_bytes() != raw or files["profile"].read_bytes() != profile:
        raise LaserContrastCaptureError("existing session recorder payload differs from the returned capture")
    diagnostics = json.loads(files["diagnostics"].read_text(encoding="utf-8"))
    result = json.loads(files["result"].read_text(encoding="utf-8"))
    if (diagnostics.get("request_id") != pair.request_id or diagnostics.get("epoch") != pair.epoch
            or diagnostics.get("width") != FRAME_WIDTH or diagnostics.get("height") != FRAME_HEIGHT
            or len(diagnostics.get("rows", ())) != FRAME_HEIGHT):
        raise LaserContrastCaptureError("existing session recorder diagnostics do not match this frame")
    if (result.get("request_id") != pair.request_id or result.get("epoch") != pair.epoch
            or result.get("session_id") != pair.session_id or result.get("raw_length") != FRAME_BYTES
            or result.get("profile_length") != len(profile)):
        raise LaserContrastCaptureError("existing session recorder result does not match this frame")
    relative = {}
    for kind, path in files.items():
        relative[kind] = str(path.resolve().relative_to(output.resolve())).replace("\\", "/")
    return {
        "files": relative,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "profile_sha256": hashlib.sha256(profile).hexdigest(),
    }


def _validate_capture_payload(pair: Any) -> tuple[bytes, str]:
    raw = getattr(pair, "raw_bytes", None)
    if not isinstance(raw, bytes) or len(raw) != FRAME_BYTES:
        raise LaserContrastCaptureError("capture raw grayscale payload must be exactly 360960 bytes")
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    if getattr(pair, "input_sha256", None) != raw_sha256:
        raise LaserContrastCaptureError("capture input hash differs from its raw grayscale payload")
    frame = getattr(pair, "raw_frame", None)
    if getattr(frame, "width", None) != FRAME_WIDTH or getattr(frame, "height", None) != FRAME_HEIGHT:
        raise LaserContrastCaptureError("capture frame geometry differs from 752x480")
    profile = getattr(pair, "profile_payload", None)
    if not isinstance(profile, bytes) or not profile:
        raise LaserContrastCaptureError("capture profile payload is absent")
    diagnostics = getattr(pair, "diagnostics", None)
    if (getattr(diagnostics, "width", None) != FRAME_WIDTH
            or getattr(diagnostics, "height", None) != FRAME_HEIGHT
            or len(getattr(diagnostics, "rows", ())) != FRAME_HEIGHT):
        raise LaserContrastCaptureError("capture diagnostics do not cover the full 752x480 frame")
    comparison = getattr(pair, "comparison", None)
    if getattr(comparison, "passed", None) is not True:
        raise LaserContrastCaptureError("same-frame raw/Profile comparison did not pass")
    return raw, raw_sha256


def _validate_export(pair: Any, metrics: object, output: Path, index: int) -> dict[str, Any]:
    if not isinstance(metrics, Mapping):
        raise LaserContrastCaptureError("b_finish_static.export_result returned no metrics mapping")
    if metrics.get("comparison_passed") is not True or metrics.get("raw_bytes") != FRAME_BYTES:
        raise LaserContrastCaptureError("export metrics must report a passing comparison and 360960 raw bytes")
    if metrics.get("request_id") != pair.request_id or metrics.get("epoch") != pair.epoch:
        raise LaserContrastCaptureError("export metrics capture identity differs from the returned pair")
    raw = pair.raw_bytes
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    if metrics.get("input_sha256") != raw_sha256:
        raise LaserContrastCaptureError("export metrics input hash differs from captured grayscale bytes")
    folder = output / f"frame_{index:03d}"
    gray_bin = folder / "gray.bin"
    gray_png = folder / "gray.png"
    if not gray_bin.is_file() or gray_bin.read_bytes() != raw:
        raise LaserContrastCaptureError("exported gray.bin differs from the captured raw frame")
    from PIL import Image

    with Image.open(gray_png) as image:
        if image.mode != "L" or image.size != (FRAME_WIDTH, FRAME_HEIGHT) or image.tobytes() != raw:
            raise LaserContrastCaptureError("exported grayscale PNG does not decode exactly to the raw frame")
    raw_copy = folder / "raw_gray8.bin"
    _write_bytes_exclusive(raw_copy, raw)
    if raw_copy.stat().st_size != FRAME_BYTES or raw_copy.read_bytes() != raw:
        raise LaserContrastCaptureError("raw_gray8.bin failed exact-length readback")
    return {
        "comparison_passed": True,
        "raw_bytes": FRAME_BYTES,
        "raw_sha256": raw_sha256,
        "gray_bin": str(gray_bin.resolve().relative_to(output.resolve())).replace("\\", "/"),
        "gray_png": str(gray_png.resolve().relative_to(output.resolve())).replace("\\", "/"),
        "raw_gray8_bin": str(raw_copy.resolve().relative_to(output.resolve())).replace("\\", "/"),
        "png_readback_exact": True,
        "metrics": dict(metrics),
    }


def _validate_cleanup(cleanup: object, *, runtime_output: Path,
                      last_capture: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(cleanup, Mapping) or cleanup.get("closed") is not True:
        raise LaserContrastCaptureError("success requires runtime.close() to confirm closed=true")
    handoff_text = cleanup.get("capture_handoff")
    if not isinstance(handoff_text, str) or not handoff_text.strip():
        raise LaserContrastCaptureError("success requires a fresh capture_handoff.json")
    handoff_path = Path(handoff_text).resolve(strict=True)
    expected_path = (runtime_output / "capture_handoff.json").resolve(strict=True)
    if handoff_path != expected_path or not handoff_path.is_file():
        raise LaserContrastCaptureError("close returned a handoff outside this unique runtime output")
    payload = json.loads(handoff_path.read_text(encoding="utf-8"))
    checkpoint = payload.get("capture_checkpoint")
    state = checkpoint.get("state") if isinstance(checkpoint, Mapping) else None
    if payload.get("loaded") is not True or not isinstance(state, Mapping):
        raise LaserContrastCaptureError("fresh handoff lacks a loaded capture checkpoint")
    if checkpoint.get("kind") != "completed":
        raise LaserContrastCaptureError("fresh handoff does not record a completed capture")
    if last_capture is None:
        raise LaserContrastCaptureError("fresh handoff cannot be accepted without a captured frame")
    if state.get("control_id") != last_capture.get("request_id") or state.get("epoch") != last_capture.get("epoch"):
        raise LaserContrastCaptureError("fresh handoff identity differs from the last phase capture")
    result = dict(cleanup)
    result["capture_handoff"] = str(handoff_path)
    result["handoff_sha256"] = hashlib.sha256(handoff_path.read_bytes()).hexdigest()
    return result


def _exception_record(exc: BaseException) -> dict[str, str]:
    return {"error_type": type(exc).__name__, "error": str(exc)}


def _load_proof(args: Any, output: Path, state: dict[str, Any]) -> dict[str, Any]:
    raw = Path(args.operator_proof).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    _write_bytes_exclusive(output / "operator_proof.json", raw)
    state["operator_proof_sha256"] = digest
    value = json.loads(raw.decode("utf-8"))
    validated = validate_operator_proof(value, phase=args.phase)
    state["operator_proof"] = validated
    return validated


def _validate_source_root(path: str | Path) -> Path:
    return _source_root(path)


def _create_sensor_access_factory(access_class: type) -> Callable[..., Any]:
    def factory(runtime: Any, trace: list[dict[str, Any]], save: Callable[[], None]) -> Any:
        diagnostic = getattr(getattr(runtime, "transport", None), "_diag_mmio", None)
        if diagnostic is None:
            raise LaserContrastCaptureError("attached runtime does not expose camera diagnostics SCCB access")
        return access_class(diagnostic, trace, save)
    return factory


def _runtime_parameters(args: Any, runtime_output: Path) -> dict[str, Any]:
    return {
        "stage": "static",
        "attach": True,
        "loaded_overlay_receipt": args.receipt,
        "output": runtime_output,
        "roi": STRIPE_ROI,
        "settle_roi": STRIPE_ROI,
        "stripe_roi": STRIPE_ROI,
        "threshold": THRESHOLD,
        "no_display": True,
    }


def execute_phase(args: Any, *, runtime_factory: Callable[..., Any],
                  sensor_access_factory: Callable[..., Any] | None,
                  exporter: Callable[[Any, Path, int], Mapping[str, Any]]) -> dict[str, Any]:
    """Run one fixed five-frame phase through injected runtime/access/export boundaries."""

    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError(f"phase output must be new and unique: {output}")
    output.mkdir(parents=True, exist_ok=False)
    runtime_output = output / "runtime"
    state: dict[str, Any] = {
        "schema": "laser_contrast_capture_v1",
        "state": "starting",
        "passed": False,
        "phase": args.phase,
        "case_id": None,
        "laser_on": PHASES.get(args.phase),
        "expected_captures": CAPTURES_PER_PHASE,
        "capture_attempts": 0,
        "capture_count": 0,
        "captures": [],
        "motion_started": False,
        "overlay_loaded": False,
        "overlay_downloads": 0,
        "hidden_retry": False,
        "sensor_write_count": 0,
        "capture_configuration_requested": {
            "stage": "static", "attach": True, "no_display": True,
            "stripe_roi": list(STRIPE_ROI), "threshold": THRESHOLD,
        },
        "operator_proof_sha256": None,
        "exposure_module_sha256": None,
        "cleanup": None,
    }
    _write_json_atomic(output / "status.json", state)
    runtime = None
    access = None
    baseline: dict[int, int] | None = None
    last_capture: Mapping[str, Any] | None = None
    error: BaseException | None = None

    def publish() -> None:
        _write_json_atomic(output / "status.json", state)
        _write_json_atomic(output / "capture_records.json", state["captures"])

    trace: list[dict[str, Any]] = []

    def save_trace() -> None:
        _write_json_atomic(output / "sensor_read_trace.json", trace)

    try:
        proof_value = _load_proof(args, output, state)
        state["case_id"] = proof_value["case_id"]
        source_root = _validate_source_root(args.source_root)
        helper_path, helper_digest = verify_module_digest(args.exposure_module, args.module_sha256)
        state["exposure_module"] = str(helper_path)
        state["exposure_module_sha256"] = helper_digest
        state["source_root"] = str(source_root)
        if sensor_access_factory is None:
            access_class, _digest = load_frozen_sccb_access(helper_path, helper_digest)
            sensor_access_factory = _create_sensor_access_factory(access_class)

        if runtime_output.exists():
            raise FileExistsError("runtime output must be fresh before attach")
        state["state"] = "attaching_static_runtime"
        publish()
        runtime = runtime_factory(args.manifest, **_runtime_parameters(args, runtime_output))
        state["runtime_output"] = str(runtime_output)
        state["attached"] = True
        state["guard_before"] = _guard_status(runtime)
        state["capture_configuration_readback"] = _capture_configuration(runtime)
        access = sensor_access_factory(runtime, trace, save_trace)
        recorder = getattr(getattr(runtime, "controller", None), "recorder", None)
        recorder_path = Path(getattr(recorder, "path", "")).resolve(strict=True)
        state["session_id"] = getattr(recorder, "session_id", None)
        state["recorder_session_dir"] = str(recorder_path.relative_to(output.resolve())).replace("\\", "/")
        sensor_before_raw = read_sensor_registers(access)
        baseline = validate_sensor_settings(sensor_before_raw)
        state["sensor_before"] = _sensor_json(baseline)
        state["state"] = "capturing"
        publish()

        for index in range(CAPTURES_PER_PHASE):
            guard_before_capture = _guard_status(runtime)
            state["capture_attempts"] = index + 1
            state["current_capture_index"] = index
            state["guard_before_capture"] = guard_before_capture
            publish()
            try:
                pair = runtime.session.run_static(raw=False)
            except BaseException as capture_error:
                attempt: dict[str, Any] = {
                    "index": index,
                    "state": "capture_error",
                    **_exception_record(capture_error),
                }
                try:
                    sensor_after_raw = read_sensor_registers(access)
                    attempt["sensor_after"] = _sensor_json(sensor_after_raw)
                    sensor_after = validate_sensor_settings(sensor_after_raw)
                    if sensor_after != baseline:
                        raise LaserContrastCaptureError("sensor settings drifted after failed capture")
                except BaseException as sensor_error:
                    attempt["post_capture_sensor_error"] = _exception_record(sensor_error)
                try:
                    attempt["guard_after"] = _guard_status(runtime)
                except BaseException as guard_error:
                    attempt["post_capture_guard_error"] = _exception_record(guard_error)
                state["captures"].append(attempt)
                publish()
                raise LaserContrastCaptureError("static capture failed; no retry was attempted") from capture_error

            attempt = {
                "index": index,
                "state": "captured_pending_checks",
                "request_id": getattr(pair, "request_id", None),
                "epoch": getattr(pair, "epoch", None),
                "session_id": getattr(pair, "session_id", None),
                "raw_bytes": len(getattr(pair, "raw_bytes", b"")),
                "input_sha256": getattr(pair, "input_sha256", None),
            }
            prior_capture = last_capture
            identity: dict[str, Any] | None = None
            identity_error: BaseException | None = None
            try:
                identity = _session_capture_identity(pair)
                attempt.update(identity)
                last_capture = identity
            except BaseException as exc:
                identity_error = exc
            post_errors: list[tuple[str, BaseException]] = []
            try:
                sensor_after_raw = read_sensor_registers(access)
                attempt["sensor_after"] = _sensor_json(sensor_after_raw)
                sensor_after = validate_sensor_settings(sensor_after_raw)
                if sensor_after != baseline:
                    raise LaserContrastCaptureError("sensor settings drifted during the phase")
            except BaseException as sensor_error:
                post_errors.append(("post_capture_sensor_error", sensor_error))
            try:
                attempt["guard_after"] = _guard_status(runtime)
            except BaseException as guard_error:
                post_errors.append(("post_capture_guard_error", guard_error))
            state["captures"].append(attempt)
            publish()
            if post_errors:
                for key, failure in post_errors:
                    attempt[key] = _exception_record(failure)
                try:
                    attempt["recorder"] = _runtime_recorder_files(runtime, pair, runtime_output, output)
                    attempt["raw_sha256"] = hashlib.sha256(pair.raw_bytes).hexdigest()
                    attempt["profile_length"] = len(pair.profile_payload)
                except BaseException as recorder_error:
                    attempt["recorder_validation_error"] = _exception_record(recorder_error)
                attempt["state"] = "post_capture_check_failed"
                publish()
                label, failure = post_errors[0]
                raise LaserContrastCaptureError(f"{label}: {failure}") from failure
            if identity_error is not None or identity is None:
                attempt["state"] = "capture_identity_invalid"
                attempt["identity_error"] = _exception_record(identity_error or RuntimeError("missing identity"))
                publish()
                raise LaserContrastCaptureError("capture identity is unavailable") from identity_error
            if prior_capture is not None:
                if identity["session_id"] != prior_capture["session_id"]:
                    attempt["state"] = "session_discontinuity"
                    publish()
                    raise LaserContrastCaptureError("capture session_id changed within the phase")
                if identity["epoch"] != prior_capture["epoch"]:
                    attempt["state"] = "epoch_discontinuity"
                    publish()
                    raise LaserContrastCaptureError("capture epoch changed within the phase")
                if identity["request_id"] != prior_capture["request_id"] + 1:
                    attempt["state"] = "request_discontinuity"
                    publish()
                    raise LaserContrastCaptureError("capture request_id is not continuous within the phase")

            raw, raw_sha256 = _validate_capture_payload(pair)
            recorder = _runtime_recorder_files(runtime, pair, runtime_output, output)
            attempt["recorder"] = recorder
            attempt["raw_sha256"] = raw_sha256
            attempt["profile_length"] = len(pair.profile_payload)
            attempt["state"] = "exporting"
            publish()
            metrics = exporter(pair, output, index)
            exported = _validate_export(pair, metrics, output, index)
            attempt["export"] = exported
            attempt["metrics"] = dict(metrics)
            attempt["state"] = "complete"
            state["capture_count"] += 1
            publish()

        if state["capture_count"] != CAPTURES_PER_PHASE:
            raise LaserContrastCaptureError("phase did not produce exactly five complete capture records")
        state["guard_after"] = _guard_status(runtime)
        state["state"] = "closing_runtime"
        publish()
    except BaseException as exc:
        error = exc
        state.update({"state": "failed", "passed": False, "failure": _exception_record(exc)})
        publish()
    finally:
        if runtime is not None:
            try:
                state["cleanup"] = runtime.close()
            except BaseException as close_error:
                state["cleanup_error"] = _exception_record(close_error)
                state["state"] = "failed"
            else:
                try:
                    state["cleanup"] = _validate_cleanup(
                        state["cleanup"], runtime_output=runtime_output,
                        last_capture=last_capture,
                    )
                except BaseException as cleanup_error:
                    state["cleanup_validation_error"] = _exception_record(cleanup_error)
                    state["state"] = "failed"
        if error is None and state.get("state") == "closing_runtime" and state.get("cleanup_validation_error") is None:
            state.update({"state": "complete", "passed": True})
        else:
            state.update({"state": "failed", "passed": False})
        state["finished_unix_ns"] = time.time_ns()
        state["runtime_closed"] = bool(
            isinstance(state.get("cleanup"), Mapping) and state["cleanup"].get("closed") is True
        )
        try:
            publish()
            _write_json_atomic(output / "summary.json", state)
        except BaseException as persist_error:
            if error is None:
                state["state"] = "failed"
                state["passed"] = False
                state["summary_persist_error"] = _exception_record(persist_error)
    return state


def _runtime_factory(source_root: Path) -> Callable[..., Any]:
    def create(manifest: str | Path, **kwargs: Any) -> Any:
        resolved_root = _validate_source_root(source_root)
        source_python = str(resolved_root / "src" / "python")
        board_scripts = str(resolved_root / "board" / "scripts")
        if source_python not in sys.path:
            sys.path.insert(0, source_python)
        if board_scripts not in sys.path:
            sys.path.insert(0, board_scripts)
        from structured_light.motion.pynq_runtime import create_stage_b_runtime

        return create_stage_b_runtime(manifest, **kwargs)
    return create


def _board_exporter(source_root: Path) -> Callable[[Any, Path, int], Mapping[str, Any]]:
    def export(pair: Any, output: Path, index: int) -> Mapping[str, Any]:
        resolved_root = _validate_source_root(source_root)
        board_scripts = str(resolved_root / "board" / "scripts")
        if board_scripts not in sys.path:
            sys.path.insert(0, board_scripts)
        from b_finish_static import export_result

        return export_result(pair, output, index)
    return export


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    source_root = Path(args.source_root)
    result = execute_phase(
        args,
        runtime_factory=_runtime_factory(source_root),
        sensor_access_factory=None,
        exporter=_board_exporter(source_root),
    )
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
