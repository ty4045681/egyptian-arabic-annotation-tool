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
        """SELECT enabled, sampling_rate_bps, revision, updated_at,
                  updated_by_admin_action_id
           FROM cross_check_settings WHERE id = 1""",
    ).fetchone()
    if not row:
        return {
            "enabled": False, "sampling_rate_bps": 0, "revision": 0,
            "updated_at": None, "updated_by_admin_action_id": None,
        }
    return {
        "enabled": bool(row[0]),
        "sampling_rate_bps": int(row[1]),
        "revision": int(row[2]),
        "updated_at": row[3],
        "updated_by_admin_action_id": row[4],
    }


def lock_settings(cur) -> dict:
    row = cur.execute(
        """SELECT enabled, sampling_rate_bps, revision, updated_at,
                  updated_by_admin_action_id
           FROM cross_check_settings WHERE id = 1 FOR UPDATE""",
    ).fetchone()
    if not row:
        raise RuntimeError("cross_check_settings row is missing")
    return {
        "enabled": bool(row[0]),
        "sampling_rate_bps": int(row[1]),
        "revision": int(row[2]),
        "updated_at": row[3],
        "updated_by_admin_action_id": row[4],
    }


def update_settings(cur, *, enabled: bool, sampling_rate_bps: int,
                    action_id) -> dict:
    row = cur.execute(
        """UPDATE cross_check_settings
           SET enabled = %s,
               sampling_rate_bps = %s,
               revision = revision + 1,
               updated_at = now(),
               updated_by_admin_action_id = %s
           WHERE id = 1
           RETURNING enabled, sampling_rate_bps, revision, updated_at,
                     updated_by_admin_action_id""",
        (enabled, sampling_rate_bps, action_id),
    ).fetchone()
    return {
        "enabled": bool(row[0]),
        "sampling_rate_bps": int(row[1]),
        "revision": int(row[2]),
        "updated_at": row[3],
        "updated_by_admin_action_id": row[4],
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


def record_cross_check_adjudicated(
    cur, *, task_id, version_id, from_status, to_status, action_id, details: dict,
) -> None:
    cur.execute(
        """INSERT INTO annotation_events
               (task_id, version_id, event_type, from_status, to_status,
                admin_action_id, details)
           VALUES (%s, %s, 'cross_check_adjudicated', %s, %s, %s, %s)""",
        (task_id, version_id, from_status, to_status, action_id, Json(details)),
    )


def record_admin_cross_check_cancelled(
    cur, *, user_id, task_id, version_id, from_status, action_id, details: dict,
) -> None:
    cur.execute(
        """INSERT INTO annotation_events
               (user_id, task_id, version_id, event_type, from_status,
                to_status, admin_action_id, details)
           VALUES (%s, %s, %s, 'cross_check_cancelled', %s, %s, %s, %s)""",
        (user_id, task_id, version_id, from_status, from_status, action_id,
         Json(details)),
    )


def record_cross_check_invalidated(
    cur, *, user_id, task_id, version_id, from_status, action_id, details: dict,
) -> None:
    cur.execute(
        """INSERT INTO annotation_events
               (user_id, task_id, version_id, event_type, from_status,
                to_status, admin_action_id, details)
           VALUES (%s, %s, %s, 'cross_check_invalidated', %s, %s, %s, %s)""",
        (user_id, task_id, version_id, from_status, from_status, action_id,
         Json(details)),
    )


def lock_open_round_for_task(cur, task_id):
    return cur.execute(
        """SELECT id, state, secondary_version_id, secondary_annotator_id,
                  original_version_id
           FROM cross_check_rounds
           WHERE task_id = %s AND state IN ('in_progress', 'awaiting_review')
           FOR UPDATE""",
        (task_id,),
    ).fetchone()


def invalidate_open_cross_check_for_task(
    cur, *, task_id, action_id, reason: str, from_status: str,
) -> dict | None:
    """Terminate an open round because the original result is going away.

    in_progress: abandon the secondary draft and delete its assignment.
    awaiting_review: keep the frozen submitted copy. Caller must already
    hold related user/assignment locks when an assignment exists.
    """
    row = lock_open_round_for_task(cur, task_id)
    if not row:
        return None
    round_id, state, secondary_version_id, secondary_annotator_id, original_version_id = row
    released = False
    if state == "in_progress":
        cur.execute(
            "SELECT id FROM annotation_versions WHERE id = %s FOR UPDATE",
            (secondary_version_id,),
        )
        cur.execute(
            """UPDATE annotation_versions
               SET lifecycle = 'abandoned', updated_at = now()
               WHERE id = %s AND lifecycle = 'draft'""",
            (secondary_version_id,),
        )
        cur.execute("DELETE FROM assignments WHERE task_id = %s", (task_id,))
        released = cur.rowcount > 0
    cur.execute(
        """UPDATE cross_check_rounds
           SET revision = revision + 1,
               state = 'invalidated',
               termination_reason = %s,
               resolved_at = now(),
               updated_at = now()
           WHERE id = %s AND state IN ('in_progress', 'awaiting_review')""",
        (reason, round_id),
    )
    record_cross_check_invalidated(
        cur, user_id=secondary_annotator_id, task_id=task_id,
        version_id=secondary_version_id, from_status=from_status,
        action_id=action_id,
        details={
            "mode": "cross_check",
            "round_id": str(round_id),
            "previous_state": state,
            "termination_reason": reason,
            "original_version_id": str(original_version_id),
        },
    )
    return {
        "round_id": round_id,
        "previous_state": state,
        "secondary_version_id": secondary_version_id,
        "secondary_annotator_id": secondary_annotator_id,
        "released": released,
    }


def peek_round(cur, round_id):
    return cur.execute(
        """SELECT r.id, r.task_id, r.revision, r.state,
                  r.original_version_id, r.original_annotator_id,
                  r.secondary_version_id, r.secondary_annotator_id,
                  a.user_id
           FROM cross_check_rounds r
           LEFT JOIN assignments a ON a.task_id = r.task_id
           WHERE r.id = %s""",
        (round_id,),
    ).fetchone()


def lock_round(cur, round_id):
    return cur.execute(
        """SELECT id, task_id, revision, state,
                  original_version_id, secondary_version_id,
                  secondary_annotator_id
           FROM cross_check_rounds
           WHERE id = %s
           FOR UPDATE""",
        (round_id,),
    ).fetchone()


def mark_round_adjudicated(
    cur, *, round_id, expected_revision: int, decision: str,
    final_version_id, action_id, reason: str,
) -> int:
    cur.execute(
        """UPDATE cross_check_rounds
           SET revision = revision + 1,
               state = 'adjudicated',
               decision = %s,
               final_version_id = %s,
               decided_by_admin_action_id = %s,
               decision_reason = %s,
               resolved_at = now(),
               updated_at = now()
           WHERE id = %s AND state = 'awaiting_review' AND revision = %s""",
        (decision, final_version_id, action_id, reason, round_id,
         expected_revision),
    )
    return cur.rowcount


def insert_adjudication_draft(
    cur, *, task_id, base_version_id, credited_annotator_id, action_id,
    base_name: str,
):
    version_no = cur.execute(
        """SELECT COALESCE(max(version_no), 0) + 1
           FROM annotation_versions WHERE task_id = %s""",
        (task_id,),
    ).fetchone()[0]
    draft_id = uuid.uuid4()
    cur.execute(
        """INSERT INTO annotation_versions
               (id, task_id, version_no, lifecycle, target_status, purpose,
                base_version_id, revision, human_modified,
                credited_annotator_id, skip_reasons, extra)
           VALUES (%s, %s, %s, 'draft', 'pending', 'adjudication',
                   %s, 0, true, %s, '{}', %s)""",
        (draft_id, task_id, version_no, base_version_id, credited_annotator_id,
         Json({
             "adjudication_base": base_name,
             "admin_action_id": str(action_id),
         })),
    )
    cur.execute(
        """INSERT INTO segments
               (version_id, segment_id, start_s, end_s, duration, asr_text,
                text, exclude_from_training, extra)
           SELECT %s, segment_id, start_s, end_s, duration, asr_text,
                  text, exclude_from_training, extra
           FROM segments WHERE version_id = %s""",
        (draft_id, base_version_id),
    )
    return draft_id


def publish_adjudication_version(
    cur, *, version_id, target_status: str, skip_reasons: list[str], action_id,
) -> int:
    cur.execute(
        """UPDATE annotation_versions
           SET lifecycle = 'published',
               target_status = %s,
               skip_reasons = %s,
               revision = revision + 1,
               submitted_by_user_id = NULL,
               submitted_at = now(),
               published_by_admin_action_id = %s,
               updated_at = now()
           WHERE id = %s
             AND lifecycle = 'draft'
             AND purpose = 'adjudication'""",
        (target_status, skip_reasons, action_id, version_id),
    )
    return cur.rowcount


def publish_secondary_version(cur, *, version_id, action_id) -> int:
    cur.execute(
        """UPDATE annotation_versions
           SET lifecycle = 'published',
               published_by_admin_action_id = %s,
               updated_at = now()
           WHERE id = %s AND lifecycle = 'cross_check_submitted'""",
        (action_id, version_id),
    )
    return cur.rowcount


def supersede_published_version(cur, *, version_id, task_id) -> int:
    cur.execute(
        """UPDATE annotation_versions
           SET lifecycle = 'superseded', updated_at = now()
           WHERE id = %s AND task_id = %s AND lifecycle = 'published'""",
        (version_id, task_id),
    )
    return cur.rowcount


def set_task_published(cur, *, task_id, version_id, status: str) -> None:
    cur.execute(
        """UPDATE annotation_tasks
           SET status = %s,
               current_published_version_id = %s,
               updated_at = now()
           WHERE id = %s""",
        (status, version_id, task_id),
    )


def load_review_by_id(cur, review_id) -> dict | None:
    if review_id is None:
        return None
    row = cur.execute(
        """SELECT id, review_no, status, note, actor_kind, actor_user_id,
                  actor_admin_action_id, operation_id, previous_review_id,
                  created_at, version_id
           FROM scene_reviews WHERE id = %s""",
        (review_id,),
    ).fetchone()
    if not row:
        return None
    labels = [
        item[0] for item in cur.execute(
            """SELECT scene_code FROM scene_review_labels
               WHERE review_id = %s ORDER BY scene_code""",
            (row[0],),
        ).fetchall()
    ]
    return {
        "id": str(row[0]),
        "review_no": int(row[1]),
        "status": row[2],
        "note": row[3] or "",
        "scene_codes": labels,
        "actor_kind": row[4],
        "actor_user_id": str(row[5]) if row[5] else None,
        "actor_admin_action_id": str(row[6]) if row[6] else None,
        "operation_id": str(row[7]) if row[7] else None,
        "previous_review_id": str(row[8]) if row[8] else None,
        "created_at": row[9].isoformat() if row[9] else None,
        "version_id": str(row[10]),
    }
