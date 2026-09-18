"""Annotator and administrator scene-review commands."""

from __future__ import annotations

from annotation_metadata.contracts import AdminSceneReviewCommand, SceneReviewInput
from annotation_metadata.features import scene_review_write_enabled
from annotation_metadata.repository import append_review, latest_review
from annotation_repository import ConflictError, ForbiddenError, ValidationError


def apply_optional_review(cur, *, version_id, payload: dict | None,
                          actor_user_id, operation_id=None,
                          actor_kind: str = "annotator"):
    """Legacy clients omit scene_review: that means no change, not clear."""
    if payload is None:
        return latest_review(cur, version_id), False
    if not scene_review_write_enabled():
        raise ForbiddenError("Scene review editing is disabled")
    from pydantic import ValidationError as PydanticValidationError
    try:
        parsed = payload if isinstance(payload, SceneReviewInput) else SceneReviewInput.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(str(exc), code="invalid_field", field="scene_review") from exc
    return append_review(
        cur,
        version_id=version_id,
        status=parsed.status,
        scene_codes=list(parsed.scene_codes),
        note=parsed.note,
        actor_kind=actor_kind,
        actor_user_id=actor_user_id,
        operation_id=operation_id,
    )


def published_review(cur, task_id) -> dict | None:
    row = cur.execute(
        "SELECT current_published_version_id FROM annotation_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if not row or not row[0]:
        return None
    return latest_review(cur, row[0])


def draft_has_active_revision(cur, task_id) -> bool:
    row = cur.execute(
        """SELECT 1 FROM assignments a
           JOIN annotation_versions v ON v.id = a.working_version_id
           WHERE a.task_id = %s AND a.mode = 'revision'""",
        (task_id,),
    ).fetchone()
    return bool(row)


def admin_correct_review(cur, *, admin_session_id, command: AdminSceneReviewCommand,
                         task_id) -> dict:
    if not scene_review_write_enabled():
        raise ForbiddenError("Scene review editing is disabled")
    from annotation_repository import (
        _begin_admin_action, _canonical_request_hash, _finish_admin_action,
        _insert_admin_action_item, _required_reason, _validate_uuid,
    )

    tid = _validate_uuid(str(task_id), "task_id")
    expected_version = _validate_uuid(command.expected_version_id, "expected_version_id")
    expected_review = (
        _validate_uuid(command.expected_review_id, "expected_review_id")
        if command.expected_review_id else None
    )
    reason = _required_reason(command.reason)
    request_payload = {**command.model_dump(), "task_id": str(tid)}
    request_hash = _canonical_request_hash(request_payload)
    action = _begin_admin_action(
        cur, admin_session_id=admin_session_id,
        operation_id=command.operation_id,
        action_type="correct_scene_review", reason=reason,
        request_hash=request_hash, request_payload=request_payload,
    )
    if action.get("replay"):
        from annotation_repository import _admin_replay_response
        return _admin_replay_response(action)

    cur.execute("SELECT id FROM annotation_tasks WHERE id = %s FOR UPDATE", (tid,))
    task = cur.execute(
        """SELECT current_published_version_id FROM annotation_tasks WHERE id = %s""",
        (tid,),
    ).fetchone()
    if not task or not task[0]:
        raise ConflictError("Task has no published version to correct")
    if task[0] != expected_version:
        raise ConflictError("expected_version_id does not match the published version")
    open_round = cur.execute(
        """SELECT state FROM cross_check_rounds
           WHERE task_id = %s
             AND state IN ('in_progress', 'awaiting_review')
           FOR UPDATE""",
        (tid,),
    ).fetchone()
    if open_round:
        raise ConflictError(
            "This task has an open cross-check and cannot change frozen scene evidence",
            code="cross_check_active",
        )
    if draft_has_active_revision(cur, tid):
        raise ConflictError(
            "An annotator has an active correction draft; resolve that assignment first"
        )
    current = latest_review(cur, task[0])
    current_id = uuid_or_none(current)
    if expected_review is None:
        if current_id is not None:
            raise ConflictError("expected_review_id does not match the current review")
    elif current_id != expected_review:
        raise ConflictError("expected_review_id does not match the current review")

    review, changed = append_review(
        cur,
        version_id=task[0],
        status=command.status,
        scene_codes=list(command.scene_codes),
        note=command.note,
        actor_kind="admin",
        actor_admin_action_id=action["action_id"],
        operation_id=command.operation_id,
    )
    summary = {
        "task_id": str(tid),
        "changed": changed,
        "review": review,
        "published_version_id": str(task[0]),
    }
    _insert_admin_action_item(
        cur, action["action_id"], task_id=tid, result="corrected",
        details={"review_id": review.get("id") if review else None,
                 "status": command.status},
    )
    _finish_admin_action(cur, action["action_id"], summary)
    cur.execute(
        """INSERT INTO annotation_events
               (task_id, version_id, event_type, details, admin_action_id)
           VALUES (%s, %s, 'scene_review_corrected', %s, %s)""",
        (tid, task[0], _json(summary), action["action_id"]),
    )
    return {"success": True, **summary}


def uuid_or_none(review: dict | None):
    if not review or not review.get("id"):
        return None
    import uuid as uuid_mod
    return uuid_mod.UUID(str(review["id"]))


def _json(value):
    from psycopg.types.json import Json
    return Json(value)
