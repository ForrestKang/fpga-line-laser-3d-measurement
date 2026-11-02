"""Bounded one-revolution motion with foreground BSG1 refresh and live preview."""

from __future__ import annotations

from concurrent.futures import Future
import math
import re
import threading
import time
from typing import Any, Callable, Mapping

from structured_light.motion.orchestrator import (
    MotionCaptureSessionError,
    MotionError,
    MotionTimeout,
)
from structured_light.motion.types import MotionCaptureResult, MotionPhase


ROTATION_POSITIONS = 1
ROTATION_PULSES = 3200
ROTATION_RPM = 5.0
MOTOR_BUDGET_S = 18.0
RAW_CAPTURE_BUDGET_S = 2.0
FRAME_STEP_BUDGET_S = 6.0
SHUTDOWN_BUDGET_S = 2.0
TOTAL_BUDGET_S = MOTOR_BUDGET_S + FRAME_STEP_BUDGET_S + SHUTDOWN_BUDGET_S
BSG_GUARD_LEASE_CYCLES = 10_000_000  # 100 ms at the contracted 100 MHz clock.
BSG_GUARD_TIMEOUT_MS = 120_000
MAX_OWNER_POLL_S = 0.010
TARGET_REFRESH_PERIOD_S = 0.005
MAX_GUARD_REFRESH_GAP_S = BSG_GUARD_LEASE_CYCLES / 100_000_000.0 / 3.0
FRAME_WIDTH = 752
FRAME_HEIGHT = 480


class RotatingPreviewError(RuntimeError):
    """A bounded rotation/preview task did not meet its contract."""


def _failure_result(message: str, *, error_type: str = "RotatingPreviewError",
                    state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result = dict(state or {})
    result.update({
        "schema": "rotating_preview_v1",
        "status": "failed",
        "passed": False,
        "error_type": error_type,
        "error": message,
        "angle_calibrated": False,
        "measurement_claim": "none",
    })
    return result


def _validate_configuration(session: Any) -> None:
    config = getattr(session, "config", None)
    if config is None:
        raise RotatingPreviewError("rotating preview requires the existing MotionSession config")
    if (getattr(config, "positions", None) != ROTATION_POSITIONS
            or getattr(config, "pulses_per_position", None) != ROTATION_PULSES
            or float(getattr(config, "rpm", float("nan"))) != ROTATION_RPM):
        raise RotatingPreviewError("rotating preview requires positions=1, pulses=3200, rpm=5")
    if getattr(config, "guard_lease_cycles", None) != BSG_GUARD_LEASE_CYCLES:
        raise RotatingPreviewError("rotating preview requires the existing 100 ms BSG1 lease")
    if getattr(config, "guard_timeout_ms", None) != BSG_GUARD_TIMEOUT_MS:
        raise RotatingPreviewError("rotating preview preserves the existing 120 s BSG1 timeout")
    bounds = (
        ("motor_timeout_s", MOTOR_BUDGET_S),
        ("capture_timeout_s", RAW_CAPTURE_BUDGET_S),
        ("capture_phase_timeout_s", FRAME_STEP_BUDGET_S),
        ("cleanup_timeout_s", SHUTDOWN_BUDGET_S),
    )
    for name, maximum in bounds:
        value = getattr(config, name, None)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0 or value > maximum):
            raise RotatingPreviewError(f"{name} must be in (0, {maximum:g}] seconds")


def _validate_frame_step_result(value: object, *, index: int,
                                previous_request_id: int | None,
                                previous_epoch: int | None,
                                previous_session_id: str | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RotatingPreviewError("frame_step must return a mapping for the same-frame capture/output")
    request_id = value.get("request_id")
    epoch = value.get("epoch")
    digest = value.get("raw_sha256")
    if type(request_id) is not int or request_id < 0 or type(epoch) is not int or epoch < 0:
        raise RotatingPreviewError("frame_step request_id/epoch is malformed")
    if previous_request_id is not None and request_id <= previous_request_id:
        raise RotatingPreviewError("frame_step camera request_id did not increase")
    if previous_epoch is not None and epoch != previous_epoch:
        raise RotatingPreviewError("frame_step camera epoch changed during the rotation")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
        raise RotatingPreviewError("frame_step raw_sha256 must be a full 64-character digest")
    valid_rows = value.get("valid_rows")
    if type(valid_rows) is int and valid_rows >= 0:
        valid_row_count = valid_rows
        valid_row_indices = None
    elif isinstance(valid_rows, (list, tuple)) and all(
        type(row) is int and 0 <= row < FRAME_HEIGHT for row in valid_rows
    ):
        valid_row_indices = list(valid_rows)
        if valid_row_indices != sorted(set(valid_row_indices)):
            raise RotatingPreviewError("frame_step valid row indices must be unique and ordered")
        valid_row_count = len(valid_row_indices)
    else:
        raise RotatingPreviewError("frame_step valid_rows must be a non-negative count or ordered row list")
    session_id = value.get("session_id")
    if session_id is not None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise RotatingPreviewError("frame_step session_id is malformed")
        if previous_session_id is not None and session_id != previous_session_id:
            raise RotatingPreviewError("frame_step session_id changed during the rotation")
    render_receipt = value.get("render_receipt")
    if not isinstance(render_receipt, Mapping):
        raise RotatingPreviewError("frame_step must return the HDMI receipt for its captured frame")
    rendered_request_id = render_receipt.get("request_id")
    rendered_epoch = render_receipt.get("camera_epoch")
    if rendered_request_id != request_id or rendered_epoch != epoch:
        raise RotatingPreviewError("frame_step HDMI receipt does not match the captured frame identity")
    if (render_receipt.get("display_deferred") is True
            or render_receipt.get("same_frame_overlay") is not True
            or render_receipt.get("reference_compared") is not False
            or render_receipt.get("rendered") is not True
            or render_receipt.get("error") is not None):
        raise RotatingPreviewError("frame_step did not return a live same-frame overlay receipt")
    if value.get("angle_calibrated", False) is not False:
        raise RotatingPreviewError("rotating preview must not claim calibrated angle")
    result = {
        "frame_index": index,
        "request_id": request_id,
        "epoch": epoch,
        "raw_sha256": digest.lower(),
        "valid_row_count": valid_row_count,
        "render_receipt": dict(render_receipt),
        "angle_calibrated": False,
    }
    if valid_row_indices is not None:
        result["valid_rows"] = valid_row_indices
    if session_id is not None:
        result["session_id"] = session_id
    persistence_path = value.get("persistence_path")
    if persistence_path is not None:
        result["persistence_path"] = str(persistence_path)
    return result


def _find_worker(session: Any, future: Future[Any]) -> Any | None:
    for record in getattr(session, "_workers", ()):
        if getattr(record, "future", None) is future:
            return record
    return None


def _join_finished_worker(session: Any, future: Future[Any], *, deadline: float,
                          monotonic: Callable[[], float]) -> None:
    record = _find_worker(session, future)
    if record is None:
        raise RotatingPreviewError("MotionSession lost the worker record for a completed operation")
    while record.alive:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise MotionTimeout("completed rotating-preview worker did not exit before its bound")
        record.thread.join(min(0.001, remaining))


def run_rotating_preview(session: Any, frame_step: Callable[[], Mapping[str, Any]],
                         stop_event: threading.Event | None = None) -> dict[str, Any]:
    """Run one 3200-pulse, 5-RPM rotation while foreground-refreshing BSG1.

    ``frame_step`` is called in one ``MotionSession._submit`` worker at a time.
    It must return a mapping with ``request_id``, ``epoch``, ``raw_sha256``,
    ``valid_rows``, and a same-frame ``render_receipt``. The receipt identifies
    the frame and reports the live overlay without reference-image comparison.
    The callback owns frame persistence. Frame timing and overlap are measured
    at host monotonic call boundaries; they are not AXI or STEP-edge times.
    """

    receipt: dict[str, Any] = {
        "schema": "rotating_preview_v1",
        "status": "starting",
        "passed": False,
        "angle_calibrated": False,
        "measurement_claim": "none",
        "configuration": {
            "positions": ROTATION_POSITIONS,
            "pulses_per_position": ROTATION_PULSES,
            "rpm": ROTATION_RPM,
            "motor_budget_s": MOTOR_BUDGET_S,
            "raw_capture_budget_s": RAW_CAPTURE_BUDGET_S,
            "frame_step_budget_s": FRAME_STEP_BUDGET_S,
            "shutdown_budget_s": SHUTDOWN_BUDGET_S,
            "overall_budget_s": TOTAL_BUDGET_S,
            "guard_lease_cycles": BSG_GUARD_LEASE_CYCLES,
            "guard_timeout_ms": BSG_GUARD_TIMEOUT_MS,
            "target_guard_refresh_period_s": TARGET_REFRESH_PERIOD_S,
            "max_guard_refresh_gap_s": MAX_GUARD_REFRESH_GAP_S,
            "owner_poll_max_s": MAX_OWNER_POLL_S,
        },
        "motion": {"start_count": 0, "requested_pulses": ROTATION_PULSES,
                   "emitted_pulses": 0, "restart_count": 0},
        "guard": {"arm_count": 0, "refresh_count": 0, "active_after_revoke": None},
        "preview": {"frame_count": 0, "frames_completed_during_motor_worker": 0,
                    "frames": [], "overlap_verified": False},
        "timings": {},
        "task_receipt": None,
        "cleanup": None,
    }
    try:
        _validate_configuration(session)
        if not callable(frame_step):
            raise RotatingPreviewError("frame_step must be callable")
        if stop_event is not None and not callable(getattr(stop_event, "is_set", None)):
            raise RotatingPreviewError("stop_event must provide is_set()")
    except BaseException as exc:
        return _failure_result(str(exc), error_type=type(exc).__name__, state=receipt)

    monotonic = getattr(session, "_monotonic", None)
    sleep = getattr(session, "_sleep", None)
    if not callable(monotonic):
        monotonic = time.monotonic
    if not callable(sleep):
        sleep = time.sleep
    if (bool(stop_event and stop_event.is_set())
            or bool(getattr(session, "stop_requested", False))):
        return _failure_result("rotating preview was cancelled before owner admission",
                               error_type="MotionCancelled", state=receipt)

    owner = getattr(session, "_owner", None)
    if owner is None or not callable(getattr(owner, "acquire", None)) or not owner.acquire(blocking=False):
        return _failure_result("another task already owns Stage B",
                               error_type="MotionBusyError", state=receipt)

    failure: MotionError | None = None
    motor_future: Future[Any] | None = None
    frame_future: Future[Any] | None = None
    motor_result: dict[str, Any] | None = None
    motor_timing: dict[str, float] = {}
    frames: list[dict[str, Any]] = []
    previous_request_id: int | None = None
    previous_epoch: int | None = None
    previous_session_id: str | None = None
    owner_acquired = True
    lifecycle_started = False
    hardware_lock = getattr(session, "hardware_lock", None)
    task_started = monotonic()
    motor_submitted: float | None = None
    motor_deadline = task_started + min(MOTOR_BUDGET_S, float(session.config.motor_timeout_s))
    task_deadline = task_started + TOTAL_BUDGET_S
    capture_deadline: float | None = None
    motor_done = False
    guard_revoke_status: Mapping[str, Any] | None = None
    motor_request_id: int | None = None
    guard_request_id: int | None = None

    def stopped() -> bool:
        return bool((stop_event is not None and stop_event.is_set())
                    or getattr(session, "stop_requested", False))

    def mark_failure(exc: BaseException, phase: MotionPhase | None = None) -> None:
        nonlocal failure
        if isinstance(exc, MotionError):
            failure = exc
        else:
            failure = MotionCaptureSessionError(str(exc), phase=phase or session.phase)
            failure.__cause__ = exc
        session.last_error = failure

    def start_frame() -> None:
        nonlocal frame_future, capture_deadline
        if motor_done:
            return
        session._set_phase(MotionPhase.CAPTURING)
        session._active_stage = MotionPhase.CAPTURING

        def capture_and_render() -> dict[str, Any]:
            started = monotonic()
            try:
                value = frame_step()
            finally:
                completed = monotonic()
            if not isinstance(value, Mapping):
                raise RotatingPreviewError("frame_step must return a mapping")
            return {"value": dict(value), "started_monotonic_s": started,
                    "completed_monotonic_s": completed}

        frame_future = session._submit(capture_and_render)
        capture_deadline = monotonic() + min(
            FRAME_STEP_BUDGET_S, float(session.config.capture_phase_timeout_s)
        )

    try:
        if getattr(session, "_closed", False):
            raise RotatingPreviewError("MotionSession is closed")
        if getattr(session, "_poisoned", False):
            raise RotatingPreviewError("MotionSession is poisoned")
        gate = getattr(session, "_gate", None)
        if gate is None or getattr(gate, "active", None) is not False:
            raise RotatingPreviewError("rotating preview requires a quiescent inactive software gate")
        if hardware_lock is not None:
            was_acquired = bool(getattr(hardware_lock, "acquired", False))
            hardware_lock.acquire()
            session._hardware_lock_owned = not was_acquired
        session._gate.begin()
        lifecycle_started = True
        session._abort_attempted = False
        session._task_position_count = ROTATION_POSITIONS
        session._task_started_monotonic_s = task_started
        session._task_deadline = task_deadline
        session.last_task_receipt = None
        session._last_settle_completed_monotonic_s = None
        session._motion_start_trace = []
        session._total_emitted_pulses = 0
        session._guard_refresh_metrics = None
        session._stop_requested_for_task = False
        session._set_phase(MotionPhase.READY)
        if stopped():
            raise MotionError("rotating preview was cancelled before ARM", phase=MotionPhase.READY)

        guard_status = session.guard.validate_identity()
        motor_status = session.motor.validate_identity()
        session._seed_request_ids(guard_status, motor_status)
        guard_request_id = session._next_id("task")
        motor_request_id = session._next_id("motion")
        session._guard_request_id = guard_request_id
        session._set_phase(MotionPhase.READY)
        session._guard_held = True
        session.guard.arm(
            request_id=guard_request_id,
            lease_cycles=BSG_GUARD_LEASE_CYCLES,
            timeout_ms=BSG_GUARD_TIMEOUT_MS,
        )
        receipt["guard"]["arm_count"] = 1
        session._motion_start_trace.append({
            "event": "motor_start_submitted",
            "command": "START",
            "position_index": 0,
            "request_id": int(motor_request_id),
            "pulses": ROTATION_PULSES,
            "rpm": ROTATION_RPM,
            "owner_submitted_monotonic_s": monotonic(),
            "source": "MotionSession owner submission; hardware START is verified by motor receipt",
        })
        session._set_phase(MotionPhase.MOVING)
        session._active_stage = MotionPhase.MOVING

        def move_once() -> dict[str, Any]:
            started = monotonic()
            motor_timing["worker_started_monotonic_s"] = started
            try:
                result = session.motor.move_pulses(
                    ROTATION_PULSES,
                    session.config.period_cycles,
                    session.config.high_cycles,
                    session.config.dir_setup_cycles,
                    session.config.enable_setup_cycles,
                    session.config.motor_lease_cycles,
                    session.config.motor_timeout_cycles,
                    session.config.direction,
                    motor_request_id,
                    min(MOTOR_BUDGET_S, float(session.config.motor_timeout_s)),
                )
                return {"receipt": dict(result),
                        "worker_started_monotonic_s": started}
            finally:
                motor_timing["worker_completed_monotonic_s"] = monotonic()

        motor_submitted = monotonic()
        motor_future = session._submit(move_once)
        start_frame()
        heartbeat_period = min(
            TARGET_REFRESH_PERIOD_S,
            float(session.config.host_poll_max_s),
            BSG_GUARD_LEASE_CYCLES / 100_000_000.0 / 3.0,
        )
        next_refresh = monotonic() + heartbeat_period
        preview_failed = False
        while True:
            now = monotonic()
            if stopped():
                session._stop_requested_for_task = True
                session.request_stop()
                try:
                    session._poll_stop_request(phase=session.phase)
                except BaseException as exc:
                    mark_failure(exc)
                preview_failed = True
                break

            if not motor_done and motor_future is not None and motor_future.done():
                try:
                    motor_result = motor_future.result()
                    motor_receipt = session._validate_motor_receipt(
                        motor_result["receipt"], motor_request_id, require_stopped_readback=True,
                    )
                    if int(motor_receipt.get("emitted", -1)) != ROTATION_PULSES:
                        raise MotionCaptureSessionError("MFP1 emitted pulse count differs from 3200", phase=MotionPhase.MOVING)
                    session._total_emitted_pulses = int(motor_receipt["emitted"])
                    session._motion_start_trace[0].update({
                        "accepted_id": int(motor_receipt.get("accepted_id", motor_request_id)),
                        "epoch": int(motor_receipt.get("epoch", 0)),
                        "emitted": int(motor_receipt["emitted"]),
                        "done": bool(motor_receipt["done"]),
                    })
                    motor_result["receipt"] = motor_receipt
                    motor_done = True
                    session._set_phase(MotionPhase.CAPTURING if frame_future is not None else MotionPhase.DISPLAYING)
                    revoke_errors: list[str] = []
                    session._revoke_guard(revoke_errors)
                    if revoke_errors:
                        raise MotionCaptureSessionError(
                            "BSG1 guard revocation failed after the finite motor rotation: " + "; ".join(revoke_errors),
                            phase=MotionPhase.MOVING,
                        )
                    guard_revoke_status = (session.last_cleanup or {}).get("guard_status")
                    if (not isinstance(guard_revoke_status, Mapping)
                            or guard_revoke_status.get("active") is not False
                            or guard_revoke_status.get("step_pin") is not False
                            or guard_revoke_status.get("enable_pin") is not True
                            or guard_revoke_status.get("fault") is not False
                            or guard_revoke_status.get("error") != 0):
                        raise MotionCaptureSessionError("BSG1 did not return a healthy revoked state", phase=MotionPhase.MOVING)
                    _join_finished_worker(
                        session, motor_future,
                        deadline=min(task_deadline, now + SHUTDOWN_BUDGET_S),
                        monotonic=monotonic,
                    )
                    motor_timing["owner_observed_done_monotonic_s"] = monotonic()
                    receipt["guard"].update({
                        "active_after_revoke": False,
                        "status_after_revoke": dict(guard_revoke_status),
                    })
                    receipt["motion"].update({
                        "start_count": 1,
                        "request_id": motor_request_id,
                        "emitted_pulses": ROTATION_PULSES,
                        "done": True,
                        "receipt": dict(motor_receipt),
                    })
                except BaseException as exc:
                    mark_failure(exc, MotionPhase.MOVING)
                    preview_failed = True
                    break

            if frame_future is not None and frame_future.done():
                current_future = frame_future
                current_deadline = capture_deadline or (now + FRAME_STEP_BUDGET_S)
                try:
                    completed = current_future.result()
                    _join_finished_worker(session, current_future,
                                          deadline=min(task_deadline, current_deadline + SHUTDOWN_BUDGET_S),
                                          monotonic=monotonic)
                    normalized = _validate_frame_step_result(
                        completed["value"], index=len(frames),
                        previous_request_id=previous_request_id,
                        previous_epoch=previous_epoch,
                        previous_session_id=previous_session_id,
                    )
                    frame_started = float(completed["started_monotonic_s"])
                    frame_completed = float(completed["completed_monotonic_s"])
                    motor_started = motor_timing.get("worker_started_monotonic_s")
                    motor_completed = motor_timing.get("worker_completed_monotonic_s")
                    overlaps_motor_worker = (
                        motor_started is not None and frame_started <= frame_completed
                        and frame_completed >= motor_started
                        and (motor_completed is None or frame_completed <= motor_completed)
                    )
                    normalized.update({
                        "frame_step_result": dict(completed["value"]),
                        "frame_step_started_monotonic_s": frame_started,
                        "frame_step_completed_monotonic_s": frame_completed,
                        "frame_step_elapsed_s": max(0.0, frame_completed - frame_started),
                        "completed_during_motor_worker": overlaps_motor_worker,
                        "overlap_timing_basis": "host monotonic frame_step and move_pulses worker call boundaries; not AXI edge times",
                    })
                    frames.append(normalized)
                    previous_request_id = normalized["request_id"]
                    previous_epoch = normalized["epoch"]
                    if "session_id" in normalized:
                        previous_session_id = normalized["session_id"]
                    receipt["preview"]["frames"] = list(frames)
                    receipt["preview"]["frame_count"] = len(frames)
                    receipt["preview"]["frames_completed_during_motor_worker"] = sum(
                        bool(item["completed_during_motor_worker"]) for item in frames
                    )
                except BaseException as exc:
                    session._stop_requested_for_task = True
                    session.request_stop()
                    mark_failure(exc, MotionPhase.CAPTURING)
                    preview_failed = True
                    break
                frame_future = None
                capture_deadline = None
                if not motor_done:
                    start_frame()
                else:
                    session._set_phase(MotionPhase.DISPLAYING)
                    break

            now = monotonic()
            if not motor_done:
                if now >= motor_deadline:
                    session._stop_requested_for_task = True
                    session.request_stop()
                    mark_failure(MotionTimeout("rotating-preview motor exceeded its 18 s bound", phase=MotionPhase.MOVING))
                    preview_failed = True
                    break
                if frame_future is not None and capture_deadline is not None and now >= capture_deadline:
                    session._stop_requested_for_task = True
                    session.request_stop()
                    mark_failure(MotionTimeout("rotating-preview frame_step exceeded its 6 s bound", phase=MotionPhase.CAPTURING))
                    preview_failed = True
                    break
                if session._guard_held and now >= next_refresh:
                    refresh_started = monotonic()
                    session._refresh_guard()
                    metrics = session._guard_refresh_metrics
                    if monotonic() - refresh_started > MAX_OWNER_POLL_S:
                        raise MotionTimeout("foreground BSG1 refresh call exceeded the 10 ms owner bound", phase=session.phase)
                    if metrics.get("max_host_call_interval_s", 0.0) > MAX_GUARD_REFRESH_GAP_S:
                        raise MotionTimeout("foreground BSG1 refresh interval exceeded one third of the 100 ms lease", phase=session.phase)
                    next_refresh = refresh_started + heartbeat_period
                    receipt["guard"]["refresh_count"] = session._guard_refresh_metrics["successful_refreshes"]
                deadlines = [motor_deadline, next_refresh]
                if capture_deadline is not None:
                    deadlines.append(capture_deadline)
                next_deadline = min(deadlines)
                sleep(min(MAX_OWNER_POLL_S, max(0.0001, next_deadline - monotonic())))
                continue

            if frame_future is None:
                break
            if capture_deadline is not None and now >= capture_deadline:
                session._stop_requested_for_task = True
                session.request_stop()
                mark_failure(MotionTimeout("final rotating-preview frame_step exceeded its 6 s post-motor bound",
                                           phase=MotionPhase.CAPTURING))
                preview_failed = True
                break
            sleep(min(MAX_OWNER_POLL_S, max(0.0001, capture_deadline - now)))

        if failure is None and preview_failed:
            failure = MotionCaptureSessionError("rotating preview did not complete cleanly", phase=session.phase)
            session.last_error = failure
        overlap_count = sum(bool(item["completed_during_motor_worker"]) for item in frames)
        receipt["preview"].update({
            "frames": list(frames),
            "frame_count": len(frames),
            "frames_completed_during_motor_worker": overlap_count,
            "overlap_verified": overlap_count > 0,
        })
        if failure is None and not motor_done:
            failure = MotionCaptureSessionError("motor worker ended without validated DONE/3200 receipt",
                                                phase=MotionPhase.MOVING)
            session.last_error = failure
        if failure is None and overlap_count < 1:
            failure = MotionCaptureSessionError("no preview frame completed during the motor worker interval",
                                                phase=MotionPhase.DISPLAYING)
            session.last_error = failure
        if failure is not None:
            receipt["status"] = "failed"
        else:
            receipt["status"] = "complete"
            receipt["passed"] = True
    except BaseException as exc:
        if failure is None:
            if isinstance(exc, MotionError):
                failure = exc
            else:
                failure = MotionCaptureSessionError(str(exc), phase=getattr(session, "phase", MotionPhase.READY))
                failure.__cause__ = exc
            session.last_error = failure
        receipt["status"] = "failed"
        receipt["error_type"] = type(failure).__name__
        receipt["error"] = str(failure)
    finally:
        if owner_acquired:
            try:
                if lifecycle_started:
                    session._cleanup(failure=failure)
            except BaseException as cleanup_error:
                if failure is None:
                    failure = MotionCaptureSessionError(
                        f"rotating-preview cleanup failed: {cleanup_error}",
                        phase=getattr(session, "phase", MotionPhase.READY),
                    )
                    receipt["status"] = "failed"
                    receipt["passed"] = False
                receipt["cleanup_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
            receipt["cleanup"] = dict(getattr(session, "last_cleanup", None) or {})
            if getattr(session, "last_task_receipt", None) is None and getattr(session, "_task_started_monotonic_s", None) is not None:
                final_frames = frames[-1:] if failure is None and frames and motor_result is not None else []
                task_results = []
                if final_frames:
                    item = final_frames[0]
                    capture_value = item.get("frame_step_result", {})
                    task_results.append(MotionCaptureResult(
                        position_index=0,
                        guard_request_id=int(guard_request_id or 0),
                        motor_receipt=dict(motor_result["receipt"]),
                        capture_result=capture_value,
                        settle_elapsed_s=0.0,
                        camera_request_id=int(item["request_id"]),
                        camera_epoch=int(item["epoch"]),
                        phase_history=tuple(session.phase_history),
                        persistence_path=item.get("persistence_path"),
                        display_receipt=dict(item["render_receipt"]),
                        metadata={"rotating_preview": True, "angle_calibrated": False},
                    ))
                try:
                    session.last_task_receipt = session._publish_task_receipt(
                        results=task_results,
                        failure=failure,
                        cleanup_completed_monotonic_s=monotonic(),
                    )
                except BaseException as publish_error:
                    receipt["task_receipt_error"] = f"{type(publish_error).__name__}: {publish_error}"
            session._task_deadline = None
            session._active_future = None
            session._active_stage = None
            if getattr(session, "_hardware_lock_owned", False) and hardware_lock is not None:
                try:
                    hardware_lock.release()
                    session._hardware_lock_owned = False
                except BaseException as release_error:
                    receipt["hardware_lock_release_error"] = f"{type(release_error).__name__}: {release_error}"
                    receipt["status"] = "failed"
                    receipt["passed"] = False
            owner.release()

    finished = monotonic()
    receipt["timings"] = {
        "started_monotonic_s": task_started,
        "finished_monotonic_s": finished,
        "total_elapsed_s": max(0.0, finished - task_started),
        "motor_submitted_monotonic_s": motor_submitted,
        "motor_worker": dict(motor_timing),
        "guard_refresh": dict(getattr(session, "_guard_refresh_metrics", None) or {}),
    }
    receipt["motion"].update({
        "start_count": len(getattr(session, "_motion_start_trace", ())),
        "verified_done_count": 1 if motor_done else 0,
        "restart_count": max(0, len(getattr(session, "_motion_start_trace", ())) - 1),
        "requested_pulses": ROTATION_PULSES,
        "emitted_pulses": int(getattr(session, "_total_emitted_pulses", 0)),
        "motor_receipt": None if motor_result is None else dict(motor_result["receipt"]),
        "guard_request_id": guard_request_id,
        "request_id": motor_request_id,
    })
    receipt["guard"].update({
        "arm_count": len(getattr(session, "_motion_start_trace", ())) and 1 or receipt["guard"]["arm_count"],
        "refresh_count": int((getattr(session, "_guard_refresh_metrics", None) or {}).get("successful_refreshes", 0)),
        "active_after_revoke": bool(getattr(session, "_guard_held", False)),
        "last_revoke_status": None if guard_revoke_status is None else dict(guard_revoke_status),
    })
    receipt["task_receipt"] = getattr(session, "last_task_receipt", None)
    receipt["angle_calibrated"] = False
    receipt["measurement_claim"] = "none"
    if failure is not None:
        receipt.setdefault("error_type", type(failure).__name__)
        receipt.setdefault("error", str(failure))
        receipt["status"] = "failed"
        receipt["passed"] = False
    return receipt


__all__ = ["run_rotating_preview", "RotatingPreviewError"]
