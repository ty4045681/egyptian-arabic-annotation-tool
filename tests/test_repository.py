from __future__ import annotations

import concurrent.futures
import hashlib
import json
import threading
import uuid

import pytest

import annotation_repository as repo


def full_segments(assignment, prefix="text"):
    return [
        {
            "id": seg["id"], "start": seg["start"], "end": seg["end"],
            "duration": seg["duration"], "text": f"{prefix} {seg['id']}",
            "exclude_from_training": False,
        }
        for seg in assignment["segments"]
    ]


def make_user(name):
    sid = str(uuid.uuid4())
    user = repo.login(name, sid, 1800)
    return user, sid


def test_claim_save_complete_and_resume(database, seed_tasks):
    seed_tasks(2)
    user, sid = make_user("alice")
    assignment = repo.claim(user["fence"])
    resumed = repo.get_assignment(user["id"])
    assert resumed["task_id"] == assignment["task_id"]
    assert resumed["lease_token"] == assignment["lease_token"]

    op = str(uuid.uuid4())
    segments = full_segments(assignment)
    body = {"segments": segments, "expected_revision": 0}
    req_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    saved = repo.save_draft(user["fence"], assignment["lease_token"], 0,
                            segments, op, req_hash)
    assert saved["revision"] == 1
    replay = repo.save_draft(user["fence"], assignment["lease_token"], 0,
                             segments, op, req_hash)
    assert replay == saved
    with pytest.raises(repo.ConflictError):
        repo.save_draft(user["fence"], assignment["lease_token"], 0,
                        segments, op, "different")

    repo.logout("alice", sid)
    assert repo.get_assignment(user["id"])["task_id"] == assignment["task_id"]
    user = repo.login("alice", str(uuid.uuid4()), 1800)

    complete_op = str(uuid.uuid4())
    result = repo.complete(user["fence"], assignment["lease_token"], 1,
                           "annotated", [], segments, complete_op, "complete-hash")
    assert result["status"] == "annotated"
    assert repo.get_assignment(user["id"]) is None
    replay = repo.complete(user["fence"], assignment["lease_token"], 1,
                           "annotated", [], segments, complete_op, "complete-hash")
    assert replay["idempotent_replay"]
    assert repo.dashboard()["stats"] == {
        "total": 2, "annotated": 1, "skipped": 0,
        "pending": 1, "percent_complete": 50.0,
        "annotated_duration_seconds": 10.0,
        "cross_check_submitted_count": 0,
        "cross_check_submitted_audio_seconds": 0.0,
    }


def test_revision_conflict_and_segment_validation(database, seed_tasks):
    seed_tasks(1)
    user, _ = make_user("alice")
    assignment = repo.claim(user["fence"])
    segments = full_segments(assignment)
    saved = repo.save_draft(user["fence"], assignment["lease_token"], 0,
                            segments, str(uuid.uuid4()), "h1")
    assert saved["revision"] == 1
    with pytest.raises(repo.RevisionConflict) as conflict:
        repo.save_draft(user["fence"], assignment["lease_token"], 0,
                        segments, str(uuid.uuid4()), "h2")
    assert conflict.value.current_revision == 1

    invented = [dict(segments[0], id=999)]
    with pytest.raises(repo.ValidationError, match="does not exist"):
        repo.save_draft(user["fence"], assignment["lease_token"], 1,
                        invented, str(uuid.uuid4()), "h3")

    overlap = [dict(segments[1], start=4.0)]
    with pytest.raises(repo.ValidationError, match="overlaps"):
        repo.complete(user["fence"], assignment["lease_token"], 1,
                      "annotated", [], overlap, str(uuid.uuid4()), "h4")


def test_complete_requires_text_or_bad_quality(database, seed_tasks):
    seed_tasks(1)
    user, _ = make_user("alice")
    assignment = repo.claim(user["fence"])
    segments = full_segments(assignment)
    segments[0]["text"] = ""
    with pytest.raises(repo.ValidationError, match="without text"):
        repo.complete(user["fence"], assignment["lease_token"], 0,
                      "annotated", [], segments, str(uuid.uuid4()), "h")
    segments[0]["exclude_from_training"] = True
    result = repo.complete(user["fence"], assignment["lease_token"], 0,
                           "annotated", [], segments, str(uuid.uuid4()), "h2")
    assert result["success"]


def test_skip_requires_valid_reason(database, seed_tasks):
    seed_tasks(1)
    user, _ = make_user("alice")
    assignment = repo.claim(user["fence"])
    with pytest.raises(repo.ValidationError):
        repo.complete(user["fence"], assignment["lease_token"], 0,
                      "skipped", [], [], str(uuid.uuid4()), "h")
    with pytest.raises(repo.ValidationError):
        repo.complete(user["fence"], assignment["lease_token"], 0,
                      "skipped", ["other"], [], str(uuid.uuid4()), "h2")
    result = repo.complete(user["fence"], assignment["lease_token"], 0,
                           "skipped", ["noisy"], [], str(uuid.uuid4()), "h3")
    assert result["status"] == "skipped"


def test_completed_privacy_and_revision_draft(database, seed_tasks):
    seed_tasks(2)
    alice, _ = make_user("alice")
    assignment = repo.claim(alice["fence"])
    segments = full_segments(assignment, "alice")
    repo.complete(alice["fence"], assignment["lease_token"], 0,
                  "annotated", [], segments, str(uuid.uuid4()), "h")

    bob, _ = make_user("bob")
    assert repo.completed_list(bob["id"])["items"] == []
    with pytest.raises(repo.ForbiddenError):
        repo.completed_detail(bob["id"], assignment["task_id"])
    with pytest.raises(repo.ForbiddenError):
        repo.reopen_completed(bob["fence"], assignment["task_id"], str(uuid.uuid4()))

    reopened = repo.reopen_completed(alice["fence"], assignment["task_id"], str(uuid.uuid4()))
    assert reopened["mode"] == "revision"
    with pytest.raises(repo.ConflictError):
        repo.reopen_completed(alice["fence"], assignment["task_id"], str(uuid.uuid4()))
    draft = repo.get_assignment(alice["id"])
    changed = full_segments(draft, "corrected")
    repo.save_draft(alice["fence"], draft["lease_token"], 0, changed,
                    str(uuid.uuid4()), "save")
    published = repo.completed_detail(alice["id"], assignment["task_id"])
    assert published["segments"][0]["text"] == "alice 1"
    repo.abandon(alice["fence"], draft["lease_token"], str(uuid.uuid4()), True)
    published = repo.completed_detail(alice["id"], assignment["task_id"])
    assert published["segments"][0]["text"] == "alice 1"


def test_concurrent_claims_are_unique(database, seed_tasks):
    count = 20
    seed_tasks(count)
    users = [make_user(f"user-{i}")[0] for i in range(count)]
    barrier = threading.Barrier(count)

    def claim_one(user):
        barrier.wait(timeout=10)
        return repo.claim(user["fence"])["task_id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as executor:
        task_ids = list(executor.map(claim_one, users))
    assert len(task_ids) == count
    assert len(set(task_ids)) == count
    assert repo.pool_state()["available"] == 0


def test_same_user_concurrent_claim_is_idempotent(database, seed_tasks):
    seed_tasks(5)
    user, _ = make_user("alice")
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait(timeout=10)
        return repo.claim(user["fence"])["task_id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        ids = list(executor.map(lambda _: claim(), range(2)))
    assert ids[0] == ids[1]
    assert repo.pool_state()["assigned"] == 1


def test_pool_state_distinguishes_unprocessed(database):
    assert repo.pool_state()["reason"] == "no_preprocessed"
    import db
    from preprocess_store import store_preprocessed_task
    with db.db_conn() as conn:
        store_preprocessed_task(
            conn, rel_path="empty.wav", filename="empty.wav", folder="",
            duration=1.0, segments=[], waveform_payload=None,
        )
    state = repo.pool_state()
    assert state["pending"] == 1
    assert state["available"] == 0
    assert state["reason"] == "no_preprocessed"


def test_pool_state_respects_user_reservations(database, seed_tasks):
    import db

    task_id = seed_tasks(1)[0]
    alice, _ = make_user("alice")
    bob, _ = make_user("bob")
    with db.db_tx() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE annotation_tasks SET reserved_for_user_id = %s WHERE id = %s",
            (alice["id"], task_id),
        )

    alice_state = repo.pool_state(alice["id"])
    assert alice_state["available"] == 1
    assert alice_state["reason"] == "available"

    bob_state = repo.pool_state(bob["id"])
    assert bob_state["available"] == 0
    assert bob_state["reason"] == "temporarily_all_assigned"
    with pytest.raises(repo.NoTaskAvailable):
        repo.claim(bob["fence"])

    assert repo.claim(alice["fence"])["task_id"] == task_id


def test_pool_state_reports_all_tasks_assigned_for_another_user(database, seed_tasks):
    seed_tasks(1)
    alice, _ = make_user("alice")
    bob, _ = make_user("bob")
    repo.claim(alice["fence"])

    state = repo.pool_state(bob["id"])
    assert state["pending"] == 1
    assert state["assigned"] == 1
    assert state["available"] == 0
    assert state["reason"] == "temporarily_all_assigned"
    with pytest.raises(repo.NoTaskAvailable):
        repo.claim(bob["fence"])


def test_pool_state_excludes_pending_task_without_draft(database, seed_tasks):
    import db

    task_id = seed_tasks(1)[0]
    user, _ = make_user("alice")
    with db.db_tx() as conn, conn.cursor() as cur:
        # Admin migrations retain an immutable baseline; remove only the
        # claimable working draft to exercise the intended pool edge case.
        cur.execute(
            "DELETE FROM annotation_versions WHERE task_id = %s AND lifecycle = 'draft'",
            (task_id,),
        )

    state = repo.pool_state(user["id"])
    assert state["pending"] == 1
    assert state["available"] == 0
    with pytest.raises(repo.NoTaskAvailable):
        repo.claim(user["fence"])


def test_claim_distinguishes_temporarily_locked_pool(database, seed_tasks):
    import db

    task_id = seed_tasks(1)[0]
    user, _ = make_user("alice")
    with db.db_conn() as blocker:
        blocker.execute(
            "SELECT id FROM annotation_tasks WHERE id = %s FOR UPDATE",
            (task_id,),
        )
        with pytest.raises(repo.TaskPoolBusy):
            repo.claim(user["fence"])


def test_abandon_and_reopen_are_idempotent(database, seed_tasks):
    seed_tasks(2)
    user, _ = make_user("alice")
    assignment = repo.claim(user["fence"])
    op = str(uuid.uuid4())
    first = repo.abandon(user["fence"], assignment["lease_token"], op, True)
    replay = repo.abandon(user["fence"], assignment["lease_token"], op, True)
    assert first["success"] and replay["idempotent_replay"]

    assignment = repo.claim(user["fence"])
    segments = full_segments(assignment)
    repo.complete(user["fence"], assignment["lease_token"], 0,
                  "annotated", [], segments, str(uuid.uuid4()), "complete")
    reopen_op = str(uuid.uuid4())
    opened = repo.reopen_completed(user["fence"], assignment["task_id"], reopen_op)
    reopened = repo.reopen_completed(user["fence"], assignment["task_id"], reopen_op)
    assert opened["lease_token"] == reopened["lease_token"]
    assert reopened["idempotent_replay"]


def test_concurrent_reopen_same_operation_is_idempotent(database, seed_tasks):
    seed_tasks(1)
    user, _ = make_user("alice")
    assignment = repo.claim(user["fence"])
    repo.complete(user["fence"], assignment["lease_token"], 0,
                  "annotated", [], full_segments(assignment),
                  str(uuid.uuid4()), "complete")
    operation = str(uuid.uuid4())
    barrier = threading.Barrier(2)

    def reopen():
        barrier.wait(timeout=10)
        return repo.reopen_completed(user["fence"], assignment["task_id"], operation)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: reopen(), range(2)))
    assert results[0]["lease_token"] == results[1]["lease_token"]
    assert sum(bool(result.get("idempotent_replay")) for result in results) == 1


def test_nan_time_is_rejected(database, seed_tasks):
    seed_tasks(1)
    user, _ = make_user("alice")
    assignment = repo.claim(user["fence"])
    bad = [dict(full_segments(assignment)[0], start=float("nan"))]
    with pytest.raises(repo.ValidationError):
        repo.save_draft(user["fence"], assignment["lease_token"], 0, bad,
                        str(uuid.uuid4()), "nan")


def test_admin_release_abandons_revision_draft(database, seed_tasks):
    import argparse
    import db
    from manage_state import command_release

    seed_tasks(1)
    user, _ = make_user("alice")
    assignment = repo.claim(user["fence"])
    repo.complete(
        user["fence"], assignment["lease_token"], 0,
        "annotated", [], full_segments(assignment),
        str(uuid.uuid4()), "complete",
    )
    repo.reopen_completed(user["fence"], assignment["task_id"], str(uuid.uuid4()))
    draft = repo.get_assignment(user["id"])
    command_release(argparse.Namespace(username="alice", reason="stuck browser"))
    assert repo.get_assignment(user["id"]) is None
    published = repo.completed_detail(user["id"], assignment["task_id"])
    assert published["segments"][0]["text"] == "text 1"
    with db.db_conn() as conn:
        lifecycle = conn.execute(
            "SELECT lifecycle FROM annotation_versions WHERE id = %s",
            (draft["version_id"],),
        ).fetchone()[0]
        drafts = conn.execute(
            "SELECT count(*) FROM annotation_versions WHERE task_id = %s AND lifecycle = 'draft'",
            (assignment["task_id"],),
        ).fetchone()[0]
    assert lifecycle == "abandoned"
    assert drafts == 0


def test_login_same_name_is_atomic(database):
    barrier = threading.Barrier(2)

    def log_in():
        barrier.wait(timeout=10)
        try:
            repo.login("same-name", str(uuid.uuid4()), 1800)
            return "ok"
        except repo.ActiveSessionConflict:
            return "conflict"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: log_in(), range(2)))
    assert sorted(results) == ["conflict", "ok"]
