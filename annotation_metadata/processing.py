"""ASR/preprocess lease: protects one task's preprocessing, not website assignments.

A missing token is a legacy compatibility path. It may proceed only when no
other worker holds an unexpired lease. It must not clear or overwrite that
lease, and an expired holder cannot finalise after a successor has taken over.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta

from annotation_repository import ConflictError, ValidationError


DEFAULT_LEASE_SECONDS = 30 * 60
DEFAULT_HEARTBEAT_SECONDS = 60


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


def try_release_processing_lease(cur, task_id, token: str | None) -> bool:
    """Drop a lease this worker still holds. False if a successor owns it."""
    if not token:
        return False
    try:
        complete_processing(cur, task_id, token)
        return True
    except ConflictError:
        return False


@contextmanager
def processing_heartbeat(renew_fn, *, interval: float = DEFAULT_HEARTBEAT_SECONDS,
                         enabled: bool = True):
    """Call renew_fn in short transactions until the context exits.

    renew_fn must open its own connection; this helper never holds a DB
    transaction across VAD/ASR. A lost lease stops the thread; the caller
    still fails later when a fenced write sees the successor's token.
    """
    if not enabled or interval is None or interval <= 0:
        yield
        return
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            try:
                renew_fn()
            except Exception:
                return

    thread = threading.Thread(target=loop, daemon=True, name="processing-heartbeat")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=min(2.0, max(0.1, float(interval) + 0.5)))
