"""Bounded sensor-line exposure proposals. Contains no hardware or laser writes."""
from __future__ import annotations

from dataclasses import dataclass

from .continuous_timing import _uint


@dataclass(frozen=True)
class ExposureOperation:
    revision: int
    lines: int
    gain: int = 16


class ExposureController:
    laser_controlled = False

    def __init__(self, *, line_values: tuple[int, ...], initial_lines: int,
                 hysteresis_frames: int = 3, initial_revision: int = 0) -> None:
        if (not line_values or any(type(x) is not int or not 1 <= x <= 32765 for x in line_values)
                or tuple(sorted(set(line_values))) != tuple(line_values)
                or type(initial_lines) is not int or initial_lines not in line_values):
            raise ValueError("exposure values must be ordered distinct positive register line counts")
        if type(hysteresis_frames) is not int or hysteresis_frames < 1:
            raise ValueError("hysteresis_frames must be positive")
        _uint("initial_revision", initial_revision)
        self.line_values = tuple(line_values)
        self.lines = initial_lines
        self.revision = initial_revision
        self.hysteresis_frames = hysteresis_frames
        self.pending: ExposureOperation | None = None
        self.failure: str | None = None
        self.history: list[dict] = []
        self._direction = 0
        self._count = 0
        self._effective_after: int | None = None

    def propose(self, quality: str) -> ExposureOperation | None:
        if quality not in {"good", "weak", "saturated", "incomplete", "ambiguous", "unvalidated"}:
            raise ValueError("unknown frame quality")
        if self.pending is not None or self.failure is not None:
            return None
        direction = 1 if quality == "weak" else -1 if quality == "saturated" else 0
        self._count = self._count + 1 if direction and direction == self._direction else int(bool(direction))
        self._direction = direction
        if self._count < self.hysteresis_frames or not direction:
            return None
        self._count = 0
        index = self.line_values.index(self.lines) + direction
        if not 0 <= index < len(self.line_values):
            return None
        if self.revision == 0xFFFFFFFF:
            self.failure = "exposure revision exhausted"
            return None
        self.pending = ExposureOperation(self.revision + 1, self.line_values[index])
        return self.pending

    def acknowledge(self, revision: int, *, accepted: bool,
                    effective_after_request_id: int | None, error: str | None = None) -> None:
        if self.pending is None or revision != self.pending.revision:
            raise ValueError("acknowledgement does not match pending exposure")
        if type(accepted) is not bool:
            raise ValueError("accepted must be bool")
        if accepted:
            if effective_after_request_id is None:
                raise ValueError("acknowledgement needs an explicit effective-after request boundary")
            _uint("effective_after_request_id", effective_after_request_id)
            if self._effective_after is not None and effective_after_request_id <= self._effective_after:
                raise ValueError("effective-after request boundary must advance")
            self.lines, self.revision = self.pending.lines, self.pending.revision
            self._effective_after = effective_after_request_id
        else:
            self.failure = error or "exposure acknowledgement failed"
        self.history.append({"revision": revision, "lines": self.pending.lines, "gain": 16,
                             "accepted": accepted, "effective_after_request_id": effective_after_request_id,
                             "error": self.failure, "laser_changed": False})
        self.pending = None
        self._direction = self._count = 0

    def frame_eligible(self, *, request_id: int, exposure_revision: int) -> bool:
        _uint("request_id", request_id)
        _uint("exposure_revision", exposure_revision)
        return (self.failure is None and self.pending is None and exposure_revision == self.revision
                and (self._effective_after is None or request_id > self._effective_after))
