"""Annotator-facing assignment views. Cross-check responses stay blind."""

from __future__ import annotations


_LEAK_KEYS = (
    "original_version_id",
    "original_annotator",
    "original_annotator_id",
    "original_review",
    "original_review_id",
    "original_text",
    "comparison",
    "decision",
    "final_version_id",
    "credited_annotator_id",
    "diff_ops",
    "word_difference_rate",
    "reason_codes",
)


def apply_assignment_visibility(payload: dict) -> dict:
    """Drop original-result fields from a cross-check assignment body."""
    if payload.get("mode") != "cross_check":
        return payload
    for key in _LEAK_KEYS:
        payload.pop(key, None)
    payload["mode"] = "cross_check"
    info = payload.get("cross_check")
    if not isinstance(info, dict):
        payload["cross_check"] = {
            "round_id": str(payload.get("round_id") or ""),
            "state": "in_progress",
        }
    else:
        payload["cross_check"] = {
            "round_id": str(info.get("round_id") or ""),
            "state": info.get("state") or "in_progress",
        }
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        metadata["reference_review"] = None
        metadata["prediction"] = None
        for key in _LEAK_KEYS:
            metadata.pop(key, None)
    return payload
