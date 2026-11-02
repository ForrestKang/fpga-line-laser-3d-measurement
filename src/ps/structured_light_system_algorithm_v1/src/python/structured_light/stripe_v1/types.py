"""Small immutable records shared by the offline stripe pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


MAX_LOCAL_MAD_SCALE_Q8 = 65535


class StripeConfigError(ValueError):
    """Raised when a stripe configuration is outside its documented bounds."""


@dataclass(frozen=True, slots=True)
class StripeConfig:
    threshold_mode: str = "fixed"
    fixed_threshold: int = 30
    min_width: int = 1
    max_width: int = 32
    min_sum: int = 30
    min_peak: int = 15
    max_candidates_per_row: int = 8
    local_window: int = 31
    local_mad_scale_q8: int = 512
    local_threshold_floor: int = 4
    saturation_level: int = 255
    roi: tuple[int, int, int, int] | None = None

    def __post_init__(self) -> None:
        if type(self.threshold_mode) is not str or self.threshold_mode not in {"fixed", "local"}:
            raise StripeConfigError("threshold_mode must be 'fixed' or 'local'")
        integer_fields = (
            ("fixed_threshold", self.fixed_threshold, 0, 255),
            ("min_width", self.min_width, 1, None),
            ("max_width", self.max_width, 1, None),
            ("min_sum", self.min_sum, 0, None),
            ("min_peak", self.min_peak, 0, 255),
            ("max_candidates_per_row", self.max_candidates_per_row, 1, None),
            ("local_window", self.local_window, 3, None),
            ("local_mad_scale_q8", self.local_mad_scale_q8, 0, MAX_LOCAL_MAD_SCALE_Q8),
            ("local_threshold_floor", self.local_threshold_floor, 1, 255),
            ("saturation_level", self.saturation_level, 1, 255),
        )
        for name, value, minimum, maximum in integer_fields:
            if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
                bound = f"[{minimum}, {maximum}]" if maximum is not None else f">= {minimum}"
                raise StripeConfigError(f"{name} must be an integer in {bound}")
        if self.min_width > self.max_width:
            raise StripeConfigError("min_width cannot exceed max_width")
        if self.local_window % 2 == 0:
            raise StripeConfigError("local_window must be odd")
        if self.roi is not None:
            if not isinstance(self.roi, tuple) or len(self.roi) != 4:
                raise StripeConfigError("roi must be a (x0, y0, x1, y1) tuple")
            if any(type(value) is not int for value in self.roi):
                raise StripeConfigError("roi coordinates must be integers")
            x0, y0, x1, y1 = self.roi
            if not (0 <= x0 < x1 and 0 <= y0 < y1):
                raise StripeConfigError("roi must satisfy 0 <= x0 < x1 and 0 <= y0 < y1")

    @property
    def fingerprint(self) -> str:
        """Canonical SHA-256 of every setting that can affect extraction or selection."""

        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Candidate:
    row: int
    index: int
    start: int
    end: int
    s0: int
    s1: int
    center_q12: int
    peak: int
    width: int
    saturated_count: int
    background: int | None
    noise_mad: int | None
    contrast: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateRow:
    row: int
    candidates: tuple[Candidate, ...]
    overflow: bool
    excluded: bool = False


@dataclass(frozen=True, slots=True)
class CandidateFrame:
    width: int
    height: int
    threshold_mode: str
    config_fingerprint: str
    rows: tuple[CandidateRow, ...]


@dataclass(frozen=True, slots=True)
class SelectedRow:
    row: int
    candidate: Candidate | None
    reason: str | None
    score: int | None
    competitor_indices: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class SelectedProfile:
    width: int
    height: int
    rows: tuple[SelectedRow, ...]


__all__ = [
    "Candidate",
    "CandidateFrame",
    "CandidateRow",
    "SelectedProfile",
    "SelectedRow",
    "MAX_LOCAL_MAD_SCALE_Q8",
    "StripeConfig",
    "StripeConfigError",
]
