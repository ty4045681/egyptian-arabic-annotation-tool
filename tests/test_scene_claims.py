from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

import annotation_repository as repo
import db


def user(name):
    return repo.login(name, str(uuid.uuid4()), 1800)["id"]


def source(task_id, scene, confidence, batch="acceptance-2026-09-14"):
    with db.db_conn() as conn:
        bid = conn.execute(
            "INSERT INTO source_batches(batch_code,name) VALUES(%s,%s) "
            "ON CONFLICT(batch_code) DO UPDATE SET name=EXCLUDED.name RETURNING id",
            (batch, batch),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO task_sources(task_id,batch_id,record_key,scene_code,
                      confidence,confidence_basis,content_digest)
               VALUES(%s,%s,%s,%s,%s,'acceptance evidence',%s)""",
            (task_id, bid, f"{task_id}:{scene}:{batch}", scene, confidence, "0" * 64),
        )


def test_batch_only_claim_excludes_unrelated_high_confidence(database, seed_tasks):
    wrong, right = seed_tasks(2)
    source(wrong, "shopping", "high", "acceptance-other-batch")
    source(right, "airport", "medium", "acceptance-chosen-batch")
    assert repo.claim(user("batch-only"), batch_code="acceptance-chosen-batch")["task_id"] == right


def test_airport_priority_uses_airport_evidence(database, seed_tasks):
    mixed, airport = seed_tasks(2)
    source(mixed, "airport", "medium")
    source(mixed, "shopping", "high")
    source(airport, "airport", "high")
    result = repo.claim(user("airport-only"), source_scene="airport")
    assert result["task_id"] == airport
    assert result["metadata"]["claim_context"]["scene_code"] == "airport"
    assert result["metadata"]["claim_context"]["confidence"] == "high"
    assert "购物" not in result["metadata"]["headline"] or "机场" in result["metadata"]["headline"]


def test_unknown_confidence_excludes_known_high_sources(database, seed_tasks):
    known, legacy = seed_tasks(2)
    source(known, "airport", "high")
    result = repo.claim(user("unknown-confidence"), source_confidence="unknown")
    assert result["task_id"] == legacy


def test_assigned_scene_is_temporary_empty_not_nonexistent(database, seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "airport", "high")
    repo.claim(user("first-airport"), source_scene="airport")
    pool = repo.pool_state(user("waiting-airport"), source_scene="airport")
    assert pool["available"] == 0
    assert pool["reason"] == "temporarily_all_assigned"


def test_scope_uses_allowed_evidence_and_existing_work_resumes(database, seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "airport", "medium")
    source(task, "shopping", "high")
    uid = user("restricted-airport")
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO annotator_scene_scopes(user_id,mode) VALUES(%s,'restricted') "
            "ON CONFLICT(user_id) DO UPDATE SET mode='restricted'",
            (uid,),
        )
        conn.execute(
            "INSERT INTO annotator_scene_access(user_id,scene_code) VALUES(%s,'airport')",
            (uid,),
        )
    first = repo.claim(uid)
    assert first["metadata"]["claim_context"]["scene_code"] == "airport"
    assert first["metadata"]["claim_context"]["confidence"] == "medium"
    with db.db_conn() as conn:
        conn.execute("UPDATE annotator_scene_scopes SET mode='none' WHERE user_id=%s", (uid,))
    resumed = repo.claim(uid, source_scene="shopping")
    assert resumed["task_id"] == task
    assert resumed["lease_token"] == first["lease_token"]
    assert resumed["resumed"] is True


def test_same_confidence_preserves_allocation_order(database, seed_tasks):
    first, second = seed_tasks(2)
    source(first, "shopping", "high")
    source(second, "airport", "high")
    assert repo.claim(user("fifo-within-confidence"))["task_id"] == first


def test_twenty_users_claim_distinct_tasks(database, seed_tasks):
    tasks = seed_tasks(20)
    for tid in tasks:
        source(tid, "airport", "high")
    users = [user(f"parallel-{i}") for i in range(20)]
    with ThreadPoolExecutor(max_workers=20) as executor:
        claims = list(executor.map(
            lambda uid: repo.claim(uid, source_scene="airport")["task_id"], users
        ))
    assert len(set(claims)) == 20
    assert set(claims) == set(tasks)


def test_unauthorized_selected_scene_is_forbidden(database, seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "airport", "high")
    uid = user("shopping-only")
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO annotator_scene_scopes(user_id,mode) VALUES(%s,'restricted') "
            "ON CONFLICT(user_id) DO UPDATE SET mode='restricted'",
            (uid,),
        )
        conn.execute(
            "INSERT INTO annotator_scene_access(user_id,scene_code) VALUES(%s,'shopping')",
            (uid,),
        )
    with pytest.raises(repo.ForbiddenError):
        repo.claim(uid, source_scene="airport")
    with pytest.raises(repo.ForbiddenError):
        repo.pool_state(uid, source_scene="airport")
