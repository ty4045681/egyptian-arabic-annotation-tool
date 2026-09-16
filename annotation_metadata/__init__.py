"""Scene provenance, source confidence, reviews, and claim policy.

Source evidence, model predictions, and human reviews are stored separately.
None of the three may overwrite the others.
"""

from __future__ import annotations

SCHEMA_VERSION = 1
CONTRACT_VERSION = 1
CLAIM_POLICY_SOURCE_CONFIDENCE = "source_confidence"
CLAIM_POLICY_FIFO = "fifo"

__all__ = [
    "SCHEMA_VERSION",
    "CONTRACT_VERSION",
    "CLAIM_POLICY_SOURCE_CONFIDENCE",
    "CLAIM_POLICY_FIFO",
]
