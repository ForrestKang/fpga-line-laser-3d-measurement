"""P2 host controller with one-in-flight, bounded, same-frame acquisition."""

from __future__ import annotations

from collections import deque
import time
from typing import Any
import uuid

from ..frame_io import GrayFrame
from ..profile import Profile, ProfileError, decode_profile
from .capabilities import Capabilities
from .contract import (
    MAX_PROFILE_BYTES, MAX_SLP2_PACKET_BYTES, REG_CONTROL, REG_CURRENT_ANGLE,
    REG_ERROR_FLAGS, REG_MECH_STEPS_HIGH, REG_MECH_STEPS_LOW, REG_MIN_PEAK,
    REG_MIN_SUM, REG_MIN_WIDTH, REG_MAX_WIDTH, REG_PROFILE_COUNT, REG_PROFILE_ID,
    REG_ROI_END, REG_ROI_START, REG_STATUS, REG_STEPS_PER_PROFILE, REG_THRESHOLD,
    REG_TIMESTAMP_HIGH, REG_TIMESTAMP_LOW,
)
from .errors import (
    BusyError, CapabilityError, CaptureCancelled, CaptureTimeout, ConfigurationError,
    DMAError, ProtocolError, VersionMismatch,
)
from .mmio import MockMMIO
from .reference import compare_same_frame
from .session import SessionRecorder
from .transport import CaptureTransport, TransportCapture
from .types import (
    CaptureHandle, CaptureResult, CentroidConfig, ControllerStatus, DiagnosticsSnapshot,
    ReferenceComparison, RoiConfig,
)


def _u16(value: int, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 0xFFFF:
        raise ConfigurationError(f"{name} must be an unsigned 16-bit integer")
    return value


class P2Controller:
    """Transport-independent API used by offline and future PYNQ programs."""

    def __init__(
        self,
        transport: CaptureTransport,
        *,
        recorder: SessionRecorder | None = None,
        session_id: str | None = None,
        default_timeout_s: float = 2.0,
    ) -> None:
        if not isinstance(transport, CaptureTransport):
            # Duck-typed hardware adapters remain possible, but require the
            # public members up front so a failed board setup is explicit.
            for name in ("mmio", "capabilities", "arm_raw", "arm_profile", "start_capture", "poll", "receive", "stop", "reset"):
                if not hasattr(transport, name):
                    raise TypeError(f"transport lacks required method/property {name}")
        if type(default_timeout_s) not in (int, float) or default_timeout_s <= 0:
            raise ConfigurationError("default_timeout_s must be positive")
        self.transport = transport
        self.mmio: MockMMIO = transport.mmio
        self.capabilities: Capabilities = transport.capabilities
        self.default_timeout_s = float(default_timeout_s)
        self.recorder = recorder
        self.session_id = session_id or (recorder.session_id if recorder else uuid.uuid4().hex)
        self._inflight: CaptureHandle | None = None
        self._inflight_mode: str | None = None
        self._completed: deque[CaptureResult] = deque(maxlen=8)
        self._closed = False
        self._configure_static_defaults()

    @property
    def frame_width(self) -> int:
        return self.mmio.frame_width

    @property
    def frame_height(self) -> int:
        return self.mmio.frame_height

    @property
    def pending_results(self) -> int:
        return len(self._completed)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("P2Controller is closed")

    def _configure_static_defaults(self) -> None:
        # The old register map defaults STEPS_PER_PROFILE to one for the
        # moving scanner.  P2 is explicitly a static single-capture overlay.
        if not self.mmio.busy:
            self.mmio.write(REG_PROFILE_COUNT, 1)
            self.mmio.write(REG_STEPS_PER_PROFILE, 0)

    def set_threshold(self, value: int) -> None:
        self._ensure_open()
        _u16(value, "threshold")
        if value > 255:
            raise ConfigurationError("threshold must be in [0, 255]")
        self.mmio.write(REG_THRESHOLD, value)
        if self.mmio.read(REG_THRESHOLD) != value:
            raise ProtocolError("threshold readback mismatch")

    def configure_roi(self, x0: int, y0: int, x1: int, y1: int) -> RoiConfig:
        self._ensure_open()
        values = (_u16(x0, "roi.x0"), _u16(y0, "roi.y0"), _u16(x1, "roi.x1"), _u16(y1, "roi.y1"))
        if not (0 <= x0 < x1 <= self.frame_width and 0 <= y0 < y1 <= self.frame_height):
            raise ConfigurationError("ROI must satisfy [x0,x1), [y0,y1) inside the frame")
        self.mmio.write(REG_ROI_START, (y0 << 16) | x0)
        self.mmio.write(REG_ROI_END, (y1 << 16) | x1)
        if self.mmio.read(REG_ROI_START) != ((y0 << 16) | x0) or self.mmio.read(REG_ROI_END) != ((y1 << 16) | x1):
            raise ProtocolError("ROI readback mismatch")
        return RoiConfig(*values)

    def configure_centroid(
        self,
        *,
        min_width: int = 1,
        max_width: int = 32,
        min_sum: int = 30,
        min_peak: int = 15,
    ) -> CentroidConfig:
        self._ensure_open()
        if type(min_width) is not int or type(max_width) is not int or not 1 <= min_width <= max_width <= self.frame_width:
            raise ConfigurationError("centroid width limits are invalid")
        if type(min_sum) is not int or not 0 <= min_sum <= 0x3_FFFF:
            raise ConfigurationError("min_sum must be an 18-bit unsigned integer")
        if type(min_peak) is not int or not 0 <= min_peak <= 255:
            raise ConfigurationError("min_peak must be in [0,255]")
        for offset, value in ((REG_MIN_WIDTH, min_width), (REG_MAX_WIDTH, max_width), (REG_MIN_SUM, min_sum), (REG_MIN_PEAK, min_peak)):
            self.mmio.write(offset, value)
            if self.mmio.read(offset) != value:
                raise ProtocolError(f"centroid register 0x{offset:X} readback mismatch")
        return CentroidConfig(self.mmio.read(REG_THRESHOLD), min_width, max_width, min_sum, min_peak)

    def _roi(self) -> tuple[int, int, int, int]:
        start, end = self.mmio.read(REG_ROI_START), self.mmio.read(REG_ROI_END)
        return start & 0xFFFF, start >> 16, end & 0xFFFF, end >> 16

    def _centroid_parameters(self) -> dict[str, int]:
        return {
            "threshold": self.mmio.read(REG_THRESHOLD),
            "min_width": self.mmio.read(REG_MIN_WIDTH),
            "max_width": self.mmio.read(REG_MAX_WIDTH),
            "min_sum": self.mmio.read(REG_MIN_SUM),
            "min_peak": self.mmio.read(REG_MIN_PEAK),
        }

    def get_status(self) -> ControllerStatus:
        self._ensure_open()
        status = self.mmio.read(REG_STATUS)
        low = self.mmio.read(REG_TIMESTAMP_LOW)
        high = self.mmio.read(REG_TIMESTAMP_HIGH)
        return ControllerStatus(
            busy=bool(status & 1), scan_done=bool(status & 2), packer_busy=bool(status & 4),
            motion_state=(status >> 4) & 0xF, error_flags=self.mmio.read(REG_ERROR_FLAGS),
            profile_id=self.mmio.read(REG_PROFILE_ID), timestamp=low | (high << 32),
            epoch=self.mmio.epoch, request_id=self.mmio.request_id,
            raw_dma_ready=bool(self.capabilities.raw_dma), profile_dma_ready=bool(self.capabilities.profile_dma),
        )

    def _require_not_inflight(self) -> None:
        if self._inflight is not None or self.mmio.busy:
            raise BusyError("one capture is already in flight")

    def _arm_and_start(self, *, raw: bool, profile: bool) -> CaptureHandle:
        self._ensure_open()
        self._require_not_inflight()
        if raw:
            self.capabilities.require("raw_dma")
        if profile:
            self.capabilities.require("profile_dma")
        requirements = getattr(self.transport, "stream_requirements", None)
        arm_raw, arm_profile = requirements(raw=raw, profile=profile) if callable(requirements) else (raw, profile)
        try:
            if arm_raw:
                self.transport.arm_raw(self.frame_width * self.frame_height)
            if arm_profile:
                self.transport.arm_profile(MAX_PROFILE_BYTES)
            # Both S2MM paths are armed before this single START reaches the
            # camera-domain gate.  A partial arm is recovered below.
            request_id = self.transport.start_capture(raw=arm_raw, profile=arm_profile)
        except Exception:
            try:
                self.transport.reset(reason="start_error")
            finally:
                self._inflight = None
                self._inflight_mode = None
            raise
        handle = CaptureHandle(request_id=request_id, epoch=self.mmio.epoch, raw_armed=arm_raw, profile_armed=arm_profile)
        self._inflight = handle
        self._inflight_mode = "pair" if raw and profile else ("raw" if raw else "profile")
        return handle

    def capture_single(self) -> CaptureHandle:
        """Arm Profile S2MM and issue exactly one static START."""

        return self._arm_and_start(raw=False, profile=True)

    def capture_raw(self, timeout_s: float | None = None) -> GrayFrame:
        handle = self._arm_and_start(raw=True, profile=False)
        transfer = self._wait_for_transfer(timeout_s)
        try:
            if transfer.raw is None or transfer.raw_length != self.frame_width * self.frame_height or len(transfer.raw) != transfer.raw_length:
                raise ProtocolError("raw S2MM returned an unexpected actual length")
            return GrayFrame.from_pixels(self.frame_width, self.frame_height, transfer.raw)
        except Exception as exc:
            self._recover_after_processing_error("raw_processing_error", exc, handle)
            raise
        finally:
            self._finish_inflight()

    def recv_profile(self, timeout_s: float | None = None) -> Profile:
        self._ensure_open()
        if self._inflight is None or self._inflight_mode not in {"profile", "pair"}:
            raise BusyError("recv_profile requires an active capture_single or capture_pair")
        handle = self._inflight
        transfer = self._wait_for_transfer(timeout_s)
        try:
            if transfer.profile is None or transfer.profile_length != len(transfer.profile):
                raise ProtocolError("Profile S2MM returned an invalid actual length")
            if transfer.profile_length > MAX_PROFILE_BYTES or transfer.profile_length > MAX_SLP2_PACKET_BYTES:
                raise ProtocolError("Profile S2MM length exceeds the frozen packet limit")
            try:
                profile = decode_profile(transfer.profile, frame_width=self.frame_width, frame_height=self.frame_height)
            except Exception as exc:
                raise ProtocolError(f"Profile packet decode failed: {exc}") from exc
            if profile.format_version != 2:
                raise ProtocolError(f"P2 profile format must be SLP2, got {profile.format_version}")
            if profile.profile_id != handle.request_id:
                raise ProtocolError("Profile ID does not match the active capture request")
            diagnostics = transfer.diagnostics
            if diagnostics is None:
                read_diagnostics = getattr(self.transport, "read_diagnostics", None)
                if callable(read_diagnostics):
                    diagnostics = read_diagnostics()
                elif hasattr(self.mmio, "diagnostics_snapshot"):
                    diagnostics = self.mmio.diagnostics_snapshot()
                else:
                    raise ProtocolError("transport did not provide the published diagnostic snapshot")
            if diagnostics.request_id != handle.request_id:
                raise ProtocolError("diagnostic request_id does not match Profile request")
            if diagnostics.epoch != handle.epoch:
                raise ProtocolError("diagnostic epoch does not match the active capture")
            if diagnostics.row_count != self.frame_height or len(diagnostics.rows) != self.frame_height:
                raise ProtocolError("diagnostic row_count does not cover the complete frame")
            if any(row.profile_id != handle.request_id for row in diagnostics.rows):
                raise ProtocolError("diagnostic row contains a mixed Profile ID")
            return profile
        except Exception as exc:
            self._recover_after_processing_error("profile_processing_error", exc, handle)
            raise
        finally:
            self._finish_inflight()

    def capture_pair(self, timeout_s: float | None = None) -> CaptureResult:
        """Capture raw and SLP2 from one START and compare them to one frame."""

        if len(self._completed) >= 8:
            raise BusyError("completed result queue is full (maximum 8)")
        handle = self._arm_and_start(raw=True, profile=True)
        transfer = self._wait_for_transfer(timeout_s)
        failed_result: CaptureResult | None = None
        try:
            expected_raw = self.frame_width * self.frame_height
            if transfer.raw is None or transfer.raw_length != expected_raw or len(transfer.raw) != expected_raw:
                raise ProtocolError(f"raw S2MM length {transfer.raw_length} != expected {expected_raw}")
            if transfer.profile is None or transfer.profile_length != len(transfer.profile):
                raise ProtocolError("Profile S2MM did not report its actual byte length")
            if transfer.profile_length > MAX_PROFILE_BYTES or transfer.profile_length > MAX_SLP2_PACKET_BYTES:
                raise ProtocolError("Profile S2MM length exceeds 8192/5792 byte limits")
            raw_frame = GrayFrame.from_pixels(self.frame_width, self.frame_height, transfer.raw)
            try:
                profile = decode_profile(transfer.profile, frame_width=self.frame_width, frame_height=self.frame_height)
            except Exception as exc:
                raise ProtocolError(f"Profile packet decode failed: {exc}") from exc
            if profile.format_version != 2:
                raise ProtocolError(f"P2 profile format must be SLP2, got {profile.format_version}")
            if profile.profile_id != handle.request_id:
                raise ProtocolError("Profile ID does not match the active capture request")
            diagnostics = transfer.diagnostics
            if diagnostics is None:
                read_diagnostics = getattr(self.transport, "read_diagnostics", None)
                if callable(read_diagnostics):
                    diagnostics = read_diagnostics()
                elif hasattr(self.mmio, "diagnostics_snapshot"):
                    diagnostics = self.mmio.diagnostics_snapshot()
                else:
                    raise ProtocolError("transport did not provide the published diagnostic snapshot")
            if diagnostics.request_id != handle.request_id:
                raise ProtocolError("diagnostic request_id does not match Profile request")
            if diagnostics.epoch != handle.epoch:
                raise ProtocolError("diagnostic epoch does not match the active capture")
            if diagnostics.row_count != self.frame_height or len(diagnostics.rows) != self.frame_height:
                raise ProtocolError("diagnostic row_count does not cover the complete frame")
            if any(row.profile_id != handle.request_id for row in diagnostics.rows):
                raise ProtocolError("diagnostic row contains a mixed Profile ID")
            params = self._centroid_parameters()
            comparison = compare_same_frame(raw_frame, profile, diagnostics=diagnostics, roi=self._roi(), **params)
            result = CaptureResult(
                request_id=handle.request_id, epoch=handle.epoch, session_id=self.session_id,
                raw_frame=raw_frame, profile_payload=bytes(transfer.profile), profile=profile,
                diagnostics=diagnostics, comparison=comparison,
                timestamp=profile.timestamp, angle_raw=profile.angle_raw,
                angle_valid=False, motion_enabled=False,
                metadata={"source": self.capabilities.source, "same_frame": True},
            )
            if not comparison.passed:
                failed_result = result
                raise ProtocolError(
                    "same-frame reference comparison failed "
                    f"with {comparison.mismatch_count} mismatch(es)"
                )
        except Exception as exc:
            self._recover_after_processing_error(
                "pair_reference_mismatch" if failed_result is not None else "pair_processing_error",
                exc, handle, failure_result=failed_result,
            )
            raise
        finally:
            self._finish_inflight()
        self._completed.append(result)
        if self.recorder is not None:
            self.recorder.record_capture(result)
        return result

    def _wait_for_transfer(self, timeout_s: float | None) -> TransportCapture:
        timeout = self.default_timeout_s if timeout_s is None else float(timeout_s)
        if timeout <= 0:
            raise ConfigurationError("timeout_s must be positive")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.transport.poll():
                    return self.transport.receive()
            except Exception:
                request_id = self._inflight.request_id if self._inflight else None
                epoch = self.mmio.epoch
                try:
                    self.transport.reset(reason="transport_error")
                finally:
                    self._inflight = None
                    self._inflight_mode = None
                    if self.recorder is not None:
                        self.recorder.record_cancel(request_id=request_id, epoch=epoch, reason="transport_error")
                raise
            time.sleep(min(0.001, max(0.0001, deadline - time.monotonic())))
        request_id = self._inflight.request_id if self._inflight else None
        epoch = self.mmio.epoch
        try:
            self.transport.reset(reason="host_timeout")
        finally:
            self._inflight = None
            self._inflight_mode = None
            if self.recorder is not None:
                self.recorder.record_cancel(request_id=request_id, epoch=epoch, reason="host_timeout")
        raise CaptureTimeout(f"capture request {request_id} exceeded {timeout:.3f}s")

    def _finish_inflight(self) -> None:
        self._inflight = None
        self._inflight_mode = None

    def _recover_after_processing_error(
        self,
        reason: str,
        error: Exception,
        handle: CaptureHandle | None,
        *,
        failure_result: CaptureResult | None = None,
    ) -> None:
        """Recover a completed DMA transaction after host-side validation fails.

        ``receive`` may already have consumed the DMA bytes when packet
        decoding, diagnostics, or same-frame comparison rejects them.  The
        controller therefore still runs the transport's bounded reset path;
        it never turns a corrupt packet into a successful result or leaves a
        half-finished request in the one-in-flight slot.
        """

        request_id = handle.request_id if handle is not None else None
        epoch = handle.epoch if handle is not None else self.mmio.epoch
        recovery_error: Exception | None = None
        try:
            self.transport.reset(reason=reason)
        except Exception as exc:
            recovery_error = exc
        finally:
            self._finish_inflight()
            if self.recorder is not None:
                recovery_text = str(recovery_error) if recovery_error else None
                if failure_result is not None:
                    record_failed = getattr(self.recorder, "record_failed_capture", None)
                    if callable(record_failed):
                        record_failed(
                            failure_result, reason=reason,
                            error_type=type(error).__name__, error=str(error),
                            recovery_error=recovery_text,
                        )
                    else:
                        raise ProtocolError("recorder cannot persist failed capture evidence")
                else:
                    record_error = getattr(self.recorder, "record_error", None)
                    if callable(record_error):
                        record_error(
                            request_id=request_id, epoch=epoch, reason=reason,
                            error_type=type(error).__name__, error=str(error),
                            recovery_error=recovery_text,
                        )
                    else:
                        self.recorder.record_cancel(request_id=request_id, epoch=epoch, reason=reason)
        # Preserve the original processing exception.  If recovery itself
        # failed, expose that fact through exception chaining for a caller that
        # must decide whether the board is safe to retry.
        if recovery_error is not None:
            raise recovery_error from error

    def stop(self) -> None:
        self._ensure_open()
        request_id = self._inflight.request_id if self._inflight else None
        if self._inflight is not None:
            try:
                self.transport.stop()
            finally:
                self._finish_inflight()
                if self.recorder is not None:
                    self.recorder.record_cancel(request_id=request_id, epoch=self.mmio.epoch, reason="stop")

    def clear_errors(self) -> None:
        self._ensure_open()
        self._require_not_inflight()
        self.mmio.write(REG_CONTROL, 8)

    def drain_results(self) -> tuple[CaptureResult, ...]:
        values = tuple(self._completed)
        self._completed.clear()
        return values

    def home(self) -> None:
        raise CapabilityError("HOME is unavailable in the P2 static profile overlay")

    def start_scan(self) -> None:
        raise CapabilityError("motion scan is intentionally outside P2")

    def close(self) -> None:
        if not self._closed:
            failure: Exception | None = None
            try:
                if self._inflight is not None:
                    self.stop()
                self.transport.close()
            except Exception as exc:
                failure = exc
            finally:
                try:
                    if self.recorder is not None:
                        self.recorder.close()
                except Exception as exc:
                    if failure is None:
                        failure = exc
                self._closed = True
            if failure is not None:
                raise failure


Controller = P2Controller
StructuredLightController = P2Controller


__all__ = ["P2Controller", "Controller", "StructuredLightController"]
