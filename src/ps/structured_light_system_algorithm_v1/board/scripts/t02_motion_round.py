"""T02 static preparation and bounded motion diagnostics on the loaded Stage B Overlay."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Callable, Mapping


FRAME_WIDTH = 752
FRAME_HEIGHT = 480
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT
STRIPE_ROI = (620, 210, 710, 430)
THRESHOLD = 60
SENSOR_REGISTERS = (0x00, 0x07, 0x0F, 0xAF, 0x35, 0x0B, 0xD5)
PREPARE_CAPTURES = 1
MOTION_POSITIONS = {"single": 1, "sequence": 10, "short": 2}
MOTION_PULSES = {"single": 320, "sequence": 320, "short": 80}
SEQUENCE_DISPLAY_BUDGET_S = 30.0


class T02RoundError(RuntimeError):
    """Raised when a T02 software round cannot satisfy its capture contract."""


class DeferredSingleDisplay:
    """Return a truthful deferred receipt while the single-position guard is held."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def show_capture(self, result: Any) -> dict[str, Any]:
        self.calls.append(result)
        return {
            "display_deferred": True,
            "rendered": False,
            "shown": False,
            "reason": "single-position HDMI output is deferred until guard revocation",
            "position_index": 0,
            "request_id": getattr(result, "request_id", None),
            "camera_epoch": getattr(result, "epoch", None),
        }


class DeferredSequenceDisplay:
    """Collect ordered sequence frames for explicitly requested after-batch output."""

    def __init__(self, expected_count: int = MOTION_POSITIONS["sequence"]) -> None:
        self.expected_count = expected_count
        self.calls: list[Any] = []

    def show_capture(self, result: Any) -> dict[str, Any]:
        position_index = len(self.calls)
        if position_index >= self.expected_count:
            raise T02RoundError("sequence display proxy received more frames than the fixed batch size")
        self.calls.append(result)
        return {
            "display_deferred": True,
            "rendered": False,
            "shown": False,
            "reason": "sequence HDMI output is deferred until the full batch and guard revocation",
            "position_index": position_index,
            "request_id": getattr(result, "request_id", None),
            "camera_epoch": getattr(result, "epoch", None),
        }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--stage", choices=("prepare", "single", "sequence", "short"), required=True)
    result.add_argument("--source-root", type=Path, required=True)
    result.add_argument("--manifest", type=Path, required=True)
    result.add_argument("--receipt", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--capture-helper", type=Path, required=True)
    result.add_argument("--capture-helper-sha256", required=True)
    result.add_argument("--exposure-module", type=Path, required=True)
    result.add_argument("--module-sha256", required=True)
    result.add_argument("--settle-roi", nargs=4, type=int, required=True)
    result.add_argument("--installation", required=True)
    result.add_argument("--motion-confirmation", type=Path)
    result.add_argument("--driver-proof", type=Path)
    result.add_argument("--calibration-proof", type=Path)
    result.add_argument("--calibration-proof-sha256")
    result.add_argument("--plane", type=Path)
    result.add_argument("--plane-sha256")
    result.add_argument("--old-plane-sha256")
    result.add_argument("--no-display", action="store_true")
    result.add_argument("--display-confirmation-timeout-s", type=float, default=0.0)
    result.add_argument("--sequence-display-policy", choices=("forbidden", "after_batch"),
                        default="forbidden")
    return result


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


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str).encode("utf-8") + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


def _exclusive_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)


def _prepend_source_path(path: str | Path) -> str:
    value = str(Path(path).resolve())
    sys.path[:] = [item for item in sys.path if item != value]
    sys.path.insert(0, value)
    return value


def _source_root(path: str | Path) -> Path:
    root = Path(path).resolve(strict=True)
    if not (root / "src" / "python" / "structured_light" / "motion" / "pynq_runtime.py").is_file():
        raise FileNotFoundError("source-root lacks the Stage B runtime")
    if not (root / "board" / "scripts" / "b_finish_static.py").is_file():
        raise FileNotFoundError("source-root lacks b_finish_static.export_result")
    return root


def _display_timeout(args: Any) -> float:
    value = getattr(args, "display_confirmation_timeout_s", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("display-confirmation-timeout-s must be a finite non-negative number")
    value = float(value)
    if value > 0 and (args.stage != "prepare" or args.no_display):
        raise ValueError("display confirmation is available only during displayed prepare")
    policy = getattr(args, "sequence_display_policy", "forbidden")
    if args.stage == "sequence":
        if args.no_display and policy != "forbidden":
            raise ValueError("--sequence-display-policy after_batch requires display enabled")
        if not args.no_display and policy != "after_batch":
            raise ValueError("sequence display requires explicit --sequence-display-policy after_batch")
    elif policy != "forbidden":
        raise ValueError("--sequence-display-policy applies only to sequence stage")
    return value


def _wait_display_confirmation(source_root: Path, output: Path, timeout_s: float,
                               waiter: Callable[..., Any] | None) -> dict[str, Any]:
    if timeout_s <= 0:
        return {}
    if waiter is None:
        _prepend_source_path(source_root / "board" / "scripts")
        from b_finish_static import wait_reply

        waiter = wait_reply
    value = waiter(output, "t02_prepare_display", timeout_s=timeout_s)
    if (not isinstance(value, Mapping)
            or value.get("phase") != "t02_prepare_display"
            or value.get("confirmed") is not True
            or not isinstance(value.get("nonce"), str) or not value["nonce"].strip()
            or not isinstance(value.get("statement"), str) or not value["statement"].strip()):
        raise T02RoundError("display confirmation must contain its phase, nonce, confirmed=true, and statement")
    return dict(value)


def _load_capture_helper(path: str | Path, expected_sha256: str):
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError("capture-helper-sha256 must be 64 hexadecimal characters")
    try:
        int(expected_sha256, 16)
    except ValueError as exc:
        raise ValueError("capture-helper-sha256 must be 64 hexadecimal characters") from exc
    path = Path(path).resolve(strict=True)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest.lower() != expected_sha256.lower():
        raise ValueError("capture helper SHA-256 differs from --capture-helper-sha256")
    spec = importlib.util.spec_from_file_location(f"_t02_capture_helpers_{digest}", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load the hash-bound capture helpers")
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    for name in ("verify_module_digest", "load_frozen_sccb_access", "validate_sensor_settings",
                 "read_sensor_registers", "_runtime_recorder_files", "_validate_capture_payload",
                 "_validate_export", "_validate_cleanup"):
        if not callable(getattr(helpers, name, None)):
            raise ImportError(f"capture helper lacks required pure API {name}")
    return helpers, path, digest


def _sha256_argument(name: str, value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be 64 hexadecimal characters")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be 64 hexadecimal characters") from exc
    return value.lower()


def verify_calibration_proof(args: Any) -> dict[str, str]:
    """Read-only gate binding a short capture to the new frozen laser plane."""
    proof_path = Path(args.calibration_proof).resolve(strict=True)
    plane_path = Path(args.plane).resolve(strict=True)
    expected_proof_sha = _sha256_argument(
        "--calibration-proof-sha256", args.calibration_proof_sha256,
    )
    expected_plane_sha = _sha256_argument("--plane-sha256", args.plane_sha256)
    old_plane_sha = _sha256_argument("--old-plane-sha256", args.old_plane_sha256)
    proof_bytes = proof_path.read_bytes()
    plane_bytes = plane_path.read_bytes()
    proof_sha = hashlib.sha256(proof_bytes).hexdigest()
    plane_sha = hashlib.sha256(plane_bytes).hexdigest()
    if proof_sha != expected_proof_sha:
        raise ValueError("calibration proof SHA-256 differs from --calibration-proof-sha256")
    if plane_sha != expected_plane_sha:
        raise ValueError("new laser plane SHA-256 differs from --plane-sha256")
    if plane_sha == old_plane_sha:
        raise ValueError("new laser plane hash must differ from --old-plane-sha256")

    proof_value = json.loads(proof_bytes)
    plane_value = json.loads(plane_bytes)
    if not isinstance(proof_value, Mapping) or proof_value.get("passed") is not True:
        raise ValueError("calibration proof must have passed=true")
    holdout = proof_value.get("holdout")
    if not isinstance(holdout, Mapping) or holdout.get("status") != "PASS":
        raise ValueError("calibration proof holdout must have status=PASS")
    frozen_plane_sha = _sha256_argument(
        "proof frozen_plane_sha256", proof_value.get("frozen_plane_sha256"),
    )
    if frozen_plane_sha != plane_sha:
        raise ValueError("calibration proof frozen plane hash differs from the supplied new plane")
    raw_camera_sha = _sha256_argument(
        "proof raw_camera_sha256", proof_value.get("raw_camera_sha256"),
    )
    intrinsics_sha = _sha256_argument(
        "proof intrinsics_sha256", proof_value.get("intrinsics_sha256"),
    )
    raw_camera_path_value = holdout.get("raw_camera_path")
    if not isinstance(raw_camera_path_value, str) or not raw_camera_path_value.strip():
        raise ValueError("calibration proof holdout must name its original raw_camera_path")
    raw_camera_path = Path(raw_camera_path_value)
    if not raw_camera_path.is_absolute():
        raw_camera_path = proof_path.parent / raw_camera_path
    raw_camera_path = raw_camera_path.resolve(strict=True)
    actual_raw_camera_sha = hashlib.sha256(raw_camera_path.read_bytes()).hexdigest()
    if actual_raw_camera_sha != raw_camera_sha:
        raise ValueError("holdout raw camera image SHA-256 differs from the calibration proof")
    if not isinstance(plane_value, Mapping):
        raise ValueError("new laser plane must be a JSON object")
    plane_intrinsics_sha = _sha256_argument(
        "plane camera_intrinsics_sha256", plane_value.get("camera_intrinsics_sha256"),
    )
    if plane_intrinsics_sha != intrinsics_sha:
        raise ValueError("new laser plane intrinsics hash differs from the calibration proof")
    return {
        "proof_path": str(proof_path),
        "proof_sha256": proof_sha,
        "plane_path": str(plane_path),
        "plane_sha256": plane_sha,
        "old_plane_sha256": old_plane_sha,
        "holdout_raw_camera_path": str(raw_camera_path),
        "holdout_raw_camera_sha256": raw_camera_sha,
        "intrinsics_sha256": intrinsics_sha,
    }


def _calibration_gate(args: Any) -> dict[str, str] | None:
    supplied = any(getattr(args, name, None) is not None for name in (
        "calibration_proof", "calibration_proof_sha256", "plane", "plane_sha256",
        "old_plane_sha256",
    ))
    if args.stage != "short":
        if supplied:
            raise ValueError("calibration proof arguments apply only to the short stage")
        return None
    if not args.no_display:
        raise ValueError("short capture requires --no-display")
    missing = [name for name in (
        "calibration_proof", "calibration_proof_sha256", "plane", "plane_sha256",
        "old_plane_sha256",
    ) if getattr(args, name, None) is None]
    if missing:
        raise ValueError("short capture requires --calibration-proof, its SHA, --plane, its SHA, and --old-plane-sha256")
    return verify_calibration_proof(args)


def _runtime_factory(root: Path) -> Callable[..., Any]:
    def create(manifest: str | Path, **kwargs: Any) -> Any:
        _prepend_source_path(root / "src" / "python")
        _prepend_source_path(root / "board" / "scripts")
        from structured_light.motion.pynq_runtime import create_stage_b_runtime

        return create_stage_b_runtime(manifest, **kwargs)
    return create


def _exporter(root: Path) -> Callable[[Any, Path, int], Mapping[str, Any]]:
    def export(pair: Any, output: Path, index: int) -> Mapping[str, Any]:
        _prepend_source_path(root / "board" / "scripts")
        from b_finish_static import export_result

        return export_result(pair, output, index)
    return export


def _runtime_arguments(args: Any, runtime_output: Path, *, stage: str) -> dict[str, Any]:
    values: dict[str, Any] = {
        "stage": stage,
        "attach": True,
        "loaded_overlay_receipt": args.receipt,
        "output": runtime_output,
        "settle_roi": tuple(args.settle_roi),
        "stripe_roi": STRIPE_ROI,
        "threshold": THRESHOLD,
        "installation": args.installation,
        "no_display": bool(args.no_display),
    }
    if args.stage in MOTION_POSITIONS:
        if args.motion_confirmation is None or args.driver_proof is None:
            raise ValueError("motion stages require fresh --motion-confirmation and --driver-proof")
        source_python = Path(args.source_root).resolve(strict=True) / "src" / "python"
        _prepend_source_path(source_python)
        from structured_light.motion.types import MotionConfig

        values["motion_confirmation"] = args.motion_confirmation
        values["driver_proof"] = args.driver_proof
        values["settle_protocol"] = "extended_6s_v2"
        values["config"] = MotionConfig(
            positions=MOTION_POSITIONS[args.stage],
            pulses_per_position=MOTION_PULSES[args.stage],
            rpm=5.0,
            host_poll_max_s=0.010,
        )
    else:
        values["roi"] = tuple(args.settle_roi)
    return values


def _read_sensor_snapshot(access: Any) -> dict[int, int]:
    read = getattr(access, "read", None)
    if not callable(read):
        raise TypeError("frozen SCCB access must expose read(register)")
    return {register: read(register) for register in SENSOR_REGISTERS}


def _guard_idle(runtime: Any) -> dict[str, Any]:
    status = runtime.guard.status()
    if not isinstance(status, Mapping):
        raise T02RoundError("guard status is unavailable")
    if (status.get("active") is not False or status.get("step_pin") is not False
            or status.get("enable_pin") is not True):
        raise T02RoundError("guard must be idle with STEP low and EN high")
    return dict(status)


def _post_disarm_guard_idle(runtime: Any) -> dict[str, Any]:
    status = _guard_idle(runtime)
    if status.get('fault') is not False or status.get('error') != 0:
        raise T02RoundError('post-disarm guard fault/error is not clear; HDMI output refused')
    return status


def _motor_idle(runtime: Any) -> dict[str, Any]:
    session = getattr(runtime, "session", None)
    motor = getattr(session, "motor", None)
    status_method = getattr(motor, "status", None)
    if not callable(status_method):
        raise T02RoundError("post-motion motor status is unavailable")

    owner = getattr(session, "_owner", None)
    gate = getattr(session, "_gate", None)
    runtime_lock = getattr(runtime, "hardware_lock", None)
    session_lock = getattr(session, "hardware_lock", None)
    if owner is None and gate is None and runtime_lock is None and session_lock is None:
        # Dependency-injected offline fakes have no generation transport.
        status = status_method()
    else:
        if owner is None or gate is None or runtime_lock is None or session_lock is not runtime_lock:
            raise T02RoundError("post-motion read-only generation/owner/hardware-lock context is incomplete")
        if getattr(runtime_lock, "acquired", None) is not True:
            raise T02RoundError("post-motion hardware process lock is not still owned")
        acquire = getattr(owner, "acquire", None)
        release = getattr(owner, "release", None)
        if not callable(acquire) or not callable(release) or not acquire(blocking=False):
            raise T02RoundError("post-motion motor status refused because the session owner is busy")
        generation: int | None = None
        read_status: Mapping[str, Any] | None = None
        primary_error: BaseException | None = None
        try:
            live_workers = getattr(session, "_live_workers", None)
            if not callable(live_workers) or live_workers():
                raise T02RoundError("post-motion motor status refused while workers remain live")
            task_receipt = getattr(session, "last_task_receipt", None)
            expected_positions = getattr(getattr(session, "config", None), "positions", None)
            if (not isinstance(task_receipt, Mapping)
                    or type(expected_positions) is not int or expected_positions < 1
                    or task_receipt.get("positions_requested") != expected_positions
                    or task_receipt.get("positions_completed") != expected_positions
                    or task_receipt.get("motion_start_count") != expected_positions
                    or task_receipt.get("guard_revoked") is not True
                    or task_receipt.get("worker_unconfirmed") is not False
                    or task_receipt.get("stop_requested") is not False
                    or task_receipt.get("failure") is not None):
                raise T02RoundError("post-motion motor status requires a verified completed task receipt")
            if getattr(gate, "active", None) not in (True, False):
                raise T02RoundError("post-motion software generation gate state is unavailable")
            timeout_s = getattr(getattr(session, "config", None), "cleanup_timeout_s", None)
            if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                    or not math.isfinite(timeout_s) or timeout_s <= 0):
                raise T02RoundError("post-motion read-only generation requires a finite cleanup timeout")
            if not gate.revoke(timeout_s=float(timeout_s)):
                raise T02RoundError("post-motion software gate did not quiesce before motor status read")
            generation = gate.begin()
            status = status_method()
            if not isinstance(status, Mapping):
                raise T02RoundError("post-motion motor status is unavailable")
            read_status = dict(status)
        except BaseException as exc:
            primary_error = exc
        finally:
            revoke_error: BaseException | None = None
            if generation is not None:
                try:
                    if not gate.revoke(timeout_s=float(timeout_s)):
                        revoke_error = T02RoundError("post-motion read-only generation did not revoke cleanly")
                except BaseException as exc:
                    revoke_error = exc
            release()
            if revoke_error is not None:
                if primary_error is not None:
                    raise T02RoundError(
                        f"post-motion status failed and its read-only generation did not revoke: {primary_error}; {revoke_error}"
                    ) from primary_error
                raise T02RoundError(f"post-motion read-only generation cleanup failed: {revoke_error}") from revoke_error
        if primary_error is not None:
            if isinstance(primary_error, T02RoundError):
                raise primary_error
            raise T02RoundError(f"post-motion gated motor status failed: {primary_error}") from primary_error
        if read_status is None:
            raise T02RoundError("post-motion gated motor status returned no readback")
        status = read_status

    if not isinstance(status, Mapping):
        raise T02RoundError("post-motion motor status is unavailable")
    if (status.get("busy") is not False or status.get("step") is not False
            or status.get("enable_n") is not True or status.get("fault") is not False
            or status.get("cancelled") is True
            or status.get("error") != 0
            or status.get("state") not in ("DONE", "IDLE")):
        raise T02RoundError("motor is not in a clean idle/done state after the single-position motion")
    return dict(status)


def _sequence_display_call_timeout(runtime: Any, adapter: Any) -> float:
    output = getattr(adapter, "display", None)
    value = getattr(output, "timeout_s", None)
    phase_timeout = getattr(getattr(runtime.session, "config", None), "capture_phase_timeout_s", None)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise T02RoundError("after-batch display requires an existing finite per-call HDMI timeout")
    if (isinstance(phase_timeout, bool) or not isinstance(phase_timeout, (int, float))
            or not math.isfinite(phase_timeout) or phase_timeout <= 0 or value > phase_timeout):
        raise T02RoundError("HDMI per-call timeout is outside the existing bounded capture-phase budget")
    return float(value)


def _validate_deferred_display(receipt: object, result: Any,
                               position_index: int = 0) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise T02RoundError("single-position display did not preserve a deferred receipt")
    if (receipt.get("display_deferred") is not True or receipt.get("rendered") is not False
            or receipt.get("shown") is not False
            or receipt.get("position_index") != position_index
            or receipt.get("request_id") != getattr(result, "request_id", None)
            or receipt.get("camera_epoch") != getattr(result, "epoch", None)):
        raise T02RoundError("deferred single-position display receipt is malformed or claims output")
    return dict(receipt)


def _validate_actual_display(receipt: object, result: Any,
                             position_index: int = 0) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise T02RoundError("post-disarm display returned no receipt")
    expected = {
        "request_id": getattr(result, "request_id", None),
        "camera_epoch": getattr(result, "epoch", None),
        "position_index": position_index,
        "canvas_width": 1280,
        "canvas_height": 720,
        "raw_width": FRAME_WIDTH,
        "raw_height": FRAME_HEIGHT,
        "comparison_passed": True,
        "angle_calibrated": False,
        "error": None,
    }
    if any(receipt.get(name) != value for name, value in expected.items()):
        raise T02RoundError("post-disarm display receipt does not match the captured single-position frame")
    if receipt.get("display_deferred") is True or not isinstance(receipt.get("display"), Mapping):
        raise T02RoundError("post-disarm display receipt indicates deferred or unverified output")
    return dict(receipt)


def _replay_sequence_after_batch(runtime: Any, results: list[Any], capture_records: list[dict[str, Any]],
                                 proxy: DeferredSequenceDisplay, *, state: dict[str, Any],
                                 publish: Callable[[], None], monotonic: Callable[[], float],
                                 per_call_timeout_s: float) -> dict[str, Any]:
    expected = MOTION_POSITIONS["sequence"]
    if len(results) != expected or len(capture_records) != expected or len(proxy.calls) != expected:
        raise T02RoundError("after-batch display requires exactly ten captured and deferred frames")
    deferred_receipts = []
    for index, motion_result in enumerate(results):
        pair = motion_result.capture_result
        if proxy.calls[index] is not pair:
            raise T02RoundError("after-batch display capture order differs from MotionSession result order")
        deferred_receipts.append(_validate_deferred_display(
            getattr(motion_result, "display_receipt", None), pair, position_index=index,
        ))

    started = monotonic()
    deadline = started + SEQUENCE_DISPLAY_BUDGET_S
    receipts: list[dict[str, Any]] = []
    state["post_batch_display"] = {
        "policy": "after_batch_not_live",
        "status": "replaying",
        "budget_s": SEQUENCE_DISPLAY_BUDGET_S,
        "per_call_timeout_s": per_call_timeout_s,
        "started_monotonic_s": started,
        "deadline_monotonic_s": deadline,
        "render_call_returned_count": 0,
        "receipts": receipts,
        "deferred_receipts": deferred_receipts,
        "user_visual_confirmation": "not_confirmed",
    }
    publish()

    def fail(index: int, *, started_call: bool, error_type: str, error: str,
             remaining_s: float) -> None:
        state["post_batch_display"] = {
            "policy": "after_batch_not_live",
            "status": "failed",
            "budget_s": SEQUENCE_DISPLAY_BUDGET_S,
            "per_call_timeout_s": per_call_timeout_s,
            "started_monotonic_s": started,
            "deadline_monotonic_s": deadline,
            "elapsed_s": max(0.0, SEQUENCE_DISPLAY_BUDGET_S - remaining_s),
            "remaining_budget_s": max(0.0, min(SEQUENCE_DISPLAY_BUDGET_S, remaining_s)),
            "render_call_returned_count": len(receipts),
            "receipts": list(receipts),
            "deferred_receipts": deferred_receipts,
            "failed_position_index": index,
            "failed_request_id": getattr(results[index].capture_result, "request_id", None),
            "failed_camera_epoch": getattr(results[index].capture_result, "epoch", None),
            "failed_output_call_started": started_call,
            "error_type": error_type,
            "error": error,
            "user_visual_confirmation": "not_confirmed",
        }
        publish()

    for index, (motion_result, capture_record) in enumerate(zip(results, capture_records)):
        pair = motion_result.capture_result
        remaining = deadline - monotonic()
        if remaining < per_call_timeout_s:
            fail(index, started_call=False, error_type="TimeoutError",
                 error="insufficient remaining after-batch budget for one bounded HDMI output call",
                 remaining_s=remaining)
            raise T02RoundError("after-batch display budget exhausted before the next frame")
        call_started = monotonic()
        try:
            actual = runtime.session._display(pair)
            actual = _validate_actual_display(actual, pair, position_index=index)
        except BaseException as exc:
            remaining = deadline - monotonic()
            fail(index, started_call=True, error_type=type(exc).__name__,
                 error=str(exc), remaining_s=remaining)
            raise T02RoundError(f"after-batch HDMI display failed at position {index}: {exc}") from exc
        call_completed = monotonic()
        receipts.append({
            "position_index": index,
            "request_id": actual["request_id"],
            "camera_epoch": actual["camera_epoch"],
            "capture_raw_sha256": capture_record["raw_sha256"],
            "render_call_returned": True,
            "elapsed_s": max(0.0, call_completed - call_started),
            "receipt": actual,
        })
        state["post_batch_display"].update({
            "render_call_returned_count": len(receipts),
            "receipts": list(receipts),
            "remaining_budget_s": max(0.0, deadline - call_completed),
        })
        publish()
        if call_completed > deadline:
            fail(index, started_call=True, error_type="TimeoutError",
                 error="bounded HDMI call returned after the after-batch display deadline",
                 remaining_s=deadline - call_completed)
            raise T02RoundError("after-batch display exceeded its fixed total budget")

    completed = monotonic()
    if completed > deadline:
        fail(expected - 1, started_call=True, error_type="TimeoutError",
             error="after-batch display exceeded its fixed total budget",
             remaining_s=deadline - completed)
        raise T02RoundError("after-batch display exceeded its fixed total budget")
    result = {
        "policy": "after_batch_not_live",
        "status": "output_call_returned",
        "budget_s": SEQUENCE_DISPLAY_BUDGET_S,
        "per_call_timeout_s": per_call_timeout_s,
        "started_monotonic_s": started,
        "completed_monotonic_s": completed,
        "elapsed_s": max(0.0, completed - started),
        "remaining_budget_s": max(0.0, deadline - completed),
        "render_call_returned_count": len(receipts),
        "receipts": receipts,
        "deferred_receipts": deferred_receipts,
        "user_visual_confirmation": "not_confirmed",
    }
    state["post_batch_display"] = result
    publish()
    return result


def _validate_capture_config(runtime: Any, settle_roi: tuple[int, int, int, int]) -> dict[str, Any]:
    config = getattr(runtime, "capture_configuration", None)
    if not isinstance(config, Mapping):
        raise T02RoundError("runtime capture configuration readback is unavailable")
    stripe = config.get("stripe_roi")
    if not isinstance(stripe, Mapping):
        raise T02RoundError("runtime stripe ROI readback is unavailable")
    if tuple(stripe.get("requested", ())) != STRIPE_ROI or tuple(stripe.get("readback", ())) != STRIPE_ROI:
        raise T02RoundError("runtime stripe ROI differs from the fixed T02 ROI")
    if tuple(config.get("settle_roi", ())) != settle_roi:
        raise T02RoundError("runtime settling ROI differs from the operator-selected ROI")
    if config.get("threshold_requested") != THRESHOLD:
        raise T02RoundError("runtime stripe threshold differs from 60")
    return dict(config)


def _fresh_monitor(runtime: Any, *, monitor_runner: Callable[..., Any],
                   signal_assessor: Callable[..., Any], sleep: Callable[[float], None]) -> dict[str, Any]:
    diagnostic = getattr(runtime.transport, "_diag_mmio", None)
    if diagnostic is None:
        raise T02RoundError("camera diagnostic MMIO is unavailable")
    pclk_samples: list[int] = []
    for index in range(3):
        value = diagnostic.read(0x0C)
        if type(value) is not int or value <= 0:
            raise T02RoundError("fresh PCLK frequency readback must be a positive integer")
        pclk_samples.append(value)
        if index != 2:
            sleep(0.030)
    if max(pclk_samples) > min(pclk_samples) * 1.01:
        raise T02RoundError("fresh PCLK frequency samples are unstable")
    snapshot = monitor_runner(diagnostic)
    signals = signal_assessor(
        snapshot,
        width=FRAME_WIDTH,
        height=FRAME_HEIGHT,
        pclk_hz=min(pclk_samples),
        frames=16,
    )
    if not isinstance(signals, Mapping) or signals.get("camera_signal_verified") is not True:
        raise T02RoundError("fresh camera signal monitor did not pass")
    period = signals.get("frame_period_s")
    if isinstance(period, bool) or not isinstance(period, (int, float)) or period <= 0:
        raise T02RoundError("fresh signal monitor did not provide a positive frame period")
    runtime.frame_period_s = float(period)
    return {
        "pclk_hz_samples": pclk_samples,
        "monitor": dict(snapshot),
        "signals": dict(signals),
        "frame_period_s": runtime.frame_period_s,
    }


def _capture_identity(pair: Any, controller: Any) -> dict[str, Any]:
    request_id = getattr(pair, "request_id", None)
    epoch = getattr(pair, "epoch", None)
    session_id = getattr(pair, "session_id", None)
    if type(request_id) is not int or request_id < 0 or type(epoch) is not int or epoch < 0:
        raise T02RoundError("profile capture request/epoch identity is malformed")
    if not isinstance(session_id, str) or not session_id.strip():
        raise T02RoundError("profile capture session_id is malformed")
    current = controller.mmio
    if current.request_id != request_id or current.epoch != epoch:
        raise T02RoundError("profile capture identity differs from the live P2 request/epoch")
    return {"request_id": request_id, "epoch": epoch, "session_id": session_id}


def _write_trace(output: Path, trace: list[dict[str, Any]]) -> None:
    _atomic_json(output / "sensor_read_trace.json", trace)


def _prepare_phase(args: Any, *, runtime: Any, output: Path,
                   helper: Any, sensor_access_factory: Callable[..., Any],
                   monitor_runner: Callable[..., Any], signal_assessor: Callable[..., Any],
                   exporter: Callable[[Any, Path, int], Mapping[str, Any]],
                   sleep: Callable[[float], None]) -> dict[str, Any]:
    trace: list[dict[str, Any]] = []

    def save_trace() -> None:
        _write_trace(output, trace)

    access = sensor_access_factory(runtime, trace, save_trace)
    before = helper.validate_sensor_settings(_read_sensor_snapshot(access))
    _write_trace(output, trace)
    signals = _fresh_monitor(runtime, monitor_runner=monitor_runner,
                             signal_assessor=signal_assessor, sleep=sleep)
    monitor_sensor = helper.validate_sensor_settings(_read_sensor_snapshot(access))
    if monitor_sensor != before:
        raise T02RoundError("sensor settings changed during fresh monitor sampling")
    _write_trace(output, trace)
    guard_before = _guard_idle(runtime)
    pair = runtime.session.run_static(raw=False)
    after = helper.validate_sensor_settings(_read_sensor_snapshot(access))
    _write_trace(output, trace)
    if after != before:
        raise T02RoundError("sensor settings drifted during the preparation capture")
    guard_after = _guard_idle(runtime)
    identity = _capture_identity(pair, runtime.controller)
    recorder = helper._runtime_recorder_files(
        runtime, pair, Path(args.output).resolve() / "runtime", output,
    )
    raw, raw_hash = helper._validate_capture_payload(pair)
    metrics = exporter(pair, output / "captures", 0)
    exported = helper._validate_export(pair, metrics, output / "captures", 0)
    if len(raw) != FRAME_BYTES:
        raise T02RoundError("preparation capture raw payload is not 752x480 gray8")
    return {
        "sensor_before": {f"0x{reg:02x}": value for reg, value in before.items()},
        "sensor_after": {f"0x{reg:02x}": value for reg, value in after.items()},
        "guard_before": guard_before,
        "guard_after": guard_after,
        "monitor": signals,
        "frame_period_s": signals["frame_period_s"],
        "capture_count": 1,
        "motion_start_count": 0,
        "captures": [{**identity, "raw_bytes": len(raw), "raw_sha256": raw_hash,
                      "metrics": dict(metrics), "export": exported, "recorder": recorder}],
        "operator_selected_settle_roi": list(args.settle_roi),
    }


def _p2_identity(controller: Any, source_root: Path) -> dict[str, int]:
    _prepend_source_path(source_root / "src" / "python")
    from structured_light.motion.pynq_runtime import _p2_capture_identity

    return _p2_capture_identity(controller)


def _read_verified_sensor(helper: Any, access: Any) -> dict[int, int]:
    return helper.validate_sensor_settings(_read_sensor_snapshot(access))


def _persist_baseline_frame(output: Path, index: int, frame: Any,
                            started: float, completed: float,
                            identity: Mapping[str, int]) -> dict[str, Any]:
    pixels = getattr(frame, "pixels", None)
    if (not isinstance(pixels, (bytes, bytearray, memoryview))
            or getattr(frame, "width", None) != FRAME_WIDTH
            or getattr(frame, "height", None) != FRAME_HEIGHT
            or len(pixels) != FRAME_BYTES):
        raise T02RoundError(f"pre-ARM baseline {index} is not a complete 752x480 gray8 frame")
    payload = bytes(pixels)
    folder = output / "baseline_prearm"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"baseline_{index:02d}.gray8.bin"
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(folder)
    return {
        "index": index,
        "capture_started_monotonic_s": started,
        "capture_completed_monotonic_s": completed,
        **dict(identity),
        "actual_bytes": len(payload),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "raw_file": str(path.relative_to(output)).replace("\\", "/"),
    }


def _task_start_count(receipt: object) -> tuple[int | None, bool]:
    if not isinstance(receipt, Mapping):
        return None, False
    value = receipt.get("motion_start_count", receipt.get("motor_start_count"))
    if type(value) is not int or value < 0:
        return None, False
    return value, True


def _pair_identity(pair: Any, motion_result: Any, index: int) -> dict[str, Any]:
    request_id = getattr(pair, "request_id", None)
    epoch = getattr(pair, "epoch", None)
    session_id = getattr(pair, "session_id", None)
    if type(request_id) is not int or request_id < 0 or type(epoch) is not int or epoch < 0:
        raise T02RoundError("motion result capture request/epoch identity is malformed")
    if not isinstance(session_id, str) or not session_id.strip():
        raise T02RoundError("motion result capture session_id is malformed")
    if getattr(motion_result, "position_index", None) != index:
        raise T02RoundError("motion result position index is discontinuous")
    if getattr(motion_result, "camera_request_id", None) != request_id:
        raise T02RoundError("motion result camera request_id differs from its profile capture")
    if getattr(motion_result, "camera_epoch", None) != epoch:
        raise T02RoundError("motion result camera epoch differs from its profile capture")
    return {"request_id": request_id, "epoch": epoch, "session_id": session_id}


def _motion_phase(args: Any, *, runtime: Any, output: Path, helper: Any,
                  sensor_access_factory: Callable[..., Any], monitor_runner: Callable[..., Any],
                  signal_assessor: Callable[..., Any], exporter: Callable[..., Any],
                  settle_sampler_factory: Callable[..., Any] | None,
                  sleep: Callable[[float], None], monotonic: Callable[[], float],
                  publish: Callable[[], None], state: dict[str, Any],
                  source_root: Path) -> dict[str, Any]:
    if not isinstance(getattr(runtime, "motion_confirmation", None), Mapping):
        raise T02RoundError("attached runtime did not validate a fresh motion confirmation")
    if not isinstance(getattr(runtime, "driver_proof", None), Mapping):
        raise T02RoundError("attached runtime did not validate a fresh read-only driver proof")
    original_display = getattr(runtime.session, "display", None)
    sequence_after_batch = (
        args.stage == "sequence" and not args.no_display
        and getattr(args, "sequence_display_policy", "forbidden") == "after_batch"
    )
    if args.stage == "single" and not args.no_display:
        if (original_display is None or getattr(original_display, "no_display", False)
                or getattr(original_display, "display", None) is None):
            raise T02RoundError("displayed single stage requires an attached HDMI output")
        state["post_disarm_display"] = {"status": "deferred_until_guard_revocation"}
    elif sequence_after_batch:
        if (original_display is None or getattr(original_display, "no_display", False)
                or getattr(original_display, "display", None) is None):
            raise T02RoundError("after-batch sequence display requires an attached HDMI output")
        sequence_display_timeout = _sequence_display_call_timeout(runtime, original_display)
        state["post_batch_display"] = {
            "policy": "after_batch_not_live",
            "status": "deferred_until_batch_guard_revocation",
            "budget_s": SEQUENCE_DISPLAY_BUDGET_S,
            "per_call_timeout_s": sequence_display_timeout,
            "render_call_returned_count": 0,
            "receipts": [],
            "user_visual_confirmation": "not_confirmed",
        }
    elif args.stage == "single":
        state["post_disarm_display"] = {
            "status": "disabled_by_request",
            "render_call_returned": False,
            "user_visual_confirmation": "not_requested",
        }

    trace: list[dict[str, Any]] = []

    def save_trace() -> None:
        _atomic_json(output / "sensor_read_trace.json", trace)

    access = sensor_access_factory(runtime, trace, save_trace)
    before = _read_verified_sensor(helper, access)
    _atomic_json(output / "sensor_before_motion.json", {
        "registers": {f"0x{register:02x}": value for register, value in before.items()},
    })
    monitor = _fresh_monitor(runtime, monitor_runner=monitor_runner,
                             signal_assessor=signal_assessor, sleep=sleep)
    monitor_sensor = _read_verified_sensor(helper, access)
    if monitor_sensor != before:
        raise T02RoundError("sensor settings changed during fresh monitor sampling")
    state["monitor"] = monitor
    runtime.frame_period_s = float(monitor["frame_period_s"])

    settle_config = runtime.session.settle_config
    if getattr(settle_config, "protocol_id", None) != "extended_6s_v2":
        raise T02RoundError("T02 motion diagnostic requires extended_6s_v2 settle protocol")
    requested_positions = MOTION_POSITIONS[args.stage]
    expected_pulses = MOTION_PULSES[args.stage]
    if (runtime.session.config.pulses_per_position != expected_pulses
            or float(runtime.session.config.rpm) != 5.0):
        raise T02RoundError("T02 motion config differs from the requested pulse count at 5 rpm")
    if runtime.session.config.positions != requested_positions:
        raise T02RoundError("runtime motion position count differs from the requested T02 stage")

    baseline_frames: list[Any] = []
    baseline_records: list[dict[str, Any]] = []
    previous_identity: dict[str, int] | None = None
    baseline_started = monotonic()
    for index in range(20):
        started = monotonic()
        frame = runtime.session.run_static(raw=True)
        completed = monotonic()
        identity = _p2_identity(runtime.controller, source_root)
        if previous_identity is not None:
            if identity["camera_epoch"] != previous_identity["camera_epoch"]:
                raise T02RoundError("P2 epoch changed during the pre-ARM baseline")
            if identity["camera_request_id"] != previous_identity["camera_request_id"] + 1:
                raise T02RoundError("P2 request_id is not continuous during the pre-ARM baseline")
        previous_identity = identity
        record_identity = {
            "camera_request_id": identity["camera_request_id"],
            "camera_epoch": identity["camera_epoch"],
        }
        record = _persist_baseline_frame(output, index, frame, started, completed, record_identity)
        baseline_frames.append(frame)
        baseline_records.append(record)
        state["baseline_capture_records"] = list(baseline_records)
        state["baseline_frames_completed"] = len(baseline_frames)
        publish()
    baseline_completed = monotonic()
    state["baseline_elapsed_s"] = baseline_completed - baseline_started
    after_baseline = _read_verified_sensor(helper, access)
    if after_baseline != before:
        state["sensor_after_baseline"] = {
            f"0x{register:02x}": value for register, value in after_baseline.items()
        }
        raise T02RoundError("sensor settings drifted across the 20-frame pre-ARM baseline")
    state["sensor_after_baseline"] = {
        f"0x{register:02x}": value for register, value in after_baseline.items()
    }

    if settle_sampler_factory is None:
        python_path = str(source_root / "src" / "python")
        if python_path not in sys.path:
            sys.path.insert(0, python_path)
        from structured_light.motion.pynq_runtime import MeasuredSettleSampler

        settle_sampler_factory = MeasuredSettleSampler
    sampler = settle_sampler_factory(
        runtime.controller,
        baseline_frames=baseline_frames,
        baseline_capture_records=[{
            "capture_started_monotonic_s": item["capture_started_monotonic_s"],
            "capture_completed_monotonic_s": item["capture_completed_monotonic_s"],
            "camera_request_id": item["camera_request_id"],
            "camera_epoch": item["camera_epoch"],
        } for item in baseline_records],
        output=runtime.output / "settle_samples",
        roi=tuple(args.settle_roi),
        config=settle_config,
        config_hash=runtime.session.settle_config_hash,
        frame_period_s=runtime.frame_period_s,
        guard=runtime.guard,
        expected_pulses=runtime.session.config.pulses_per_position,
        capture_timeout_s=runtime.session.config.capture_timeout_s,
        persistence_timeout_s=runtime.session.config.capture_phase_timeout_s,
        monotonic=monotonic,
        sleep=sleep,
    )
    before_arm = _read_verified_sensor(helper, access)
    if before_arm != before:
        state["sensor_before_arm"] = {
            f"0x{register:02x}": value for register, value in before_arm.items()
        }
        raise T02RoundError("sensor settings drifted after baseline and before ARM")
    state["sensor_before_arm"] = {
        f"0x{register:02x}": value for register, value in before_arm.items()
    }
    state["guard_before_arm"] = _guard_idle(runtime)
    state["state"] = "pre_motion_countdown"
    state["motion_countdown_seconds"] = 5
    state["motion_countdown_remaining_s"] = 5
    state["movement_notice"] = (
        "Twenty raw baseline frames and fixed-sensor checks passed; "
        "the bounded T02 motion command will be issued after this countdown."
    )
    publish()
    for remaining in range(5, 0, -1):
        state["motion_countdown_remaining_s"] = remaining
        publish()
        print(f"T02 {args.stage}: bounded motion starts in {remaining} second(s).", flush=True)
        sleep(1.0)
    state["motion_attempted"] = True
    state["state"] = "motion_running"
    publish()

    task_error: BaseException | None = None
    results: list[Any] = []
    if args.stage == "single" and not args.no_display:
        display_proxy: DeferredSingleDisplay | DeferredSequenceDisplay | None = DeferredSingleDisplay()
    elif sequence_after_batch:
        display_proxy = DeferredSequenceDisplay()
    else:
        display_proxy = None
    try:
        if display_proxy is not None:
            runtime.session.display = display_proxy
        if args.stage == "single":
            result = runtime.session.run_single(settle_sampler=sampler)
            results = [result]
        else:
            results = list(runtime.session.run_sequence(
                positions=requested_positions, settle_sampler=sampler,
            ))
    except BaseException as exc:
        task_error = exc
    finally:
        if display_proxy is not None:
            runtime.session.display = original_display
            if task_error is not None:
                failure_state = {
                    "status": "not_attempted_motion_failed",
                    "render_call_returned": False,
                    "deferred_capture_count": len(display_proxy.calls),
                    "deferred_capture_ids": [{
                        "position_index": index,
                        "request_id": getattr(item, "request_id", None),
                        "camera_epoch": getattr(item, "epoch", None),
                        "display_deferred": True,
                        "rendered": False,
                        "shown": False,
                    } for index, item in enumerate(display_proxy.calls)],
                    "user_visual_confirmation": "not_confirmed",
                }
                if isinstance(display_proxy, DeferredSequenceDisplay):
                    state["post_batch_display"] = {
                        **failure_state,
                        "policy": "after_batch_not_live",
                        "budget_s": SEQUENCE_DISPLAY_BUDGET_S,
                        "receipts": [],
                    }
                else:
                    state["post_disarm_display"] = failure_state
                publish()
        task_receipt = getattr(runtime.session, "last_task_receipt", None)
        if isinstance(task_receipt, Mapping):
            state["motion_task_receipt"] = dict(task_receipt)
            count, count_known = _task_start_count(task_receipt)
            state["motion_start_count"] = count
            state["motion_start_count_known"] = count_known
            state["motion_started"] = bool(count) if count_known else None
            try:
                state["motion_evidence"] = runtime._publish_motion_evidence()
            except BaseException as publish_error:
                state["motion_evidence_error"] = f"{type(publish_error).__name__}: {publish_error}"
            publish()
    if task_error is not None:
        raise task_error

    receipt = getattr(runtime.session, "last_task_receipt", None)
    if not isinstance(receipt, Mapping):
        raise T02RoundError("MotionSession did not publish a task receipt")
    count, count_known = _task_start_count(receipt)
    if not count_known or count != requested_positions:
        raise T02RoundError("motion START count does not match the requested T02 position count")
    if receipt.get("positions_completed") != requested_positions or len(results) != requested_positions:
        raise T02RoundError("MotionSession did not complete the requested number of positions")
    if receipt.get("guard_revoked") is not True:
        raise T02RoundError("MotionSession task receipt does not confirm guard revocation")
    observations = list(getattr(sampler, "observations", ()))
    if len(observations) != requested_positions or any(
        not isinstance(item, Mapping)
        or item.get("settled") is not True
        or not isinstance(item.get("estimate"), Mapping)
        or item["estimate"].get("settled") is not True
        for item in observations
    ):
        raise T02RoundError("measured sampler did not settle every requested position")
    state["settle_observations"] = observations
    state["motion_positions_completed"] = requested_positions
    state["motion_start_count"] = count
    state["motion_start_count_known"] = True
    state["motion_started"] = count > 0
    state["angle_calibrated"] = False

    if args.stage == "single":
        result_items = [results[0]]
    else:
        result_items = results
    capture_records: list[dict[str, Any]] = []
    previous_session: str | None = None
    previous_request_id: int | None = None
    previous_epoch: int | None = None
    for index, motion_result in enumerate(result_items):
        pair = getattr(motion_result, "capture_result", None)
        identity = _pair_identity(pair, motion_result, index)
        if previous_session is not None and identity["session_id"] != previous_session:
            raise T02RoundError("profile capture session_id changed within the T02 motion round")
        if previous_epoch is not None and identity["epoch"] != previous_epoch:
            raise T02RoundError("profile capture epoch changed within the T02 motion round")
        if previous_request_id is not None and identity["request_id"] <= previous_request_id:
            raise T02RoundError("profile capture request_id did not increase within the T02 motion round")
        previous_session = identity["session_id"]
        previous_request_id = identity["request_id"]
        previous_epoch = identity["epoch"]
        session_display_receipt = getattr(motion_result, "display_receipt", None)
        if args.stage == "single" and display_proxy is not None:
            session_display_receipt = _validate_deferred_display(session_display_receipt, pair)
        elif isinstance(display_proxy, DeferredSequenceDisplay):
            session_display_receipt = _validate_deferred_display(
                session_display_receipt, pair, position_index=index,
            )
        recorder = helper._runtime_recorder_files(
            runtime, pair, runtime.output, output,
        )
        raw, raw_hash = helper._validate_capture_payload(pair)
        metrics = exporter(pair, output / "captures", index)
        exported = helper._validate_export(pair, metrics, output / "captures", index)
        capture_records.append({
            "position_index": index,
            **identity,
            "raw_bytes": len(raw),
            "raw_sha256": raw_hash,
            "metrics": dict(metrics),
            "export": exported,
            "recorder": recorder,
            "settle_elapsed_s": float(motion_result.settle_elapsed_s),
            "angle_calibrated": False,
            "session_display_receipt": (
                dict(session_display_receipt) if isinstance(session_display_receipt, Mapping) else None
            ),
        })
        state["captures"] = list(capture_records)
        state["capture_count"] = len(capture_records)
        publish()

    after_motion = _read_verified_sensor(helper, access)
    state["sensor_after_motion"] = {
        f"0x{register:02x}": value for register, value in after_motion.items()
    }
    if after_motion != before:
        raise T02RoundError("sensor settings drifted during the T02 motion round")
    state["guard_after_motion"] = _post_disarm_guard_idle(runtime)
    if isinstance(display_proxy, DeferredSequenceDisplay):
        if len(display_proxy.calls) != requested_positions or any(
            display_proxy.calls[index] is not results[index].capture_result
            for index in range(requested_positions)
        ):
            raise T02RoundError("after-batch display proxy did not collect each sequence frame exactly once")
        motor_status = _motor_idle(runtime)
        state["post_batch_display"].update({
            "status": "ready_after_batch_guard_revocation",
            "guard_status": dict(state["guard_after_motion"]),
            "motor_status": motor_status,
        })
        publish()
        _replay_sequence_after_batch(
            runtime, results, capture_records, display_proxy,
            state=state, publish=publish, monotonic=monotonic,
            per_call_timeout_s=sequence_display_timeout,
        )
    elif isinstance(display_proxy, DeferredSingleDisplay):
        if len(display_proxy.calls) != 1 or display_proxy.calls[0] is not results[0].capture_result:
            raise T02RoundError("single-position deferred display was not called exactly once for its capture")
        deferred_receipt = _validate_deferred_display(
            getattr(results[0], "display_receipt", None), results[0].capture_result,
        )
        motor_status = _motor_idle(runtime)
        state["post_disarm_display"] = {
            "status": "ready_after_guard_revocation",
            "render_call_returned": False,
            "deferred_receipt": deferred_receipt,
            "guard_status": dict(state["guard_after_motion"]),
            "motor_status": motor_status,
            "user_visual_confirmation": "not_confirmed",
        }
        publish()
        try:
            actual_receipt = runtime.session._display(results[0].capture_result)
            actual_receipt = _validate_actual_display(actual_receipt, results[0].capture_result)
        except BaseException as exc:
            state["post_disarm_display"] = {
                "status": "failed",
                "render_call_returned": False,
                "deferred_receipt": deferred_receipt,
                "guard_status": dict(state["guard_after_motion"]),
                "motor_status": motor_status,
                "user_visual_confirmation": "not_confirmed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            publish()
            raise T02RoundError(f"post-disarm HDMI display failed: {exc}") from exc
        state["post_disarm_display"] = {
            "status": "output_call_returned",
            "render_call_returned": True,
            "receipt": actual_receipt,
            "deferred_receipt": deferred_receipt,
            "guard_status": dict(state["guard_after_motion"]),
            "motor_status": motor_status,
            "user_visual_confirmation": "not_confirmed",
        }
        publish()
    state["state"] = "closing"
    return {
        "motion_task_receipt": dict(receipt),
        "motion_evidence": state.get("motion_evidence"),
        "motion_start_count": count,
        "motion_start_count_known": True,
        "motion_started": count > 0,
        "motion_positions_completed": requested_positions,
        "settle_observations": observations,
        "capture_count": len(capture_records),
        "captures": capture_records,
        "angle_calibrated": False,
        "sensor_before": {f"0x{register:02x}": value for register, value in before.items()},
        "sensor_after_motion": state["sensor_after_motion"],
    }


def execute_phase(args: Any, *, runtime_factory: Callable[..., Any],
                  sensor_access_factory: Callable[..., Any] | None,
                  monitor_runner: Callable[..., Any], signal_assessor: Callable[..., Any],
                  exporter: Callable[[Any, Path, int], Mapping[str, Any]],
                  sleep: Callable[[float], None] = time.sleep,
                  monotonic: Callable[[], float] = time.monotonic,
                  settle_sampler_factory: Callable[..., Any] | None = None,
                  display_confirmation_waiter: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Run one T02 stage; runtime and sensor access are injected for offline tests."""

    calibration_gate = _calibration_gate(args)
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError(f"T02 output must be unique and unused: {output}")
    output.mkdir(parents=True, exist_ok=False)
    runtime_output = output / "runtime"
    state: dict[str, Any] = {
        "schema": "t02_motion_round_v1",
        "stage": args.stage,
        "calibration_gate": calibration_gate,
        "state": "starting",
        "passed": False,
        "motion_started": False,
        "motion_start_count": 0,
        "motion_start_count_known": True,
        "motion_attempted": False,
        "overlay_downloads": 0,
        "hidden_retry": False,
        "sensor_write_count": 0,
        "capture_configuration_requested": {
            "stripe_roi": list(STRIPE_ROI), "threshold": THRESHOLD,
            "settle_roi": list(args.settle_roi), "display_enabled": not args.no_display,
            "display_confirmation_timeout_s": getattr(args, "display_confirmation_timeout_s", 0.0),
            "sequence_display_policy": getattr(args, "sequence_display_policy", "forbidden"),
        },
        "sequence_board_clearance": (
            "not_cleared_pending_display_policy" if args.stage == "sequence" and args.no_display
            else "after_batch_not_live_pending_board_validation"
            if args.stage == "sequence" and getattr(args, "sequence_display_policy", "forbidden") == "after_batch"
            else None
        ),
        "captures": [],
        "baseline_capture_records": [],
        "cleanup": None,
    }
    _atomic_json(output / "status.json", state)
    runtime = None
    helper = None
    root = None
    last_capture: Mapping[str, Any] | None = None
    close_valid = False

    def publish() -> None:
        _atomic_json(output / "status.json", state)
        _atomic_json(output / "capture_records.json", state.get("captures", []))

    try:
        display_timeout = _display_timeout(args)
        if not isinstance(args.installation, str) or not args.installation.strip():
            raise ValueError("a unique non-empty T02 --installation is required")
        if args.stage in MOTION_POSITIONS and (args.motion_confirmation is None or args.driver_proof is None):
            raise ValueError("motion stages require fresh --motion-confirmation and --driver-proof")
        if args.stage == "prepare" and (args.motion_confirmation is not None or args.driver_proof is not None):
            raise ValueError("prepare must not receive motion confirmation or driver proof")
        root = _source_root(args.source_root)
        helper, capture_helper_path, capture_helper_sha = _load_capture_helper(
            args.capture_helper, args.capture_helper_sha256,
        )
        exposure_path, exposure_sha = helper.verify_module_digest(args.exposure_module, args.module_sha256)
        state["source_root"] = str(root)
        state["capture_helper"] = str(capture_helper_path)
        state["capture_helper_sha256"] = capture_helper_sha
        state["exposure_module"] = str(exposure_path)
        state["exposure_module_sha256"] = exposure_sha
        if sensor_access_factory is None:
            access_class, _ = helper.load_frozen_sccb_access(exposure_path, exposure_sha)

            def sensor_access_factory(runtime, trace, save):
                diagnostic = runtime.transport._diag_mmio
                return access_class(diagnostic, trace, save)

        state["state"] = "attaching"
        publish()
        runtime_stage = (
            "static" if args.stage == "prepare"
            else "sequence" if args.stage == "short"
            else args.stage
        )
        runtime = runtime_factory(args.manifest, **_runtime_arguments(
            args, runtime_output, stage=runtime_stage,
        ))
        state["runtime_output"] = str(runtime_output)
        state["attached"] = True
        state["guard_before"] = _guard_idle(runtime)
        state["capture_configuration_readback"] = _validate_capture_config(runtime, tuple(args.settle_roi))
        if args.stage == "prepare":
            payload = _prepare_phase(
                args, runtime=runtime, output=output, helper=helper,
                sensor_access_factory=sensor_access_factory,
                monitor_runner=monitor_runner, signal_assessor=signal_assessor,
                exporter=exporter, sleep=sleep,
            )
            state.update(payload)
            if payload.get("captures"):
                last_capture = payload["captures"][-1]
            if display_timeout > 0:
                state["state"] = "awaiting_display_confirmation"
                publish()
                confirmation = _wait_display_confirmation(
                    root, output, display_timeout, display_confirmation_waiter,
                )
                payload["display_confirmation"] = confirmation
                state["display_confirmation"] = confirmation
            state["state"] = "closing"
            publish()
        else:
            payload = _motion_phase(
                args, runtime=runtime, output=output, helper=helper,
                sensor_access_factory=sensor_access_factory,
                monitor_runner=monitor_runner, signal_assessor=signal_assessor,
                exporter=exporter, settle_sampler_factory=settle_sampler_factory,
                sleep=sleep, monotonic=monotonic, publish=publish,
                state=state, source_root=root,
            )
            state.update(payload)
            state["state"] = "closing"
            publish()
    except BaseException as exc:
        state.update({"state": "failed", "passed": False,
                      "error_type": type(exc).__name__, "error": str(exc)})
        publish()
    finally:
        if runtime is not None:
            task_receipt = getattr(getattr(runtime, "session", None), "last_task_receipt", None)
            if isinstance(task_receipt, Mapping):
                state["motion_task_receipt"] = dict(task_receipt)
                count, count_known = _task_start_count(task_receipt)
                state["motion_start_count"] = count
                state["motion_start_count_known"] = count_known
                state["motion_started"] = (count > 0) if count_known else None
                if state.get("motion_attempted") and state.get("motion_evidence") is None:
                    try:
                        state["motion_evidence"] = runtime._publish_motion_evidence()
                    except BaseException as evidence_error:
                        state["motion_evidence_error"] = f"{type(evidence_error).__name__}: {evidence_error}"
            elif state.get("motion_attempted"):
                state["motion_start_count"] = None
                state["motion_start_count_known"] = False
                state["motion_started"] = None
            if last_capture is None and root is not None:
                try:
                    current = _p2_identity(runtime.controller, root)
                    last_capture = {
                        "request_id": current["camera_request_id"],
                        "epoch": current["camera_epoch"],
                    }
                except BaseException as identity_error:
                    state["handoff_identity_error"] = f"{type(identity_error).__name__}: {identity_error}"
            try:
                cleanup = runtime.close()
                state["cleanup"] = cleanup
                if isinstance(cleanup, Mapping) and cleanup.get("closed") is True and helper is not None and last_capture is not None:
                    try:
                        state["cleanup"] = helper._validate_cleanup(
                            cleanup, runtime_output=runtime_output,
                            last_capture=last_capture,
                        )
                        close_valid = True
                    except BaseException as cleanup_error:
                        state["cleanup_validation_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
                else:
                    state["cleanup_validation_error"] = "runtime close did not confirm closed=true"
            except BaseException as close_error:
                state["close_error"] = f"{type(close_error).__name__}: {close_error}"
        if state.get("state") == "closing" and close_valid:
            state.update({"state": "complete", "passed": True})
        else:
            state.update({"state": "failed", "passed": False})
        state["runtime_closed"] = bool(
            isinstance(state.get("cleanup"), Mapping) and state["cleanup"].get("closed") is True
        )
        state["finished_unix_ns"] = time.time_ns()
        publish()
        _atomic_json(output / "summary.json", state)
    return state


def _monitor_runner(diag: Any, *, source_root: Path | None = None) -> Any:
    root = source_root or Path(__file__).resolve().parents[2]
    _prepend_source_path(root / "src" / "python")
    from structured_light.p2.camera_evidence import run_monitor

    return run_monitor(diag)


def _signal_assessor(snapshot: Any, *, source_root: Path | None = None, **kwargs: Any) -> Mapping[str, Any]:
    root = source_root or Path(__file__).resolve().parents[2]
    _prepend_source_path(root / "src" / "python")
    from structured_light.p2.camera_evidence import assess_signals

    return assess_signals(snapshot, **kwargs)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.stage in MOTION_POSITIONS and (args.motion_confirmation is None or args.driver_proof is None):
        parser().error("motion stages require --motion-confirmation and --driver-proof")
    if args.stage == "prepare" and (args.motion_confirmation is not None or args.driver_proof is not None):
        parser().error("prepare does not accept motion confirmation or driver proof")
    try:
        _calibration_gate(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser().error(str(exc))
    root = Path(args.source_root)
    state = execute_phase(
        args,
        runtime_factory=_runtime_factory(root),
        sensor_access_factory=None,
        monitor_runner=lambda diag: _monitor_runner(diag, source_root=root),
        signal_assessor=lambda snapshot, **kwargs: _signal_assessor(
            snapshot, source_root=root, **kwargs,
        ),
        exporter=_exporter(root),
    )
    return 0 if state.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
