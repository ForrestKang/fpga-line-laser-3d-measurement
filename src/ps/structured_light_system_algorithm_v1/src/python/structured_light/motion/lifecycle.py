"""Cooperative worker cancellation and generation barriers for Stage B.

The barrier is intentionally transport-level: after invalidation, a worker
cannot issue a late motor register write or a new camera START even if its
future has not returned yet.  STOP/RESET/CLOSE remain available to the owner
for bounded cleanup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from threading import Condition, Lock, Thread, local
import time
from typing import Any, Callable

from concurrent.futures import Future


class GenerationRevoked(RuntimeError):
    """A stale worker attempted to use a revoked hardware generation."""


class HardwareOwnershipError(RuntimeError):
    """The process could not acquire the shared board owner lock."""


class HardwareProcessLock:
    """Reusable POSIX fcntl lock shared with the existing P2/preview owner.

    ``fcntl`` is imported only when ``acquire`` is called, so importing motion
    APIs on Windows remains offline-safe.  The lock path is intentionally the
    existing ``/tmp/sl_camera_preview.lock`` path; legacy writers that do not
    use that path cannot be coordinated by this lock and remain an explicit
    deployment limitation.
    """

    DEFAULT_PATH = Path("/tmp/sl_camera_preview.lock")

    def __init__(self, path: str | Path = DEFAULT_PATH, *, label: str = "stage_b") -> None:
        self.path = Path(path)
        self.label = str(label)
        self._handle: Any | None = None
        self._fcntl: Any | None = None
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self) -> "HardwareProcessLock":
        if self._acquired:
            return self
        if os.name != "posix":
            raise HardwareOwnershipError("hardware process ownership requires POSIX fcntl on the PYNQ target")
        try:
            import fcntl  # type: ignore
        except ImportError as exc:  # pragma: no cover - Windows host path
            raise HardwareOwnershipError("hardware process ownership requires POSIX fcntl") from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Linux protected_regular can reject root O_CREAT on an existing
        # user-owned file in sticky /tmp. Open that inode without O_CREAT;
        # never unlink/recreate it, which would split cooperative ownership.
        try:
            handle = self.path.open("r+")
        except FileNotFoundError:
            try:
                handle = self.path.open("x+")
            except FileExistsError:
                handle = self.path.open("r+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise HardwareOwnershipError(f"hardware owner lock is already held: {self.path}") from exc
        handle.write(f"{self.label} pid={os.getpid()}\n")
        handle.flush()
        self._handle = handle
        self._fcntl = fcntl
        self._acquired = True
        return self

    def release(self) -> None:
        if not self._acquired:
            return
        handle, fcntl = self._handle, self._fcntl
        self._handle = self._fcntl = None
        self._acquired = False
        try:
            if handle is not None and fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            if handle is not None:
                handle.close()

    def __enter__(self) -> "HardwareProcessLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


ProcessHardwareLock = HardwareProcessLock
hardware_owner_lock = HardwareProcessLock


class GenerationGate:
    """Serialize register calls with generation invalidation."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._generation = 0
        self._active = False
        self._local = local()
        self._inflight = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def begin(self) -> int:
        with self._lock:
            self._generation = (self._generation + 1) & 0xFFFF_FFFF
            if self._generation == 0:
                self._generation = 1
            self._active = True
            return self._generation

    def revoke(self, timeout_s: float | None = None) -> bool:
        with self._condition:
            self._active = False
            if timeout_s is None:
                while self._inflight:
                    self._condition.wait()
                return True
            deadline = time.monotonic() + max(0.0, float(timeout_s))
            while self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def check(self, generation: int) -> None:
        if generation != self._generation or not self._active:
            raise GenerationRevoked(
                f"hardware generation {generation} is no longer active"
            )

    def bind_worker(self, generation: int) -> None:
        self._local.generation = generation

    def unbind_worker(self) -> None:
        self._local.__dict__.pop("generation", None)

    def expected_generation(self) -> int:
        return int(getattr(self._local, "generation", self.generation))

    def call(self, generation: int, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        with self._condition:
            if generation != self._generation or not self._active:
                raise GenerationRevoked(
                    f"hardware generation {generation} is no longer active"
                )
            self._inflight += 1
        try:
            return operation(*args, **kwargs)
        finally:
            with self._condition:
                self._inflight -= 1
                if self._inflight == 0:
                    self._condition.notify_all()


class GatedMotorTransport:
    """read32/write32 adapter which rejects stale MFP1 writes."""

    def __init__(self, transport: Any, gate: GenerationGate) -> None:
        self._transport = transport
        self._gate = gate
        self.base_address = getattr(transport, "base_address", None)
        self.address_range = getattr(transport, "address_range", None)

    def read32(self, offset: int) -> int:
        return self._gate.call(self._gate.expected_generation(), self._transport.read32, offset)

    def write32(self, offset: int, value: int) -> None:
        return self._gate.call(self._gate.expected_generation(), self._transport.write32, offset, value)

    def close(self) -> None:
        closer = getattr(self._transport, "close", None)
        if callable(closer):
            closer()


class GatedCaptureTransport:
    """P2 transport adapter with cleanup-only STOP/RESET/CLOSE calls."""

    _gated_methods = frozenset({
        "arm_raw", "arm_profile", "start_capture", "poll", "receive",
        "read_diagnostics", "stream_requirements",
        "admit_pmc2_dma", "configure_pmc2", "claim_pmc2_packet",
    })
    _cleanup_methods = frozenset({"stop", "reset", "close"})

    def __init__(self, transport: Any, gate: GenerationGate) -> None:
        self._transport = transport
        self._gate = gate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._transport, name)

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        operation = getattr(self._transport, name)
        if name in self._cleanup_methods:
            return operation(*args, **kwargs)
        if name in self._gated_methods:
            return self._gate.call(self._gate.expected_generation(), operation, *args, **kwargs)
        return operation(*args, **kwargs)

    def arm_raw(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("arm_raw", *args, **kwargs)

    def arm_profile(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("arm_profile", *args, **kwargs)

    def start_capture(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("start_capture", *args, **kwargs)

    def poll(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("poll", *args, **kwargs)

    def receive(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("receive", *args, **kwargs)

    def read_diagnostics(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("read_diagnostics", *args, **kwargs)

    def stream_requirements(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("stream_requirements", *args, **kwargs)

    def admit_pmc2_dma(self,*args:Any,**kwargs:Any)->Any:
        return self._call('admit_pmc2_dma',*args,**kwargs)

    def configure_pmc2(self,*args:Any,**kwargs:Any)->Any:
        return self._call('configure_pmc2',*args,**kwargs)

    def claim_pmc2_packet(self,*args:Any,**kwargs:Any)->Any:
        return self._call('claim_pmc2_packet',*args,**kwargs)

    def stop(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("stop", *args, **kwargs)

    def reset(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("reset", *args, **kwargs)

    def close(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("close", *args, **kwargs)


@dataclass(slots=True)
class WorkerRecord:
    future: Future[Any]
    thread: Thread
    generation: int
    stage: str

    @property
    def alive(self) -> bool:
        return self.thread.is_alive()


__all__ = [
    "GenerationGate", "GenerationRevoked", "GatedCaptureTransport",
    "GatedMotorTransport", "HardwareOwnershipError", "HardwareProcessLock",
    "ProcessHardwareLock", "hardware_owner_lock", "WorkerRecord",
]
