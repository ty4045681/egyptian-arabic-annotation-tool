"""Cross-annotation quality: independent re-annotation, comparison, and adjudication.

This package holds request/response contracts, database access, claiming,
comparison, and admin APIs. P0 ships schema and models only.
"""

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
