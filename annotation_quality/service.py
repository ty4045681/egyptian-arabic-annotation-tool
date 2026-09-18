"""Cross-check submit: compare outside write locks, then pass or queue.

Plan 6.2: snapshot + dirty merge + worddiff_v1 without holding user/task
write locks; the final txn re-checks revision, original pointer, and
input digests before freezing the secondary version.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid

from annotation_quality.comparison import (
    COMPARISON_VERSION,
    ComparisonResult,
    REASON_COMPARISON_UNAVAILABLE,
    UNAVAILABLE_COMPUTE_FAILURE,
    compare_transcripts,
)
from psycopg.errors import IntegrityError

from annotation_quality.contracts import (
    OPEN_ROUND_STATES,
    CrossCheckCancelCommand,
    CrossCheckDecision,
    CrossCheckDecisionCommand,
    CrossCheckEditBase,
    CrossCheckListQuery,
    CrossCheckMineQuery,
    CrossCheckSettingsUpdateCommand,
)
from annotation_quality.queries import (
    applied_list_filters,
    list_filter_digest,
    list_where_sql,
)
from annotation_quality.repository import (
    freeze_cross_check_version,
    insert_adjudication_draft,
    load_review_by_id,
    load_settings,
    lock_round,
    lock_settings,
    mark_round_adjudicated,
    peek_round,
    publish_adjudication_version,
    publish_secondary_version,
    record_admin_cross_check_cancelled,
    record_cross_check_adjudicated,
    record_cross_check_passed,
    record_cross_check_submitted,
    set_task_published,
    store_round_submission,
    supersede_published_version,
    update_settings,
)
from annotation_quality.serializers import (
    decision_result_payload,
    detail_payload,
    list_item_payload,
    mine_item_payload,
    settings_payload,
    submission_payload,
    word_difference_rate,
)
from db import db_tx

import annotation_repository as repo


@dataclass(frozen=True)
class _SubmitSnapshot:
    task_id: uuid.UUID
    version_id: uuid.UUID
    round_id: uuid.UUID
    original_version_id: uuid.UUID
    original_revision: int
    secondary_revision: int
    original_status: str
    threshold_bps: int
    original_digest: str
    secondary_digest: str
    original_segments: list[dict]
    merged_segments: list[dict]


def submit_cross_check(
    fence: repo.SessionFence,
    lease_token: str,
    expected_revision: int,
    target_status: str,
    skip_reasons: list[str],
    dirty_segments: list[dict],
    operation_id,
    request_hash: str,
    scene_review=None,
    policy: repo.SessionPolicy | None = None,
) -> dict:
    """Complete a cross-check assignment without publishing the secondary."""
    fence = repo._as_fence(fence)
    uid = fence.user_id
    op_uuid = (
        operation_id
        if isinstance(operation_id, uuid.UUID)
        else repo._validate_uuid(operation_id, "operation_id")
    )
    expected_revision = int(expected_revision)

    prior = _peek_complete_replay(uid, op_uuid, request_hash)
    if prior:
        return _replay_payload(prior)

    snapshot = _load_submit_snapshot(
        fence, lease_token, expected_revision, dirty_segments, scene_review,
        operation_id=op_uuid, request_hash=request_hash,
    )
    if isinstance(snapshot, dict):
        return snapshot
    comparison = _run_comparison(
        snapshot, secondary_status=target_status,
    )
    return _commit_cross_check_submit(
        fence=fence,
        lease_token=lease_token,
        expected_revision=expected_revision,
        target_status=target_status,
        skip_reasons=skip_reasons,
        dirty_segments=dirty_segments,
        operation_id=op_uuid,
        request_hash=request_hash,
        scene_review=scene_review,
        policy=policy,
        snapshot=snapshot,
        comparison=comparison,
    )


def _replay_payload(prior: dict) -> dict:
    return {
        "idempotent_replay": True,
        "status_code": prior["status_code"],
        "response": prior["response"],
    }


def _peek_complete_replay(uid, operation_id, request_hash: str) -> dict | None:
    with db_tx() as conn, conn.cursor() as cur:
        return repo._operation_replay(
            cur, operation_id, uid, "complete", request_hash,
        )


def _load_submit_snapshot(
    fence: repo.SessionFence,
    lease_token: str,
    expected_revision: int,
    dirty_segments: list[dict],
    scene_review,
    *,
    operation_id,
    request_hash: str,
) -> _SubmitSnapshot | dict:
    uid = fence.user_id
    with db_tx() as conn, conn.cursor() as cur:
        repo._lock_active_annotator(cur, uid)
        repo._require_session_fence(cur, fence)
        prior = repo._operation_replay(
            cur, operation_id, uid, "complete", request_hash,
        )
        if prior:
            return _replay_payload(prior)
        asg = repo._lock_assignment(cur, uid, lease_token)
        if asg["mode"] != "cross_check":
            raise repo.ConflictError(
                "Assignment is no longer a cross-check"
            )
        round_id = asg["cross_check_round_id"]
        if not round_id:
            raise repo.ConflictError("Cross-check assignment is missing its round")
        rnd = cur.execute(
            """SELECT original_version_id, secondary_version_id, state,
                      threshold_bps
               FROM cross_check_rounds
               WHERE id = %s AND task_id = %s""",
            (round_id, asg["task_id"]),
        ).fetchone()
        if not rnd or rnd[2] != "in_progress":
            raise repo.ConflictError("Cross-check round is no longer in progress")
        if rnd[1] != asg["version_id"]:
            raise repo.ConflictError(
                "Cross-check assignment does not match the round"
            )
        task = cur.execute(
            """SELECT duration, current_published_version_id
               FROM annotation_tasks WHERE id = %s""",
            (asg["task_id"],),
        ).fetchone()
        if not task:
            raise repo.NotFoundError("Task not found")
        if task[1] != rnd[0]:
            raise repo.ConflictError(
                "Original published version changed during cross-check"
            )
        original = cur.execute(
            """SELECT revision, target_status, lifecycle
               FROM annotation_versions WHERE id = %s""",
            (rnd[0],),
        ).fetchone()
        if not original:
            raise repo.ConflictError("Original version no longer exists")
        working = cur.execute(
            """SELECT revision, lifecycle, purpose
               FROM annotation_versions WHERE id = %s""",
            (asg["version_id"],),
        ).fetchone()
        if not working:
            raise repo.ConflictError("Working version no longer exists")
        if working[0] != expected_revision:
            raise repo.RevisionConflict(working[0])
        if working[1] != "draft" or working[2] != "cross_check":
            raise repo.ConflictError("Cross-check draft is no longer editable")

        if scene_review is not None:
            _validate_scene_review_payload(scene_review)

        original_segments = repo._load_segments(cur, rnd[0])
        draft_segments = repo._load_segments(cur, asg["version_id"])
        merged = repo._merge_dirty_segments(draft_segments, dirty_segments)
        duration = float(task[0])
        repo._validate_segment_timeline(merged, duration)

        original_digest = _comparison_input_digest(
            original_segments, original[1], rnd[0], original[0],
        )
        secondary_digest = _comparison_input_digest(
            merged, "pending", asg["version_id"], working[0],
        )
        return _SubmitSnapshot(
            task_id=asg["task_id"],
            version_id=asg["version_id"],
            round_id=round_id,
            original_version_id=rnd[0],
            original_revision=int(original[0]),
            secondary_revision=int(working[0]),
            original_status=original[1],
            threshold_bps=int(rnd[3]),
            original_digest=original_digest,
            secondary_digest=secondary_digest,
            original_segments=original_segments,
            merged_segments=merged,
        )


def _validate_scene_review_payload(payload) -> None:
    from annotation_metadata.contracts import SceneReviewInput
    from pydantic import ValidationError as PydanticValidationError

    if isinstance(payload, SceneReviewInput):
        return
    try:
        SceneReviewInput.model_validate(payload)
    except PydanticValidationError as exc:
        raise repo.ValidationError(
            str(exc), code="invalid_field", field="scene_review",
        ) from exc


def _run_comparison(snapshot: _SubmitSnapshot, *, secondary_status: str):
    try:
        return compare_transcripts(
            snapshot.original_segments,
            snapshot.merged_segments,
            original_status=snapshot.original_status,
            secondary_status=secondary_status,
            threshold_bps=snapshot.threshold_bps,
        )
    except Exception:
        return _unavailable_result(snapshot.threshold_bps)


def _unavailable_result(threshold_bps: int) -> ComparisonResult:
    return ComparisonResult(
        comparison_version=COMPARISON_VERSION,
        threshold_bps=int(threshold_bps),
        n_original=None,
        n_secondary=None,
        edit_distance=None,
        substitutions=None,
        insertions=None,
        deletions=None,
        word_difference_rate=None,
        needs_word_review=False,
        needs_review=True,
        reason_codes=(REASON_COMPARISON_UNAVAILABLE,),
        ops=(),
        original_bad_quality=(),
        secondary_bad_quality=(),
        original_normalized=None,
        secondary_normalized=None,
        comparison_unavailable=UNAVAILABLE_COMPUTE_FAILURE,
    )


def _commit_cross_check_submit(
    *,
    fence: repo.SessionFence,
    lease_token: str,
    expected_revision: int,
    target_status: str,
    skip_reasons: list[str],
    dirty_segments: list[dict],
    operation_id,
    request_hash: str,
    scene_review,
    policy,
    snapshot: _SubmitSnapshot,
    comparison: ComparisonResult,
) -> dict:
    uid = fence.user_id
    with db_tx() as conn, conn.cursor() as cur:
        repo._lock_active_annotator(cur, uid)
        repo._require_session_fence(cur, fence)
        prior = repo._operation_replay(
            cur, operation_id, uid, "complete", request_hash,
        )
        if prior:
            return _replay_payload(prior)

        asg = repo._lock_assignment(cur, uid, lease_token)
        if asg["mode"] != "cross_check":
            raise repo.ConflictError("Assignment is no longer a cross-check")
        if asg["task_id"] != snapshot.task_id:
            raise repo.ConflictError("Cross-check assignment changed")
        if asg["version_id"] != snapshot.version_id:
            raise repo.ConflictError("Cross-check working version changed")
        if asg["cross_check_round_id"] != snapshot.round_id:
            raise repo.ConflictError(
                "Cross-check assignment does not match the round"
            )

        task = cur.execute(
            """SELECT id, current_published_version_id, status, duration
               FROM annotation_tasks WHERE id = %s FOR UPDATE""",
            (asg["task_id"],),
        ).fetchone()
        if not task:
            raise repo.NotFoundError("Task not found")

        rnd = cur.execute(
            """SELECT original_version_id, secondary_version_id, state,
                      threshold_bps
               FROM cross_check_rounds
               WHERE id = %s AND task_id = %s
               FOR UPDATE""",
            (snapshot.round_id, asg["task_id"]),
        ).fetchone()
        if not rnd or rnd[2] != "in_progress":
            raise repo.ConflictError("Cross-check round is no longer in progress")
        if rnd[0] != snapshot.original_version_id or rnd[1] != snapshot.version_id:
            raise repo.ConflictError("Cross-check round versions changed")

        version_ids = sorted(
            {snapshot.original_version_id, snapshot.version_id},
            key=lambda value: str(value),
        )
        locked = {}
        for version_id in version_ids:
            row = cur.execute(
                """SELECT id, revision, lifecycle, purpose, target_status
                   FROM annotation_versions WHERE id = %s FOR UPDATE""",
                (version_id,),
            ).fetchone()
            if not row:
                raise repo.ConflictError("Working version no longer exists")
            locked[row[0]] = row

        working = locked[snapshot.version_id]
        if working[1] != expected_revision:
            raise repo.RevisionConflict(working[1])
        if working[2] != "draft" or working[3] != "cross_check":
            raise repo.ConflictError("Cross-check draft is no longer editable")

        if task[1] != snapshot.original_version_id:
            raise repo.ConflictError(
                "Original published version changed during cross-check"
            )

        original_row = locked[snapshot.original_version_id]
        original_segments = repo._load_segments(cur, snapshot.original_version_id)
        merged = repo._merge_dirty_segments(
            repo._load_segments(cur, snapshot.version_id), dirty_segments,
        )
        original_digest = _comparison_input_digest(
            original_segments, original_row[4], snapshot.original_version_id,
            original_row[1],
        )
        secondary_digest = _comparison_input_digest(
            merged, "pending", snapshot.version_id, working[1],
        )
        if (
            original_digest != snapshot.original_digest
            or secondary_digest != snapshot.secondary_digest
        ):
            raise repo.ConflictError("Cross-check comparison inputs changed")

        repo._apply_dirty_segments(cur, snapshot.version_id, dirty_segments)
        repo._validate_version_segments(cur, snapshot.version_id, snapshot.task_id)

        from annotation_metadata.reviews import apply_optional_review
        apply_optional_review(
            cur, version_id=snapshot.version_id, payload=scene_review,
            actor_user_id=uid, operation_id=operation_id,
        )

        frozen = freeze_cross_check_version(
            cur, version_id=snapshot.version_id, user_id=uid,
            target_status=target_status, skip_reasons=skip_reasons,
        )
        if frozen != 1:
            raise repo.ConflictError("Cross-check draft is no longer editable")

        state = "awaiting_review" if comparison.needs_review else "passed"
        stored = store_round_submission(
            cur,
            round_id=snapshot.round_id,
            state=state,
            comparison=comparison,
            original_input_revision=snapshot.original_revision,
            secondary_input_revision=snapshot.secondary_revision,
            diff_ops=_ops_json(comparison.ops),
            segment_map=_segment_map(
                comparison, snapshot.original_digest, snapshot.secondary_digest,
            ),
        )
        if stored != 1:
            raise repo.ConflictError("Cross-check round is no longer in progress")

        response = _submit_response(
            task_id=snapshot.task_id,
            target_status=target_status,
            skip_reasons=skip_reasons,
            round_id=snapshot.round_id,
            state=state,
        )
        repo._store_operation(
            cur, operation_id, uid, "complete", request_hash, response,
        )
        details = {
            "mode": "cross_check",
            "round_id": str(snapshot.round_id),
            "state": state,
            "comparison_version": comparison.comparison_version,
            "threshold_bps": comparison.threshold_bps,
            "reason_codes": list(comparison.reason_codes),
            "edit_distance": comparison.edit_distance,
            "comparison_unavailable": comparison.comparison_unavailable,
            "skip_reasons": skip_reasons,
        }
        record_cross_check_submitted(
            cur,
            operation_id=operation_id,
            user_id=uid,
            task_id=snapshot.task_id,
            version_id=snapshot.version_id,
            from_status=asg["task_status"],
            to_status=target_status,
            details=details,
        )
        if state == "passed":
            # Companion event must not reuse annotation_events.operation_id.
            record_cross_check_passed(
                cur,
                user_id=uid,
                task_id=snapshot.task_id,
                version_id=snapshot.version_id,
                from_status=asg["task_status"],
                details={
                    **details,
                    "parent_event_type": "cross_check_submitted",
                },
            )
        cur.execute("DELETE FROM assignments WHERE user_id = %s", (uid,))
        repo._touch_real_activity(cur, fence, policy=policy, assignment=False)
        return response


def _submit_response(*, task_id, target_status, skip_reasons, round_id, state) -> dict:
    return {
        "success": True,
        "task_id": str(task_id),
        "status": target_status,
        "published": False,
        "skip_reasons": list(skip_reasons),
        "cross_check": {
            "round_id": str(round_id),
            "state": state,
            "training_export_blocked": state in OPEN_ROUND_STATES,
        },
    }


def _comparison_input_digest(
    segments: list[dict], status: str, version_id, revision: int,
) -> str:
    payload = {
        "version_id": str(version_id),
        "revision": int(revision),
        "status": str(status),
        "segments": [
            {
                "id": int(seg["id"]),
                "start": float(seg["start"]),
                "end": float(seg["end"]),
                "text": seg.get("text") or "",
                "exclude_from_training": bool(seg.get("exclude_from_training")),
            }
            for seg in sorted(
                segments, key=lambda item: (int(item["id"]), float(item["start"]))
            )
        ],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _ops_json(ops) -> list[dict]:
    return [
        {
            "op": op.op,
            "original_word": op.original_word,
            "secondary_word": op.secondary_word,
            "original": _mapping_json(op.original),
            "secondary": _mapping_json(op.secondary),
        }
        for op in ops
    ]


def _mapping_json(mapping) -> dict | None:
    if mapping is None:
        return None
    return {
        "segment_id": mapping.segment_id,
        "text_start": mapping.text_start,
        "text_end": mapping.text_end,
        "start_s": mapping.start_s,
        "end_s": mapping.end_s,
    }


def _segment_map(comparison: ComparisonResult, original_digest: str,
                 secondary_digest: str) -> dict:
    return {
        "original_digest": original_digest,
        "secondary_digest": secondary_digest,
        "comparison_unavailable": comparison.comparison_unavailable,
        "word_difference_rate": comparison.word_difference_rate,
        "needs_word_review": comparison.needs_word_review,
        "original_bad_quality": [
            {"start_ms": item.start_ms, "end_ms": item.end_ms}
            for item in comparison.original_bad_quality
        ],
        "secondary_bad_quality": [
            {"start_ms": item.start_ms, "end_ms": item.end_ms}
            for item in comparison.secondary_bad_quality
        ],
    }


def get_cross_check_settings() -> dict:
    with db_tx() as conn, conn.cursor() as cur:
        return settings_payload(load_settings(cur))


def update_cross_check_settings(
    admin_session_id: str, command: CrossCheckSettingsUpdateCommand,
) -> dict:
    request_payload = command.model_dump(mode="json")
    request_hash = repo._canonical_request_hash(request_payload)
    with db_tx() as conn, conn.cursor() as cur:
        action = repo._begin_admin_action(
            cur, admin_session_id=admin_session_id,
            operation_id=command.operation_id,
            action_type="update_cross_check_settings",
            reason=command.reason, request_hash=request_hash,
            request_payload=request_payload,
        )
        if action["replay"]:
            return settings_payload(
                action["summary"], action_id=action["action_id"],
                idempotent_replay=True,
            )
        current = lock_settings(cur)
        if int(current["revision"]) != int(command.expected_revision):
            raise repo.ConflictError(
                "Cross-check settings revision changed",
                code="stale_revision",
            )
        updated = update_settings(
            cur, enabled=bool(command.enabled),
            sampling_rate_bps=int(command.sampling_rate_bps),
            action_id=action["action_id"],
        )
        body = settings_payload(updated, action_id=action["action_id"])
        repo._finish_admin_action(cur, action["action_id"], body)
        return body


def list_cross_checks(query: CrossCheckListQuery, *, timezone_name: str) -> dict:
    digest = list_filter_digest(query, timezone_name=timezone_name)
    where_sql, params = list_where_sql(query)
    if query.cursor:
        created_at, cursor_id, cursor_digest = repo._decode_admin_cursor(
            query.cursor, 3,
        )
        if cursor_digest != digest:
            raise repo.ValidationError("Invalid cursor")
        cursor_id = repo._validate_uuid(cursor_id, "cursor")
        where_sql = f"{where_sql} AND (r.created_at, r.id) < (%s::timestamptz, %s::uuid)"
        params.extend([created_at, cursor_id])
    limit = int(query.limit)
    with db_tx() as conn, conn.cursor() as cur:
        rows = cur.execute(
            f"""SELECT r.id, r.task_id, r.state,
                       r.original_annotator_id, r.secondary_annotator_id,
                       t.duration, r.original_word_count, r.secondary_word_count,
                       r.edit_distance, r.reason_codes, r.created_at,
                       r.submitted_at, r.resolved_at, r.claim_filters
                FROM cross_check_rounds r
                JOIN annotation_tasks t ON t.id = r.task_id
                WHERE {where_sql}
                ORDER BY r.created_at DESC, r.id DESC
                LIMIT %s""",
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items_raw = []
        for row in rows:
            items_raw.append({
                "round_id": row[0],
                "task_id": row[1],
                "state": row[2],
                "original_annotator_id": row[3],
                "secondary_annotator_id": row[4],
                "duration_seconds": row[5],
                "original_word_count": row[6],
                "secondary_word_count": row[7],
                "word_difference_rate": word_difference_rate(
                    row[8], row[6], row[7],
                ),
                "reason_codes": list(row[9] or []),
                "created_at": row[10],
                "submitted_at": row[11],
                "resolved_at": row[12],
                "claim_filters": row[13] or {},
            })
        summaries = {}
        if items_raw:
            from annotation_metadata.contracts import TaskFilter, parse_strict
            from annotation_metadata.repository import metadata_summaries
            meta = parse_strict(TaskFilter, {
                "source_scene": query.source_scene,
                "batch_code": query.batch_code,
            })
            summaries = metadata_summaries(
                cur, [item["task_id"] for item in items_raw], filters=meta,
            )
    items = []
    for item in items_raw:
        summary = summaries.get(str(item["task_id"]), {})
        claim = item["claim_filters"] if isinstance(item["claim_filters"], dict) else {}
        scenes = summary.get("source_scenes") or []
        batches = summary.get("batch_codes") or []
        item["source_scene"] = scenes[0] if scenes else claim.get("source_scene")
        item["batch_code"] = batches[0] if batches else claim.get("batch_code")
        items.append(list_item_payload(item))
    next_cursor = None
    if has_more and rows:
        next_cursor = repo._encode_admin_cursor(rows[-1][10], rows[-1][0], digest)
    return {
        "items": items,
        "next_cursor": next_cursor,
        "applied_filters": applied_list_filters(
            query, timezone_name=timezone_name,
        ),
    }


def get_cross_check_detail(round_id: str) -> dict:
    rid = repo._validate_uuid(round_id, "round_id")
    with db_tx() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.id, r.task_id, r.revision, r.state,
                      r.original_version_id, r.original_annotator_id,
                      r.original_review_id, r.secondary_version_id,
                      r.secondary_annotator_id, r.baseline_version_id,
                      r.baseline_quality, r.settings_revision, r.sampling_rate_bps,
                      r.claim_policy, r.claim_filters, r.comparison_version,
                      r.threshold_bps, r.original_word_count, r.secondary_word_count,
                      r.edit_distance, r.substitutions, r.insertions, r.deletions,
                      r.original_normalized_summary, r.secondary_normalized_summary,
                      r.original_input_revision, r.secondary_input_revision,
                      r.diff_ops, r.segment_map, r.reason_codes, r.decision,
                      r.final_version_id, r.decided_by_admin_action_id,
                      r.decision_reason, r.created_at, r.submitted_at,
                      r.compared_at, r.resolved_at, r.termination_reason,
                      t.current_published_version_id, t.duration AS duration_seconds,
                      t.filename,
                      ov.target_status AS original_target_status,
                      ov.skip_reasons AS original_skip_reasons,
                      sv.target_status AS secondary_target_status,
                      sv.skip_reasons AS secondary_skip_reasons
               FROM cross_check_rounds r
               JOIN annotation_tasks t ON t.id = r.task_id
               JOIN annotation_versions ov ON ov.id = r.original_version_id
               JOIN annotation_versions sv ON sv.id = r.secondary_version_id
               WHERE r.id = %s""",
            (rid,),
        )
        fetched = cur.fetchone()
        if not fetched:
            raise repo.NotFoundError("Cross-check round not found")
        cols = [desc.name for desc in cur.description]
        row = dict(zip(cols, fetched))
        original_segments = repo._load_segments(cur, row["original_version_id"])
        secondary_segments = repo._load_segments(cur, row["secondary_version_id"])
        from annotation_metadata.repository import latest_review
        original_review = load_review_by_id(cur, row["original_review_id"])
        if original_review is None:
            original_review = latest_review(cur, row["original_version_id"])
        secondary_review = latest_review(cur, row["secondary_version_id"])
    return detail_payload(
        row,
        original_segments=original_segments,
        secondary_segments=secondary_segments,
        original_review=original_review,
        secondary_review=secondary_review,
    )


def list_my_cross_checks(user_id, query: CrossCheckMineQuery) -> dict:
    uid = repo._validate_uuid(str(user_id), "user_id")
    params: list = [uid]
    where = (
        "r.secondary_annotator_id = %s AND r.submitted_at IS NOT NULL"
    )
    if query.cursor:
        submitted_at, cursor_id = repo._decode_admin_cursor(query.cursor, 2)
        cursor_id = repo._validate_uuid(cursor_id, "cursor")
        where += " AND (r.submitted_at, r.id) < (%s::timestamptz, %s::uuid)"
        params.extend([submitted_at, cursor_id])
    limit = int(query.limit)
    with db_tx() as conn, conn.cursor() as cur:
        rows = cur.execute(
            f"""SELECT r.id, r.task_id, r.state, r.submitted_at,
                       r.secondary_version_id
                FROM cross_check_rounds r
                WHERE {where}
                ORDER BY r.submitted_at DESC, r.id DESC
                LIMIT %s""",
            (*params, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        mine_item_payload({
            "round_id": row[0],
            "task_id": row[1],
            "state": row[2],
            "submitted_at": row[3],
            "version_id": row[4],
        })
        for row in rows
    ]
    next_cursor = None
    if has_more and rows:
        next_cursor = repo._encode_admin_cursor(rows[-1][3], rows[-1][0])
    return {"items": items, "next_cursor": next_cursor}


def get_my_submission(user_id, round_id: str) -> dict:
    uid = repo._validate_uuid(str(user_id), "user_id")
    rid = repo._validate_uuid(round_id, "round_id")
    with db_tx() as conn, conn.cursor() as cur:
        row = cur.execute(
            """SELECT r.id, r.task_id, r.state, r.submitted_at,
                      r.secondary_version_id, r.secondary_annotator_id,
                      v.target_status, v.skip_reasons
               FROM cross_check_rounds r
               JOIN annotation_versions v ON v.id = r.secondary_version_id
               WHERE r.id = %s""",
            (rid,),
        ).fetchone()
        if (
            not row
            or row[5] != uid
            or row[3] is None
        ):
            raise repo.NotFoundError("Cross-check submission not found")
        segments = repo._load_segments(cur, row[4])
        from annotation_metadata.repository import latest_review
        review = latest_review(cur, row[4])
    return submission_payload(
        {
            "round_id": row[0],
            "task_id": row[1],
            "state": row[2],
            "submitted_at": row[3],
            "version_id": row[4],
            "target_status": row[6],
            "skip_reasons": list(row[7] or []),
        },
        segments=segments,
        review=review,
    )


def decide_cross_check(
    admin_session_id: str, round_id: str, command: CrossCheckDecisionCommand,
) -> dict:
    rid = repo._validate_uuid(round_id, "round_id")
    request_payload = {**command.model_dump(mode="json"), "round_id": str(rid)}
    request_hash = repo._canonical_request_hash(request_payload)
    try:
        with db_tx() as conn, conn.cursor() as cur:
            action = repo._begin_admin_action(
                cur, admin_session_id=admin_session_id,
                operation_id=command.operation_id,
                action_type="cross_check_decision",
                reason=command.reason, request_hash=request_hash,
                request_payload=request_payload,
            )
            if action["replay"]:
                summary = action["summary"] or {}
                return decision_result_payload(
                    action_id=action["action_id"],
                    round_id=summary.get("round_id") or rid,
                    state=summary.get("state") or "adjudicated",
                    final_version_id=summary.get("final_version_id"),
                    final_status=summary.get("final_status"),
                    training_blocked=bool(
                        summary.get("training_export_blocked", False)
                    ),
                    idempotent_replay=True,
                )
            return _decide_locked(cur, rid, command, action)
    except IntegrityError as exc:
        raise repo.ConflictError(
            "Cross-check decision conflicted with another update",
            code="cross_check_state_conflict",
        ) from exc


def cancel_cross_check(
    admin_session_id: str, round_id: str, command: CrossCheckCancelCommand,
) -> dict:
    rid = repo._validate_uuid(round_id, "round_id")
    request_payload = {**command.model_dump(mode="json"), "round_id": str(rid)}
    request_hash = repo._canonical_request_hash(request_payload)
    try:
        with db_tx() as conn, conn.cursor() as cur:
            action = repo._begin_admin_action(
                cur, admin_session_id=admin_session_id,
                operation_id=command.operation_id,
                action_type="cancel_cross_check",
                reason=command.reason, request_hash=request_hash,
                request_payload=request_payload,
            )
            if action["replay"]:
                summary = action["summary"] or {}
                return {
                    "success": True,
                    "action_id": str(action["action_id"]),
                    "round_id": str(summary.get("round_id") or rid),
                    "state": summary.get("state") or "cancelled",
                    "training_export_blocked": bool(
                        summary.get("training_export_blocked", False)
                    ),
                    "idempotent_replay": True,
                }
            return _cancel_locked(cur, rid, command, action)
    except IntegrityError as exc:
        raise repo.ConflictError(
            "Cross-check cancel conflicted with another update",
            code="cross_check_state_conflict",
        ) from exc


def _lock_users_stable(cur, user_ids) -> None:
    for uid in sorted({value for value in user_ids if value}, key=str):
        row = cur.execute(
            "SELECT id FROM annotators WHERE id = %s FOR UPDATE",
            (uid,),
        ).fetchone()
        if not row:
            raise repo.ConflictError("Annotator no longer exists")


def _lock_versions_stable(cur, version_ids) -> dict:
    locked = {}
    for version_id in sorted({value for value in version_ids if value}, key=str):
        row = cur.execute(
            """SELECT id, revision, lifecycle, purpose, target_status,
                      credited_annotator_id, submitted_by_user_id, skip_reasons,
                      submitted_at
               FROM annotation_versions WHERE id = %s FOR UPDATE""",
            (version_id,),
        ).fetchone()
        if not row:
            raise repo.ConflictError(
                "Version no longer exists",
                code="cross_check_version_changed",
            )
        locked[row[0]] = {
            "id": row[0],
            "revision": int(row[1]),
            "lifecycle": row[2],
            "purpose": row[3],
            "target_status": row[4],
            "credited_annotator_id": row[5],
            "submitted_by_user_id": row[6],
            "skip_reasons": list(row[7] or []),
            "submitted_at": row[8],
        }
    return locked


def _decide_locked(cur, round_id, command: CrossCheckDecisionCommand, action: dict) -> dict:
    hint = peek_round(cur, round_id)
    if not hint:
        raise repo.NotFoundError("Cross-check round not found")
    if hint[3] != "awaiting_review":
        raise repo.ConflictError(
            "Cross-check round is not awaiting review",
            code="cross_check_state_conflict",
        )
    _lock_users_stable(cur, (hint[5], hint[7], hint[8]))
    assignment = cur.execute(
        """SELECT user_id, working_version_id, mode
           FROM assignments WHERE task_id = %s FOR UPDATE""",
        (hint[1],),
    ).fetchone()
    if assignment:
        raise repo.ConflictError(
            "Task has an active assignment",
            code="cross_check_active",
        )
    task = cur.execute(
        """SELECT id, status, current_published_version_id
           FROM annotation_tasks WHERE id = %s FOR UPDATE""",
        (hint[1],),
    ).fetchone()
    if not task:
        raise repo.NotFoundError("Task not found")
    rnd = lock_round(cur, round_id)
    if not rnd:
        raise repo.NotFoundError("Cross-check round not found")
    if rnd[3] != "awaiting_review":
        raise repo.ConflictError(
            "Cross-check round is not awaiting review",
            code="cross_check_state_conflict",
        )
    if int(rnd[2]) != int(command.expected_revision):
        raise repo.ConflictError(
            "Cross-check round revision changed",
            code="stale_revision",
        )
    expected_original = repo._validate_uuid(
        command.expected_original_version_id, "expected_original_version_id",
    )
    expected_secondary = repo._validate_uuid(
        command.expected_secondary_version_id, "expected_secondary_version_id",
    )
    if rnd[4] != expected_original or rnd[5] != expected_secondary:
        raise repo.ConflictError(
            "Cross-check versions changed",
            code="cross_check_version_changed",
        )
    if task[2] != rnd[4]:
        raise repo.ConflictError(
            "Original published version changed",
            code="cross_check_version_changed",
        )
    draft = cur.execute(
        """SELECT id FROM annotation_versions
           WHERE task_id = %s AND lifecycle = 'draft' FOR UPDATE""",
        (task[0],),
    ).fetchone()
    if draft:
        raise repo.ConflictError(
            "Task has an open draft",
            code="cross_check_state_conflict",
        )
    versions = _lock_versions_stable(cur, (rnd[4], rnd[5]))
    original = versions[rnd[4]]
    secondary = versions[rnd[5]]
    from_status = task[1]
    decision = command.decision
    if decision == CrossCheckDecision.ORIGINAL:
        final_version_id = original["id"]
        final_status = original["target_status"]
    elif decision == CrossCheckDecision.SECONDARY:
        if supersede_published_version(
            cur, version_id=original["id"], task_id=task[0],
        ) != 1:
            raise repo.ConflictError(
                "Original published version changed",
                code="cross_check_version_changed",
            )
        if publish_secondary_version(
            cur, version_id=secondary["id"], action_id=action["action_id"],
        ) != 1:
            raise repo.ConflictError(
                "Secondary version is no longer frozen",
                code="cross_check_state_conflict",
            )
        final_version_id = secondary["id"]
        final_status = secondary["target_status"]
        set_task_published(
            cur, task_id=task[0], version_id=final_version_id,
            status=final_status,
        )
    else:
        final_version_id, final_status = _publish_edited(
            cur, task=task, original=original, secondary=secondary,
            command=command, action=action,
        )
    stored = mark_round_adjudicated(
        cur, round_id=round_id, expected_revision=int(command.expected_revision),
        decision=str(decision), final_version_id=final_version_id,
        action_id=action["action_id"], reason=command.reason,
    )
    if stored != 1:
        raise repo.ConflictError(
            "Cross-check round is no longer awaiting review",
            code="cross_check_state_conflict",
        )
    result = decision_result_payload(
        action_id=action["action_id"],
        round_id=round_id,
        state="adjudicated",
        final_version_id=final_version_id,
        final_status=final_status,
        training_blocked=False,
    )
    repo._insert_admin_action_item(
        cur, action["action_id"], task_id=task[0],
        annotator_id=rnd[6],
        expected_version_id=expected_secondary,
        before_version_id=original["id"],
        after_version_id=final_version_id,
        result="adjudicated",
        details={
            "decision": str(decision),
            "round_id": str(round_id),
            "base": str(command.base) if command.base else None,
        },
    )
    repo._finish_admin_action(cur, action["action_id"], result)
    record_cross_check_adjudicated(
        cur, task_id=task[0], version_id=final_version_id,
        from_status=from_status, to_status=final_status,
        action_id=action["action_id"],
        details={
            "round_id": str(round_id),
            "decision": str(decision),
            "final_version_id": str(final_version_id),
            "reason": command.reason,
        },
    )
    return result


def _publish_edited(cur, *, task, original, secondary, command, action):
    if command.base == CrossCheckEditBase.ORIGINAL:
        base = original
        base_name = "original"
    else:
        base = secondary
        base_name = "secondary"
    target_status = command.target_status
    skip_reasons = list(command.skip_reasons or [])
    if target_status == "skipped":
        if not skip_reasons:
            raise repo.ValidationError("Skip requires at least one skip reason")
        bad = set(skip_reasons) - repo.ALLOWED_SKIP_REASONS
        if bad:
            raise repo.ValidationError(f"Invalid skip reasons: {sorted(bad)}")
    else:
        skip_reasons = []
    base_segments = repo._load_segments(cur, base["id"])
    base_ids = {int(seg["id"]) for seg in base_segments}
    payload_ids: set[int] = set()
    for seg in command.segments or []:
        try:
            payload_ids.add(int(seg["id"]))
        except (TypeError, ValueError, KeyError) as exc:
            raise repo.ValidationError("segment entry missing valid id") from exc
    if payload_ids != base_ids:
        raise repo.ValidationError(
            "edited decision must include every segment from the chosen base"
        )
    credited = base["credited_annotator_id"] or base["submitted_by_user_id"]
    draft_id = insert_adjudication_draft(
        cur, task_id=task[0], base_version_id=base["id"],
        credited_annotator_id=credited, action_id=action["action_id"],
        base_name=base_name,
    )
    repo._apply_dirty_segments(cur, draft_id, command.segments or [])
    repo._validate_version_segments(cur, draft_id, task[0])
    if target_status == "annotated":
        empty = cur.execute(
            """SELECT segment_id FROM segments
               WHERE version_id = %s AND exclude_from_training = false
                 AND btrim(text) = '' ORDER BY segment_id""",
            (draft_id,),
        ).fetchall()
        if empty:
            ids = [str(item[0]) for item in empty]
            raise repo.ValidationError(
                "Segments without text must be annotated or marked Bad Quality: "
                + ", ".join(ids)
            )
    from annotation_metadata.contracts import SceneReviewInput, parse_strict
    from annotation_metadata.features import scene_review_write_enabled
    from annotation_metadata.repository import append_review, latest_review
    copied = latest_review(cur, base["id"])
    if copied and copied.get("id"):
        append_review(
            cur, version_id=draft_id, status=copied["status"],
            scene_codes=list(copied.get("scene_codes") or []),
            note=copied.get("note") or "",
            actor_kind="admin",
            actor_admin_action_id=action["action_id"],
            operation_id=command.operation_id,
        )
    if command.scene_review is not None:
        if not scene_review_write_enabled():
            raise repo.ForbiddenError("Scene review editing is disabled")
        parsed = parse_strict(SceneReviewInput, command.scene_review)
        append_review(
            cur, version_id=draft_id, status=parsed.status,
            scene_codes=list(parsed.scene_codes), note=parsed.note,
            actor_kind="admin",
            actor_admin_action_id=action["action_id"],
            operation_id=command.operation_id,
        )
    if supersede_published_version(
        cur, version_id=original["id"], task_id=task[0],
    ) != 1:
        raise repo.ConflictError(
            "Original published version changed",
            code="cross_check_version_changed",
        )
    if publish_adjudication_version(
        cur, version_id=draft_id, target_status=target_status,
        skip_reasons=skip_reasons, action_id=action["action_id"],
    ) != 1:
        raise repo.ConflictError(
            "Adjudication version could not be published",
            code="cross_check_state_conflict",
        )
    set_task_published(
        cur, task_id=task[0], version_id=draft_id, status=target_status,
    )
    return draft_id, target_status


def _cancel_locked(cur, round_id, command: CrossCheckCancelCommand, action: dict) -> dict:
    hint = peek_round(cur, round_id)
    if not hint:
        raise repo.NotFoundError("Cross-check round not found")
    if hint[3] == "awaiting_review":
        raise repo.ConflictError(
            "Awaiting-review rounds cannot be cancelled",
            code="cross_check_state_conflict",
        )
    if hint[3] != "in_progress":
        raise repo.ConflictError(
            "Cross-check round is not in progress",
            code="cross_check_state_conflict",
        )
    # Peek owner without holding the assignment lock, then lock user first.
    _lock_users_stable(cur, (hint[5], hint[7], hint[8]))
    assignment = cur.execute(
        """SELECT user_id, working_version_id, mode, cross_check_round_id
           FROM assignments WHERE task_id = %s FOR UPDATE""",
        (hint[1],),
    ).fetchone()
    if assignment and assignment[0] != hint[7]:
        raise repo.ConflictError("Task assignment changed during cancel")
    task = cur.execute(
        """SELECT id, status, current_published_version_id
           FROM annotation_tasks WHERE id = %s FOR UPDATE""",
        (hint[1],),
    ).fetchone()
    if not task:
        raise repo.NotFoundError("Task not found")
    rnd = lock_round(cur, round_id)
    if not rnd:
        raise repo.NotFoundError("Cross-check round not found")
    if rnd[3] != "in_progress":
        raise repo.ConflictError(
            "Cross-check round is not in progress",
            code="cross_check_state_conflict",
        )
    if int(rnd[2]) != int(command.expected_revision):
        raise repo.ConflictError(
            "Cross-check round revision changed",
            code="stale_revision",
        )
    version_id = rnd[5]
    repo._cancel_in_progress_cross_check(
        cur, round_id=round_id, task_id=rnd[1], version_id=version_id,
        reason=command.reason,
    )
    cur.execute(
        "DELETE FROM assignments WHERE task_id = %s",
        (rnd[1],),
    )
    result = {
        "success": True,
        "action_id": str(action["action_id"]),
        "round_id": str(round_id),
        "state": "cancelled",
        "training_export_blocked": False,
    }
    repo._insert_admin_action_item(
        cur, action["action_id"], task_id=rnd[1],
        annotator_id=rnd[6],
        before_version_id=version_id,
        after_version_id=task[2],
        result="cancelled",
        details={"round_id": str(round_id), "reason": command.reason},
    )
    repo._finish_admin_action(cur, action["action_id"], result)
    record_admin_cross_check_cancelled(
        cur, user_id=rnd[6], task_id=rnd[1], version_id=version_id,
        from_status=task[1], action_id=action["action_id"],
        details={
            "mode": "cross_check",
            "round_id": str(round_id),
            "termination_reason": command.reason,
        },
    )
    return result
