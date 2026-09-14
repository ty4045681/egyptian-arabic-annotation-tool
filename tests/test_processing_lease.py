from __future__ import annotations

from datetime import timedelta

import pytest

import db
from annotation_metadata.processing import (
    acquire_processing_lease,
    complete_processing,
    renew_processing_lease,
    require_processing_token,
)
from annotation_repository import ConflictError


def test_legacy_no_token_cannot_bypass_active_lease(database, seed_tasks):
    task_id = seed_tasks(1)[0]
    with db.db_conn() as conn, conn.cursor() as cur:
        lease = acquire_processing_lease(cur, task_id, ttl_seconds=600)
        with pytest.raises(ConflictError):
            require_processing_token(cur, task_id, None)
        with pytest.raises(ConflictError):
            complete_processing(cur, task_id, None)
        renew_processing_lease(cur, task_id, lease["token"], ttl_seconds=600)
        complete_processing(cur, task_id, lease["token"])
        require_processing_token(cur, task_id, None)


def test_expired_holder_cannot_finalize_after_takeover(database, seed_tasks):
    task_id = seed_tasks(1)[0]
    with db.db_conn() as conn, conn.cursor() as cur:
        first = acquire_processing_lease(cur, task_id, ttl_seconds=600)
        cur.execute(
            "UPDATE annotation_tasks SET processing_lease_until = now() - interval '1 second' "
            "WHERE id = %s",
            (task_id,),
        )
        second = acquire_processing_lease(cur, task_id, ttl_seconds=600)
        with pytest.raises(ConflictError):
            complete_processing(cur, task_id, first["token"])
        complete_processing(cur, task_id, second["token"])
