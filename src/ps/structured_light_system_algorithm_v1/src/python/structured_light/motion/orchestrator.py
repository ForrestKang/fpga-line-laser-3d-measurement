"""Bounded Stage B motion/capture orchestration."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import asdict
import json
import os
import secrets
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from .guard import GuardController, GuardError
from .lifecycle import (
    GatedCaptureTransport,
    GatedMotorTransport,
    GenerationGate,
    GenerationRevoked,
    HardwareProcessLock,
    WorkerRecord,
)
from .settle import SettleConfig, SettleEstimate, estimate_settle, run_diagnostic_settle
from .types import CarrierMode, MotionCaptureResult, MotionConfig, MotionPhase, TaskReceipt


class MotionError(RuntimeError):
    """Base orchestration failure with phase and bounded-cleanup evidence."""

    def __init__(self, message: str, *, phase: MotionPhase | None = None) -> None:
        super().__init__(message)
        self.phase = phase
        self.cleanup_errors: list[str] = []
        self.diagnostics: dict[str, Any] = {}


class MotionBusyError(MotionError):
    pass


class MotionTimeout(MotionError, TimeoutError):
    pass


class MotionIdentityError(MotionError):
    pass


class MotionCaptureSessionError(MotionError):
    pass


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return str(value)


class MotionSession:
    """Own BSG1, MFP1 and P2 for one finite task.

    There is deliberately one owner lock and one bounded polling loop.  Worker
    futures run motor/capture calls, while the owner refreshes BSG1 only while
    the expected phase remains before its deadline.
    """

    def __init__(
        self,
        guard: GuardController,
        motor: Any,
        camera: Any,
        *,
        display: Any | None = None,
        output: str | Path | None = None,
        config: MotionConfig | None = None,
        settle_config: SettleConfig | None = None,
        settle_roi: tuple[int, int, int, int] | None = None,
        target: str = "pynq_z2",
        installation: str = "stage_b_candidate",
        carrier_mode: CarrierMode | str = CarrierMode.DIRECT_WIRE,
        profile: str = "SLP2",
        mode: str = "offline_mock",
        hardware_lock: HardwareProcessLock | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not isinstance(guard, GuardController):
            raise TypeError("guard must be a GuardController")
        self.guard, self.motor, self.camera, self.display = guard, motor, camera, display
        self.output = Path(output) if output is not None else None
        if self.output is not None:
            self.output.mkdir(parents=True, exist_ok=True)
        self.config = config or getattr(motor, "stage_b_default_config", None) or MotionConfig()
        self.settle_config = settle_config or SettleConfig()
        self.target = str(target)
        self.installation = str(installation)
        self.carrier_mode = carrier_mode.value if isinstance(carrier_mode, CarrierMode) else str(carrier_mode)
        self.profile = str(profile)
        self.mode = str(mode)
        self.hardware_lock = hardware_lock
        self._hardware_lock_owned = False
        width = int(getattr(camera, "frame_width", 0) or getattr(getattr(camera, "mmio", None), "frame_width", 0))
        height = int(getattr(camera, "frame_height", 0) or getattr(getattr(camera, "mmio", None), "frame_height", 0))
        if settle_roi is None:
            if width <= 0 or height <= 0:
                raise ValueError("settle_roi is required when camera dimensions are unavailable")
            settle_roi = (0, 0, width, height)
        self.settle_roi = tuple(int(item) for item in settle_roi)
        self.settle_config_hash = self.settle_config.bound_hash(
            target=self.target, installation=self.installation, roi=self.settle_roi,
            mode=self.carrier_mode, profile=self.profile,
        )
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._owner = threading.Lock()
        self._closed = False
        self.phase = MotionPhase.READY
        self.phase_history: list[str] = [MotionPhase.READY.value]
        self.last_error: MotionError | None = None
        self._task_request_id: int | None = None
        self._motion_request_id: int | None = None
        self._camera_request_ids: set[int] = set()
        self._active_future: Future[Any] | None = None
        self._active_stage: MotionPhase | None = None
        self._guard_request_id: int | None = None
        self._guard_held = False
        self._task_deadline: float | None = None
        self._task_started_monotonic_s: float | None = None
        self._task_position_count: int | None = None
        self._last_settle_completed_monotonic_s: float | None = None
        self._motion_start_trace: list[dict[str, Any]] = []
        self._total_emitted_pulses = 0
        self._stop_event = threading.Event()
        self._stop_requested_for_task = False
        self._gate = GenerationGate()
        self._workers: list[WorkerRecord] = []
        self._poisoned = False
        self._unconfirmed_workers = False
        self._abort_attempted = False
        self.last_cleanup: dict[str, Any] | None = None
        self.last_task_receipt: dict[str, Any] | None = None
        self._motor_transport_original: Any | None = None
        self._camera_transport_original: Any | None = None
        self._install_generation_adapters()

    def _install_generation_adapters(self) -> None:
        motor_transport = getattr(self.motor, "_transport", None)
        if motor_transport is not None and callable(getattr(motor_transport, "read32", None)) and callable(getattr(motor_transport, "write32", None)):
            self._motor_transport_original = motor_transport
            self.motor._transport = GatedMotorTransport(motor_transport, self._gate)
        camera_transport = getattr(self.camera, "transport", None)
        if camera_transport is not None and callable(getattr(camera_transport, "start_capture", None)):
            self._camera_transport_original = camera_transport
            self.camera.transport = GatedCaptureTransport(camera_transport, self._gate)

    def _set_phase(self, phase: MotionPhase) -> None:
        self.phase = phase
        self.phase_history.append(phase.value)

    def _refresh_guard(self) -> int:
        """Measure owner call boundaries; these are not exact AXI pin times."""
        metrics = getattr(self, '_guard_refresh_metrics', None)
        if metrics is None:
            metrics = self._guard_refresh_metrics = {
                'attempts': 0, 'successful_refreshes': 0,
                'max_host_call_interval_s': 0.0, 'max_refresh_call_duration_s': 0.0,
                'measurement': 'host monotonic call boundaries, not AXI edge timestamps',
            }
        started = self._monotonic()
        previous = metrics.get('last_call_started_monotonic_s')
        interval = max(0.0, started-previous) if previous is not None else 0.0
        if interval >= metrics['max_host_call_interval_s']:
            metrics['max_host_call_interval_s'] = interval
            metrics['max_interval_phase'] = self.phase.value
        metrics['last_call_started_monotonic_s'] = started
        metrics['attempts'] += 1
        try:
            value = self.guard.refresh()
        except BaseException as exc:
            metrics['last_error'] = {'phase': self.phase.value, 'type': type(exc).__name__,
                'message': str(exc), 'host_call_interval_s': interval}
            raise
        else:
            metrics['successful_refreshes'] += 1
            return value
        finally:
            metrics['max_refresh_call_duration_s'] = max(
                metrics['max_refresh_call_duration_s'], max(0.0, self._monotonic()-started))

    def request_stop(self) -> None:
        """Request owner-side cancellation at the next bounded poll boundary.

        The caller only sets an event.  BSG1/MFP1/P2 writes remain in the
        owner thread, where the normal generation invalidation and cleanup
        ordering is preserved.
        """

        self._stop_event.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def _poll_stop_request(self, *, phase: MotionPhase | None = None) -> None:
        if not self._stop_event.is_set():
            return
        self._stop_requested_for_task = True
        self._stop_event.clear()
        # The owner invalidates worker generations before touching hardware.
        # This path is intentionally never called by request_stop() itself.
        self._abort_workers()
        raise MotionError("Stage B stop requested", phase=phase or self.phase)

    def _task_remaining_s(self, now: float | None = None) -> float | None:
        if self._task_deadline is None:
            return None
        current = self._monotonic() if now is None else float(now)
        return max(0.0, self._task_deadline - current)

    def _bounded_phase_timeout(self, timeout_s: float) -> float:
        remaining = self._task_remaining_s()
        if remaining is None:
            return float(timeout_s)
        return min(float(timeout_s), remaining)

    def _next_id(self, kind: str) -> int:
        if kind == "task":
            if self._task_request_id is None:
                self._task_request_id = self._seed_request_id(0xB000_0000, 0)
            self._task_request_id = (self._task_request_id + 1) & 0xFFFF_FFFF
            if self._task_request_id == 0:
                self._task_request_id = 1
            return self._task_request_id
        if self._motion_request_id is None:
            self._motion_request_id = self._seed_request_id(0x4D00_0000, 0)
        self._motion_request_id = (self._motion_request_id + 1) & 0xFFFF_FFFF
        if self._motion_request_id == 0:
            self._motion_request_id = 1
        return self._motion_request_id

    @staticmethod
    def _seed_request_id(prefix: int, accepted_id: int) -> int:
        candidate = (prefix | secrets.randbits(24)) & 0xFFFF_FFFF
        if candidate <= int(accepted_id):
            candidate = (int(accepted_id) + 1) & 0xFFFF_FFFF
            if candidate == 0:
                candidate = 1
        return candidate

    def _seed_request_ids(self, guard_status: Mapping[str, Any], motor_status: Mapping[str, Any]) -> None:
        self._task_request_id = self._seed_request_id(0xB000_0000, int(guard_status.get("accepted_id", 0)))
        self._motion_request_id = self._seed_request_id(0x4D00_0000, int(motor_status.get("accepted_id", 0)))

    def _submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        generation = self._gate.generation
        stage = self._active_stage.value if self._active_stage is not None else self.phase.value

        def worker() -> None:
            if not future.set_running_or_notify_cancel():
                return
            self._gate.bind_worker(generation)
            try:
                self._gate.check(generation)
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)
            finally:
                self._gate.unbind_worker()

        thread = threading.Thread(target=worker, name="stage-b-task", daemon=True)
        record = WorkerRecord(future=future, thread=thread, generation=generation, stage=stage)
        self._workers.append(record)
        thread.start()
        return future

    def _live_workers(self) -> list[WorkerRecord]:
        self._workers = [record for record in self._workers if record.alive or not record.future.done()]
        return [record for record in self._workers if record.alive]

    def _join_workers(self, deadline: float) -> list[WorkerRecord]:
        for record in list(self._workers):
            remaining = max(0.0, deadline - self._monotonic())
            if remaining <= 0:
                break
            record.thread.join(remaining)
        return self._live_workers()

    def _bounded_call(self, name: str, operation: Callable[[], Any], deadline: float, errors: list[str]) -> None:
        outcome: list[BaseException] = []

        def invoke() -> None:
            try:
                operation()
            except BaseException as exc:
                outcome.append(exc)

        thread = threading.Thread(target=invoke, name=f"stage-b-cleanup-{name}", daemon=True)
        thread.start()
        thread.join(max(0.0, deadline - self._monotonic()))
        if thread.is_alive():
            errors.append(f"{name}: cleanup call exceeded bounded deadline")
            self._poisoned = True
        elif outcome:
            errors.append(f"{name}: {type(outcome[0]).__name__}: {outcome[0]}")

    def _revoke_guard(self, errors: list[str]) -> None:
        if not self._guard_held:
            return
        try:
            status = self.guard.revoke(deadline_s=self.config.cleanup_timeout_s)
            self.last_cleanup = {"guard_status": status}
            if status.get("active"):
                errors.append("guard.revoke returned active=True")
                self._poisoned = True
        except Exception as exc:
            errors.append(f"guard.revoke: {type(exc).__name__}: {exc}")
            self._poisoned = True
        finally:
            self._guard_held = False

    def _abort_workers(self) -> list[str]:
        """Invalidate workers, revoke the permit, stop components, then join."""

        errors: list[str] = []
        self._abort_attempted = True
        gate_quiescent = self._gate.revoke(timeout_s=self.config.cleanup_timeout_s)
        if not gate_quiescent:
            errors.append("generation gate still has an in-flight operation at cleanup deadline")
            self._poisoned = True
        self._revoke_guard(errors)
        deadline = self._monotonic() + self.config.cleanup_timeout_s
        cleanup_generation: int | None = None
        if gate_quiescent:
            cleanup_generation = self._gate.begin()
        try:
            for name, component in (("motor.stop", self.motor), ("camera.stop", self.camera)):
                method = getattr(component, "stop", None) or getattr(component, "cancel", None)
                if callable(method):
                    self._bounded_call(name, method, deadline, errors)
        finally:
            if cleanup_generation is not None and not self._gate.revoke(timeout_s=self.config.cleanup_timeout_s):
                errors.append("cleanup generation still has an in-flight operation at cleanup deadline")
                self._poisoned = True
        live = self._join_workers(deadline)
        if live:
            self._unconfirmed_workers = True
            self._poisoned = True
            errors.append("unfinished worker remains after bounded cleanup")
        else:
            self._unconfirmed_workers = False
        if errors:
            self.last_cleanup = {**(self.last_cleanup or {}), "errors": list(errors), "worker_unconfirmed": bool(live)}
        return errors

    def _await_future(self, future: Future[Any], *, phase: MotionPhase, timeout_s: float) -> Any:
        self._active_future, self._active_stage = future, phase
        deadline = self._monotonic() + float(timeout_s)
        if self._task_deadline is not None:
            deadline = min(deadline, self._task_deadline)
        next_heartbeat = self._monotonic()
        heartbeat_period = min(self.config.host_poll_max_s, self.config.guard_lease_cycles / 100_000_000.0 / 3.0)
        try:
            while True:
                now = self._monotonic()
                if now >= deadline:
                    # Invalidate the generation before revoking the permit so
                    # a stale worker cannot issue a late START/write while the
                    # owner performs bounded safety cleanup.
                    self._abort_workers()
                    raise MotionTimeout(f"{phase.value} future exceeded its bounded deadline", phase=phase)
                self._poll_stop_request(phase=phase)
                if future.done():
                    return future.result()
                # Refresh only while the expected future is still in its
                # declared stage and before its deadline.  No daemon heartbeat
                # survives a task or deadline.
                if self._guard_held and phase in {
                    MotionPhase.MOVING, MotionPhase.SETTLING, MotionPhase.CAPTURING,
                    MotionPhase.SAVING, MotionPhase.DISPLAYING,
                } and now >= next_heartbeat:
                    self._refresh_guard()
                    next_heartbeat = now + heartbeat_period
                remaining = min(deadline - self._monotonic(), max(0.0, next_heartbeat - self._monotonic()))
                self._sleep(min(self.config.host_poll_max_s, max(0.0001, remaining)))
        finally:
            self._active_future, self._active_stage = None, None

    def _validate_motor_receipt(
        self,
        receipt: Mapping[str, Any],
        request_id: int,
        *,
        require_stopped_readback: bool = False,
    ) -> dict[str, Any]:
        if int(receipt.get("request_id", receipt.get("accepted_id", 0))) != request_id:
            raise MotionIdentityError("MFP1 receipt request_id does not match motion request", phase=MotionPhase.MOVING)
        if receipt.get("fault") or receipt.get("cancelled") or receipt.get("busy"):
            raise MotionCaptureSessionError("MFP1 did not finish in a clean idle DONE state", phase=MotionPhase.MOVING)
        if receipt.get("done") is not True or int(receipt.get("emitted", -1)) != self.config.pulses_per_position:
            raise MotionCaptureSessionError("MFP1 emitted count or DONE state mismatch", phase=MotionPhase.MOVING)
        if require_stopped_readback:
            required = {
                "request_id", "accepted_id", "epoch", "done", "busy",
                "fault", "cancelled", "emitted", "error",
            }
            missing = sorted(required.difference(receipt))
            if missing:
                raise MotionCaptureSessionError(
                    f"MFP1 receipt lacks final-stop evidence fields: {', '.join(missing)}",
                    phase=MotionPhase.MOVING,
                )
            if any(receipt[name] is not False for name in ("busy", "fault", "cancelled")):
                raise MotionCaptureSessionError(
                    "MFP1 terminal receipt is not clean DONE/idle state",
                    phase=MotionPhase.MOVING,
                )
        if "accepted_id" in receipt and int(receipt["accepted_id"]) != request_id:
            raise MotionIdentityError("MFP1 accepted_id does not match the motion request", phase=MotionPhase.MOVING)
        if require_stopped_readback:
            try:
                if int(receipt["epoch"]) < 0:
                    raise ValueError("negative epoch")
                if int(receipt["error"]) != 0:
                    raise ValueError("nonzero error")
            except (TypeError, ValueError) as exc:
                raise MotionCaptureSessionError(
                    "MFP1 terminal error or epoch evidence is invalid", phase=MotionPhase.MOVING,
                ) from exc
        result = dict(receipt)
        stopped = result.get(
            "cleanup_status",
            result.get("final_status", result.get("stop_readback")),
        )
        if stopped is None and all(name in result for name in ("busy", "step", "enable_n", "error")):
            stopped = result
        if stopped is None:
            if require_stopped_readback:
                raise MotionCaptureSessionError(
                    "MFP1 receipt has no final idle/disabled output readback",
                    phase=MotionPhase.MOVING,
                )
            return result
        if not isinstance(stopped, Mapping):
            raise MotionCaptureSessionError("MFP1 final output readback is malformed", phase=MotionPhase.MOVING)
        if stopped.get("busy") not in (False, 0):
            raise MotionCaptureSessionError("MFP1 final readback is not idle", phase=MotionPhase.MOVING)
        if stopped.get("step") not in (False, 0):
            raise MotionCaptureSessionError("MFP1 STEP is not low after DONE", phase=MotionPhase.MOVING)
        if stopped.get("enable_n") not in (True, 1):
            raise MotionCaptureSessionError("MFP1 EN is not high after DONE", phase=MotionPhase.MOVING)
        try:
            error = int(stopped.get("error", -1))
        except (TypeError, ValueError) as exc:
            raise MotionCaptureSessionError("MFP1 final error readback is malformed", phase=MotionPhase.MOVING) from exc
        if error != 0:
            raise MotionCaptureSessionError("MFP1 final error readback is nonzero", phase=MotionPhase.MOVING)
        for key in ("request_id", "accepted_id"):
            value = stopped.get(key)
            if value is not None and int(value) != request_id:
                raise MotionIdentityError("MFP1 final readback identity changed after DONE", phase=MotionPhase.MOVING)
        return result

    def _settle(
        self,
        position: int,
        *,
        settle_frames: Sequence[Any] | None,
        settle_sampler: Callable[[int], Sequence[Any]] | None,
    ) -> SettleEstimate:
        self._set_phase(MotionPhase.SETTLING)
        settle_started = self._monotonic()
        frames: Sequence[Any] | None = settle_frames
        if settle_sampler is not None:
            future = self._submit(settle_sampler, position)
            sampler_deadline = getattr(settle_sampler, "settle_deadline_s", None)
            explicit_settle_deadline = isinstance(sampler_deadline, (int, float)) and float(sampler_deadline) > 0
            if not explicit_settle_deadline:
                sampler_deadline = getattr(settle_sampler, "accepted_wait_s", None)
            settle_timeout = self.config.guard_timeout_ms / 1000.0
            if isinstance(sampler_deadline, (int, float)) and float(sampler_deadline) > 0:
                extra_dma_slack = 0.0 if explicit_settle_deadline else self.config.capture_timeout_s
                settle_timeout = min(
                    settle_timeout,
                    float(sampler_deadline) + extra_dma_slack + self.config.host_poll_max_s,
                )
            frames = self._await_future(
                future, phase=MotionPhase.SETTLING,
                timeout_s=settle_timeout,
            )
            cached = getattr(frames, "settle_estimate", None)
            if cached is not None:
                self._validate_cached_settle(frames, cached)
                return cached
        if frames is None:
            # Offline mock/replay can derive a repeated frame from the pinned
            # transport as a deterministic fixture.  Real mode must provide
            # measured samples and therefore fails closed here.
            if self.mode in {"offline_mock", "mock", "replay"}:
                source = getattr(getattr(self.camera, "transport", None), "frame", None)
                if source is None:
                    raise MotionCaptureSessionError("settle samples are required", phase=MotionPhase.SETTLING)
                frame = source if not callable(source) else source()
                frames = [getattr(frame, "pixels", frame)] * (self.settle_config.baseline_frames + 1)
            else:
                raise MotionCaptureSessionError("real mode requires measured settle samples", phase=MotionPhase.SETTLING)
        estimate = estimate_settle(
            frames, roi=self.settle_roi, config=self.settle_config,
            config_hash=self.settle_config_hash,
            elapsed_s=max(0.0, self._monotonic() - settle_started),
        )
        if not estimate.settled:
            raise MotionCaptureSessionError(f"settle rejected: {estimate.reason}", phase=MotionPhase.SETTLING)
        return estimate

    def _validate_cached_settle(self, samples: Any, estimate: Any) -> None:
        source_name = type(samples).__name__
        source_module = type(samples).__module__
        record_source = getattr(samples, "record_source", getattr(estimate, "record_source", None))
        known_source = source_name in {"CachedSettleSamples", "MeasuredSettleSamples"} and source_module.endswith("pynq_runtime")
        if not known_source and not isinstance(record_source, str):
            raise MotionCaptureSessionError(
                "settle estimate record source is not an accepted measured runtime sample",
                phase=MotionPhase.SETTLING,
            )
        if getattr(estimate, "settled", None) is not True:
            raise MotionCaptureSessionError("cached settle estimate is not settled", phase=MotionPhase.SETTLING)
        if getattr(estimate, "config_hash", None) != self.settle_config_hash:
            raise MotionCaptureSessionError("cached settle config_hash does not match this session", phase=MotionPhase.SETTLING)
        try:
            estimate_roi = tuple(int(item) for item in estimate.roi)
        except (AttributeError, TypeError, ValueError) as exc:
            raise MotionCaptureSessionError("cached settle estimate has no valid ROI", phase=MotionPhase.SETTLING) from exc
        if estimate_roi != self.settle_roi:
            raise MotionCaptureSessionError("cached settle estimate ROI does not match this session", phase=MotionPhase.SETTLING)

    def _capture(self, timings: dict[str, Any]) -> Any:
        self._set_phase(MotionPhase.CAPTURING)
        phase_started = self._monotonic()
        task_remaining = None
        if self._task_deadline is not None:
            task_remaining = max(0.0, self._task_deadline - phase_started)
        future_timeout = self.config.capture_phase_timeout_s
        if task_remaining is not None:
            future_timeout = min(future_timeout, task_remaining)
        capture_timing: dict[str, Any] = {
            "started_monotonic_s": phase_started,
            "dma_timeout_s": self.config.capture_timeout_s,
            "capture_phase_timeout_s": self.config.capture_phase_timeout_s,
            "effective_future_timeout_s": future_timeout,
            "task_deadline_remaining_s": task_remaining,
        }
        timings["capture"] = capture_timing
        if future_timeout <= 0:
            raise MotionTimeout("CAPTURING has no remaining Stage B task time", phase=MotionPhase.CAPTURING)
        future_deadline = phase_started + future_timeout
        future = self._submit(self.camera.capture_pair, self.config.capture_timeout_s)
        try:
            result = self._await_future(
                future,
                phase=MotionPhase.CAPTURING,
                timeout_s=max(0.0, future_deadline - self._monotonic()),
            )
        finally:
            capture_timing["future_elapsed_s"] = max(0.0, self._monotonic() - phase_started)
        self._set_phase(MotionPhase.COMPARING)
        comparison_started = self._monotonic()
        if self._task_deadline is not None and self._monotonic() >= self._task_deadline:
            raise MotionTimeout("COMPARING reached the bounded task deadline", phase=MotionPhase.COMPARING)
        if self._guard_held:
            self._refresh_guard()
        request_id = int(getattr(result, "request_id", 0))
        epoch = int(getattr(result, "epoch", -1))
        # P2's existing offline contract starts its epoch-local request stream
        # at zero.  Zero is valid for that camera domain; uniqueness across
        # motion/guard domains is the Stage B requirement.
        if request_id < 0 or request_id in self._camera_request_ids:
            raise MotionIdentityError("camera request_id is invalid or has already been used", phase=MotionPhase.COMPARING)
        if epoch < 0:
            raise MotionIdentityError("camera epoch is invalid", phase=MotionPhase.COMPARING)
        if getattr(result, "angle_raw", 0) != 0 or getattr(result, "angle_valid", False):
            raise MotionIdentityError("SLP2 angle is not calibrated in Stage B", phase=MotionPhase.COMPARING)
        comparison = getattr(result, "comparison", None)
        if comparison is None or getattr(comparison, "passed", None) is not True:
            raise MotionCaptureSessionError("P2 same-frame comparison failed", phase=MotionPhase.COMPARING)
        if self._guard_held:
            self._refresh_guard()
        self._camera_request_ids.add(request_id)
        completed = self._monotonic()
        capture_timing["comparison_elapsed_s"] = max(0.0, completed - comparison_started)
        capture_timing["elapsed_s"] = max(0.0, completed - phase_started)
        capture_timing["completed_monotonic_s"] = completed
        return result

    def _persist(
        self,
        position: int,
        result: Any,
        settle: SettleEstimate,
        timings: Mapping[str, Any],
        *,
        guard_request_id: int,
        motor_receipt: Mapping[str, Any],
    ) -> str | None:
        path: Path | None = None
        if self.output is not None:
            path = self.output / f"position_{position:04d}.json"
            if path.exists():
                raise OSError(f"refusing to overwrite existing Stage B result {path}")
            payload = {
                "schema_version": 1,
                "position_index": position,
                "guard_request_id": int(guard_request_id),
                "motor_request_id": int(motor_receipt.get("request_id", motor_receipt.get("accepted_id", 0))),
                "motor_receipt": dict(motor_receipt),
                "camera_request_id": int(result.request_id),
                "camera_epoch": int(result.epoch),
                "raw_sha256": getattr(result, "input_sha256", None),
                "angle_raw": 0,
                "angle_valid": False,
                "settle": settle.as_dict(),
                "timings": dict(timings),
                "comparison_passed": bool(getattr(getattr(result, "comparison", None), "passed", False)),
                "hardware_evidence": self.mode,
            }
            temporary = path.with_suffix(path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        # SessionRecorder has persisted the P2 payload before capture_pair
        # returns.  Consume the bounded P2 queue only after our own metadata is
        # durable, so eight-entry backpressure cannot silently accumulate.
        drain = getattr(self.camera, "drain_results", None)
        if callable(drain):
            drained = tuple(drain())
            if not any(item is result or getattr(item, "request_id", None) == getattr(result, "request_id", None) for item in drained):
                raise MotionCaptureSessionError("capture result was not present in persisted P2 queue", phase=MotionPhase.SAVING)
        return str(path) if path is not None else None

    def _finalize_persisted_record(
        self,
        path: str | Path,
        timings: Mapping[str, Any],
        display_receipt: Mapping[str, Any] | None,
    ) -> None:
        """Patch post-display timing/receipt fields without changing evidence identity."""

        target = Path(path)
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise MotionCaptureSessionError("persisted Stage B position record is not a JSON object", phase=MotionPhase.SAVING)
        payload["timings"] = dict(timings)
        payload["display_receipt"] = None if display_receipt is None else dict(display_receipt)
        temporary = target.with_suffix(target.suffix + ".final.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)

    def _display(self, result: Any) -> Mapping[str, Any] | None:
        if self.display is None:
            return None
        show_capture = getattr(self.display, "show_capture", None)
        if callable(show_capture):
            receipt = show_capture(result)
            return dict(receipt) if isinstance(receipt, Mapping) else {"receipt": receipt}
        show = getattr(self.display, "show", None)
        if not callable(show) or not bool(getattr(self.display, "accepts_gray8", False)):
            return {"display_deferred": True, "reason": "adapter requires show_capture(CaptureResult)"}
        receipt = show(bytes(getattr(result, "raw_bytes", b"")))
        return dict(receipt) if isinstance(receipt, Mapping) else {"receipt": receipt}

    def _cleanup(self, *, failure: MotionError | None) -> None:
        errors: list[str] = []
        live = self._live_workers()
        if live:
            if self._abort_attempted:
                self._unconfirmed_workers = True
                self._poisoned = True
                errors.append("unfinished worker remains after bounded cleanup")
            else:
                errors.extend(self._abort_workers())
        else:
            self._unconfirmed_workers = False
            gate_quiescent = True
            if failure is not None:
                gate_quiescent = self._gate.revoke(timeout_s=self.config.cleanup_timeout_s)
                if not gate_quiescent:
                    errors.append("generation gate still has an in-flight operation at cleanup deadline")
                    self._poisoned = True
            self._revoke_guard(errors)
            deadline = self._monotonic() + self.config.cleanup_timeout_s
            cleanup_generation: int | None = None
            if gate_quiescent and failure is not None:
                cleanup_generation = self._gate.begin()
            try:
                for name, component in (("motor.stop", self.motor), ("camera.stop", self.camera)):
                    method = getattr(component, "stop", None) or getattr(component, "cancel", None)
                    if callable(method):
                        self._bounded_call(name, method, deadline, errors)
            finally:
                if cleanup_generation is not None and not self._gate.revoke(timeout_s=self.config.cleanup_timeout_s):
                    errors.append("cleanup generation still has an in-flight operation at cleanup deadline")
                    self._poisoned = True
            self.last_cleanup = {**(self.last_cleanup or {}), "worker_unconfirmed": False}
        if failure is not None:
            failure.cleanup_errors.extend(errors)

    def _publish_task_receipt(
        self,
        *,
        results: Sequence[MotionCaptureResult],
        failure: MotionError | None,
        cleanup_completed_monotonic_s: float,
    ) -> dict[str, Any]:
        """Publish one task boundary after every worker and cleanup action.

        The end timestamp is sampled after ``_cleanup``.  In particular, the
        last settle completion is retained as its own boundary and is never
        reused as the task end.
        """

        task_end = self._monotonic()
        guard_status = (self.last_cleanup or {}).get("guard_status")
        guard_revoked = not self._guard_held
        if isinstance(guard_status, Mapping):
            guard_revoked = guard_revoked and not bool(guard_status.get("active", False))
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "task_request_id": int(self._guard_request_id or self._task_request_id or 0),
            "positions_requested": self._task_position_count,
            "positions_completed": len(results),
            "task_started_monotonic_s": self._task_started_monotonic_s,
            "task_deadline_monotonic_s": self._task_deadline,
            "last_settle_completed_monotonic_s": self._last_settle_completed_monotonic_s,
            "cleanup_completed_monotonic_s": cleanup_completed_monotonic_s,
            "task_end_monotonic_s": task_end,
            "task_deadline_remaining_s": (
                None if self._task_deadline is None else self._task_deadline - task_end
            ),
            "phase": self.phase.value,
            "phase_history": list(self.phase_history),
            "guard_revoked": guard_revoked,
            "guard_refresh_telemetry": dict(getattr(self, '_guard_refresh_metrics', None) or {}),
            "stop_requested": self._stop_requested_for_task,
            "worker_unconfirmed": bool(self._unconfirmed_workers),
            "cleanup": dict(self.last_cleanup or {}),
            "result_paths": [item.persistence_path for item in results],
            "path": None,
        }
        if failure is not None:
            receipt["failure"] = {
                "type": type(failure).__name__,
                "message": str(failure),
                "phase": failure.phase.value if failure.phase is not None else None,
                "cleanup_errors": list(failure.cleanup_errors),
            }
        else:
            receipt["failure"] = None

        receipt["mode"] = "single" if self._task_position_count == 1 else "sequence"
        receipt["motion_config"] = self._motion_configuration()
        receipt["motion_configuration"] = dict(receipt["motion_config"])
        receipt["motion_start_count"] = len(self._motion_start_trace)
        receipt["total_emitted_pulses"] = int(self._total_emitted_pulses)
        receipt["motion_start_trace"] = [dict(item) for item in self._motion_start_trace]

        if self.output is not None:
            path = self.output / "task_receipt.json"
            receipt["path"] = str(path)
            try:
                with path.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(receipt, indent=2, sort_keys=True, default=_json_default) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                receipt["path_existing"] = True
            except Exception as exc:
                receipt["receipt_write_error"] = f"{type(exc).__name__}: {exc}"
        self.last_task_receipt = receipt
        return receipt

    def _motion_configuration(self) -> dict[str, Any]:
        return {
            "positions": int(self._task_position_count or self.config.positions),
            "pulses_per_position": int(self.config.pulses_per_position),
            "rpm": float(self.config.rpm),
            "direction": int(self.config.direction),
            "period_cycles": int(self.config.period_cycles),
            "high_cycles": int(self.config.high_cycles),
            "dir_setup_cycles": int(self.config.dir_setup_cycles),
            "enable_setup_cycles": int(self.config.enable_setup_cycles),
            "motor_lease_cycles": int(self.config.motor_lease_cycles),
            "motor_timeout_cycles": int(self.config.motor_timeout_cycles),
            "motor_timeout_s": float(self.config.motor_timeout_s),
            "guard_lease_cycles": int(self.config.guard_lease_cycles),
            "guard_timeout_ms": int(self.config.guard_timeout_ms),
            "capture_timeout_s": float(self.config.capture_timeout_s),
            "capture_phase_timeout_s": float(self.config.capture_phase_timeout_s),
        }

    @property
    def task_receipt(self) -> Mapping[str, Any] | None:
        """Most recent task receipt, including the post-cleanup boundary."""

        return self.last_task_receipt

    def _run_position(
        self,
        position: int,
        *,
        settle_frames: Sequence[Any] | None,
        settle_sampler: Callable[[int], Sequence[Any]] | None,
    ) -> MotionCaptureResult:
        mark = getattr(self.guard.transport, "note_position", None)
        if callable(mark):
            mark()
        motion_request_id = self._next_id("motion")
        timings: dict[str, Any] = {}
        try:
            self._set_phase(MotionPhase.MOVING)
            motor_started = self._monotonic()
            start_trace = {
                "event": "motor_start_submitted",
                "command": "START",
                "position_index": int(position),
                "request_id": int(motion_request_id),
                "pulses": int(self.config.pulses_per_position),
                "rpm": float(self.config.rpm),
                "owner_submitted_monotonic_s": motor_started,
                "source": "MotionSession owner submission; hardware START readback is in motor_receipt",
            }
            self._motion_start_trace.append(start_trace)
            future = self._submit(
                self.motor.move_pulses,
                self.config.pulses_per_position,
                self.config.period_cycles,
                self.config.high_cycles,
                self.config.dir_setup_cycles,
                self.config.enable_setup_cycles,
                self.config.motor_lease_cycles,
                self.config.motor_timeout_cycles,
                self.config.direction,
                motion_request_id,
                self.config.motor_timeout_s,
            )
            receipt = self._validate_motor_receipt(
                self._await_future(future, phase=MotionPhase.MOVING, timeout_s=self.config.motor_timeout_s),
                motion_request_id,
                require_stopped_readback=callable(getattr(settle_sampler, "mark_motor_stopped", None)),
            )
            if int(self._guard_request_id or 0) == int(receipt.get("request_id", 0)):
                raise MotionIdentityError("BSG1 and MFP1 request IDs must use distinct domains", phase=MotionPhase.MOVING)
            self._total_emitted_pulses += int(receipt.get("emitted", 0))
            start_trace.update({
                "accepted_id": int(receipt.get("accepted_id", receipt.get("request_id", 0))),
                "epoch": int(receipt.get("epoch", 0)),
                "emitted": int(receipt.get("emitted", 0)),
                "done": bool(receipt.get("done", False)),
            })
            stop_confirmed = self._monotonic()
            timings["motor"] = {
                "started_monotonic_s": motor_started,
                "stop_confirmed_monotonic_s": stop_confirmed,
                "elapsed_s": max(0.0, stop_confirmed - motor_started),
            }
            timings["stop_confirmed_monotonic_s"] = stop_confirmed
            mark_stopped = getattr(settle_sampler, "mark_motor_stopped", None)
            if callable(mark_stopped):
                mark_stopped(position, monotonic_s=stop_confirmed, motor_receipt=receipt)
            settle_started = self._monotonic()
            settle = self._settle(position, settle_frames=settle_frames, settle_sampler=settle_sampler)
            settle_completed = self._monotonic()
            timings["settle"] = {
                "started_monotonic_s": settle_started,
                "completed_monotonic_s": settle_completed,
                "elapsed_s": max(0.0, settle_completed - settle_started),
                "accepted_elapsed_s": float(settle.elapsed_s),
            }
            self._last_settle_completed_monotonic_s = settle_completed
            result = self._capture(timings)
            if int(receipt.get("request_id", 0)) == int(result.request_id):
                raise MotionIdentityError("MFP1 and P2 request IDs must use distinct domains", phase=MotionPhase.COMPARING)
            result_metadata = getattr(result, "metadata", None)
            if isinstance(result_metadata, dict):
                result_metadata.setdefault("nominal_motor_steps", int(self.config.pulses_per_position))
                result_metadata.setdefault("nominal_motor_steps_calibrated", False)
                result_metadata.setdefault("nominal_motor_rpm", float(self.config.rpm))
                result_metadata.setdefault("motor_request_id", int(motion_request_id))
            persist_started = self._monotonic()
            self._set_phase(MotionPhase.SAVING)
            persist_future = self._submit(
                self._persist, position, result, settle, timings,
                guard_request_id=int(self._guard_request_id or 0), motor_receipt=receipt,
            )
            persist_timeout = self._bounded_phase_timeout(self.config.guard_timeout_ms / 1000.0)
            if persist_timeout <= 0:
                self._abort_workers()
                raise MotionTimeout("SAVING has no remaining Stage B task time", phase=MotionPhase.SAVING)
            path = self._await_future(
                persist_future, phase=MotionPhase.SAVING,
                timeout_s=persist_timeout,
            )
            persist_completed = self._monotonic()
            timings["saving"] = {
                "started_monotonic_s": persist_started,
                "completed_monotonic_s": persist_completed,
                "elapsed_s": max(0.0, persist_completed - persist_started),
                "effective_future_timeout_s": persist_timeout,
                "task_deadline_remaining_s": self._task_remaining_s(persist_started),
            }
            timings["saving_elapsed_s"] = max(0.0, persist_completed - persist_started)
            display_started = self._monotonic()
            self._set_phase(MotionPhase.DISPLAYING)
            display_future = self._submit(self._display, result)
            display_timeout = self._bounded_phase_timeout(self.config.guard_timeout_ms / 1000.0)
            if display_timeout <= 0:
                self._abort_workers()
                raise MotionTimeout("DISPLAYING has no remaining Stage B task time", phase=MotionPhase.DISPLAYING)
            display = self._await_future(
                display_future, phase=MotionPhase.DISPLAYING,
                timeout_s=display_timeout,
            )
            display_completed = self._monotonic()
            timings["display"] = {
                "started_monotonic_s": display_started,
                "completed_monotonic_s": display_completed,
                "elapsed_s": max(0.0, display_completed - display_started),
                "effective_future_timeout_s": display_timeout,
                "task_deadline_remaining_s": self._task_remaining_s(display_started),
            }
            timings["display_elapsed_s"] = max(0.0, display_completed - display_started)
            if path is not None:
                finalize_future = self._submit(
                    self._finalize_persisted_record, path, timings, display,
                )
                finalize_timeout = self._bounded_phase_timeout(self.config.guard_timeout_ms / 1000.0)
                if finalize_timeout <= 0:
                    self._abort_workers()
                    raise MotionTimeout("DISPLAYING record finalization has no remaining Stage B task time", phase=MotionPhase.DISPLAYING)
                self._await_future(
                    finalize_future, phase=MotionPhase.DISPLAYING,
                    timeout_s=finalize_timeout,
                )
            return MotionCaptureResult(
                position_index=position,
                guard_request_id=int(self._guard_request_id or 0),
                motor_receipt=receipt,
                capture_result=result,
                settle_elapsed_s=float(settle.elapsed_s),
                camera_request_id=int(result.request_id),
                camera_epoch=int(result.epoch),
                phase_history=tuple(self.phase_history),
                persistence_path=path,
                display_receipt=display,
                metadata={
                    "settle_config_hash": self.settle_config_hash,
                    "angle_calibrated": False,
                    "carrier_mode": self.carrier_mode,
                    "hardware_evidence": self.mode,
                    "timings": timings,
                },
            )
        except MotionError as exc:
            exc.diagnostics.setdefault("stage_timings", {}).update(timings)
            raise
        except Exception as exc:
            failure = MotionCaptureSessionError(str(exc), phase=self.phase)
            failure.diagnostics["stage_timings"] = dict(timings)
            raise failure from exc
        finally:
            finish = getattr(self.guard.transport, "finish_position", None)
            if callable(finish):
                finish()

    def run_sequence(
        self,
        positions: int | None = None,
        *,
        settle_frames: Sequence[Any] | None = None,
        settle_sampler: Callable[[int], Sequence[Any]] | None = None,
    ) -> tuple[MotionCaptureResult, ...]:
        """Run one finite task with one permit held over all positions."""

        count = self.config.positions if positions is None else int(positions)
        if count < 1:
            raise ValueError("positions must be positive and finite")
        if not self._owner.acquire(blocking=False):
            raise MotionBusyError("another task already owns Stage B")
        failure: MotionError | None = None
        results: list[MotionCaptureResult] = []
        self._task_position_count = count
        self._task_started_monotonic_s = None
        self._last_settle_completed_monotonic_s = None
        self._motion_start_trace = []
        self._total_emitted_pulses = 0
        self._guard_refresh_metrics = None
        self._stop_requested_for_task = False
        try:
            if self._closed:
                raise MotionError("motion session is closed")
            if self._poisoned:
                raise MotionError("motion session is poisoned after unfinished or unsafe cleanup")
            if self.hardware_lock is not None:
                was_acquired = self.hardware_lock.acquired
                self.hardware_lock.acquire()
                self._hardware_lock_owned = not was_acquired
            self._abort_attempted = False
            self._gate.begin()
            self._task_started_monotonic_s = self._monotonic()
            self._poll_stop_request(phase=MotionPhase.READY)
            guard_status = self.guard.validate_identity()
            validate_motor = getattr(self.motor, "validate_identity", None)
            if not callable(validate_motor):
                raise MotionIdentityError("MFP1 adapter lacks validate_identity", phase=MotionPhase.READY)
            motor_status = validate_motor()
            self._seed_request_ids(guard_status, motor_status)
            self._set_phase(MotionPhase.READY)
            self._guard_request_id = self._next_id("task")
            # The bounded task window starts at the owner admission boundary;
            # derive both values from one clock sample so the receipt records
            # the exact configured 120 s window rather than including identity
            # validation time twice.
            self._task_started_monotonic_s = self._monotonic()
            self._task_deadline = self._task_started_monotonic_s + self.config.guard_timeout_ms / 1000.0
            # Mark ownership before ARM so a fault during ARM still executes
            # the bounded revoke path in finally.
            self._guard_held = True
            self.guard.arm(
                request_id=self._guard_request_id,
                lease_cycles=self.config.guard_lease_cycles,
                timeout_ms=self.config.guard_timeout_ms,
            )
            for position in range(count):
                self._poll_stop_request(phase=self.phase)
                if self._task_deadline is not None and self._monotonic() >= self._task_deadline:
                    raise MotionTimeout("Stage B task exceeded its bounded 120 s guard deadline", phase=self.phase)
                results.append(self._run_position(position, settle_frames=settle_frames, settle_sampler=settle_sampler))
                self._poll_stop_request(phase=self.phase)
            # Keep the final observable phase as DISPLAYING while the permit is
            # revoked by the cleanup path below.
            return tuple(results)
        except MotionError as exc:
            failure = exc
            self.last_error = exc
            raise
        except Exception as exc:
            failure = MotionCaptureSessionError(str(exc), phase=self.phase)
            failure.__cause__ = exc
            self.last_error = failure
            raise failure from exc
        finally:
            try:
                self._cleanup(failure=failure)
            finally:
                if self._task_started_monotonic_s is not None:
                    cleanup_completed = self._monotonic()
                    self._publish_task_receipt(
                        results=results,
                        failure=failure,
                        cleanup_completed_monotonic_s=cleanup_completed,
                    )
                self._task_deadline = None
                self._owner.release()


    def run_single(
        self,
        *,
        position_index: int = 0,
        settle_frames: Sequence[Any] | None = None,
        settle_sampler: Callable[[int], Sequence[Any]] | None = None,
    ) -> MotionCaptureResult:
        values = self.run_sequence(positions=1, settle_frames=settle_frames, settle_sampler=settle_sampler)
        result = values[0]
        if position_index != 0:
            return MotionCaptureResult(
                position_index=position_index, guard_request_id=result.guard_request_id,
                motor_receipt=result.motor_receipt, capture_result=result.capture_result,
                settle_elapsed_s=result.settle_elapsed_s, camera_request_id=result.camera_request_id,
                camera_epoch=result.camera_epoch, phase_history=result.phase_history,
                persistence_path=result.persistence_path, display_receipt=result.display_receipt,
                metadata=result.metadata,
            )
        return result

    # Short aliases used by the board notebook/CLI.
    single = run_single
    sequence = run_sequence

    def run_static(self, *, raw: bool = False) -> Any:
        """Perform a camera-only P2 capture without BSG1 ARM or MFP1 motion.

        The camera transport is still run inside one generation context.  This
        matters because ``__init__`` installs ``GatedCaptureTransport`` even
        for static users; direct calls through ``self.camera`` while the gate
        is inactive would be rejected before the first real START.
        """

        if not self._owner.acquire(blocking=False):
            raise MotionBusyError("another task already owns Stage B")
        try:
            if self._closed or self._poisoned:
                raise MotionError("motion session is closed or poisoned")
            if self.hardware_lock is not None:
                was_acquired = self.hardware_lock.acquired
                self.hardware_lock.acquire()
                self._hardware_lock_owned = not was_acquired
            self._gate.begin()
            self._set_phase(MotionPhase.CAPTURING)
            result = self.camera.capture_raw(self.config.capture_timeout_s) if raw else self.camera.capture_pair(self.config.capture_timeout_s)
            if raw:
                self._set_phase(MotionPhase.SAVING)
                expected = int(getattr(self.camera, "frame_width", 0)) * int(getattr(self.camera, "frame_height", 0))
                if expected <= 0 or len(getattr(result, "pixels", b"")) != expected:
                    raise MotionCaptureSessionError("static raw capture returned an unexpected frame length", phase=self.phase)
                return result
            self._set_phase(MotionPhase.COMPARING)
            if getattr(result, "angle_raw", 0) != 0 or getattr(result, "angle_valid", False):
                raise MotionIdentityError("SLP2 angle is not calibrated in Stage B", phase=self.phase)
            comparison = getattr(result, "comparison", None)
            if comparison is None or getattr(comparison, "passed", None) is not True:
                raise MotionCaptureSessionError("P2 same-frame comparison failed", phase=self.phase)
            self._set_phase(MotionPhase.SAVING)
            drain = getattr(self.camera, "drain_results", None)
            if callable(drain):
                drained = tuple(drain())
                if not any(getattr(item, "request_id", None) == result.request_id for item in drained):
                    raise MotionCaptureSessionError("static capture was not drained after persistence", phase=self.phase)
            self._set_phase(MotionPhase.DISPLAYING)
            self._display(result)
            return result
        finally:
            self._gate.revoke(timeout_s=self.config.cleanup_timeout_s)
            if self._hardware_lock_owned and self.hardware_lock is not None:
                self.hardware_lock.release()
                self._hardware_lock_owned = False
            self._owner.release()

    def run_recovery(self) -> dict[str, Any]:
        """Delegate explicit recovery to existing P2 bounded APIs.

        Recovery is an owner operation too: invalidate any stale workers,
        admit one cleanup-only generation, then revoke it before returning.
        """

        if not self._owner.acquire(blocking=False):
            raise MotionBusyError("another task already owns Stage B")
        try:
            if self._live_workers():
                self._abort_workers()
            if self._live_workers():
                self._poisoned = True
                raise MotionError("recovery requires all prior workers to be confirmed stopped")
            if not self._gate.revoke(timeout_s=self.config.cleanup_timeout_s):
                self._poisoned = True
                raise MotionError("recovery requires the prior generation to be quiescent")
            generation = self._gate.begin()
            self._set_phase(MotionPhase.READY)
            try:
                result: dict[str, Any] = {
                    "stage": "recovery", "motion_started": False,
                    "generation": generation,
                }
                stop = getattr(self.camera, "stop", None)
                if callable(stop):
                    stop()
                clear = getattr(self.camera, "clear_errors", None)
                if callable(clear):
                    clear()
                    result["p2_clear"] = True
                else:
                    result["p2_clear"] = False
                return result
            finally:
                self._gate.revoke(timeout_s=self.config.cleanup_timeout_s)
        finally:
            self._owner.release()

    def run_settle_diagnostic(
        self,
        sampler: Callable[[int, int], Sequence[Any]],
        *,
        positions: int | None = None,
    ) -> tuple[dict[str, Any], ...]:
        return run_diagnostic_settle(
            sampler, positions=self.config.positions if positions is None else positions,
            monotonic=self._monotonic, sleep=self._sleep,
        )

    def close(self) -> dict[str, Any]:
        if not self._owner.acquire(blocking=False):
            raise MotionBusyError("cannot close while Stage B task is active")
        try:
            if getattr(self,'_external_worker_unconfirmed',False):
                return {'closed':False,'worker_unconfirmed':True,'poisoned':True,
                        'cleanup':{'reason':'external continuous worker has not joined; retain DMA memory and process ownership'}}
            if self._closed:
                return {"closed": True, "already_closed": True}
            self._cleanup(failure=None)
            if self._unconfirmed_workers or self._poisoned and (self.last_cleanup or {}).get("worker_unconfirmed"):
                return {
                    "closed": False,
                    "worker_unconfirmed": bool(self._unconfirmed_workers),
                    "poisoned": True,
                    "cleanup": self.last_cleanup,
                }
            # Component close methods may perform readback/DISARM through the
            # generation-gated transports (notably MFP1.close).  Workers are
            # already confirmed absent above, so open one cleanup-only
            # generation for those bounded close calls; no motion ARM/START is
            # issued in this context, and revoke it immediately afterwards.
            self._gate.begin()
            try:
                for component in (self.display, self.camera, self.motor, self.guard):
                    close = getattr(component, "close", None)
                    if callable(close):
                        close()
            finally:
                self._gate.revoke(timeout_s=self.config.cleanup_timeout_s)
            if self._hardware_lock_owned and self.hardware_lock is not None:
                self.hardware_lock.release()
                self._hardware_lock_owned = False
            self._closed = True
            return {"closed": True, "cleanup": self.last_cleanup}
        finally:
            self._owner.release()


# Keep the public board-facing names explicit while retaining one implementation.
MotionCaptureOrchestrator = MotionSession
BMotionSession = MotionSession


__all__ = [
    "MotionBusyError", "MotionCaptureSessionError", "MotionError", "MotionIdentityError",
    "MotionSession", "MotionTimeout", "MotionCaptureOrchestrator", "BMotionSession",
]
