"""P2 closeout acceptance runner for the real PYNQ-Z2.

This module is intentionally a thin board-side orchestration layer.  The
transport, controller, Profile codec and Python oracle remain the single
implementation of their respective contracts.  The runner adds the evidence
which a long board run needs: an exclusive owner lock, a per-request receipt,
an atomic progress file, and a recovery record that is written before the
next request is allowed.

The script has three hardware stages:

``raw``
    1000 one-frame requests through the already accepted
    ``camera_raw_monitor`` Overlay.
``profile``
    1000 paired raw/Profile requests through the new Profile Overlay.  The
    default closeout configuration uses the vertical ROI ``[0,120,w,300]``
    and threshold 200 so the repaired ROI flags are exercised on hardware.
``recovery``
    24 trials by default: eight STOP, eight diagnostic abort plus bounded DMA
    reset, and eight PL watchdog trials.  ``--include-host-timeout`` adds a
    separate host deadline class.

No stage substitutes a Mock transport, retries a failed request, controls the
laser, or changes camera wiring.  A failed request is recorded and terminates
the stage so a later success cannot hide it.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterator
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/python"))

RAW_MODE = "camera_raw_monitor"
PROFILE_MODES = {"structured_light_profile", "structured_light_profile_hdmi"}
RAW_BYTES = 752 * 480
PROFILE_BUFFER_BYTES = 8192
DEFAULT_RECOVERY_COUNT = 24
DEFAULT_WATCHDOG_CYCLES = 500_000  # 5 ms at the frozen 100 MHz SYS clock


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    """Atomically publish a status or summary JSON file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    """Append one durable event; every request has its own line."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def board_owner_lock(path: str | Path = "/tmp/sl_camera_preview.lock") -> Iterator[None]:
    """Take the same non-blocking owner lock used by the live preview.

    ``fcntl`` is available on the PYNQ Linux target.  Importing this module on
    Windows remains possible for host tests, but entering the lock there
    raises a clear error instead of silently claiming hardware ownership.
    """

    try:
        import fcntl  # type: ignore
    except ImportError as exc:  # pragma: no cover - only exercised on Windows
        raise RuntimeError("P2 board ownership requires POSIX fcntl on the PYNQ target") from exc
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"camera owner lock is already held: {lock_path}") from exc
        handle.write(f"p2_closeout pid={os.getpid()} started={_utc()}\n")
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _read_config(path: Path) -> dict[str, Any]:
    import yaml

    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("board config must be a mapping")
    return value


def _int_config(config: dict[str, Any], name: str, default: int) -> int:
    value = config.get(name, default)
    return int(value, 0) if isinstance(value, str) else int(value)


def _roi(config: dict[str, Any], width: int, *, fixed_vertical: bool = True) -> tuple[int, int, int, int]:
    """Resolve closeout parameters without modifying the evidence-bound YAML.

    The existing Gate-A/run configuration is preserved.  Closeout uses a
    separate ``closeout_roi`` if supplied; otherwise the vertical crop is the
    explicit default required to exercise the repaired excluded-row flags.
    """

    if "closeout_roi" in config:
        raw = config["closeout_roi"]
    elif fixed_vertical:
        raw = (0, 120, width, 300)
    else:
        raw = config.get("roi", (0, 0, width, 480))
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ValueError("ROI must contain x0,y0,x1,y1")
    result = tuple(int(item) for item in raw)
    x0, y0, x1, y1 = result
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= 480):
        raise ValueError(f"ROI {result} is outside the {width}x480 frame")
    return result


def effective_parameters(config: dict[str, Any], *, fixed_vertical: bool = True) -> dict[str, Any]:
    width = _int_config(config, "width", _int_config(config, "frame_width", 752))
    height = _int_config(config, "height", _int_config(config, "frame_height", 480))
    if height != 480 or width not in (640, 752):
        raise ValueError("P2 closeout supports only 640/752 x 480")
    return {
        "width": width,
        "height": height,
        "threshold": _int_config(config, "closeout_threshold", _int_config(config, "threshold", 200)),
        "roi": _roi(config, width, fixed_vertical=fixed_vertical),
        "min_width": _int_config(config, "min_width", 1),
        "max_width": _int_config(config, "max_width", 32),
        "min_sum": _int_config(config, "min_sum", 30),
        "min_peak": _int_config(config, "min_peak", 15),
        "normal_timeout_cycles": _int_config(config, "normal_timeout_cycles", 100_000_000),
        "watchdog_timeout_cycles": _int_config(config, "watchdog_timeout_cycles", DEFAULT_WATCHDOG_CYCLES),
    }


class ReceiptPynqTransport:  # populated below on demand; keeps PC imports PYNQ-free
    pass


def _make_receipt_transport():
    """Build a PYNQ transport subclass without importing PYNQ on the PC."""

    from structured_light.p2.pynq_transport import PynqTransport

    class _ReceiptTransport(PynqTransport):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.last_receipt: dict[str, Any] = {}

        @staticmethod
        def _dma_snapshot(dma: Any) -> dict[str, Any] | None:
            if dma is None:
                return None
            item: dict[str, Any] = {}
            try:
                item["status"] = int(dma._rd(dma.status_offset))
                item["control"] = int(dma._rd(dma.control_offset))
                item["length_register"] = int(dma._rd(dma.length_offset))
            except Exception as exc:
                item["read_error"] = str(exc)
            state = getattr(dma, "transfer_state", None)
            if state is not None:
                item.update({
                    "requested_length": state.requested_length,
                    "actual_length": state.actual_length,
                    "armed": state.armed,
                    "completed": state.completed,
                })
            return item

        def _receipt_snapshot(self, phase: str) -> dict[str, Any]:
            receipt: dict[str, Any] = {
                "phase": phase,
                "captured_utc": _utc(),
                "request_id": getattr(self, "_accepted_request_id", None),
                "epoch": getattr(self, "_accepted_epoch", None),
                "raw_dma": self._dma_snapshot(getattr(self, "raw_dma", None)),
                "profile_dma": self._dma_snapshot(getattr(self, "profile_dma", None)),
            }
            try:
                receipt["control_status"] = int(self.mmio.read(4))
            except Exception as exc:
                receipt["control_status_error"] = str(exc)
            try:
                receipt["diag_request_id"] = int(self._diag_mmio.read(0x48))
                receipt["diag_epoch"] = int(self._diag_mmio.read(0x44))
                receipt["diag_error"] = int(self._diag_mmio.read(0x50))
            except Exception as exc:
                receipt["diag_read_error"] = str(exc)
            return receipt

        def receive(self):  # type: ignore[no-untyped-def]
            before = self._receipt_snapshot("before_receive")
            try:
                result = super().receive()
            except Exception as exc:
                self.last_receipt = {
                    **before,
                    "phase": "receive_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "recovery": getattr(self, "last_recovery", {}),
                }
                raise
            self.last_receipt = {
                **before,
                "phase": "received",
                "raw_actual_length": result.raw_length,
                "profile_actual_length": result.profile_length,
                "raw_sha256": _sha256_bytes(result.raw or b"") if result.raw is not None else None,
                "profile_sha256": _sha256_bytes(result.profile or b"") if result.profile is not None else None,
                "diagnostic_request_id": result.diagnostics.request_id if result.diagnostics else None,
                "diagnostic_epoch": result.diagnostics.epoch if result.diagnostics else None,
                "recovery": getattr(self, "last_recovery", {}),
            }
            return result

    return _ReceiptTransport


def _dma_snapshot(transport: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("raw", "profile"):
        dma = getattr(transport, f"{name}_dma", None)
        if dma is None:
            continue
        item: dict[str, Any] = {}
        try:
            item.update({
                "status": int(dma._rd(dma.status_offset)),
                "control": int(dma._rd(dma.control_offset)),
                "length_register": int(dma._rd(dma.length_offset)),
            })
        except Exception as exc:
            item["read_error"] = str(exc)
        state = getattr(dma, "transfer_state", None)
        if state is not None:
            item.update({
                "requested_length": state.requested_length,
                "actual_length": state.actual_length,
                "armed": bool(state.armed),
                "completed": bool(state.completed),
            })
        result[name] = item
    return result


def _control_snapshot(transport: Any) -> dict[str, Any]:
    from structured_light.p2.contract import DIAG_OFFSETS, REG_PROFILE_ID, REG_STATUS

    mmio = transport.mmio
    diag = transport._diag_mmio
    status = int(mmio.read(REG_STATUS)) & 0xFFFF_FFFF
    snap = {
        "captured_utc": _utc(),
        "status": status,
        "busy": bool(status & 1),
        "done": bool(status & 2),
        "packer_busy": bool(status & 4),
        "profile_id": int(mmio.read(REG_PROFILE_ID)) & 0xFFFF_FFFF,
        "diag_request_id": int(diag.read(DIAG_OFFSETS["request_id"])) & 0xFFFF_FFFF,
        "diag_epoch": int(diag.read(DIAG_OFFSETS["epoch"])) & 0xFFFF_FFFF,
        "diag_ready": int(diag.read(DIAG_OFFSETS["ready"])) & 0xFFFF_FFFF,
        "diag_error": int(diag.read(DIAG_OFFSETS["error"])) & 0xFFFF_FFFF,
        "dma": _dma_snapshot(transport),
    }
    return snap


def _wait_active_evidence(transport: Any, handle: Any, timeout_s: float = 0.5) -> dict[str, Any]:
    """Require an actually active request before injecting a recovery fault."""

    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _control_snapshot(transport)
        dma_values = [value for value in last["dma"].values() if value]
        no_ioc = all(not (int(value.get("status", 0)) & (1 << 12)) for value in dma_values)
        identity = (
            last["profile_id"] == int(handle.request_id)
            and last["diag_request_id"] == int(handle.request_id)
            and last["diag_epoch"] == int(handle.epoch)
        )
        if last["busy"] and not last["done"] and identity and no_ioc:
            last["active_verified"] = True
            return last
        time.sleep(0.001)
    last["active_verified"] = False
    last["active_failure"] = (
        "request completed before fault injection or identity/DMA activity was not observable"
    )
    return last


def _prepare_board(config: dict[str, Any], config_path: Path, output: Path, *, mode: str) -> dict[str, Any]:
    """Validate target/artifacts, load one Overlay and pass camera Gate A."""

    from verify_p2_board import (
        _check_gray_gate_a,
        _check_gray_target_identity,
        release_camera,
        validate_overlay_contract,
        write_json,
    )
    from gray_capture_board import verify_camera_control
    from structured_light.p2.camera_evidence import assess_signals, run_monitor
    from structured_light.p2.pynq_transport import validate_runtime_contract
    from pynq import MMIO, Overlay

    if mode == RAW_MODE:
        if config.get("raw_overlay_mode", RAW_MODE) != RAW_MODE:
            raise ValueError("raw closeout requires the accepted camera_raw_monitor Overlay")
    elif mode not in PROFILE_MODES:
        raise ValueError(f"unsupported closeout overlay mode {mode!r}")
    identity = _check_gray_target_identity(config)
    gate_a = _check_gray_gate_a(config, config_path)
    validation = validate_overlay_contract(config, mode)
    write_json(output / "deployment_gate.json", {"target_identity": identity, "gate_a": gate_a})
    write_json(output / "artifact_validation.json", validation)
    overlay = Overlay(validation["bitfile"], download=True)
    control = MMIO(0x43C00000, 0x10000)
    diag = MMIO(0x43C10000, 0x10000)
    runtime = validate_runtime_contract(control, diag, overlay_mode=mode)
    write_json(output / "runtime_contract.json", runtime)
    release_camera(diag)
    camera = verify_camera_control(diag, output)
    width = _int_config(config, "width", 752)
    height = _int_config(config, "height", 480)
    signal_snapshot = run_monitor(diag, timeout_s=6.0)
    signal = assess_signals(signal_snapshot, width=width, height=height,
                            pclk_hz=min(camera["pclk_hz"]), frames=16)
    write_json(output / "camera_gate.json", {
        "id_reads": camera["id_reads"], "pclk_hz": camera["pclk_hz"], "signals": signal,
    })
    if not signal.get("camera_signal_verified"):
        raise RuntimeError("16-frame camera signal gate failed; no closeout request was started")
    receipt_type = _make_receipt_transport()
    transport = receipt_type(
        validation["bitfile"], validation["hwh"], validation["manifest"],
        frame_width=width, frame_height=height, overlay_mode=mode, overlay=overlay,
    )
    return {
        "validation": validation, "runtime": runtime, "camera": camera,
        "signal": signal, "overlay": overlay, "control": control, "diag": diag,
        "transport": transport,
    }


def _configure_controller(controller: Any, parameters: dict[str, Any]) -> None:
    controller.set_threshold(parameters["threshold"])
    controller.configure_roi(*parameters["roi"])
    controller.configure_centroid(
        min_width=parameters["min_width"], max_width=parameters["max_width"],
        min_sum=parameters["min_sum"], min_peak=parameters["min_peak"],
    )
    controller.mmio.write(0x50, int(parameters["normal_timeout_cycles"]))


def _status(output: Path, *, stage: str, requested: int, completed: int, passed: bool, **extra: Any) -> None:
    _write_json(output / "status.json", {
        "schema": "p2_closeout_status_v1", "stage": stage, "requested": requested,
        "completed": completed, "passed": passed, "updated_utc": _utc(), **extra,
    })


def run_raw_stage(config: dict[str, Any], config_path: Path, output: Path, count: int) -> dict[str, Any]:
    from structured_light.p2.controller import P2Controller

    params = effective_parameters(config, fixed_vertical=False)
    _write_json(output / "parameters.json", {"schema": "p2_closeout_parameters_v1", "parameters": params})
    setup = _prepare_board(config, config_path, output, mode=RAW_MODE)
    transport = setup["transport"]
    controller = P2Controller(transport, default_timeout_s=max(2.0, 3 * setup["signal"]["frame_period_s"] + 1.0))
    _configure_controller(controller, params)
    raw_dir = output / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    records = output / "raw_records.jsonl"
    completed = 0
    try:
        for index in range(count):
            started = _utc()
            try:
                frame = controller.capture_raw(timeout_s=controller.default_timeout_s)
                receipt = dict(getattr(transport, "last_receipt", {}))
                if receipt.get("raw_actual_length") != params["width"] * params["height"]:
                    raise RuntimeError("raw receipt does not contain the exact frame length")
                name = f"raw_{index:06d}_e{int(getattr(transport, 'last_accepted_epoch', 0) or 0):08x}.bin"
                path = raw_dir / name
                path.write_bytes(frame.pixels)
                digest = _sha256_file(path)
                record = {
                    "index": index, "started_utc": started, "finished_utc": _utc(),
                    "request_id": receipt.get("request_id"), "epoch": receipt.get("epoch"),
                    "actual_length": receipt.get("raw_actual_length"), "expected_length": params["width"] * params["height"],
                    "raw_file": str(path.relative_to(output)), "sha256": digest,
                    "receipt": receipt, "passed": True,
                }
                _append_jsonl(records, record)
                completed += 1
                _status(output, stage="raw", requested=count, completed=completed, passed=False)
            except Exception as exc:
                failure = {"index": index, "started_utc": started, "finished_utc": _utc(),
                           "passed": False, "error_type": type(exc).__name__, "error": str(exc),
                           "receipt": getattr(transport, "last_receipt", {}),
                           "recovery": getattr(transport, "last_recovery", {})}
                _append_jsonl(records, failure)
                _status(output, stage="raw", requested=count, completed=completed, passed=False, failure=failure)
                raise
        result = {"schema": "p2_closeout_raw_v1", "stage": "raw", "count_requested": count,
                  "count_completed": completed, "passed": completed == count,
                  "parameters": params, "runtime": setup["runtime"], "no_hidden_retry": True}
        _write_json(output / "summary.json", result)
        return result
    finally:
        controller.close()


def _session_record_path(recorder: Any, result: Any) -> Path:
    return recorder.path / f"capture_e{int(result.epoch):08x}_r{int(result.request_id):08x}.result.json"


def run_profile_stage(config: dict[str, Any], config_path: Path, output: Path, count: int) -> dict[str, Any]:
    from structured_light.p2.controller import P2Controller
    from structured_light.p2.session import SessionRecorder

    params = effective_parameters(config, fixed_vertical=True)
    _write_json(output / "parameters.json", {"schema": "p2_closeout_parameters_v1", "parameters": params})
    setup = _prepare_board(config, config_path, output, mode=str(config.get("overlay_mode", "structured_light_profile_hdmi")))
    transport = setup["transport"]
    recorder = SessionRecorder(output / "sessions", metadata={
        "stage": "profile", "hardware": "real_pynq_z2", "same_frame": True,
        "parameters": params,
    })
    controller = P2Controller(
        transport, recorder=recorder, session_id=recorder.session_id,
        default_timeout_s=max(2.0, 3 * setup["signal"]["frame_period_s"] + 1.0),
    )
    _configure_controller(controller, params)
    records = output / "profile_records.jsonl"
    completed = 0
    try:
        for index in range(count):
            started = _utc()
            try:
                result = controller.capture_pair(timeout_s=controller.default_timeout_s)
                receipt = dict(getattr(transport, "last_receipt", {}))
                record_path = _session_record_path(recorder, result)
                if not record_path.is_file():
                    raise RuntimeError(f"SessionRecorder did not publish {record_path.name}")
                if receipt.get("raw_actual_length") != params["width"] * params["height"]:
                    raise RuntimeError("raw S2MM actual length is not the full frame")
                if not 0 < int(receipt.get("profile_actual_length", 0)) <= 5792:
                    raise RuntimeError("Profile S2MM actual length is outside SLP2 limit")
                record = {
                    "index": index, "started_utc": started, "finished_utc": _utc(),
                    "request_id": result.request_id, "epoch": result.epoch,
                    "actual_raw_length": receipt.get("raw_actual_length"),
                    "actual_profile_length": receipt.get("profile_actual_length"),
                    "raw_sha256": result.input_sha256,
                    "profile_sha256": _sha256_bytes(result.profile_payload),
                    "session_id": recorder.session_id,
                    "session_record": str(record_path.relative_to(output)),
                    "comparison_passed": bool(result.comparison and result.comparison.passed),
                    "mismatch_count": result.comparison.mismatch_count if result.comparison else None,
                    "valid_rows": sum(1 for row in result.diagnostics.rows if row.valid and row.candidate_count == 1),
                    "receipt": receipt, "passed": True,
                }
                _append_jsonl(records, record)
                controller.drain_results()
                completed += 1
                _status(output, stage="profile", requested=count, completed=completed, passed=False)
            except Exception as exc:
                failure = {"index": index, "started_utc": started, "finished_utc": _utc(),
                           "passed": False, "error_type": type(exc).__name__, "error": str(exc),
                           "receipt": getattr(transport, "last_receipt", {}),
                           "recovery": getattr(transport, "last_recovery", {})}
                _append_jsonl(records, failure)
                _status(output, stage="profile", requested=count, completed=completed, passed=False, failure=failure)
                raise
        result = {"schema": "p2_closeout_profile_v1", "stage": "profile", "count_requested": count,
                  "count_completed": completed, "passed": completed == count,
                  "parameters": params, "runtime": setup["runtime"], "no_hidden_retry": True}
        _write_json(output / "summary.json", result)
        return result
    finally:
        controller.close()
        recorder.close()


def _cancel_evidence(transport: Any) -> dict[str, Any]:
    diag = transport._diag_mmio
    error = int(diag.read(0x50)) & 0xFFFF_FFFF
    return {
        "diag_error": error,
        "timeout_bit": bool(error & 1),
        "stop_bit": bool(error & (1 << 6)),
        "diag_request_id": int(diag.read(0x48)) & 0xFFFF_FFFF,
        "diag_epoch": int(diag.read(0x44)) & 0xFFFF_FFFF,
        "diag_ready": int(diag.read(0x08)) & 0xFFFF_FFFF,
    }


def _recovery_post(controller: Any, transport: Any, handle: Any, params: dict[str, Any], recorder: Any) -> dict[str, Any]:
    result = controller.capture_pair(timeout_s=controller.default_timeout_s)
    expected_id = (int(handle.request_id) + 1) & 0xFFFF_FFFF
    expected_epoch = (int(handle.epoch) + 1) & 0xFFFF
    if result.request_id != expected_id or result.epoch != expected_epoch:
        raise RuntimeError(
            f"recovery identity did not advance by one: got id={result.request_id} epoch={result.epoch}, "
            f"expected id={expected_id} epoch={expected_epoch}"
        )
    if result.comparison is None or not result.comparison.passed:
        raise RuntimeError("post-recovery same-frame comparison failed")
    record_path = _session_record_path(recorder, result)
    controller.drain_results()
    return {
        "request_id": result.request_id, "epoch": result.epoch,
        "session_record": str(record_path.relative_to(recorder.path.parent.parent)),
        "comparison_passed": True,
        "raw_sha256": result.input_sha256,
        "profile_sha256": _sha256_bytes(result.profile_payload),
    }


def clear_recovery_errors(controller: Any, transport: Any, timeout_s: float = 0.5) -> dict[str, Any]:
    """Save sticky cancellation/reset events, then CLEAR only at the idle barrier.

    STOP sets ERROR_FLAGS bit17 (the digital reset-event latch); the next START
    clears per-capture diagnostics but does not clear that sticky ABI event.
    No error bit is masked in the receiver. A new request is allowed only after
    explicit CLEAR has read back zero without changing the request identity.
    """
    from structured_light.p2.contract import DIAG_OFFSETS, REG_ERROR_FLAGS, REG_STATUS

    status = int(controller.mmio.read(REG_STATUS))
    ready = int(transport._diag_mmio.read(DIAG_OFFSETS["ready"]))
    if status & 5 or not ready & 1 or ready & 4:
        raise RuntimeError("CLEAR requires an idle, ready pipeline after DMA recovery")
    identity = (int(transport._diag_mmio.read(DIAG_OFFSETS["request_id"])),
                int(transport._diag_mmio.read(DIAG_OFFSETS["epoch"])))
    result = {
        "control_error_before": int(controller.mmio.read(REG_ERROR_FLAGS)),
        "diag_error_before": int(transport._diag_mmio.read(DIAG_OFFSETS["error"])),
        "request_id_before": identity[0], "epoch_before": identity[1],
        "status_before": status, "ready_before": ready, "timeout_s": timeout_s,
    }
    controller.clear_errors()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result["control_error_after"] = int(controller.mmio.read(REG_ERROR_FLAGS))
        result["diag_error_after"] = int(transport._diag_mmio.read(DIAG_OFFSETS["error"]))
        if result["control_error_after"] == result["diag_error_after"] == 0:
            after = (int(transport._diag_mmio.read(DIAG_OFFSETS["request_id"])),
                     int(transport._diag_mmio.read(DIAG_OFFSETS["epoch"])))
            result.update(request_id_after=after[0], epoch_after=after[1])
            if after != identity:
                raise RuntimeError("CLEAR changed the capture identity")
            result["passed"] = True
            return result
        time.sleep(0.001)
    raise RuntimeError("sticky recovery events did not clear before the bounded deadline")


def run_recovery_stage(config: dict[str, Any], config_path: Path, output: Path, count: int,
                       *, include_host_timeout: bool = False) -> dict[str, Any]:
    from structured_light.p2.controller import P2Controller
    from structured_light.p2.session import SessionRecorder
    from structured_light.p2.errors import CaptureTimeout
    from structured_light.p2.contract import DIAG_OFFSETS, REG_CONTROL, REG_TIMEOUT_CYCLES

    if count < 20:
        raise ValueError("recovery count must be at least 20")
    params = effective_parameters(config, fixed_vertical=True)
    _write_json(output / "parameters.json", {"schema": "p2_closeout_parameters_v1", "parameters": params})
    mode = str(config.get("overlay_mode", "structured_light_profile_hdmi"))
    setup = _prepare_board(config, config_path, output, mode=mode)
    transport = setup["transport"]
    recorder = SessionRecorder(output / "sessions", metadata={
        "stage": "recovery", "hardware": "real_pynq_z2", "parameters": params,
    })
    controller = P2Controller(
        transport, recorder=recorder, session_id=recorder.session_id,
        default_timeout_s=max(2.0, 3 * setup["signal"]["frame_period_s"] + 1.0),
    )
    _configure_controller(controller, params)
    records_path = output / "recovery_records.jsonl"
    if include_host_timeout:
        kinds = (["stop"] * 8 + ["diag_abort_dma_reset"] * 8 + ["pl_watchdog"] * 8 + ["host_timeout"] * 8)
    else:
        kinds = (["stop"] * 8 + ["diag_abort_dma_reset"] * 8 + ["pl_watchdog"] * 8)
    kinds = kinds[:count]
    if len(kinds) < count:
        kinds.extend(["stop"] * (count - len(kinds)))
    completed = 0
    try:
        for index, kind in enumerate(kinds):
            started = _utc()
            record: dict[str, Any] = {
                "index": index, "kind": kind, "started_utc": started, "passed": False,
            }
            try:
                controller.mmio.write(REG_TIMEOUT_CYCLES, params["watchdog_timeout_cycles"] if kind == "pl_watchdog" else params["normal_timeout_cycles"])
                handle = controller.capture_single()
                record["request_id"] = handle.request_id
                record["epoch"] = handle.epoch
                before = _wait_active_evidence(transport, handle)
                record["before_cancel"] = before
                if not before.get("active_verified"):
                    raise RuntimeError("fault injection was not performed because request was not observably active")
                recovery_evidence: dict[str, Any] | None = None
                if kind == "stop":
                    controller.stop()
                elif kind == "diag_abort_dma_reset":
                    transport._diag_mmio.write(DIAG_OFFSETS["abort"], 1)
                    record["abort_write"] = {"offset": DIAG_OFFSETS["abort"], "value": 1}
                    transport.reset(reason="diag_abort_dma_reset")
                    # Preserve the evidence from the in-flight recovery.  The
                    # controller cleanup below is intentionally idempotent
                    # and would otherwise overwrite it with an idle STOP.
                    recovery_evidence = dict(getattr(transport, "last_recovery", {}))
                    controller.stop()
                elif kind == "pl_watchdog":
                    deadline = time.monotonic() + 0.75
                    timeout_seen = False
                    while time.monotonic() < deadline:
                        evidence = _cancel_evidence(transport)
                        if evidence["timeout_bit"]:
                            timeout_seen = True
                            record["watchdog_evidence"] = evidence
                            break
                        time.sleep(0.001)
                    if not timeout_seen:
                        raise RuntimeError("PL watchdog did not publish DIAG_ERROR bit0 before deadline")
                    transport.reset(reason="pl_watchdog")
                    recovery_evidence = dict(getattr(transport, "last_recovery", {}))
                    controller.stop()
                elif kind == "host_timeout":
                    # This is intentionally shorter than one camera frame and
                    # much shorter than the PL watchdog.  It must not be
                    # reported as a PL timeout.
                    try:
                        controller.recv_profile(timeout_s=0.001)
                    except CaptureTimeout as exc:
                        record["host_timeout_error"] = str(exc)
                    else:
                        raise RuntimeError("host timeout injection unexpectedly completed")
                    record["host_timeout_evidence"] = _cancel_evidence(transport)
                else:  # pragma: no cover - kinds are constructed above
                    raise ValueError(f"unknown recovery kind {kind}")
                record["after_cancel"] = _cancel_evidence(transport)
                recovery = recovery_evidence or dict(getattr(transport, "last_recovery", {}))
                record["transport_recovery"] = recovery
                record["buffers_released"] = (
                    getattr(transport, "_raw_buffer", None) is None
                    and getattr(transport, "_profile_buffer", None) is None
                    and getattr(getattr(transport, "raw_dma", None), "transfer_state", None) is None
                    and getattr(getattr(transport, "profile_dma", None), "transfer_state", None) is None
                )
                if not record["buffers_released"]:
                    raise RuntimeError("DMA ownership was not released after bounded recovery")
                # Preserve after_cancel and the original recovery evidence
                # above before clearing sticky ERROR_FLAGS bit17/18. CLEAR is
                # intentionally separate from STOP and only legal when idle.
                record["error_clear"] = clear_recovery_errors(controller, transport)
                # A PL-watchdog trial deliberately programs a 5 ms timeout,
                # shorter than one camera frame.  Restore the ordinary,
                # measured timeout before the post-recovery capture; leaving
                # the injected value in place would make the proof capture
                # fail for the same reason that was just tested.
                controller.mmio.write(REG_TIMEOUT_CYCLES, params["normal_timeout_cycles"])
                if controller.mmio.read(REG_TIMEOUT_CYCLES) != params["normal_timeout_cycles"]:
                    raise RuntimeError("normal PL timeout was not restored before post-recovery capture")
                record["post_recovery"] = _recovery_post(controller, transport, handle, params, recorder)
                record["passed"] = True
                completed += 1
                _append_jsonl(records_path, {**record, "finished_utc": _utc()})
                _status(output, stage="recovery", requested=count, completed=completed, passed=False, last_kind=kind)
            except Exception as exc:
                failure = {**record, "finished_utc": _utc(), "passed": False,
                           "error_type": type(exc).__name__, "error": str(exc),
                           "failure_snapshot": _control_snapshot(transport),
                           "transport_recovery": getattr(transport, "last_recovery", {})}
                _append_jsonl(records_path, failure)
                _status(output, stage="recovery", requested=count, completed=completed, passed=False, failure=failure)
                raise
        result = {
            "schema": "p2_closeout_recovery_v1", "stage": "recovery",
            "count_requested": count, "count_completed": completed,
            "kind_counts": {kind: kinds.count(kind) for kind in sorted(set(kinds))},
            "passed": completed == count, "parameters": params,
            "include_host_timeout": include_host_timeout, "no_hidden_retry": True,
        }
        _write_json(output / "summary.json", result)
        return result
    finally:
        controller.close()
        recorder.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("raw", "profile", "recovery"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=None)
    parser.add_argument("--include-host-timeout", action="store_true")
    parser.add_argument("--lock", type=Path, default=Path("/tmp/sl_camera_preview.lock"))
    args = parser.parse_args(argv)
    count = args.count if args.count is not None else (DEFAULT_RECOVERY_COUNT if args.stage == "recovery" else 1000)
    if count <= 0 or (args.stage in {"raw", "profile"} and count > 1000):
        parser.error("count must be positive and raw/profile count must not exceed 1000")
    if args.stage == "recovery" and count < 20:
        parser.error("recovery count must be at least 20")
    config_path = args.config.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    run_id = args.stage + '_' + config_path.parent.name.removeprefix('sl_p2_closeout_')
    started = _utc()
    _write_json(output / "run_context.json", {
        "schema": "p2_closeout_run_v1", "run_id": run_id, "stage": args.stage,
        "started_utc": started, "config": str(config_path), "count_requested": count,
        "no_automatic_retry": True, "hardware": "real_pynq_z2",
    })
    try:
        config = _read_config(config_path)
        with board_owner_lock(args.lock):
            if args.stage == "raw":
                result = run_raw_stage(config, config_path, output, count)
            elif args.stage == "profile":
                result = run_profile_stage(config, config_path, output, count)
            else:
                result = run_recovery_stage(config, config_path, output, count,
                                            include_host_timeout=args.include_host_timeout)
        result["run_id"] = run_id
        result["completed_utc"] = _utc()
        _write_json(output / "summary.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("passed") else 1
    except Exception as exc:
        result = {
            "schema": "p2_closeout_failure_v1", "run_id": run_id, "stage": args.stage,
            "count_requested": count, "count_completed": 0,
            "passed": False, "no_automatic_retry": True,
            "error_type": type(exc).__name__, "error": str(exc), "completed_utc": _utc(),
        }
        _write_json(output / "summary.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

