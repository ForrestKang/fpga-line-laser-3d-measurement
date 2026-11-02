"""Bounded FIFO persistence for continuous scan measurements.

The writer accepts immutable snapshots without waiting for storage. The default
sink fsyncs each payload and appends a frame/event manifest only after the
corresponding files have been committed.
"""
from __future__ import annotations

import atexit
import copy
from collections import deque
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from typing import Any, Callable, Mapping

import numpy as np
from .payload_digest import payload_sha256


_RECORD_KEYS = frozenset({
    "request_id",
    "capture_epoch",
    "raw",
    "raw_sha256",
    "profile_payload",
    "points_camera_mm",
    "points_axis_mm",
    "summary",
    "motion",
    "exposure",
})
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_JSON_ATOMIC_TYPES = (type(None), str, bool, int, float)


@dataclass(frozen=True)
class WriteItem:
    """One immutable queue envelope passed to the configured sink."""

    sequence: int
    kind: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class WriterReceipt:
    """Auditable snapshot of accepted, committed, and still-pending work."""

    accepted: int
    written: int
    pending: int
    error: str | None
    worker_joined: bool
    durable: bool
    worker_unconfirmed: bool = False
    failure_receipt_persisted: bool | None = None


class WriterFailure(RuntimeError):
    """Raised after a sink failure; ``receipt`` preserves its persistence state."""

    def __init__(self, message: str, receipt: WriterReceipt | None = None) -> None:
        super().__init__(message)
        self.receipt = receipt


def _validate_json_value(value: Any, *, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return
    if isinstance(value, np.generic):
        _validate_json_value(value.item(), path=path)
        return
    if isinstance(value, np.ndarray):
        if value.dtype.hasobject:
            raise ValueError(f"{path} contains an object array")
        _validate_json_value(value.tolist(), path=path)
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            _validate_json_value(child, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_json_value(child, path=f"{path}[{index}]")
        return
    raise ValueError(f"{path} contains unsupported JSON value {type(value).__name__}")


def _snapshot_json_value(value: Any) -> Any:
    """Deep-copy JSON data and make every nested NumPy array read-only."""
    if type(value) in _JSON_ATOMIC_TYPES:
        return value
    if isinstance(value, np.ndarray):
        result = np.array(value, copy=True, order="C")
        if result.dtype.hasobject:
            raise ValueError("object arrays are not supported")
        result.setflags(write=False)
        return result
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {copy.deepcopy(key): _snapshot_json_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_snapshot_json_value(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_snapshot_json_value(child) for child in value)
    return copy.deepcopy(value)


def _snapshot_json_checked(value: Any) -> tuple[Any, bool]:
    """Copy plain builtin trees once; certify only already valid JSON trees.

    Subclasses, NumPy and custom deepcopy retain the original snapshot and
    validation path. Invalid plain trees are not raised here: all metadata
    snapshots must finish before the original ordered validation phase.
    """
    kind = type(value)
    if kind in _JSON_ATOMIC_TYPES:
        return value, kind is not float or math.isfinite(value)
    if kind is dict:
        result = {}
        checked = True
        for key, child in value.items():
            copied_key = key if type(key) is str else copy.deepcopy(key)
            copied_child, child_checked = _snapshot_json_checked(child)
            result[copied_key] = copied_child
            checked = checked and type(copied_key) is str and child_checked
        return result, checked
    if kind is list or kind is tuple:
        result = []
        checked = True
        for child in value:
            copied_child, child_checked = _snapshot_json_checked(child)
            result.append(copied_child)
            checked = checked and child_checked
        return (tuple(result) if kind is tuple else result), checked
    return _snapshot_json_value(value), False


def _json_default(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"cannot encode {type(value).__name__} as JSON")


def _json_line(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                       default=_json_default) + "\n").encode("utf-8")


def _write_exclusive(path: Path, writer: Callable[[Any], None]) -> None:
    """Create a new file, flush it, and fsync it; preserve partial writes on error."""
    with path.open("xb") as stream:
        writer(stream)
        stream.flush()
        os.fsync(stream.fileno())


def _append_fsynced(path: Path, data: bytes) -> None:
    with path.open("ab") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def profile_payload_filename(payload: bytes) -> str:
    """Name already-admitted wire bytes; preserve legacy naming for old data.

    This dispatch does not validate a packet or promote it to wire evidence.
    Unknown legacy/injected payloads retain the historical SLP2 filename.
    """
    if type(payload) is not bytes:
        raise TypeError('profile payload must be immutable bytes')
    magic = int.from_bytes(payload[:4],'little') if len(payload) >= 4 else None
    return {0x534c4331:'profile.slc1.bin',0x534c4631:'profile.slf1.bin',
            0x534c5032:'profile.slp2.bin'}.get(magic,'profile.slp2.bin')


def _default_sink(output_dir: Path, item: WriteItem) -> None:
    if item.kind == "event":
        event_row = {"sequence": item.sequence, "event": item.payload}
        _append_fsynced(output_dir / "events.jsonl", _json_line(event_row))
        return

    if item.kind != "frame":
        raise ValueError(f"unknown write item kind: {item.kind}")
    record = item.payload
    request_id = record["request_id"]
    frame_dir = output_dir / f"frame_{request_id:010d}"
    frame_dir.mkdir(exist_ok=False)

    _write_exclusive(frame_dir / "raw.gray8.bin", lambda f: f.write(record["raw"]))
    profile_file = profile_payload_filename(record['profile_payload'])
    _write_exclusive(frame_dir / profile_file, lambda f: f.write(record["profile_payload"]))
    point_files: dict[str, str | None] = {}
    for key, filename in (("points_camera_mm", "points_camera_mm.npy"),
                          ("points_axis_mm", "points_axis_mm.npy")):
        points = record[key]
        point_files[key] = filename if points is not None else None
        if points is not None:
            _write_exclusive(frame_dir / filename,
                             lambda f, value=points: np.save(f, value, allow_pickle=False))

    metadata = {
        "sequence": item.sequence,
        "request_id": request_id,
        "capture_epoch": record["capture_epoch"],
        "raw_sha256": record["raw_sha256"],
        "profile_sha256": hashlib.sha256(record["profile_payload"]).hexdigest(),
        "raw_bytes": len(record["raw"]),
        "profile_bytes": len(record["profile_payload"]),
        "raw_file": "raw.gray8.bin",
        "profile_file": profile_file,
        "point_files": point_files,
        "summary": record["summary"],
        "motion": record["motion"],
        "exposure": record["exposure"],
    }
    _write_exclusive(frame_dir / "frame.json",
                     lambda f: f.write(_json_line(metadata)))

    # The tail append is the session commit point; no frame index row exists if
    # any payload or per-frame metadata write above fails.
    _append_fsynced(output_dir / "frames.jsonl", _json_line(metadata))


class BoundedAsyncWriter:
    """Persist frames and events on one bounded FIFO worker.

    ``capacity`` bounds all accepted but not yet fsynced work, including the
    item currently handled by the worker. A custom ``sink(output_dir, item)``
    must return only after that item is durably committed. ``BufferError`` means
    the attempted item was not accepted and remains the caller's responsibility.
    """

    def __init__(self, output_dir: str | os.PathLike[str], capacity: int = 8,
                 sink: Callable[[Path, WriteItem], None] | None = None) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        self.output_dir = Path(output_dir)
        if not callable(sink) and sink is not None:
            raise TypeError("sink must be callable")
        self._sink = _default_sink if sink is None else sink
        self._capacity = capacity
        self.output_dir.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(exist_ok=False)

        self._queue: queue.Queue[WriteItem] = queue.Queue()
        self._condition = threading.Condition()
        self._closing = threading.Event()
        self._accepting = True
        self._accepted = 0
        self._written = 0
        # One clock domain for queue, worker and fsync observations. Pending
        # entries are capacity bounded; completed diagnostics retain 1024 items.
        self._persistence_pending: dict[int, dict[str, Any]] = {}
        self._persistence_history: deque[dict[str, Any]] = deque(maxlen=1024)
        self._last_request_id: int | None = None
        self._capture_epoch: int | None = None
        self._worker_error: BaseException | None = None
        self._error_text: str | None = None
        self._failure_receipts: list[dict[str, Any]] = []
        self._failure_receipt_persisted: bool | None = None
        self._worker_stopped = False
        self._joined = False
        self._thread = threading.Thread(target=self._run, name="scan3d-persistence-writer", daemon=True)
        self._thread.start()
        self._exit_hook = self._log_interpreter_exit
        atexit.register(self._exit_hook)

    @property
    def persistence_timeline(self) -> tuple[Mapping[str, Any], ...]:
        """Bounded copies; acceptance is never promoted to durable completion.

        Timestamps are perf_counter seconds, meaningful within this process.
        Pending snapshots remain inspectable while a worker is blocked in fsync.
        """
        with self._condition:
            return tuple(dict(row) for row in self._persistence_history) + tuple(
                dict(row) for _, row in sorted(self._persistence_pending.items()))

    @property
    def persistence_timeline_summary(self) -> Mapping[str, Any]:
        """Explicitly report retained versus omitted completed diagnostics."""
        with self._condition:
            retained = len(self._persistence_history)
            return dict(clock='perf_counter', capacity=1024,
                        completed=self._written, completed_retained=retained,
                        completed_omitted=max(0, self._written-retained),
                        pending_retained=len(self._persistence_pending),
                        full_history=self._written <= 1024)

    def _persistence_phase(self, group, phase: str) -> None:
        now = time.perf_counter()
        with self._condition:
            for item in group:
                row = self._persistence_pending.get(item.sequence)
                if row is not None:
                    row['phase'] = phase
                    row.setdefault(phase + '_perf_s', now)
                    if phase == 'worker':
                        row.setdefault('accepted_to_worker_s', now-row['accepted_perf_s'])

    def _persistence_complete(self, group) -> None:
        now = time.perf_counter()
        # Called only after the sink returned from all required fsync calls.
        for item in group:
            row = self._persistence_pending.pop(item.sequence, None)
            if row is not None:
                row.update(phase='durable', durable_perf_s=now,
                           accepted_to_durable_s=now-row['accepted_perf_s'])
                self._persistence_history.append(row)

    @property
    def failure_receipts(self) -> tuple[Mapping[str, Any], ...]:
        with self._condition:
            return tuple(copy.deepcopy(item) for item in self._failure_receipts)

    def submit(self, record: Mapping[str, Any]) -> int:
        """Snapshot and enqueue one frame without waiting for storage."""
        snapshot = self._snapshot_record(record)
        return self._enqueue("frame", snapshot)

    def submit_event(self, event: Mapping[str, Any]) -> int:
        """Snapshot and enqueue an event on the same capacity-limited FIFO."""
        if not isinstance(event, Mapping):
            raise TypeError("event must be a mapping")
        snapshot = _snapshot_json_value(dict(event))
        _validate_json_value(snapshot, path="event")
        return self._enqueue("event", snapshot)

    def flush(self, timeout_s: float = 5.0) -> WriterReceipt:
        """Wait up to ``timeout_s`` for work accepted before this call."""
        timeout = _timeout(timeout_s)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            target = self._accepted
            while self._written < target and self._worker_error is None and not self._worker_stopped:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._condition.wait(remaining)
            receipt = self._receipt_locked()
            if self._worker_error is not None:
                raise WriterFailure(self._error_text or "persistence writer failed", receipt)
            return receipt

    def close(self, timeout_s: float = 5.0) -> WriterReceipt:
        """Stop accepting, drain accepted work, and join within the deadline."""
        timeout = _timeout(timeout_s)
        with self._condition:
            self._accepting = False
            self._closing.set()
            self._condition.notify_all()
        self._thread.join(timeout)
        with self._condition:
            self._joined = not self._thread.is_alive()
            receipt = self._receipt_locked()
            if self._worker_error is not None:
                raise WriterFailure(self._error_text or "persistence writer failed", receipt)
            if receipt.durable:
                atexit.unregister(self._exit_hook)
            return receipt

    def _snapshot_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(record, Mapping):
            raise TypeError("record must be a mapping")
        keys = set(record)
        if any(not isinstance(key, str) for key in keys):
            raise ValueError("frame record keys must be strings")
        missing = _RECORD_KEYS - keys
        extra = keys - _RECORD_KEYS
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing keys: {sorted(missing)}")
            if extra:
                details.append(f"unexpected keys: {sorted(extra)}")
            raise ValueError("invalid frame record (" + "; ".join(details) + ")")

        request_id = record["request_id"]
        epoch = record["capture_epoch"]
        if type(request_id) is not int or not 0 <= request_id < (1 << 32):
            raise ValueError("request_id must be uint32")
        if type(epoch) is not int or not 0 <= epoch < (1 << 16):
            raise ValueError("capture_epoch must be uint16")
        raw = record["raw"]
        profile = record["profile_payload"]
        if not isinstance(raw, bytes) or not isinstance(profile, bytes):
            raise TypeError("raw and profile_payload must be immutable bytes")
        raw_hash = record["raw_sha256"]
        if not isinstance(raw_hash, str) or _HASH_RE.fullmatch(raw_hash) is None:
            raise ValueError("raw_sha256 must be a lowercase SHA-256 hex digest")
        if payload_sha256(raw) != raw_hash:
            raise ValueError("raw_sha256 does not match raw bytes")

        arrays: dict[str, np.ndarray | None] = {}
        for key in ("points_camera_mm", "points_axis_mm"):
            points = record[key]
            if points is None:
                arrays[key] = None
                continue
            if not isinstance(points, np.ndarray):
                raise TypeError(f"{key} must be a numpy array or None")
            if points.dtype.hasobject or points.dtype.kind not in "iuf":
                raise ValueError(f"{key} must have a non-object numeric dtype")
            if points.ndim != 2 or points.shape[1] != 3:
                raise ValueError(f"{key} must have shape (N, 3)")
            copied = np.array(points, copy=True, order="C")
            if not np.isfinite(copied).all():
                raise ValueError(f"{key} contains non-finite coordinates")
            copied.setflags(write=False)
            arrays[key] = copied

        for key in ("summary", "exposure"):
            if not isinstance(record[key], dict):
                raise TypeError(f"{key} must be a dict")
        if record["motion"] is not None and not isinstance(record["motion"], dict):
            raise TypeError("motion must be a dict or None")

        metadata = {key: _snapshot_json_checked(record[key])
                    for key in ("summary", "motion", "exposure")}
        snapshot = {
            "request_id": request_id,
            "capture_epoch": epoch,
            "raw": raw,
            "raw_sha256": raw_hash,
            "profile_payload": profile,
            "points_camera_mm": arrays["points_camera_mm"],
            "points_axis_mm": arrays["points_axis_mm"],
            "summary": metadata["summary"][0],
            "motion": metadata["motion"][0],
            "exposure": metadata["exposure"][0],
        }
        for key in ("summary", "motion", "exposure"):
            if not metadata[key][1]:
                _validate_json_value(snapshot[key], path=key)
        return snapshot

    def _check_enqueue_locked(self, kind: str, payload: Mapping[str, Any]) -> None:
        """Check admission without mutation while the caller holds the condition.

        Derived writers can reject full queues before reserving filesystem
        space. Final enqueue repeats this check before assigning a sequence;
        only the durable written watermark releases capacity.
        """
        if self._worker_error is not None:
            raise WriterFailure(self._error_text or "persistence writer failed",
                                self._receipt_locked())
        if not self._accepting:
            raise RuntimeError("writer is closed")
        if kind == "frame":
            epoch = payload["capture_epoch"]
            request_id = payload["request_id"]
            if self._capture_epoch is not None and epoch != self._capture_epoch:
                raise ValueError("capture epoch changed; create a new writer")
            if self._last_request_id is not None and request_id <= self._last_request_id:
                raise ValueError("request_id repeated or regressed")
        if self._accepted - self._written >= self._capacity:
            raise BufferError("persistence writer capacity is full; item was not accepted")

    def _enqueue(self, kind: str, payload: Mapping[str, Any]) -> int:
        with self._condition:
            self._check_enqueue_locked(kind, payload)
            sequence = self._accepted + 1
            item = WriteItem(sequence, kind, payload)
            self._queue.put_nowait(item)
            self._accepted = sequence
            self._persistence_pending[sequence] = dict(
                sequence=sequence, kind=kind, request_id=payload.get('request_id'),
                accepted_perf_s=time.perf_counter(), phase='queued')
            if kind == "frame":
                self._capture_epoch = payload["capture_epoch"]
                self._last_request_id = payload["request_id"]
            self._condition.notify_all()
            return sequence

    def _run(self) -> None:
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.1)
                except queue.Empty:
                    with self._condition:
                        if self._closing.is_set() and self._queue.empty():
                            return
                    continue
                try:
                    # The submitter accounts the accepted ticket under this
                    # condition before any sink can commit it.
                    with self._condition:
                        self._condition.notify_all()
                    self._persistence_phase([item], "sink")
                    self._sink(self.output_dir, item)
                except BaseException as exc:
                    self._mark_failed(item, exc)
                    return
                else:
                    with self._condition:
                        self._persistence_complete([item])
                        self._written += 1
                        self._condition.notify_all()
                finally:
                    self._queue.task_done()
        finally:
            with self._condition:
                self._worker_stopped = True
                self._condition.notify_all()

    def _mark_failed(self, item: WriteItem, exc: BaseException) -> None:
        error_text = f"{type(exc).__name__}: {exc}"
        failure = {
            "sequence": item.sequence,
            "kind": item.kind,
            "request_id": item.payload.get("request_id") if item.kind == "frame" else None,
            "capture_epoch": item.payload.get("capture_epoch") if item.kind == "frame" else None,
            "error": error_text,
            "durable": False,
        }
        with self._condition:
            failure.update(accepted=self._accepted, written=self._written,
                           pending=self._accepted-self._written,
                           pending_sequences=sorted(self._persistence_pending),
                           persistence_timeline=[dict(row) for _, row in
                                                 sorted(self._persistence_pending.items())],
                           automatic_retry=False)
            self._worker_error = exc
            self._error_text = error_text
            self._accepting = False
            self._closing.set()
            self._failure_receipts.append(failure)
            self._failure_receipt_persisted = False
            self._condition.notify_all()
        try:
            _append_fsynced(self.output_dir / "failures.jsonl", _json_line(failure))
        except BaseException:
            return
        with self._condition:
            self._failure_receipt_persisted = True
            self._condition.notify_all()

    def _receipt_locked(self) -> WriterReceipt:
        pending = self._accepted - self._written
        joined = self._joined or (self._worker_stopped and not self._thread.is_alive())
        unconfirmed = self._thread.is_alive() and pending > 0
        durable = pending == 0 and self._worker_error is None
        return WriterReceipt(
            accepted=self._accepted,
            written=self._written,
            pending=pending,
            error=self._error_text,
            worker_joined=joined,
            durable=durable,
            worker_unconfirmed=unconfirmed,
            failure_receipt_persisted=(self._failure_receipt_persisted
                                       if self._worker_error is not None else None),
        )

    def _log_interpreter_exit(self) -> None:
        with self._condition:
            receipt = self._receipt_locked()
            if receipt.durable:
                return
            report = {
                "accepted": receipt.accepted,
                "written": receipt.written,
                "pending": receipt.pending,
                "error": receipt.error,
                "worker_joined": receipt.worker_joined,
                "durable": False,
            }
        try:
            _write_exclusive(self.output_dir / "shutdown.json",
                             lambda f: f.write(_json_line(report)))
        except BaseException as exc:
            try:
                sys.stderr.write(f"scan3d writer exit state was not durable: {exc}\n")
            except BaseException:
                pass


def _timeout(timeout_s: float) -> float:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise TypeError("timeout_s must be a finite non-negative number")
    timeout = float(timeout_s)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout_s must be a finite non-negative number")
    return timeout
