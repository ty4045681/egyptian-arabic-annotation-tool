"""Cursor-owned reads/writes for cross-check claims. Callers commit."""

from __future__ import annotations

import uuid

from psycopg.types.json import Json

from annotation_quality.contracts import COMPARISON_VERSION, WORD_DIFFERENCE_THRESHOLD_BPS


# Technical ASR/VAD keys only. Text, times, and identity never copy through extra.
SEGMENT_EXTRA_TECHNICAL_KEYS = frozenset({
    "channel", "speaker", "speaker_id", "sample_rate", "codec",
    "lang", "language", "energy", "snr", "vad", "vad_score",
})


def load_settings(cur) -> dict:
    row = cur.execute(
        """SELECT enabled, sampling_rate_bps, revision
           FROM cross_check_settings WHERE id = 1""",
    ).fetchone()
    if not row:
        return {"enabled": False, "sampling_rate_bps": 0, "revision": 0}
    return {
        "enabled": bool(row[0]),
        "sampling_rate_bps": int(row[1]),
        "revision": int(row[2]),
    }


def cross_check_allowed(settings: dict) -> bool:
    return bool(settings.get("enabled")) and int(settings.get("sampling_rate_bps") or 0) > 0


def ensure_participant(cur, task_id, user_id) -> None:
    cur.execute(
        """INSERT INTO task_annotation_participants
               (task_id, user_id, first_participated_at)
           VALUES (%s, %s, now())
           ON CONFLICT DO NOTHING""",
        (task_id, user_id),
    )


def original_review_id(cur, version_id):
    row = cur.execute(
        """SELECT id FROM scene_reviews
           WHERE version_id = %s AND NOT superseded
           ORDER BY review_no DESC
           LIMIT 1""",
        (version_id,),
    ).fetchone()
    return row[0] if row else None


def whitelisted_segment_extra(extra) -> dict:
    if not isinstance(extra, dict):
        return {}
    return {
        key: value
        for key, value in extra.items()
        if key in SEGMENT_EXTRA_TECHNICAL_KEYS
    }


def copy_baseline_draft(cur, *, task_id, user_id, baseline_version_id,
                        baseline_quality: str):
    """New cross_check draft from baseline ASR/cuts. Human text and BQ reset."""
    version_no = cur.execute(
        """SELECT COALESCE(max(version_no), 0) + 1
           FROM annotation_versions WHERE task_id = %s""",
        (task_id,),
    ).fetchone()[0]
    draft_id = uuid.uuid4()
    extra = {
        "reset_from_baseline": True,
        "baseline_quality": baseline_quality,
    }
    cur.execute(
        """INSERT INTO annotation_versions
               (id, task_id, version_no, lifecycle, target_status, purpose,
                base_version_id, revision, human_modified, created_by_user_id,
                skip_reasons, extra)
           VALUES (%s, %s, %s, 'draft', 'pending', 'cross_check',
                   %s, 0, false, %s, '{}', %s)""",
        (draft_id, task_id, version_no, baseline_version_id, user_id, Json(extra)),
    )
    rows = cur.execute(
        """SELECT segment_id, start_s, end_s, duration, asr_text, extra
           FROM segments WHERE version_id = %s ORDER BY segment_id""",
        (baseline_version_id,),
    ).fetchall()
    for row in rows:
        cur.execute(
            """INSERT INTO segments
                   (version_id, segment_id, start_s, end_s, duration,
                    asr_text, text, exclude_from_training, extra)
               VALUES (%s, %s, %s, %s, %s, %s, '', false, %s)""",
            (draft_id, row[0], row[1], row[2], row[3], row[4] or "",
             Json(whitelisted_segment_extra(row[5]))),
        )
    return draft_id


def insert_in_progress_round(
    cur, *, task_id, user_id, original_version_id, original_annotator_id,
    original_review_id, secondary_version_id, baseline_version_id,
    baseline_quality, settings, claim_policy: str, claim_filters: dict,
):
    round_id = uuid.uuid4()
    cur.execute(
        """INSERT INTO cross_check_rounds (
               id, task_id, revision,
               original_version_id, original_annotator_id, original_review_id,
               secondary_version_id, secondary_annotator_id,
               baseline_version_id, baseline_quality,
               state, settings_revision, sampling_rate_bps, claim_policy,
               claim_filters, comparison_version, threshold_bps
           ) VALUES (
               %s, %s, 0,
               %s, %s, %s,
               %s, %s,
               %s, %s,
               'in_progress', %s, %s, %s,
               %s, %s, %s
           )""",
        (
            round_id, task_id,
            original_version_id, original_annotator_id, original_review_id,
            secondary_version_id, user_id,
            baseline_version_id, baseline_quality,
            settings["revision"], settings["sampling_rate_bps"], claim_policy,
            Json(claim_filters), COMPARISON_VERSION, WORD_DIFFERENCE_THRESHOLD_BPS,
        ),
    )
    return round_id


def insert_cross_check_assignment(
    cur, *, user_id, task_id, draft_id, lease_token, claim_policy,
    best_scene, best_id, best_confidence, round_id,
):
    inserted = cur.execute(
        """INSERT INTO assignments
               (user_id, task_id, working_version_id, mode, lease_token,
                base_revision, assigned_at, last_activity_at,
                claim_scene_code, claim_source_id, claim_policy,
                claim_confidence, cross_check_round_id)
           VALUES (%s, %s, %s, 'cross_check', %s, 0, now(), now(),
                   %s, %s, %s, %s, %s)
           ON CONFLICT DO NOTHING
           RETURNING lease_token""",
        (user_id, task_id, draft_id, lease_token,
         best_scene, best_id, claim_policy, best_confidence, round_id),
    ).fetchone()
    return inserted[0] if inserted else None


def record_cross_check_claimed(
    cur, *, user_id, task_id, draft_id, round_id, task_status,
    best_scene, best_confidence, claim_policy, best_id, original_version_id,
    settings,
):
    cur.execute(
        """INSERT INTO annotation_events
               (user_id, task_id, version_id, event_type, to_status, details)
           VALUES (%s, %s, %s, 'cross_check_claimed', %s, %s)""",
        (user_id, task_id, draft_id, task_status, Json({
            "mode": "cross_check",
            "round_id": str(round_id),
            "claim_scene_code": best_scene,
            "claim_confidence": best_confidence,
            "claim_policy": claim_policy,
            "claim_source_id": str(best_id) if best_id else None,
            "original_version_id": str(original_version_id),
            "settings_revision": settings["revision"],
            "sampling_rate_bps": settings["sampling_rate_bps"],
        })),
    )


def create_cross_check_claim(
    cur, *, user_id, task_id, rel_path, filename, folder, duration, status,
    original_version_id, original_annotator_id, baseline_version_id,
    baseline_quality, filters, claim_policy: str, settings: dict,
    best_id, best_scene, best_confidence,
) -> dict | None:
    """Insert draft, round, assignment, participant, and event. Same transaction."""
    review_id = original_review_id(cur, original_version_id)
    draft_id = copy_baseline_draft(
        cur, task_id=task_id, user_id=user_id,
        baseline_version_id=baseline_version_id,
        baseline_quality=baseline_quality,
    )
    claim_filters = {
        "source_scene": filters.source_scene_value(),
        "batch_code": filters.batch_code,
        "source_confidence": filters.source_confidence,
    }
    round_id = insert_in_progress_round(
        cur, task_id=task_id, user_id=user_id,
        original_version_id=original_version_id,
        original_annotator_id=original_annotator_id,
        original_review_id=review_id,
        secondary_version_id=draft_id,
        baseline_version_id=baseline_version_id,
        baseline_quality=baseline_quality,
        settings=settings, claim_policy=claim_policy,
        claim_filters=claim_filters,
    )
    lease_token = uuid.uuid4()
    inserted = insert_cross_check_assignment(
        cur, user_id=user_id, task_id=task_id, draft_id=draft_id,
        lease_token=lease_token, claim_policy=claim_policy,
        best_scene=best_scene, best_id=best_id,
        best_confidence=best_confidence, round_id=round_id,
    )
    if inserted is None:
        return None
    ensure_participant(cur, task_id, user_id)
    record_cross_check_claimed(
        cur, user_id=user_id, task_id=task_id, draft_id=draft_id,
        round_id=round_id, task_status=status,
        best_scene=best_scene, best_confidence=best_confidence,
        claim_policy=claim_policy, best_id=best_id,
        original_version_id=original_version_id, settings=settings,
    )
    return {
        "round_id": round_id,
        "draft_id": draft_id,
        "lease_token": inserted,
        "revision": 0,
        "rel_path": rel_path,
        "filename": filename,
        "folder": folder,
        "duration": duration,
        "status": status,
        "task_id": task_id,
    }


def freeze_cross_check_version(cur, *, version_id, user_id, target_status,
                               skip_reasons) -> int:
    """Freeze the secondary draft. Never publishes it."""
    cur.execute(
        """UPDATE annotation_versions
           SET lifecycle = 'cross_check_submitted',
               target_status = %s,
               skip_reasons = %s,
               revision = revision + 1,
               human_modified = true,
               modified_by_user_id = %s,
               submitted_by_user_id = %s,
               submitted_at = now(),
               credited_annotator_id = %s,
               updated_at = now()
           WHERE id = %s
             AND lifecycle = 'draft'
             AND purpose = 'cross_check'""",
        (target_status, skip_reasons, user_id, user_id, user_id, version_id),
    )
    return cur.rowcount


def store_round_submission(
    cur, *, round_id, state: str, comparison, original_input_revision: int,
    secondary_input_revision: int, diff_ops, segment_map,
) -> int:
    """Persist worddiff_v1 evidence and the auto-pass / queue state."""
    reason_codes = list(comparison.reason_codes)
    resolved = state == "passed"
    cur.execute(
        """UPDATE cross_check_rounds
           SET revision = revision + 1,
               state = %s,
               original_word_count = %s,
               secondary_word_count = %s,
               edit_distance = %s,
               substitutions = %s,
               insertions = %s,
               deletions = %s,
               original_normalized_summary = %s,
               secondary_normalized_summary = %s,
               original_input_revision = %s,
               secondary_input_revision = %s,
               diff_ops = %s,
               segment_map = %s,
               reason_codes = %s,
               submitted_at = now(),
               compared_at = now(),
               resolved_at = CASE WHEN %s THEN now() ELSE resolved_at END,
               updated_at = now()
           WHERE id = %s AND state = 'in_progress'""",
        (
            state,
            comparison.n_original,
            comparison.n_secondary,
            comparison.edit_distance,
            comparison.substitutions,
            comparison.insertions,
            comparison.deletions,
            comparison.original_normalized,
            comparison.secondary_normalized,
            original_input_revision,
            secondary_input_revision,
            Json(diff_ops),
            Json(segment_map),
            reason_codes,
            resolved,
            round_id,
        ),
    )
    return cur.rowcount


def record_cross_check_submitted(
    cur, *, operation_id, user_id, task_id, version_id, from_status,
    to_status, details: dict,
) -> None:
    cur.execute(
        """INSERT INTO annotation_events
               (operation_id, user_id, task_id, version_id, event_type,
                from_status, to_status, details)
           VALUES (%s, %s, %s, %s, 'cross_check_submitted', %s, %s, %s)""",
        (operation_id, user_id, task_id, version_id, from_status, to_status,
         Json(details)),
    )


def record_cross_check_passed(
    cur, *, user_id, task_id, version_id, from_status, details: dict,
) -> None:
    # operation_id stays NULL: companion events cannot reuse the submitter's
    # annotation_events.operation_id unique value (plan 10.3).
    cur.execute(
        """INSERT INTO annotation_events
               (user_id, task_id, version_id, event_type,
                from_status, to_status, details)
           VALUES (%s, %s, %s, 'cross_check_passed', %s, %s, %s)""",
        (user_id, task_id, version_id, from_status, from_status, Json(details)),
    )
