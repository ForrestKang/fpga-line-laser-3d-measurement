"""Conservative, row-local segmentation of raw gray8 stripe candidates."""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .types import Candidate, CandidateFrame, CandidateRow, StripeConfig


Q12 = 1 << 12
S0_MAX = (1 << 18) - 1
S1_MAX = (1 << 27) - 1
XC_MAX = (1 << 22) - 1


class CandidateError(ValueError):
    """Raised when the image is not a two-dimensional gray8 array."""


def _floor_median_axis1(values: np.ndarray) -> np.ndarray:
    count = values.shape[1]
    lower = (count - 1) // 2
    upper = count // 2
    partitioned = np.partition(values, (lower, upper), axis=1)
    lower_values = partitioned[:, lower].astype(np.int64)
    upper_values = partitioned[:, upper].astype(np.int64)
    return (lower_values + upper_values) // 2


def _local_maps(row: np.ndarray, config: StripeConfig) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    pad = config.local_window // 2
    padded = np.pad(row.astype(np.int16), (pad, pad), mode="edge")
    windows = sliding_window_view(padded, config.local_window)
    background = _floor_median_axis1(windows)
    deviations = np.abs(windows.astype(np.int64) - background[:, None])
    noise_mad = _floor_median_axis1(deviations)
    scaled_noise = (
        noise_mad.astype(np.int64) * np.int64(config.local_mad_scale_q8) + np.int64(255)
    ) // np.int64(256)
    margin = np.maximum(np.int64(config.local_threshold_floor), scaled_noise)
    thresholds = np.minimum(np.int64(255), background + margin).astype(np.int16)
    return background, noise_mad, thresholds


def _runs(support: np.ndarray) -> tuple[tuple[int, int], ...]:
    positions = np.flatnonzero(support)
    if positions.size == 0:
        return ()
    breaks = np.flatnonzero(np.diff(positions) > 1)
    starts = np.concatenate((positions[:1], positions[breaks + 1]))
    ends = np.concatenate((positions[breaks], positions[-1:]))
    return tuple((int(start), int(end)) for start, end in zip(starts, ends))


def _validate_gray(gray: np.ndarray) -> np.ndarray:
    try:
        image = np.asarray(gray)
    except Exception as exc:  # pragma: no cover - unusual array protocol failure
        raise CandidateError("gray must be a 2D uint8 array") from exc
    if image.ndim != 2 or image.shape[0] <= 0 or image.shape[1] <= 0:
        raise CandidateError("gray must be a non-empty 2D uint8 array")
    if image.dtype != np.uint8:
        raise CandidateError("gray dtype must be uint8")
    return image


def extract_candidates(gray: np.ndarray, config: StripeConfig) -> CandidateFrame:
    """Return every contiguous above-threshold run in original image coordinates.

    ``fixed`` mode uses the same ``pixel - threshold`` weights as the frozen
    ``gray_reference`` implementation. ``local`` mode estimates a median and
    median absolute deviation independently in each horizontal neighborhood.
    It never closes gaps or interpolates missing pixels.
    """

    if not isinstance(config, StripeConfig):
        raise CandidateError("config must be a StripeConfig")
    image = _validate_gray(gray)
    height, width = image.shape
    x0, y0, x1, y1 = config.roi or (0, 0, width, height)
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise CandidateError("roi must lie within the gray image")

    rows: list[CandidateRow] = []
    for y in range(height):
        if y < y0 or y >= y1:
            rows.append(CandidateRow(y, (), False, True))
            continue
        source_row = image[y]
        if config.threshold_mode == "fixed":
            background_map: np.ndarray | None = None
            noise_map: np.ndarray | None = None
            threshold_map = np.full(width, config.fixed_threshold, dtype=np.int16)
        else:
            background_map, noise_map, threshold_map = _local_maps(source_row, config)

        weights = source_row.astype(np.int16) - threshold_map
        weights[weights <= 0] = 0
        roi_weights = weights[x0:x1]
        runs = _runs(roi_weights > 0)
        candidates: list[Candidate] = []
        for index, (local_start, local_end) in enumerate(runs):
            start = x0 + local_start
            end = x0 + local_end
            width_px = end - start + 1
            support_weights = weights[start : end + 1].astype(np.int64)
            support_pixels = source_row[start : end + 1]
            s0 = int(support_weights.sum())
            s1 = int((support_weights * np.arange(start, end + 1, dtype=np.int64)).sum())
            center_q12 = (s1 << 12) // s0
            peak = int(support_weights.max())
            saturated_count = int(np.count_nonzero(support_pixels >= config.saturation_level))
            truncated = start == x0 or end == x1 - 1
            overflow = s0 > S0_MAX or s1 > S1_MAX or center_q12 > XC_MAX
            if background_map is None or noise_map is None:
                background = None
                noise_mad = None
                contrast = peak
            else:
                background = int(np.median(background_map[start : end + 1]))
                noise_mad = int(np.median(noise_map[start : end + 1]))
                contrast = max(0, int(support_pixels.max()) - background)

            reasons: list[str] = []
            if width_px < config.min_width or width_px > config.max_width:
                reasons.append("candidate_width")
            if s0 < config.min_sum or peak < config.min_peak:
                reasons.append("insufficient_contrast")
            if saturated_count:
                reasons.append("saturated_support")
            if truncated:
                reasons.append("roi_truncated")
            if overflow:
                reasons.append("overflow")
            candidates.append(
                Candidate(
                    row=y,
                    index=index,
                    start=start,
                    end=end,
                    s0=s0,
                    s1=s1,
                    center_q12=center_q12,
                    peak=peak,
                    width=width_px,
                    saturated_count=saturated_count,
                    background=background,
                    noise_mad=noise_mad,
                    contrast=contrast,
                    reasons=tuple(reasons),
                )
            )
        rows.append(
            CandidateRow(
                row=y,
                candidates=tuple(candidates),
                overflow=len(candidates) > config.max_candidates_per_row,
            )
        )
    return CandidateFrame(width, height, config.threshold_mode, config.fingerprint, tuple(rows))


__all__ = ["CandidateError", "extract_candidates"]
