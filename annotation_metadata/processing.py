"""ASR/preprocess lease: protects one task's preprocessing, not website assignments.

A missing token is a legacy compatibility path. It may proceed only when no
other worker holds an unexpired lease. It must not clear or overwrite that
lease, and an expired holder cannot finalise after a successor has taken over.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from annotation_repository import ConflictError, ValidationError


DEFAULT_LEASE_SECONDS = 30 * 60


def _lease_active(token, until, now) -> bool:
    return token is not None and until is not None and until > now


def acquire_processing_lease(cur, task_id, *, ttl_seconds: int = DEFAULT_LEASE_SECONDS,
                             expected_version: int | None = None) -> dict:
    row = cur.execute(
        """SELECT processing_token, processing_lease_until, processing_version
           FROM annotation_tasks WHERE id = %s FOR UPDATE""",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValidationError("task not found for processing lease")
    token, until, version = row
    if expected_version is not None and int(version) != int(expected_version):
        raise ConflictError("processing version conflict")
    now = cur.execute("SELECT now()").fetchone()[0]
    if _lease_active(token, until, now):
        raise ConflictError("task is already being processed")
    new_token = uuid.uuid4()
    new_version = int(version) + 1
    cur.execute(
        """UPDATE annotation_tasks
           SET processing_token = %s,
               processing_lease_until = now() + %s,
               processing_version = %s,
               updated_at = now()
           WHERE id = %s""",
        (new_token, timedelta(seconds=ttl_seconds), new_version, task_id),
    )
    return {"token": str(new_token), "processing_version": new_version}


def renew_processing_lease(cur, task_id, token: str,
                           *, ttl_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
    token_uuid = uuid.UUID(str(token))
    updated = cur.execute(
        """UPDATE annotation_tasks
           SET processing_lease_until = now() + %s, updated_at = now()
           WHERE id = %s AND processing_token = %s
             AND processing_lease_until > now()
           RETURNING processing_version""",
        (timedelta(seconds=ttl_seconds), task_id, token_uuid),
    ).fetchone()
    if not updated:
        raise ConflictError("processing lease is not held by this token")


def require_processing_token(cur, task_id, token: str | None) -> int:
    row = cur.execute(
        """SELECT processing_token, processing_lease_until, processing_version
           FROM annotation_tasks WHERE id = %s""",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValidationError("task not found")
    now = cur.execute("SELECT now()").fetchone()[0]
    held_token, until, version = row
    if token is None:
        if _lease_active(held_token, until, now):
            raise ConflictError("task is already being processed")
        return int(version or 0)
    token_uuid = uuid.UUID(str(token))
    if held_token != token_uuid or until is None or until <= now:
        raise ConflictError("processing lease is not held by this token")
    return int(version or 0)


def complete_processing(cur, task_id, token: str | None) -> None:
    row = cur.execute(
        """SELECT processing_token, processing_lease_until, processing_version
           FROM annotation_tasks WHERE id = %s FOR UPDATE""",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValidationError("task not found")
    now = cur.execute("SELECT now()").fetchone()[0]
    held_token, until, _version = row
    if token is None:
        if _lease_active(held_token, until, now):
            raise ConflictError("task is already being processed")
        return
    token_uuid = uuid.UUID(str(token))
    if held_token != token_uuid or until is None or until <= now:
        raise ConflictError("processing lease is not held by this token")
    cur.execute(
        """UPDATE annotation_tasks
           SET processing_token = NULL, processing_lease_until = NULL,
               updated_at = now()
           WHERE id = %s AND processing_token = %s""",
        (task_id, token_uuid),
    )
