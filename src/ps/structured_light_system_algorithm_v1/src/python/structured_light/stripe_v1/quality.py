"""Ordinal candidate quality scoring for conservative row selection."""

from __future__ import annotations

from dataclasses import dataclass

from .types import Candidate, StripeConfig


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    eligible: bool
    score: int | None
    reason: str | None


_REASON_PRIORITY = (
    "overflow",
    "saturated_support",
    "roi_truncated",
    "candidate_width",
    "insufficient_contrast",
)


def assess_candidate(candidate: Candidate, config: StripeConfig) -> QualityAssessment:
    """Return an ordinal quality score; the value is never a probability."""

    for reason in _REASON_PRIORITY:
        if reason in candidate.reasons:
            return QualityAssessment(False, None, reason)
    if config.threshold_mode == "local":
        noise = max(1, candidate.noise_mad or 0)
        score = candidate.contrast * 256 // noise
    else:
        score = candidate.contrast
    return QualityAssessment(True, int(score), None)


__all__ = ["QualityAssessment", "assess_candidate"]
