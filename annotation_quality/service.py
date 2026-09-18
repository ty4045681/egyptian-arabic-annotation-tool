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
from annotation_quality.contracts import OPEN_ROUND_STATES
from annotation_quality.repository import (
    freeze_cross_check_version,
    record_cross_check_passed,
    record_cross_check_submitted,
    store_round_submission,
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
    task_status: str
    duration: float
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
        return {
            "idempotent_replay": True,
            "status_code": prior["status_code"],
            "response": prior["response"],
        }

    snapshot = _load_submit_snapshot(
        fence, lease_token, expected_revision, dirty_segments, scene_review,
    )
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
) -> _SubmitSnapshot:
    uid = fence.user_id
    with db_tx() as conn, conn.cursor() as cur:
        repo._lock_active_annotator(cur, uid)
        repo._require_session_fence(cur, fence)
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
            """SELECT duration, current_published_version_id, status
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

        original_digest = comparison_input_digest(
            original_segments, original[1], rnd[0], original[0],
        )
        secondary_digest = comparison_input_digest(
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
            task_status=asg["task_status"],
            duration=duration,
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
    except repo.RepositoryError:
        raise
    except Exception:
        return _unavailable_result(snapshot.threshold_bps)


def _unavailable_result(threshold_bps: int) -> ComparisonResult:
    return ComparisonResult(
        comparison_version=COMPARISON_VERSION,
        threshold_bps=int(threshold_bps),
        n_original=0,
        n_secondary=0,
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
        original_normalized="",
        secondary_normalized="",
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
            return {
                "idempotent_replay": True,
                "status_code": prior["status_code"],
                "response": prior["response"],
            }

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
        original_digest = comparison_input_digest(
            original_segments, original_row[4], snapshot.original_version_id,
            original_row[1],
        )
        secondary_digest = comparison_input_digest(
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

        state = _round_state_for(comparison)
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


def _round_state_for(comparison: ComparisonResult) -> str:
    if (
        comparison.needs_review
        or comparison.needs_word_review
        or comparison.edit_distance is None
        or comparison.n_original <= 0
        or comparison.n_secondary <= 0
        or comparison.reason_codes
    ):
        return "awaiting_review"
    return "passed"


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


def comparison_input_digest(
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
