from __future__ import annotations

import time
from datetime import timedelta

import pytest

import db
from annotation_metadata.processing import (
    acquire_processing_lease,
    complete_processing,
    processing_heartbeat,
    renew_processing_lease,
    require_processing_token,
    try_release_processing_lease,
)
from annotation_repository import ConflictError
from preprocess_store import store_preprocessed_task


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


def _segments():
    return [{"id": 1, "start": 0.0, "end": 1.0, "duration": 1.0,
             "asr_text": "asr", "text": "asr", "exclude_from_training": False}]


def test_no_token_store_cannot_bypass_active_lease(database, seed_tasks):
    seed_tasks(1)
    with db.db_conn() as conn, conn.cursor() as cur:
        task_id = cur.execute("SELECT id FROM annotation_tasks").fetchone()[0]
        acquire_processing_lease(cur, task_id, ttl_seconds=600)
        with pytest.raises(ConflictError):
            store_preprocessed_task(
                conn, rel_path="audio-000.wav", filename="audio-000.wav",
                folder="", duration=1.0, segments=_segments(),
                processing_token=None,
            )


def test_expired_holder_cannot_store_after_takeover(database, seed_tasks):
    seed_tasks(1)
    with db.db_conn() as conn, conn.cursor() as cur:
        task_id = cur.execute("SELECT id FROM annotation_tasks").fetchone()[0]
        first = acquire_processing_lease(cur, task_id, ttl_seconds=600)
        cur.execute(
            "UPDATE annotation_tasks SET processing_lease_until = now() - interval '1 second' "
            "WHERE id = %s",
            (task_id,),
        )
        second = acquire_processing_lease(cur, task_id, ttl_seconds=600)
        with pytest.raises(ConflictError):
            store_preprocessed_task(
                conn, rel_path="audio-000.wav", filename="audio-000.wav",
                folder="", duration=1.0, segments=_segments(),
                processing_token=first["token"],
            )
        result = store_preprocessed_task(
            conn, rel_path="audio-000.wav", filename="audio-000.wav",
            folder="", duration=1.0, segments=_segments(),
            processing_token=second["token"],
        )
        assert result["action"] == "updated"


def test_exception_release_allows_successor_acquire(database, seed_tasks):
    task_id = seed_tasks(1)[0]
    with db.db_conn() as conn, conn.cursor() as cur:
        first = acquire_processing_lease(cur, task_id, ttl_seconds=600)
        assert try_release_processing_lease(cur, task_id, first["token"]) is True
        second = acquire_processing_lease(cur, task_id, ttl_seconds=600)
        assert second["token"] != first["token"]
        assert try_release_processing_lease(cur, task_id, first["token"]) is False
        complete_processing(cur, task_id, second["token"])


def test_heartbeat_keeps_lease_alive(database, seed_tasks):
    task_id = seed_tasks(1)[0]
    with db.db_conn() as conn, conn.cursor() as cur:
        lease = acquire_processing_lease(cur, task_id, ttl_seconds=2)
        conn.commit()

    def renew():
        with db.db_conn() as conn, conn.cursor() as cur:
            renew_processing_lease(cur, task_id, lease["token"], ttl_seconds=2)
            conn.commit()

    with processing_heartbeat(renew, interval=0.4):
        time.sleep(2.4)
    with db.db_conn() as conn, conn.cursor() as cur:
        require_processing_token(cur, task_id, lease["token"])
        complete_processing(cur, task_id, lease["token"])
