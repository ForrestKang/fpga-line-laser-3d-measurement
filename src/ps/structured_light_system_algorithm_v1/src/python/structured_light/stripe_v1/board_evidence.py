"""Pure-CPU evidence generation for 752x480 algorithm-v1 board captures.

This module deliberately imports no PYNQ, Vivado, transport, overlay, or board
control code. ``BoundedCPUWorker`` is an optional reusable process for batch
analysis; it accepts only a raw-file path and plain values and can be killed
without affecting the caller or any hardware process.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import traceback
from typing import Any

import numpy as np

from .candidates import extract_candidates
from .selection import select_candidates
from .types import StripeConfig


WIDTH = 752
HEIGHT = 480
RAW_BYTES = WIDTH * HEIGHT
SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MODE_WINDOWS = {"local31": 31, "local63": 63}
_IDENTITY_REQUIRED = {"session_id", "epoch", "request_id", "expected_raw_sha256"}
_IDENTITY_OPTIONAL = {"capture_index"}


class EvidenceError(ValueError):
    """Raised when a capture, identity, or analysis request is invalid."""


class EvidenceTimeout(TimeoutError):
    """Raised after the isolated CPU worker exceeds its per-frame deadline."""


class EvidenceWorkerError(RuntimeError):
    """Raised when an isolated CPU worker fails unexpectedly."""


def _strict_integer(value: Any, name: str, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bound = f"[{minimum}, {maximum}]" if maximum is not None else f">= {minimum}"
        raise EvidenceError(f"{name} must be an integer in {bound}")
    return value


def _validate_request(
    raw_path: Path,
    *,
    mode: str,
    roi: tuple[int, int, int, int],
    threshold: int,
    identity: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw_path, Path):
        raise EvidenceError("raw_path must be a pathlib.Path")
    if type(mode) is not str or mode not in {"fixed", *_MODE_WINDOWS}:
        raise EvidenceError("mode must be exactly 'fixed', 'local31', or 'local63'")
    if not isinstance(roi, tuple) or len(roi) != 4:
        raise EvidenceError("roi must be a four-integer (x0, y0, x1, y1) tuple")
    for index, value in enumerate(roi):
        _strict_integer(value, f"roi[{index}]", 0)
    x0, y0, x1, y1 = roi
    if not (0 <= x0 < x1 <= WIDTH and 0 <= y0 < y1 <= HEIGHT):
        raise EvidenceError(f"roi must lie within the {WIDTH}x{HEIGHT} source frame")
    _strict_integer(threshold, "threshold", 0, 255)
    if mode != "fixed" and threshold == 0:
        raise EvidenceError("local threshold floor must be in [1, 255]")
    if not isinstance(identity, dict) or any(type(key) is not str for key in identity):
        raise EvidenceError("identity must be an object with string keys")
    missing = _IDENTITY_REQUIRED - identity.keys()
    extra = identity.keys() - _IDENTITY_REQUIRED - _IDENTITY_OPTIONAL
    if missing:
        raise EvidenceError(f"identity is missing field(s): {', '.join(sorted(missing))}")
    if extra:
        raise EvidenceError(f"identity has unknown field(s): {', '.join(sorted(extra))}")
    session_id = identity["session_id"]
    if type(session_id) is not str or not session_id.strip():
        raise EvidenceError("identity.session_id must be a non-empty string")
    _strict_integer(identity["epoch"], "identity.epoch", 0, 0xFFFF_FFFF)
    _strict_integer(identity["request_id"], "identity.request_id", 0, 0xFFFF_FFFF)
    expected_sha = identity["expected_raw_sha256"]
    if type(expected_sha) is not str or not _SHA256_RE.fullmatch(expected_sha):
        raise EvidenceError("identity.expected_raw_sha256 must be 64 lowercase hexadecimal characters")
    if "capture_index" in identity:
        _strict_integer(identity["capture_index"], "identity.capture_index", 0)
    return {key: identity[key] for key in sorted(identity)}


def choose_indices(count: int = 1000) -> dict[str, list[int]]:
    """Return deterministic capture indices for the required comparison modes."""

    _strict_integer(count, "count", 0)
    return {
        "fixed": list(range(count)),
        "local31": list(range(0, count, 10)),
        "local63": list(range(0, count, 10)),
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def canonical_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Return the timing-independent payload used for exact PC/PS comparison."""

    payload = result.get("canonical_payload")
    if not isinstance(payload, dict):
        raise EvidenceError("analysis result has no canonical_payload object")
    return payload


def canonical_sha256(result: dict[str, Any]) -> str:
    """Hash a result's canonical fields, excluding elapsed time and peak RSS."""

    return hashlib.sha256(_canonical_json(canonical_payload(result)).encode("ascii")).hexdigest()


def _peak_rss() -> tuple[int | None, str | None]:
    """Read this process's peak resident set size where the OS exposes it."""

    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # macOS reports bytes; Linux and the BSDs report KiB.
        return (value if os.uname().sysname == "Darwin" else value * 1024), "resource.getrusage"
    except (ImportError, AttributeError, OSError):
        pass
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _Counters()
            counters.cb = ctypes.sizeof(counters)
            get_process = ctypes.windll.kernel32.GetCurrentProcess
            get_info = ctypes.windll.psapi.GetProcessMemoryInfo
            get_process.argtypes = []
            get_process.restype = ctypes.c_void_p
            get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Counters), wintypes.DWORD]
            get_info.restype = wintypes.BOOL
            if get_info(get_process(), ctypes.byref(counters), counters.cb):
                return int(counters.PeakWorkingSetSize), "GetProcessMemoryInfo.PeakWorkingSetSize"
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    return None, None


def analyze_frame(
    raw_path: Path,
    *,
    mode: str,
    roi: tuple[int, int, int, int],
    threshold: int,
    identity: dict[str, Any],
) -> dict[str, Any]:
    """Hash and analyze one exact 752x480 gray8 raw frame using CPU only.

    ``threshold`` is the fixed-threshold setting in every mode. It affects the
    algorithm only in ``fixed`` mode; the local modes retain their locked
    default MAD scale/floor and differ only in window size. Every candidate and
    every selected row is serialized. The selection score is ordinal and is never
    interpreted as a probability. Human accuracy metrics remain null without
    explicit labels.
    """

    total_started = time.perf_counter()
    normalized_identity = _validate_request(
        raw_path, mode=mode, roi=roi, threshold=threshold, identity=identity
    )
    read_started = time.perf_counter()
    try:
        raw = raw_path.read_bytes()
    except OSError as exc:
        raise EvidenceError(f"cannot read raw frame {raw_path}: {exc}") from exc
    if len(raw) != RAW_BYTES:
        raise EvidenceError(f"raw frame has {len(raw)} bytes; expected exactly {RAW_BYTES}")
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != normalized_identity["expected_raw_sha256"]:
        raise EvidenceError("raw frame SHA-256 differs from identity.expected_raw_sha256")
    read_hash_s = time.perf_counter() - read_started

    local = mode != "fixed"
    config = StripeConfig(
        threshold_mode="local" if local else "fixed",
        fixed_threshold=threshold,
        local_window=_MODE_WINDOWS.get(mode, 31),
        roi=roi,
    )
    gray = np.frombuffer(raw, dtype=np.uint8).reshape(HEIGHT, WIDTH)
    extract_started = time.perf_counter()
    candidates = extract_candidates(gray, config)
    extract_s = time.perf_counter() - extract_started
    select_started = time.perf_counter()
    selected = select_candidates(candidates, config)
    select_s = time.perf_counter() - select_started

    candidate_count = sum(len(row.candidates) for row in candidates.rows)
    selected_count = sum(row.candidate is not None for row in selected.rows)
    rejected = Counter(row.reason for row in selected.rows if row.reason is not None)
    canonical = json.loads(
        _canonical_json(
            {
                "schema": "structured_light_algorithm_v1.cpu_evidence",
                "schema_version": SCHEMA_VERSION,
                "identity": normalized_identity,
                "raw": {"width": WIDTH, "height": HEIGHT, "byte_count": len(raw), "sha256": actual_sha},
                "mode": mode,
                "configuration": asdict(config),
                "configuration_sha256": config.fingerprint,
                "candidate_frame": asdict(candidates),
                "selected_profile": asdict(selected),
            }
        )
    )
    rss_bytes, rss_source = _peak_rss()
    return {
        "canonical_payload": canonical,
        "canonical_sha256": hashlib.sha256(_canonical_json(canonical).encode("ascii")).hexdigest(),
        "measurements": {
            "read_hash_s": read_hash_s,
            "extract_s": extract_s,
            "select_s": select_s,
            "total_s": time.perf_counter() - total_started,
            "peak_rss_bytes": rss_bytes,
            "peak_rss_source": rss_source,
        },
        "summary": {
            "candidate_count": candidate_count,
            "selected_rows": selected_count,
            "rejected_rows_by_reason": dict(sorted(rejected.items())),
            "human_labels_present": False,
            "wrong_accept_rate": None,
            "correct_coverage_rate": None,
            "score_interpretation": "ordinal only; no probability interpretation",
            "hardware_accessed": False,
        },
    }


def _cpu_worker_main(connection: Any) -> None:
    """Spawn target: accept only serializable CPU analysis requests."""

    try:
        connection.send({"status": "ready"})
        while True:
            request = connection.recv()
            if request is None:
                return
            try:
                result = analyze_frame(
                    Path(request["raw_path"]),
                    mode=request["mode"],
                    roi=tuple(request["roi"]),
                    threshold=request["threshold"],
                    identity=request["identity"],
                )
                result_path = Path(request["result_path"])
                encoded = json.dumps(
                    result, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
                temporary_path = result_path.with_name(result_path.name + ".tmp")
                with temporary_path.open("xb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, result_path)
                connection.send({
                    "status": "ok",
                    "path": str(result_path),
                    "byte_count": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                })
            except BaseException as exc:
                try:
                    temporary_path = Path(request.get("result_path", "")).with_name(
                        Path(request.get("result_path", "")).name + ".tmp"
                    )
                    temporary_path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
                connection.send(
                    {
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    }
                )
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        try:
            connection.close()
        except OSError:
            pass


def _deadline(timeout_s: float) -> float:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise EvidenceError("timeout_s must be a finite positive number")
    value = float(timeout_s)
    if not math.isfinite(value) or value <= 0:
        raise EvidenceError("timeout_s must be a finite positive number")
    return value


class BoundedCPUWorker:
    """Reusable, single-process worker for bounded batches of raw-frame jobs.

    Use one instance per batch to avoid starting a new Python process for each
    frame. Calls must be sequential. A timeout terminates this worker and
    permanently closes the instance; no retry or replacement process is made.
    """

    def __init__(self, *, startup_timeout_s: float = 30.0) -> None:
        self._startup_timeout_s = _deadline(startup_timeout_s)
        self._context = multiprocessing.get_context("spawn")
        self._process: multiprocessing.Process | None = None
        self._connection: Any = None
        self._closed = False
        self._result_dir = Path(tempfile.mkdtemp(prefix="algorithm-v1-cpu-"))
        self._job_counter = 0

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def _start(self, *, timeout_s: float | None = None) -> None:
        if self._closed:
            raise EvidenceWorkerError("CPU worker is closed")
        if self._process is not None:
            if not self._process.is_alive():
                raise EvidenceWorkerError("CPU worker exited before the next request")
            return
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(target=_cpu_worker_main, args=(child,), name="algorithm-v1-cpu-evidence")
        process.daemon = True
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            raise
        child.close()
        self._process = process
        self._connection = parent
        startup_limit = self._startup_timeout_s if timeout_s is None else min(self._startup_timeout_s, timeout_s)
        if startup_limit <= 0 or not parent.poll(startup_limit):
            self._terminate()
            raise EvidenceTimeout(f"CPU worker did not start before the {startup_limit:g}-second analysis deadline")
        try:
            ready = parent.recv()
        except (EOFError, OSError) as exc:
            self._terminate()
            raise EvidenceWorkerError("CPU worker exited during startup") from exc
        if ready != {"status": "ready"}:
            self._terminate()
            raise EvidenceWorkerError("CPU worker sent an invalid startup message")

    def _terminate(self) -> None:
        process = self._process
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=2.0)
            if process.is_alive():
                process.kill()
                process.join(timeout=2.0)
        if self._connection is not None:
            try:
                self._connection.close()
            except OSError:
                pass
        self._closed = True
        self._remove_result_directory()

    def _remove_result_directory(self) -> None:
        try:
            shutil.rmtree(self._result_dir)
        except FileNotFoundError:
            pass

    def _read_result_file(
        self,
        path: Path,
        *,
        expected_bytes: int,
        expected_sha256: str,
        deadline: float,
        timeout_s: float,
    ) -> dict[str, Any]:
        try:
            if path.is_symlink() or not path.is_file():
                raise EvidenceWorkerError("CPU worker result file is missing or is a symlink")
            resolved = path.resolve(strict=True)
            resolved.relative_to(self._result_dir.resolve(strict=True))
            if type(expected_bytes) is not int or not 0 < expected_bytes <= 32 * 1024 * 1024:
                raise EvidenceWorkerError("CPU worker result byte count is outside its bound")
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            total = 0
            with resolved.open("rb") as stream:
                while True:
                    if time.monotonic() >= deadline:
                        raise EvidenceTimeout(f"CPU analysis exceeded its {timeout_s:g}-second deadline")
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected_bytes or total > 32 * 1024 * 1024:
                        raise EvidenceWorkerError("CPU worker result grew beyond its announced byte count")
                    digest.update(chunk)
                    chunks.append(chunk)
            if total != expected_bytes or digest.hexdigest() != expected_sha256:
                raise EvidenceWorkerError("CPU worker result byte count or SHA-256 differs from its notification")
            result = json.loads(b"".join(chunks).decode("utf-8"))
            if time.monotonic() >= deadline:
                raise EvidenceTimeout(f"CPU analysis exceeded its {timeout_s:g}-second deadline")
            if not isinstance(result, dict) or set(result) != {
                "canonical_payload", "canonical_sha256", "measurements", "summary"
            }:
                raise EvidenceWorkerError("CPU worker result JSON has a malformed top-level schema")
            return result
        except EvidenceTimeout:
            raise
        except EvidenceWorkerError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise EvidenceWorkerError(f"cannot load CPU worker result file: {exc}") from exc

    def analyze_frame(
        self,
        raw_path: Path,
        *,
        mode: str,
        roi: tuple[int, int, int, int],
        threshold: int,
        identity: dict[str, Any],
        timeout_s: float = 60.0,
    ) -> dict[str, Any]:
        """Analyze one frame in the isolated CPU worker before its deadline."""

        limit = _deadline(timeout_s)
        deadline = time.monotonic() + limit
        # Validate types and identity in the parent before starting an isolated process.
        normalized_identity = _validate_request(
            raw_path, mode=mode, roi=roi, threshold=threshold, identity=identity
        )
        if time.monotonic() >= deadline:
            self._terminate()
            raise EvidenceTimeout(f"CPU analysis exceeded its {limit:g}-second deadline")
        self._start(timeout_s=deadline - time.monotonic())
        self._job_counter += 1
        result_path = self._result_dir / f"result_{self._job_counter:08d}.json"
        request = {
            "raw_path": str(raw_path),
            "mode": mode,
            "roi": list(roi),
            "threshold": threshold,
            "identity": normalized_identity,
            "result_path": str(result_path),
        }
        try:
            self._connection.send(request)
        except (BrokenPipeError, EOFError, OSError) as exc:
            self._terminate()
            raise EvidenceWorkerError("cannot send request to CPU worker") from exc
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._connection.poll(remaining):
            self._terminate()
            raise EvidenceTimeout(f"CPU analysis exceeded its {limit:g}-second deadline")
        try:
            response = self._connection.recv()
        except (EOFError, OSError) as exc:
            self._terminate()
            raise EvidenceWorkerError("CPU worker exited before returning a result") from exc
        if time.monotonic() >= deadline:
            self._terminate()
            raise EvidenceTimeout(f"CPU analysis exceeded its {limit:g}-second deadline")
        if not isinstance(response, dict):
            self._terminate()
            raise EvidenceWorkerError("CPU worker returned a malformed response")
        if response.get("status") == "ok":
            try:
                if response.get("path") != str(result_path):
                    raise EvidenceWorkerError("CPU worker result path differs from this request")
                result = self._read_result_file(
                    result_path,
                    expected_bytes=response.get("byte_count"),
                    expected_sha256=response.get("sha256"),
                    deadline=deadline,
                    timeout_s=limit,
                )
                result_path.unlink(missing_ok=True)
                if time.monotonic() >= deadline:
                    self._terminate()
                    raise EvidenceTimeout(f"CPU analysis exceeded its {limit:g}-second deadline")
                return result
            except EvidenceTimeout:
                self._terminate()
                raise
            except EvidenceWorkerError:
                self._terminate()
                raise
        if response.get("status") == "error":
            raise EvidenceWorkerError(
                f"CPU worker raised {response.get('error_type')}: {response.get('message')}\n"
                f"{response.get('traceback', '')}"
            )
        self._terminate()
        raise EvidenceWorkerError("CPU worker returned an unknown response")

    def close(self) -> None:
        if self._closed:
            return
        if self._process is None:
            self._closed = True
            self._remove_result_directory()
            return
        try:
            if self._process.is_alive():
                self._connection.send(None)
                self._process.join(timeout=2.0)
            if self._process.is_alive():
                self._terminate()
                return
        except (BrokenPipeError, EOFError, OSError):
            self._terminate()
            return
        finally:
            if self._connection is not None:
                try:
                    self._connection.close()
                except OSError:
                    pass
        self._closed = True
        self._remove_result_directory()

    def __enter__(self) -> "BoundedCPUWorker":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def analyze_with_deadline(
    raw_path: Path,
    *,
    mode: str,
    roi: tuple[int, int, int, int],
    threshold: int,
    identity: dict[str, Any],
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Run one frame in a killable CPU-only subprocess with no automatic retry."""

    with BoundedCPUWorker() as worker:
        return worker.analyze_frame(
            raw_path,
            mode=mode,
            roi=roi,
            threshold=threshold,
            identity=identity,
            timeout_s=timeout_s,
        )


__all__ = [
    "BoundedCPUWorker",
    "EvidenceError",
    "EvidenceTimeout",
    "EvidenceWorkerError",
    "analyze_frame",
    "analyze_with_deadline",
    "canonical_payload",
    "canonical_sha256",
    "choose_indices",
]
