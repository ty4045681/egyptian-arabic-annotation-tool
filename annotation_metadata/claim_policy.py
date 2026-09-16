"""Claim ordering strategies. Identity, scope, and transactions stay outside."""

from __future__ import annotations

from annotation_metadata.features import claim_policy_name
from annotation_metadata.queries import order_sql


class ClaimPolicy:
    name = "source_confidence"

    def order_clause(self) -> str:
        return order_sql(self.name)


class SourceConfidencePolicy(ClaimPolicy):
    name = "source_confidence"


class FifoPolicy(ClaimPolicy):
    name = "fifo"


_POLICIES: dict[str, type[ClaimPolicy]] = {
    "source_confidence": SourceConfidencePolicy,
    "fifo": FifoPolicy,
}


def get_claim_policy(name: str | None = None) -> ClaimPolicy:
    key = name or claim_policy_name()
    cls = _POLICIES.get(key) or SourceConfidencePolicy
    return cls()
