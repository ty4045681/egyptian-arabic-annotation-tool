"""Cross-annotation quality: independent re-annotation, comparison, and adjudication contracts."""

from __future__ import annotations

from annotation_quality.contracts import (
    COMPARISON_VERSION,
    WORD_DIFFERENCE_THRESHOLD_BPS,
    CrossCheckDecision,
    CrossCheckReasonCode,
    CrossCheckState,
    VersionPurpose,
)

__all__ = [
    "COMPARISON_VERSION",
    "WORD_DIFFERENCE_THRESHOLD_BPS",
    "CrossCheckDecision",
    "CrossCheckReasonCode",
    "CrossCheckState",
    "VersionPurpose",
]
