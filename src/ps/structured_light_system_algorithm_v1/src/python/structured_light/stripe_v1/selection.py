"""Independent per-row candidate selection with explicit ambiguity rejection."""

from __future__ import annotations

from .quality import QualityAssessment, assess_candidate
from .types import CandidateFrame, SelectedProfile, SelectedRow, StripeConfig


class SelectionError(ValueError):
    """Raised when extraction output cannot be used with the supplied config."""


def _invalid_reason(assessments: tuple[QualityAssessment, ...]) -> str:
    for reason in ("overflow", "saturated_support", "roi_truncated", "candidate_width", "insufficient_contrast"):
        if any(item.reason == reason for item in assessments):
            return reason
    return "insufficient_contrast"


def select_candidates(frame: CandidateFrame, config: StripeConfig) -> SelectedProfile:
    """Select zero or one candidate on each row, with no continuity smoothing.

    A row is accepted only when it has exactly one eligible candidate. Multiple
    eligible candidates remain ambiguous regardless of their ordinal scores;
    brightness alone never makes one the presumed surface. The score stored for
    a single eligible candidate ranks its above-background contrast, scaled by
    local MAD when that mode is enabled. It has no probability interpretation.
    Missing rows and abrupt center changes do not constrain neighboring rows.
    """

    if not isinstance(frame, CandidateFrame):
        raise SelectionError("frame must be a CandidateFrame")
    if not isinstance(config, StripeConfig):
        raise SelectionError("config must be a StripeConfig")
    if frame.config_fingerprint != config.fingerprint:
        raise SelectionError("configuration fingerprint does not match extracted candidates")
    if len(frame.rows) != frame.height or any(row.row != y for y, row in enumerate(frame.rows)):
        raise SelectionError("candidate rows must cover the frame in source row order")

    selected_rows: list[SelectedRow] = []
    for row in frame.rows:
        if row.excluded:
            selected_rows.append(SelectedRow(row.row, None, "roi_excluded", None))
            continue
        if row.overflow:
            selected_rows.append(SelectedRow(row.row, None, "candidate_overflow", None, tuple(
                candidate.index for candidate in row.candidates
            )))
            continue
        if not row.candidates:
            selected_rows.append(SelectedRow(row.row, None, "no_support", None))
            continue

        assessments = tuple(assess_candidate(candidate, config) for candidate in row.candidates)
        eligible = [
            (candidate, assessment)
            for candidate, assessment in zip(row.candidates, assessments)
            if assessment.eligible
        ]
        if not eligible:
            selected_rows.append(
                SelectedRow(row.row, None, _invalid_reason(assessments), None, tuple(
                    candidate.index for candidate in row.candidates
                ))
            )
            continue
        if len(eligible) > 1:
            selected_rows.append(
                SelectedRow(
                    row.row,
                    None,
                    "ambiguous_candidates",
                    None,
                    tuple(candidate.index for candidate, _assessment in eligible),
                )
            )
            continue
        candidate, assessment = eligible[0]
        all_competitors = tuple(
            item.index for item in row.candidates if item.index != candidate.index
        )
        selected_rows.append(
            SelectedRow(row.row, candidate, None, assessment.score, all_competitors)
        )
    return SelectedProfile(frame.width, frame.height, tuple(selected_rows))


__all__ = ["SelectionError", "select_candidates"]
