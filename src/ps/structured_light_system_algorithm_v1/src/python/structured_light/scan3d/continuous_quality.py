"""Profile quality statistics. Saturation is observed at DN >= 250, never inferred."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from .continuous_timing import _finite


@dataclass(frozen=True)
class QualityThresholds:
    min_intensity: float = 60
    max_width_px: float = 32
    min_completeness: float = 0.8
    max_saturation_fraction: float = 0.01
    max_ambiguous_fraction: float = 0.05
    max_saturated_row_fraction: float = 0.05

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            _finite(name, getattr(self, name))
        if not 0 <= self.min_intensity <= 255 or self.max_width_px <= 0:
            raise ValueError("invalid intensity or width threshold")
        for name in ("min_completeness", "max_saturation_fraction", "max_ambiguous_fraction", "max_saturated_row_fraction"):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must be in 0..1")


@dataclass(frozen=True)
class FrameQuality:
    status: str
    completeness: float
    median_intensity: float | None
    median_width_px: float | None
    saturation_fraction: float | None
    ambiguous_fraction: float | None
    valid_rows: int
    expected_rows: int
    rejection_counts: dict[str, int]
    raw_observed: bool
    saturated_rows: int | None = None


def _quality_status(*, completeness, median_i, median_w, saturation, saturated_rows,
                    expected_rows, ambiguous, any_wide, raw_peak_median, thresholds):
    if saturation is not None and (saturation > thresholds.max_saturation_fraction
                                  or saturated_rows / expected_rows > thresholds.max_saturated_row_fraction):
        return 'saturated'
    if ((ambiguous is not None and ambiguous > thresholds.max_ambiguous_fraction) or any_wide):
        return 'ambiguous'
    if median_i is None and raw_peak_median is not None and raw_peak_median < thresholds.min_intensity:
        return 'weak'
    if completeness < thresholds.min_completeness:
        return 'incomplete'
    if median_i is not None and median_i < thresholds.min_intensity:
        return 'weak'
    if any(v is None for v in (saturation,median_i,median_w,ambiguous)):
        return 'unvalidated'
    return 'good'


def assess_quality(raw: np.ndarray | None, *, row_valid: Sequence[bool],
                   widths_px: Sequence[float] | None = None,
                   intensities: Sequence[float] | None = None,
                   candidate_counts: Sequence[int] | None = None,
                   rejection_counts: Mapping[str, int] | None = None,
                   thresholds: QualityThresholds = QualityThresholds()) -> FrameQuality:
    """Rows refer to the explicit ROI, not every sensor row by default.

    Raw pixels must be that same ROI. Width/intensity/candidate statistics may
    come from PL, but they cannot establish absence of saturated pixels.
    Unknown statistics stay None and preclude a ``good`` status.
    """
    thresholds.__post_init__()
    valid = np.asarray(row_valid)
    if valid.ndim != 1 or valid.dtype.kind != "b" or len(valid) == 0:
        raise ValueError("row_valid must be a nonempty boolean vector")
    n = len(valid)
    counts = dict(rejection_counts or {})
    if any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in counts.items()):
        raise ValueError("rejection counts must be nonnegative integers")

    def stats(values: Sequence[float] | None, name: str, maximum: float | None = None) -> np.ndarray | None:
        if values is None:
            return None
        result = np.asarray(values)
        if (result.shape != (n,) or result.dtype.kind not in "iuf" or not np.isfinite(result).all()
                or np.any(result < 0) or (maximum is not None and np.any(result > maximum))):
            raise ValueError(f"invalid {name}")
        return result

    widths = stats(widths_px, "widths")
    intensity = stats(intensities, "intensities", 255)
    candidates = stats(candidate_counts, "candidate counts")
    if candidates is not None and np.any(candidates != np.floor(candidates)):
        raise ValueError("candidate counts must be integers")
    saturation = None
    saturated_rows = None
    if raw is not None:
        raw = np.asarray(raw)
        if raw.ndim != 2 or raw.shape[0] != n or raw.size == 0 or raw.dtype != np.uint8:
            raise ValueError("raw must be the same nonempty uint8 ROI")
        saturation = float(np.count_nonzero(raw >= 250) / raw.size)
        saturated_rows = int(np.count_nonzero(np.any(raw >= 250, axis=1)))
    completeness = float(np.count_nonzero(valid) / n)
    median_i = float(np.median(intensity[valid])) if intensity is not None and valid.any() else None
    median_w = float(np.median(widths[valid])) if widths is not None and valid.any() else None
    ambiguous = float(np.count_nonzero(candidates > 1) / n) if candidates is not None else None
    status = _quality_status(completeness=completeness,median_i=median_i,median_w=median_w,
        saturation=saturation,saturated_rows=saturated_rows,expected_rows=n,ambiguous=ambiguous,
        any_wide=widths is not None and bool(np.any(widths[valid] > thresholds.max_width_px)),
        raw_peak_median=float(np.median(np.max(raw,axis=1))) if raw is not None and median_i is None else None,thresholds=thresholds)
    return FrameQuality(status, completeness, median_i, median_w, saturation, ambiguous,
                        int(valid.sum()), n, counts, raw is not None, saturated_rows)


def assess_observed_quality(*, row_valid, widths_px, intensities, candidate_counts,
                            raw_row_peaks, saturation_pixels, saturated_rows, roi_width,
                            rejection_counts=None, thresholds=QualityThresholds()):
    """Observed same-ROI PL counters/vectors; callers admit their provenance.

    This shares scalar policy with ``assess_quality`` without manufacturing a
    Raw image. Wire consistency is not a proof that counters match real pixels;
    the explicit same-frame Raw oracle establishes that QA boundary.
    """
    q = assess_quality(None,row_valid=row_valid,widths_px=widths_px,intensities=intensities,
        candidate_counts=candidate_counts,rejection_counts=rejection_counts,thresholds=thresholds)
    n = q.expected_rows
    peaks = np.asarray(raw_row_peaks)
    if (peaks.shape != (n,) or peaks.dtype.kind not in 'iu' or np.any(peaks<0) or np.any(peaks>255)
            or type(roi_width) is not int or roi_width < 1
            or type(saturation_pixels) is not int or type(saturated_rows) is not int
            or saturated_rows != int(np.count_nonzero(peaks>=250))
            or not saturated_rows <= saturation_pixels <= saturated_rows*roi_width):
        raise ValueError('invalid observed ROI saturation/peak statistics')
    valid = np.asarray(row_valid)
    saturation = saturation_pixels/(n*roi_width)
    status = _quality_status(completeness=q.completeness,median_i=q.median_intensity,median_w=q.median_width_px,
        saturation=saturation,saturated_rows=saturated_rows,expected_rows=n,ambiguous=q.ambiguous_fraction,
        any_wide=bool(np.any(np.asarray(widths_px)[valid]>thresholds.max_width_px)),
        raw_peak_median=float(np.median(peaks)),thresholds=thresholds)
    return FrameQuality(status,q.completeness,q.median_intensity,q.median_width_px,saturation,
        q.ambiguous_fraction,q.valid_rows,n,q.rejection_counts,True,saturated_rows)
