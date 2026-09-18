"""Annotator-facing assignment views. Cross-check responses stay blind."""

from __future__ import annotations

from annotation_quality.repository import SEGMENT_PROTECTED_KEYS


_CROSS_CHECK_SEGMENT_KEYS = (
    "id", "start", "end", "duration", "asr_text", "text", "exclude_from_training",
)

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


def _sanitize_cross_check_segments(segments: list | None) -> list:
    clean = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        item = {key: segment.get(key) for key in _CROSS_CHECK_SEGMENT_KEYS}
        item["text"] = "" if item.get("text") is None else item.get("text")
        item["asr_text"] = item.get("asr_text") or ""
        item["exclude_from_training"] = bool(item.get("exclude_from_training"))
        for key in list(item):
            if key in SEGMENT_PROTECTED_KEYS and key not in _CROSS_CHECK_SEGMENT_KEYS:
                item.pop(key, None)
        clean.append(item)
    return clean


def apply_assignment_visibility(payload: dict) -> dict:
    """Drop original-result fields from a cross-check assignment body."""
    if payload.get("mode") != "cross_check":
        return payload
    for key in _LEAK_KEYS:
        payload.pop(key, None)
    payload["segments"] = _sanitize_cross_check_segments(payload.get("segments"))
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
