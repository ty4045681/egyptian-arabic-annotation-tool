"""Annotator-facing assignment views and admin/self cross-check payloads."""

from __future__ import annotations

from annotation_quality.contracts import (
    COMPARISON_VERSION,
    OPEN_ROUND_STATES,
    WORD_DIFFERENCE_THRESHOLD_BPS,
    CrossCheckDetailView,
    CrossCheckListItem,
    CrossCheckMineItem,
    CrossCheckSettingsView,
    CrossCheckSubmissionView,
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


def _iso(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _opt_str(value) -> str | None:
    return str(value) if value is not None else None


def training_export_blocked(state: str) -> bool:
    return str(state) in OPEN_ROUND_STATES


def settings_payload(row: dict, *, action_id=None,
                     idempotent_replay: bool = False) -> dict:
    payload = {
        "enabled": bool(row["enabled"]),
        "sampling_rate_bps": int(row["sampling_rate_bps"]),
        "revision": int(row["revision"]),
        "word_difference_threshold_bps": WORD_DIFFERENCE_THRESHOLD_BPS,
        "comparison_version": COMPARISON_VERSION,
        "updated_at": _iso(row.get("updated_at")),
        "updated_by_admin_action_id": _opt_str(row.get("updated_by_admin_action_id")),
        "action_id": _opt_str(action_id or row.get("updated_by_admin_action_id")),
    }
    body = CrossCheckSettingsView.model_validate(payload).model_dump(mode="json")
    if idempotent_replay:
        body["idempotent_replay"] = True
    return body


def word_difference_rate(edit_distance, n_original, n_secondary,
                         segment_map=None) -> float | None:
    if isinstance(segment_map, dict):
        stored = segment_map.get("word_difference_rate")
        if stored is not None:
            return float(stored)
    if edit_distance is None or n_original is None or n_secondary is None:
        return None
    denom = max(int(n_original), int(n_secondary))
    if denom <= 0:
        return None
    return int(edit_distance) / denom


def list_item_payload(row: dict) -> dict:
    item = CrossCheckListItem.model_validate({
        "round_id": str(row["round_id"]),
        "task_id": str(row["task_id"]),
        "state": row["state"],
        "original_annotator_id": _opt_str(row.get("original_annotator_id")),
        "secondary_annotator_id": str(row["secondary_annotator_id"]),
        "duration_seconds": (
            float(row["duration_seconds"]) if row.get("duration_seconds") is not None
            else None
        ),
        "original_word_count": row.get("original_word_count"),
        "secondary_word_count": row.get("secondary_word_count"),
        "word_difference_rate": row.get("word_difference_rate"),
        "reason_codes": list(row.get("reason_codes") or []),
        "created_at": _iso(row["created_at"]),
        "submitted_at": _iso(row.get("submitted_at")),
        "resolved_at": _iso(row.get("resolved_at")),
        "training_export_blocked": training_export_blocked(row["state"]),
        "source_scene": row.get("source_scene"),
        "batch_code": row.get("batch_code"),
    })
    return item.model_dump(mode="json")


def _comparison_unavailable(reason_codes, segment_map) -> tuple[bool, str | None]:
    codes = list(reason_codes or [])
    reason = None
    if isinstance(segment_map, dict):
        stored = segment_map.get("comparison_unavailable")
        if stored:
            reason = str(stored)
    unavailable = "comparison_unavailable" in codes or bool(reason)
    return unavailable, reason


def detail_payload(row: dict, *, original_segments, secondary_segments,
                   original_review, secondary_review) -> dict:
    segment_map = row.get("segment_map")
    unavailable, unavailable_reason = _comparison_unavailable(
        row.get("reason_codes"), segment_map,
    )
    view = CrossCheckDetailView.model_validate({
        "round_id": str(row["id"]),
        "task_id": str(row["task_id"]),
        "revision": int(row["revision"]),
        "state": row["state"],
        "original_version_id": str(row["original_version_id"]),
        "original_annotator_id": _opt_str(row.get("original_annotator_id")),
        "original_review_id": _opt_str(row.get("original_review_id")),
        "secondary_version_id": str(row["secondary_version_id"]),
        "secondary_annotator_id": str(row["secondary_annotator_id"]),
        "baseline_version_id": str(row["baseline_version_id"]),
        "baseline_quality": row["baseline_quality"],
        "current_published_version_id": _opt_str(row.get("current_published_version_id")),
        "settings_revision": int(row["settings_revision"]),
        "sampling_rate_bps": int(row["sampling_rate_bps"]),
        "claim_policy": row["claim_policy"],
        "claim_filters": dict(row.get("claim_filters") or {}),
        "comparison_version": row.get("comparison_version") or COMPARISON_VERSION,
        "threshold_bps": int(row.get("threshold_bps") or WORD_DIFFERENCE_THRESHOLD_BPS),
        "original_word_count": row.get("original_word_count"),
        "secondary_word_count": row.get("secondary_word_count"),
        "edit_distance": row.get("edit_distance"),
        "substitutions": row.get("substitutions"),
        "insertions": row.get("insertions"),
        "deletions": row.get("deletions"),
        "word_difference_rate": word_difference_rate(
            row.get("edit_distance"), row.get("original_word_count"),
            row.get("secondary_word_count"), segment_map,
        ),
        "original_normalized_summary": row.get("original_normalized_summary"),
        "secondary_normalized_summary": row.get("secondary_normalized_summary"),
        "original_input_revision": row.get("original_input_revision"),
        "secondary_input_revision": row.get("secondary_input_revision"),
        "diff_ops": row.get("diff_ops"),
        "segment_map": segment_map,
        "reason_codes": list(row.get("reason_codes") or []),
        "comparison_unavailable": unavailable,
        "original_segments": original_segments,
        "secondary_segments": secondary_segments,
        "original_review": original_review,
        "secondary_review": secondary_review,
        "original_target_status": row.get("original_target_status"),
        "secondary_target_status": row.get("secondary_target_status"),
        "original_skip_reasons": list(row.get("original_skip_reasons") or []),
        "secondary_skip_reasons": list(row.get("secondary_skip_reasons") or []),
        "audio_url": f"/api/admin/audio/{row['task_id']}",
        "filename": row.get("filename"),
        "duration_seconds": (
            float(row["duration_seconds"]) if row.get("duration_seconds") is not None
            else None
        ),
        "comparison_unavailable_reason": unavailable_reason,
        "decision": row.get("decision"),
        "final_version_id": _opt_str(row.get("final_version_id")),
        "decided_by_admin_action_id": _opt_str(row.get("decided_by_admin_action_id")),
        "decision_reason": row.get("decision_reason"),
        "created_at": _iso(row["created_at"]),
        "submitted_at": _iso(row.get("submitted_at")),
        "compared_at": _iso(row.get("compared_at")),
        "resolved_at": _iso(row.get("resolved_at")),
        "training_export_blocked": training_export_blocked(row["state"]),
        "termination_reason": row.get("termination_reason"),
    })
    return view.model_dump(mode="json")


def mine_item_payload(row: dict) -> dict:
    return CrossCheckMineItem.model_validate({
        "round_id": str(row["round_id"]),
        "task_id": str(row["task_id"]),
        "state": row["state"],
        "submitted_at": _iso(row.get("submitted_at")),
        "version_id": str(row["version_id"]),
    }).model_dump(mode="json")


def submission_payload(row: dict, *, segments, review) -> dict:
    return CrossCheckSubmissionView.model_validate({
        "round_id": str(row["round_id"]),
        "task_id": str(row["task_id"]),
        "state": row["state"],
        "version_id": str(row["version_id"]),
        "submitted_at": _iso(row.get("submitted_at")),
        "target_status": row.get("target_status"),
        "segments": segments,
        "skip_reasons": list(row.get("skip_reasons") or []),
        "review": review,
    }).model_dump(mode="json")


def decision_result_payload(*, action_id, round_id, state, final_version_id,
                            final_status, training_blocked: bool,
                            idempotent_replay: bool = False) -> dict:
    body = {
        "success": True,
        "action_id": str(action_id),
        "round_id": str(round_id),
        "state": str(state),
        "final_version_id": _opt_str(final_version_id),
        "final_status": final_status,
        "training_export_blocked": bool(training_blocked),
    }
    if idempotent_replay:
        body["idempotent_replay"] = True
    return body
