"""Finite scan and bounded transport policy, with no automatic fault recovery."""
from __future__ import annotations

import math
from queue import Empty, Full, Queue
from typing import Generic, TypeVar

from .continuous_timing import _finite

T = TypeVar("T")


class LosslessFrameQueue(Generic[T]):
    """Fail explicitly on pressure. Caller must stop/control acquisition on error.

    This queue never discards an older measurement frame to fit a new one.
    Rendering may use a separate latest-frame channel; measurements may not.
    """
    def __init__(self, *, capacity: int) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("queue capacity must be positive")
        self._queue: Queue[T] = Queue(capacity)
        self.overflow_count = 0

    def put(self, item: T) -> None:
        try:
            self._queue.put_nowait(item)
        except Full as exc:
            self.overflow_count += 1
            raise BufferError("measurement queue full; acquisition must stop") from exc

    def get(self) -> T:
        try:
            return self._queue.get_nowait()
        except Empty as exc:
            raise IndexError("measurement queue empty") from exc


class ContinuousScanPolicy:
    """One nominal 3200-command revolution plus at most one quality rescan.

    Coverage counts only timing/geometry/quality-valid profiles supplied by the
    caller. Emitted commands are not encoder positions. Bad observed bins and
    never-observed bins remain distinguishable in the report.
    """
    pulses_per_rotation = 3200
    max_rescans = 1

    def __init__(self, *, angle_bins: int = 36) -> None:
        if type(angle_bins) is not int or not 1 <= angle_bins <= 3200:
            raise ValueError("angle_bins must be in 1..3200")
        self.angle_bins = angle_bins
        self.state = "ready"
        self.error: str | None = None
        self.rescan_count = 0
        self.total_pulses = 0
        self.good_angle_bins: set[int] = set()
        self.bad_angle_bins: set[int] = set()
        self.valid_profile_count = 0
        self.invalid_profile_count = 0

    def start(self) -> int:
        if self.state != "ready":
            raise RuntimeError("scan is not ready; no automatic reset or rearm")
        self.state = "running"
        return self.pulses_per_rotation

    def record_profile(self, *, angle_rad: float, valid: bool) -> None:
        if self.state not in {"running", "rescan"}:
            raise RuntimeError("scan is not acquiring")
        angle = _finite("angle_rad", angle_rad)
        if type(valid) is not bool:
            raise ValueError("valid must be bool")
        index = min(self.angle_bins - 1, int((angle % math.tau) / math.tau * self.angle_bins))
        if valid:
            self.good_angle_bins.add(index)
            self.valid_profile_count += 1
        else:
            self.bad_angle_bins.add(index)
            self.invalid_profile_count += 1

    def finish_rotation(self, *, emitted_pulses: int) -> tuple[int, ...]:
        if self.state not in {"running", "rescan"}:
            raise RuntimeError("no active rotation")
        if type(emitted_pulses) is not int or emitted_pulses != self.pulses_per_rotation:
            self.fault("rotation command count differs from finite 3200-pulse budget")
            raise ValueError(self.error)
        self.total_pulses += emitted_pulses
        missing = tuple(sorted(set(range(self.angle_bins)) - self.good_angle_bins))
        if not missing:
            self.state = "complete"
        elif self.rescan_count < self.max_rescans:
            self.rescan_count += 1
            self.state = "rescan"
        else:
            self.fault("quality coverage incomplete after one rescan")
        return missing

    def fault(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("fault reason is required")
        self.error = reason
        self.state = "failed"
