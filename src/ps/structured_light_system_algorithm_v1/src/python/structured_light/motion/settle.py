"""Measured settling estimation for Stage B.

The estimator consumes grayscale frames and never substitutes a fixed sleep
for an unknown settling time.  ``run_diagnostic_settle`` is the explicit
measurement path used by the board CLI; all timing is sampled from a monotonic
clock and its schedule is finite.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import math
import time
from typing import Any, Callable, Iterable, Sequence

from .settle_protocols import SettleProtocol, get_settle_protocol

SAMPLE_SCHEDULE_MS = (100, 250, 500, 1000, 2000, 2500, 3000)


@dataclass(frozen=True, slots=True)
class SettleConfig:
    baseline_frames: int = 20
    p99_quantile: float = 0.99
    max_phase_shift_px: float = 0.25
    phase_multiplier: float = 3.0
    diff_multiplier: float = 1.5
    diff_offset_dn: float = 2.0
    min_texture_std: float = 1.0
    min_gradient_energy: float = 0.05
    consecutive_stable: int = 1
    protocol_id: str = "legacy_3s_v1"

    def __post_init__(self) -> None:
        if self.baseline_frames < 2:
            raise ValueError("baseline_frames must be at least 2")
        if not 0.0 < self.p99_quantile <= 1.0:
            raise ValueError("p99_quantile must be in (0,1]")
        for name in (
            "max_phase_shift_px", "phase_multiplier", "diff_multiplier",
            "diff_offset_dn", "min_texture_std", "min_gradient_energy",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.consecutive_stable < 1:
            raise ValueError("consecutive_stable must be positive")
        if not isinstance(self.protocol_id, str):
            raise ValueError("protocol_id must be a known settle protocol string")
        get_settle_protocol(self.protocol_id)

    @property
    def protocol(self) -> SettleProtocol:
        """Return the immutable canonical protocol selected by this config."""

        return get_settle_protocol(self.protocol_id)

    @property
    def settle_protocol(self) -> SettleProtocol:
        """Descriptive alias for :attr:`protocol`."""

        return self.protocol

    @property
    def protocol_descriptor(self) -> SettleProtocol:
        """Explicit descriptor spelling used by persisted evidence callers."""

        return self.protocol

    def bound_hash(
        self,
        *,
        target: str,
        installation: str,
        roi: tuple[int, int, int, int],
        mode: str,
        profile: str,
    ) -> str:
        config_payload = asdict(self)
        # A schema-1 legacy receipt predates protocol_id.  Keep its exact
        # canonical payload so the established legacy hash remains stable.
        if self.protocol_id == "legacy_3s_v1":
            config_payload.pop("protocol_id", None)
        payload = {
            "target": target,
            "installation": installation,
            "roi": list(roi),
            "mode": mode,
            "profile": profile,
            "config": config_payload,
        }
        if self.protocol_id != "legacy_3s_v1":
            payload["settle_protocol"] = self.protocol.as_dict()
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SettleEstimate:
    settled: bool
    elapsed_s: float
    samples: int
    baseline_phase_p99: float
    baseline_diff_p99: float
    phase_shift_px: float | None
    difference_dn: float | None
    phase_threshold_px: float
    difference_threshold_dn: float
    reason: str
    config_hash: str
    roi: tuple[int, int, int, int]

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["roi"] = list(self.roi)
        return value


def _np():
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - exercised only without optional deps
        raise RuntimeError("settling estimation requires numpy") from exc
    return np


def _array(frame: Any):
    np = _np()
    value = np.asarray(frame)
    if value.ndim != 2:
        raise ValueError("settling frames must be two-dimensional grayscale arrays")
    if value.size == 0:
        raise ValueError("settling frame cannot be empty")
    return value.astype(np.float64, copy=False)


def _crop(frame: Any, roi: tuple[int, int, int, int]):
    value = _array(frame)
    if len(roi) != 4:
        raise ValueError("ROI must be (x0,y0,x1,y1)")
    x0, y0, x1, y1 = (int(item) for item in roi)
    if not (0 <= x0 < x1 <= value.shape[1] and 0 <= y0 < y1 <= value.shape[0]):
        raise ValueError("ROI must lie inside every frame")
    return value[y0:y1, x0:x1]


def _parabolic_offset(left: float, peak: float, right: float) -> float:
    denominator = left - 2.0 * peak + right
    if denominator == 0.0:
        return 0.0
    return float(max(-0.5, min(0.5, 0.5 * (left - right) / denominator)))


def phase_correlation_shift(reference: Any, current: Any) -> tuple[float, float]:
    """Estimate (x, y) translation using normalized FFT phase correlation."""

    np = _np()
    first = _array(reference)
    second = _array(current)
    if first.shape != second.shape:
        raise ValueError("phase correlation requires equal frame shapes")
    first = first - float(first.mean())
    second = second - float(second.mean())
    first_fft = np.fft.fft2(first)
    second_fft = np.fft.fft2(second)
    # The returned vector is the displacement from ``reference`` to
    # ``current`` (a positive x value means current moved right).
    cross = second_fft * np.conj(first_fft)
    magnitude = np.abs(cross)
    cross = np.divide(cross, magnitude, out=np.zeros_like(cross), where=magnitude > 1e-12)
    correlation = np.fft.ifft2(cross).real
    peak_y, peak_x = (int(item) for item in np.unravel_index(int(np.argmax(correlation)), correlation.shape))
    rows, columns = correlation.shape
    shift_y = float(peak_y if peak_y <= rows // 2 else peak_y - rows)
    shift_x = float(peak_x if peak_x <= columns // 2 else peak_x - columns)
    shift_y += _parabolic_offset(
        float(correlation[(peak_y - 1) % rows, peak_x]),
        float(correlation[peak_y, peak_x]),
        float(correlation[(peak_y + 1) % rows, peak_x]),
    )
    shift_x += _parabolic_offset(
        float(correlation[peak_y, (peak_x - 1) % columns]),
        float(correlation[peak_y, peak_x]),
        float(correlation[peak_y, (peak_x + 1) % columns]),
    )
    return shift_x, shift_y


def brightness_normalized_difference(reference: Any, current: Any) -> float:
    """Return mean absolute difference after affine brightness normalization."""

    np = _np()
    first = _array(reference)
    second = _array(current)
    if first.shape != second.shape:
        raise ValueError("brightness comparison requires equal frame shapes")
    first_mean, second_mean = float(first.mean()), float(second.mean())
    first_std, second_std = float(first.std()), float(second.std())
    if second_std > 1e-9:
        normalized = (second - second_mean) * (first_std / second_std) + first_mean
    else:
        normalized = second - second_mean + first_mean
    return float(np.mean(np.abs(first - normalized)))


def _texture_ok(frame: Any, config: SettleConfig) -> bool:
    np = _np()
    value = _array(frame)
    if float(value.std()) < config.min_texture_std:
        return False
    gradient_x = np.diff(value, axis=1)
    gradient_y = np.diff(value, axis=0)
    energy = float((np.mean(np.square(gradient_x)) + np.mean(np.square(gradient_y))) / 2.0)
    return energy >= config.min_gradient_energy


def _bound_hash(config: SettleConfig, roi: tuple[int, int, int, int], config_hash: str | None) -> str:
    if config_hash:
        return str(config_hash)
    return hashlib.sha256(
        json.dumps({"roi": list(roi), "config": asdict(config)}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def estimate_settle(
    frames: Sequence[Any] | Iterable[Any],
    *,
    roi: tuple[int, int, int, int],
    config: SettleConfig | None = None,
    config_hash: str | None = None,
    elapsed_s: float | None = None,
) -> SettleEstimate:
    """Estimate settling from a finite baseline and subsequent samples.

    The first ``baseline_frames`` frames establish both baseline noise metrics
    and texture.  A subsequent frame is accepted only when phase and
    brightness metrics are under their measured thresholds.
    """

    cfg = config or SettleConfig()
    values = list(frames)
    bound = _bound_hash(cfg, roi, config_hash)
    if len(values) < cfg.baseline_frames:
        return SettleEstimate(False, float(elapsed_s or 0.0), len(values), 0.0, 0.0, None, None, cfg.max_phase_shift_px, cfg.diff_offset_dn, "insufficient_baseline", bound, tuple(roi))
    baseline = [_crop(frame, roi) for frame in values[: cfg.baseline_frames]]
    if not _texture_ok(baseline[0], cfg):
        return SettleEstimate(False, float(elapsed_s or 0.0), len(values), 0.0, 0.0, None, None, cfg.max_phase_shift_px, cfg.diff_offset_dn, "low_texture", bound, tuple(roi))
    shifts = [math.hypot(*phase_correlation_shift(baseline[index - 1], baseline[index])) for index in range(1, len(baseline))]
    diffs = [brightness_normalized_difference(baseline[index - 1], baseline[index]) for index in range(1, len(baseline))]
    np = _np()
    phase_p99 = float(np.quantile(np.asarray(shifts), cfg.p99_quantile)) if shifts else 0.0
    diff_p99 = float(np.quantile(np.asarray(diffs), cfg.p99_quantile)) if diffs else 0.0
    phase_threshold = max(cfg.max_phase_shift_px, cfg.phase_multiplier * phase_p99)
    diff_threshold = cfg.diff_multiplier * diff_p99 + cfg.diff_offset_dn
    if len(values) == cfg.baseline_frames:
        return SettleEstimate(False, float(elapsed_s or 0.0), len(values), phase_p99, diff_p99, None, None, phase_threshold, diff_threshold, "awaiting_post_move_sample", bound, tuple(roi))
    reference = baseline[-1]
    stable = 0
    last_phase: float | None = None
    last_diff: float | None = None
    for frame in values[cfg.baseline_frames :]:
        current = _crop(frame, roi)
        x_shift, y_shift = phase_correlation_shift(reference, current)
        last_phase = math.hypot(x_shift, y_shift)
        last_diff = brightness_normalized_difference(reference, current)
        if last_phase <= phase_threshold and last_diff <= diff_threshold:
            stable += 1
            if stable >= cfg.consecutive_stable:
                return SettleEstimate(True, float(elapsed_s or 0.0), len(values), phase_p99, diff_p99, last_phase, last_diff, phase_threshold, diff_threshold, "stable", bound, tuple(roi))
        else:
            stable = 0
    return SettleEstimate(False, float(elapsed_s or 0.0), len(values), phase_p99, diff_p99, last_phase, last_diff, phase_threshold, diff_threshold, "unstable", bound, tuple(roi))


def diagnostic_settle_schedule(*, positions: int = 10, config: SettleConfig | None = None) -> tuple[int, ...]:
    """Return the finite, explicit diagnostic schedule in milliseconds."""

    if isinstance(positions, bool) or not isinstance(positions, int) or positions < 1:
        raise ValueError("positions must be a positive finite count")
    return (config or SettleConfig()).protocol.schedule_ms


def run_diagnostic_settle(
    sampler: Callable[[int, int], Sequence[Any]],
    *,
    positions: int = 10,
    config: SettleConfig | None = None,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Collect finite settle observations at actual monotonic schedule times.

    ``sampler(position_index, scheduled_ms)`` must return measured grayscale
    frames.  Omitting it is intentionally impossible: an unknown real settle
    time must remain unknown rather than becoming an arbitrary delay.
    """

    if not callable(sampler):
        raise ValueError("a measured sampler is required; fixed waits are not a settle estimate")
    schedule = diagnostic_settle_schedule(positions=positions, config=config)
    clock = monotonic or time.monotonic
    sleeper = sleep or time.sleep
    records: list[dict[str, Any]] = []
    for position in range(positions):
        start = clock()
        for scheduled_ms in schedule:
            deadline = start + scheduled_ms / 1000.0
            while clock() < deadline:
                sleeper(min(0.005, max(0.0, deadline - clock())))
            actual = max(0.0, clock() - start)
            measured = sampler(position, scheduled_ms)
            records.append({
                "position_index": position,
                "scheduled_ms": scheduled_ms,
                "actual_elapsed_s": actual,
                "frame_count": len(measured),
                "frames": measured,
            })
    return tuple(records)


estimate_settling = estimate_settle


__all__ = [
    "SAMPLE_SCHEDULE_MS", "SettleConfig", "SettleEstimate",
    "brightness_normalized_difference", "diagnostic_settle_schedule",
    "estimate_settle", "estimate_settling", "get_settle_protocol",
    "phase_correlation_shift", "run_diagnostic_settle", "SettleProtocol",
]
