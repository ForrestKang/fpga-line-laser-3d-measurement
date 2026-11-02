"""Offline transports for the P2 controller.

``MockTransport`` models both S2MM streams from one immutable frame.  It is
intentionally deterministic and caches the expensive row reference, making
long lifecycle tests meaningful without silently using a second frame.
``ReplayTransport`` replays the exact bytes saved by :class:`SessionRecorder`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
import time
from typing import Any

from ..frame_io import GrayFrame
from ..gray_reference import RowResult, reference_rows
from ..profile import Profile, ProfilePoint, decode_profile, encode_profile
from .capabilities import Capabilities
from .contract import MAX_PROFILE_BYTES, MAX_SLP2_PACKET_BYTES
from .errors import BusyError, CaptureCancelled, DMAError, ProtocolError
from .mmio import MockMMIO
from .types import DiagnosticRow, DiagnosticsSnapshot


@dataclass(frozen=True, slots=True)
class TransportCapture:
    raw: bytes | None
    profile: bytes | None
    diagnostics: DiagnosticsSnapshot | None
    raw_length: int
    profile_length: int


class CaptureTransport:
    """Small transport interface consumed by :class:`P2Controller`."""

    capabilities: Capabilities
    mmio: MockMMIO

    def stream_requirements(self, *, raw: bool, profile: bool) -> tuple[bool, bool]:
        """Return the physical S2MM streams that must be armed for START.

        A full profile overlay emits both the raw bypass and the Profile on
        every START.  Offline transports and the raw-only overlay can retain
        the caller's requested subset.
        """
        return raw, profile

    def arm_raw(self, size: int) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def arm_profile(self, size: int) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def start_capture(self, *, raw: bool, profile: bool) -> int:  # pragma: no cover
        raise NotImplementedError

    def poll(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    def receive(self) -> TransportCapture:  # pragma: no cover
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def reset(self, *, reason: str = "reset") -> None:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        raise NotImplementedError


class MockTransport(CaptureTransport):
    """Deterministic same-frame software model of raw and Profile S2MM paths."""

    def __init__(
        self,
        frame: GrayFrame | bytes | bytearray | memoryview | Callable[[], GrayFrame],
        *,
        width: int | None = None,
        height: int | None = None,
        mmio: MockMMIO | None = None,
        faults: Iterable[str] = (),
        latency_s: float = 0.0,
    ) -> None:
        if isinstance(frame, GrayFrame):
            self._frame = frame
            self._source: Callable[[], GrayFrame] | None = None
        elif callable(frame):
            self._frame = None
            self._source = frame
        else:
            if width is None or height is None:
                raise ValueError("width and height are required for bytes-like frames")
            self._frame = GrayFrame.from_pixels(width, height, frame)
            self._source = None
        initial = self._frame if self._frame is not None else self._source()
        self.mmio = mmio or MockMMIO(frame_width=initial.width, frame_height=initial.height)
        if (self.mmio.frame_width, self.mmio.frame_height) != (initial.width, initial.height):
            raise ValueError("MMIO and transport frame dimensions must match")
        self.capabilities = Capabilities.offline_mock()
        self.faults = frozenset(str(item) for item in faults)
        if latency_s < 0:
            raise ValueError("latency_s must be nonnegative")
        self.latency_s = float(latency_s)
        self._raw_armed = 0
        self._profile_armed = 0
        self._want_raw = False
        self._want_profile = False
        self._inflight = False
        self._done_at = 0.0
        self._request_id: int | None = None
        self._result: TransportCapture | None = None
        self._row_cache: dict[tuple[Any, ...], tuple[RowResult, ...]] = {}
        self._frame_count = 0
        self.cancellation_reasons: list[str] = []

    @property
    def frame(self) -> GrayFrame:
        if self._source is not None:
            frame = self._source()
            if not isinstance(frame, GrayFrame):
                raise ProtocolError("frame source must return GrayFrame")
            if (frame.width, frame.height) != (self.mmio.frame_width, self.mmio.frame_height):
                raise ProtocolError("frame source changed dimensions")
            return frame
        assert self._frame is not None
        return self._frame

    @property
    def request_id(self) -> int | None:
        return self._request_id

    def arm_raw(self, size: int) -> None:
        expected = self.mmio.frame_width * self.mmio.frame_height
        if type(size) is not int or size < expected:
            raise DMAError(f"raw buffer must hold at least {expected} bytes")
        self._raw_armed = size

    def arm_profile(self, size: int) -> None:
        if type(size) is not int or size < MAX_PROFILE_BYTES:
            raise DMAError(f"profile buffer must hold at least {MAX_PROFILE_BYTES} bytes")
        self._profile_armed = size

    def _cached_rows(self, frame: GrayFrame) -> tuple[RowResult, ...]:
        values = tuple(self.mmio.read(offset) for offset in (8, 12, 16, 20, 24, 88, 92))
        roi = (values[1] & 0xFFFF, values[1] >> 16, values[2] & 0xFFFF, values[2] >> 16)
        key = (frame.sha256, *values)
        if key not in self._row_cache:
            self._row_cache[key] = reference_rows(
                frame,
                threshold=values[0],
                roi=roi,
                min_width=values[3],
                max_width=values[4],
                min_sum=values[5],
                min_peak=values[6],
            )
        return self._row_cache[key]

    @staticmethod
    def _diag_rows(rows: Iterable[RowResult], profile_id: int) -> tuple[DiagnosticRow, ...]:
        result: list[DiagnosticRow] = []
        for row in rows:
            diagnostics = (int(row.too_narrow) | (int(row.roi_excluded) << 1) |
                           (int(row.line_bad) << 2) | (int(row.overflow) << 3))
            result.append(DiagnosticRow(
                row=row.row, xc_fixed=row.xc_fixed, s0=row.s0, s1=row.s1,
                peak=row.peak, width=row.width, candidate_count=row.candidate_count,
                flags=row.flags, quality=row.quality, valid=row.valid,
                diagnostics=diagnostics, profile_id=profile_id,
            ))
        return tuple(result)

    def _make_result(self, frame: GrayFrame, request_id: int) -> TransportCapture:
        rows = self._cached_rows(frame)
        points = tuple(row.to_profile_point() for row in rows)
        timestamp = self.mmio.timestamp
        profile = Profile(
            format_version=2, profile_id=request_id, angle_raw=0,
            timestamp=timestamp, error_flags=0, points=points,
            frame_width=frame.width, frame_height=frame.height,
        )
        profile_payload = encode_profile(profile)
        if len(profile_payload) > MAX_SLP2_PACKET_BYTES:
            raise ProtocolError("generated Profile exceeds the SLP2 maximum")
        diag_rows = self._diag_rows(rows, request_id)
        self.mmio.set_diagnostics(diag_rows, frame_count=self._frame_count + 1, last_frame=self._frame_count + 1)
        return TransportCapture(
            raw=frame.pixels if self._want_raw else None,
            profile=profile_payload if self._want_profile else None,
            diagnostics=self.mmio.diagnostics_snapshot(),
            raw_length=len(frame.pixels) if self._want_raw else 0,
            profile_length=len(profile_payload) if self._want_profile else 0,
        )

    def start_capture(self, *, raw: bool, profile: bool) -> int:
        if self._inflight:
            raise BusyError("Mock transport has one capture already in flight")
        if not raw and not profile:
            raise DMAError("at least one S2MM stream must be armed")
        if raw and not self._raw_armed:
            raise DMAError("raw S2MM must be armed before START")
        if profile and not self._profile_armed:
            raise DMAError("Profile S2MM must be armed before START")
        self._want_raw, self._want_profile = raw, profile
        self._request_id = self.mmio.accept_request()
        self._inflight = True
        self._result = None
        self._done_at = time.monotonic() + self.latency_s
        if "timeout" not in self.faults and "camera_stopped" not in self.faults:
            frame = self.frame
            self._frame_count += 1
            if "profile_error" in self.faults:
                self._result = TransportCapture(
                    raw=frame.pixels if raw else None, profile=b"\x00" * 32 if profile else None,
                    diagnostics=None, raw_length=len(frame.pixels) if raw else 0,
                    profile_length=32 if profile else 0,
                )
            else:
                self._result = self._make_result(frame, self._request_id)
        return self._request_id

    def poll(self) -> bool:
        if not self._inflight:
            return self._result is not None
        if time.monotonic() < self._done_at:
            return False
        return self._result is not None

    def receive(self) -> TransportCapture:
        if not self.poll():
            raise DMAError("capture is not complete")
        assert self._result is not None
        result = self._result
        self._inflight = False
        self._raw_armed = 0
        self._profile_armed = 0
        self.mmio.complete_request(profile_id=self._request_id or 0)
        return result

    def stop(self) -> None:
        if self._inflight:
            self.cancellation_reasons.append("stop")
            self.mmio.stop_capture()
        self._inflight = False
        self._result = None
        self._raw_armed = self._profile_armed = 0

    def reset(self, *, reason: str = "reset") -> None:
        if self._inflight:
            self.cancellation_reasons.append(reason)
        self.mmio.stop_capture()
        self._inflight = False
        self._result = None
        self._raw_armed = self._profile_armed = 0

    def close(self) -> None:
        self.reset(reason="close")


class ReplayTransport(MockTransport):
    """Replay one atomically recorded capture without recomputing its Profile.

    In addition to :class:`SessionRecorder` directories, this accepts a
    versioned ``record.json`` fixture.  The latter is used for historical RTL
    exports whose original SLP2 bytes must remain byte-for-byte intact; it is
    deliberately kept separate from ``MockTransport`` provenance.
    """

    def __init__(self, source: str | Path, *, mmio: MockMMIO | None = None) -> None:
        root = Path(source)
        record_path: Path | None = None
        if root.is_file() and root.name.lower() == "record.json":
            record_path = root
            root = root.parent
        elif root.is_file():
            root = root.parent
        elif root.is_dir() and (root / "record.json").is_file():
            record_path = root / "record.json"

        record: dict[str, Any] = {}
        if record_path is not None:
            try:
                loaded = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ProtocolError(f"cannot read replay record {record_path}: {exc}") from exc
            if not isinstance(loaded, dict) or loaded.get("record_type") != "p2_replay":
                raise ProtocolError("replay record must declare record_type='p2_replay'")
            if loaded.get("record_version") != 1:
                raise ProtocolError("unsupported replay record_version")
            record = loaded

        def record_file(name: str) -> Path | None:
            value = record.get(name)
            if not isinstance(value, str) or not value:
                return None
            path = Path(value)
            return path if path.is_absolute() else root / path

        # SessionRecorder writes ``root/<session_id>/...``.  Accept that
        # parent directory when it contains exactly one unambiguous session,
        # while still rejecting a directory containing multiple captures.
        if not any((root / name).is_file() for name in ("raw.bin", "raw_gray.bin", "frame.bin", "profile.bin", "profile.slp2", "profile_payload.bin")):
            candidates = [child for child in root.iterdir() if child.is_dir() and any(child.glob("*.raw.bin")) and any(child.glob("*.profile.bin"))] if root.is_dir() else []
            if len(candidates) == 1:
                root = candidates[0]
        raw_path = record_file("raw_file") if record else next((root / name for name in ("raw.bin", "raw_gray.bin", "frame.bin") if (root / name).is_file()), None)
        profile_path = record_file("profile_file") if record else next((root / name for name in ("profile.bin", "profile.slp2", "profile_payload.bin") if (root / name).is_file()), None)
        if raw_path is None:
            raw_path = next(iter(sorted(root.glob("*.raw.bin"))), None)
        if profile_path is None:
            profile_path = next(iter(sorted(root.glob("*.profile.bin"))), None)
        if raw_path is None or profile_path is None:
            raise FileNotFoundError(f"replay requires raw.bin and profile.bin under {root}")
        metadata_path = root / "metadata.json"
        metadata: dict[str, Any] = dict(record)
        nested_metadata = metadata.get("metadata")
        if isinstance(nested_metadata, dict):
            metadata = {**nested_metadata, **metadata}
        if not record and metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if isinstance(metadata.get("metadata"), dict):
            metadata = {**metadata["metadata"], **{key: value for key, value in metadata.items() if key != "metadata"}}
        result_files = sorted(root.glob("*.result.json"))
        if result_files:
            try:
                result_meta = json.loads(result_files[0].read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                result_meta = {}
            metadata = {**result_meta, **metadata}
        width = int(metadata.get("frame_width", 640))
        height = int(metadata.get("frame_height", 480))
        raw = raw_path.read_bytes()
        if len(raw) != width * height:
            raise ProtocolError(f"replay raw length {len(raw)} != {width}*{height}")
        self._replay_raw = raw
        self._replay_profile = profile_path.read_bytes()
        if record:
            expected_raw_sha = record.get("raw_sha256")
            expected_profile_sha = record.get("profile_sha256")
            if not isinstance(expected_raw_sha, str) or hashlib.sha256(raw).hexdigest() != expected_raw_sha.lower():
                raise ProtocolError("versioned replay raw_sha256 does not match raw_file")
            if not isinstance(expected_profile_sha, str) or hashlib.sha256(self._replay_profile).hexdigest() != expected_profile_sha.lower():
                raise ProtocolError("versioned replay profile_sha256 does not match profile_file")
            metadata["replay_record_path"] = str(record_path)
            metadata["replay_source_kind"] = "versioned_record"
        try:
            replay_profile = decode_profile(self._replay_profile, frame_width=width, frame_height=height)
        except Exception as exc:
            raise ProtocolError(f"invalid replay SLP2 payload: {exc}") from exc
        if replay_profile.format_version != 2:
            raise ProtocolError("ReplayTransport accepts SLP2 only")
        self._replay_profile_id = replay_profile.profile_id
        if record:
            record_request_id = record.get("request_id")
            record_epoch = record.get("epoch")
            if type(record_request_id) is not int or not 0 <= record_request_id <= 0xFFFF_FFFF:
                raise ProtocolError("versioned replay request_id must be a uint32")
            if type(record_epoch) is not int or not 0 <= record_epoch <= 0xFFFF_FFFF:
                raise ProtocolError("versioned replay epoch must be a uint32")
            if record_request_id != self._replay_profile_id:
                raise ProtocolError("versioned replay request_id does not match the SLP2 profile_id")
            self._replay_epoch = record_epoch
        else:
            self._replay_epoch = 0
        self._replay_metadata = metadata
        super().__init__(GrayFrame.from_pixels(width, height, raw), mmio=mmio)
        self.capabilities = Capabilities.replay()
        diagnostic_files = sorted(root.glob("*.diagnostics.json"))
        self._replay_diagnostics: DiagnosticsSnapshot | None = None
        if diagnostic_files:
            try:
                raw_diag = json.loads(diagnostic_files[0].read_text(encoding="utf-8"))
                rows = tuple(DiagnosticRow(**item) for item in raw_diag.get("rows", ()))
                self._replay_diagnostics = DiagnosticsSnapshot(
                    version=int(raw_diag.get("version", 1)), capabilities=int(raw_diag.get("capabilities", 0)),
                    ready=bool(raw_diag.get("ready", True)), pclk_hz=int(raw_diag.get("pclk_hz", 0)),
                    width=int(raw_diag.get("width", width)), height=int(raw_diag.get("height", height)),
                    last_line=int(raw_diag.get("last_line", 0)), last_frame=int(raw_diag.get("last_frame", 0)),
                    frame_count=int(raw_diag.get("frame_count", 0)),
                    camera_device_address=int(raw_diag.get("camera_device_address", 0)),
                    camera_register_address=int(raw_diag.get("camera_register_address", 0)),
                    camera_write_data=int(raw_diag.get("camera_write_data", 0)),
                    camera_read_data=int(raw_diag.get("camera_read_data", 0)),
                    camera_command=int(raw_diag.get("camera_command", 0)), camera_status=int(raw_diag.get("camera_status", 0)),
                    camera_control=int(raw_diag.get("camera_control", 0)), abort=int(raw_diag.get("abort", 0)),
                    epoch=int(raw_diag.get("epoch", 0)), request_id=int(raw_diag.get("request_id", 0)),
                    row_count=int(raw_diag.get("row_count", len(rows))), error=int(raw_diag.get("error", 0)), rows=rows,
                )
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ProtocolError(f"invalid replay diagnostics: {exc}") from exc
            if record and (
                self._replay_diagnostics.request_id != self._replay_profile_id
                or self._replay_diagnostics.epoch != self._replay_epoch
            ):
                raise ProtocolError("versioned replay diagnostics are not bound to request_id/epoch")

    @property
    def replay_metadata(self) -> dict[str, Any]:
        """Metadata and provenance from the replay source."""

        return dict(self._replay_metadata)

    def start_capture(self, *, raw: bool, profile: bool) -> int:
        # A replay represents one previously recorded request.  Reuse its
        # packet ID so the controller can still enforce the no-mixed-ID rule;
        # replaying the same bytes must not invent a different request ID.
        self.mmio.epoch = self._replay_epoch
        super().start_capture(raw=raw, profile=profile)
        self._request_id = self._replay_profile_id
        return self._request_id

    def _make_result(self, frame: GrayFrame, request_id: int) -> TransportCapture:
        profile = decode_profile(self._replay_profile, frame_width=frame.width, frame_height=frame.height)
        if profile.format_version != 2:
            raise ProtocolError("ReplayTransport accepts SLP2 only")
        diagnostics = self._replay_diagnostics or self.mmio.diagnostics_snapshot()
        return TransportCapture(
            raw=frame.pixels if self._want_raw else None,
            profile=self._replay_profile if self._want_profile else None,
            diagnostics=diagnostics,
            raw_length=len(frame.pixels) if self._want_raw else 0,
            profile_length=len(self._replay_profile) if self._want_profile else 0,
        )


FileReplayTransport = ReplayTransport


__all__ = ["CaptureTransport", "TransportCapture", "MockTransport", "ReplayTransport", "FileReplayTransport"]
