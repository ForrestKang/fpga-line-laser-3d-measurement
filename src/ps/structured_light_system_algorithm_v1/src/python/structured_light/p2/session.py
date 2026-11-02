"""Streaming, recoverable capture-session records."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any
import uuid

from .types import CaptureResult


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SessionRecorder:
    """Persist one capture at a time with atomic payload replacement.

    Large raw/Profile buffers are written to temporary files in the same
    directory and atomically renamed.  The JSONL event stream is flushed and
    fsynced after every record, so an interrupted run remains inspectable.
    """

    def __init__(self, root: str | Path, *, session_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        self.root = Path(root)
        self.session_id = session_id or uuid.uuid4().hex
        if not self.session_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in self.session_id):
            raise ValueError("session_id contains unsupported path characters")
        self.path = self.root / self.session_id
        self.path.mkdir(parents=True, exist_ok=True)
        self.events_path = self.path / "events.jsonl"
        self.manifest_path = self.path / "metadata.json"
        self._events = self.events_path.open("a", encoding="utf-8", newline="\n")
        if not self.manifest_path.exists():
            self._atomic_json(self.manifest_path, {
                "session_id": self.session_id,
                "created_unix": time.time(),
                "metadata": metadata or {},
            })

    def _atomic_bytes(self, target: Path, payload: bytes) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Session payloads are non-sensitive acquisition evidence.  mkstemp
            # deliberately creates 0600 files, which would carry through the
            # atomic rename and make board-side SFTP retrieval fail for the
            # normal unprivileged account.  Set the intended evidence mode
            # before replace so a reader never observes a partially written
            # file and the final inode has the documented permissions.
            os.chmod(temp_name, 0o644)
            os.replace(temp_name, target)
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass

    def _atomic_json(self, target: Path, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str).encode("utf-8")
        self._atomic_bytes(target, payload)

    def event(self, name: str, **fields: Any) -> None:
        record = {"time_unix": time.time(), "event": name, **fields}
        self._events.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        self._events.flush()
        os.fsync(self._events.fileno())

    def _write_capture_payload(
        self,
        result: CaptureResult,
        *,
        status: str | None = None,
        failure: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        # Request IDs may restart after an FPGA reset.  Include the epoch in
        # every filename and fail closed on a collision rather than replacing
        # evidence from a previous epoch.
        prefix = f"capture_e{result.epoch:08x}_r{result.request_id:08x}"
        raw_name = f"{prefix}.raw.bin"
        profile_name = f"{prefix}.profile.bin"
        diag_name = f"{prefix}.diagnostics.json"
        result_name = f"{prefix}.result.json"
        targets = tuple(self.path / name for name in (raw_name, profile_name, diag_name, result_name))
        collisions = [str(path) for path in targets if path.exists()]
        if collisions:
            raise FileExistsError("capture evidence filename collision: " + ", ".join(collisions))
        self._atomic_bytes(self.path / raw_name, result.raw_bytes)
        self._atomic_bytes(self.path / profile_name, result.profile_payload)
        diagnostics = {
            "version": result.diagnostics.version,
            "capabilities": result.diagnostics.capabilities,
            "ready": result.diagnostics.ready,
            "pclk_hz": result.diagnostics.pclk_hz,
            "width": result.diagnostics.width,
            "height": result.diagnostics.height,
            "last_line": result.diagnostics.last_line,
            "last_frame": result.diagnostics.last_frame,
            "frame_count": result.diagnostics.frame_count,
            "camera_device_address": result.diagnostics.camera_device_address,
            "camera_register_address": result.diagnostics.camera_register_address,
            "camera_write_data": result.diagnostics.camera_write_data,
            "camera_read_data": result.diagnostics.camera_read_data,
            "camera_command": result.diagnostics.camera_command,
            "camera_status": result.diagnostics.camera_status,
            "camera_control": result.diagnostics.camera_control,
            "abort": result.diagnostics.abort,
            "epoch": result.diagnostics.epoch,
            "request_id": result.diagnostics.request_id,
            "row_count": result.diagnostics.row_count,
            "error": result.diagnostics.error,
            "rows": [asdict(row) for row in result.diagnostics.rows],
        }
        self._atomic_json(self.path / diag_name, diagnostics)
        result_record: dict[str, Any] = {
            "request_id": result.request_id,
            "epoch": result.epoch,
            "session_id": result.session_id,
            "frame_width": result.raw_frame.width,
            "frame_height": result.raw_frame.height,
            "raw_file": raw_name,
            "raw_length": len(result.raw_bytes),
            "raw_sha256": _sha256(result.raw_bytes),
            "profile_file": profile_name,
            "profile_length": len(result.profile_payload),
            "profile_sha256": _sha256(result.profile_payload),
            "diagnostics_file": diag_name,
            "timestamp": result.timestamp,
            "angle_raw": result.angle_raw,
            "angle_valid": result.angle_valid,
            "motion_enabled": result.motion_enabled,
            "comparison": asdict(result.comparison) if result.comparison is not None else None,
        }
        if status is not None:
            result_record["capture_status"] = status
        if failure:
            result_record["failure"] = dict(failure)
        self._atomic_json(self.path / result_name, result_record)
        return {
            "raw_file": raw_name,
            "profile_file": profile_name,
            "diagnostics_file": diag_name,
            "raw_sha256": _sha256(result.raw_bytes),
            "profile_sha256": _sha256(result.profile_payload),
        }

    def record_capture(self, result: CaptureResult) -> Path:
        files = self._write_capture_payload(result)
        self.event(
            "capture_complete", request_id=result.request_id, epoch=result.epoch,
            raw_file=files["raw_file"], profile_file=files["profile_file"],
            diagnostics_file=files["diagnostics_file"],
            raw_sha256=files["raw_sha256"], profile_sha256=files["profile_sha256"],
            comparison_passed=result.comparison.passed if result.comparison else None,
        )
        return self.path

    def record_failed_capture(
        self,
        result: CaptureResult,
        *,
        reason: str,
        error_type: str,
        error: str,
        recovery_error: str | None = None,
    ) -> Path:
        """Persist a completed transfer that failed the host reference check.

        The raw frame, Profile bytes, diagnostics and comparison mismatches
        remain available for replay, while the event is explicitly
        ``capture_failed`` and is never reported as ``capture_complete``.
        """

        failure = {
            "reason": reason,
            "error_type": error_type,
            "error": error,
            "recovery_error": recovery_error,
        }
        files = self._write_capture_payload(result, status="failed", failure=failure)
        self.event(
            "capture_failed", request_id=result.request_id, epoch=result.epoch,
            raw_file=files["raw_file"], profile_file=files["profile_file"],
            diagnostics_file=files["diagnostics_file"],
            raw_sha256=files["raw_sha256"], profile_sha256=files["profile_sha256"],
            comparison_passed=result.comparison.passed if result.comparison else None,
            **failure,
        )
        return self.path

    def record_cancel(self, *, request_id: int | None, epoch: int, reason: str) -> None:
        self.event("capture_cancelled", request_id=request_id, epoch=epoch, reason=reason)

    def record_error(
        self,
        *,
        request_id: int | None,
        epoch: int,
        reason: str,
        error_type: str,
        error: str,
        recovery_error: str | None = None,
    ) -> None:
        """Record a rejected completed transfer without publishing payloads."""

        self.event(
            "capture_error", request_id=request_id, epoch=epoch, reason=reason,
            error_type=error_type, error=error, recovery_error=recovery_error,
        )

    def close(self) -> None:
        if not self._events.closed:
            self._events.flush()
            os.fsync(self._events.fileno())
            self._events.close()

    def __enter__(self) -> "SessionRecorder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


__all__ = ["SessionRecorder"]
