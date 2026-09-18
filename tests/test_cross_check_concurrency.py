"""Concurrent cross-check claims on isolated Postgres."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import annotation_repository as repo
import db
from tests.test_cross_check_claim import annotate_all, enable_cross_check, fence_of
from tests.test_scene_claims import source, user


def _open_rounds():
    with db.db_conn() as conn:
        return conn.execute(
            """SELECT task_id, secondary_annotator_id, state
               FROM cross_check_rounds
               WHERE state IN ('in_progress', 'awaiting_review')
               ORDER BY created_at, id""",
        ).fetchall()


def _assignments():
    with db.db_conn() as conn:
        return conn.execute(
            """SELECT user_id, task_id, mode, cross_check_round_id
               FROM assignments ORDER BY assigned_at, user_id""",
        ).fetchall()


def test_same_user_double_claim_resumes_one_assignment(database, seed_tasks):
    tasks = seed_tasks(3)
    for task_id in tasks:
        source(task_id, "airport", "high")
    annotate_all("alice-cc-same", tasks, prefix="alice", source_scene="airport")
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = user("bob-cc-same")
    fence = fence_of(bob)
    barrier = threading.Barrier(2)

    def claim_one():
        barrier.wait(timeout=10)
        return repo.claim(fence, source_scene="airport")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(claim_one)
        second = pool.submit(claim_one)
        payloads = [first.result(timeout=15), second.result(timeout=15)]

    left, right = payloads
    assert left["task_id"] == right["task_id"]
    assert left["version_id"] == right["version_id"]
    assert left["lease_token"] == right["lease_token"]
    assert left["mode"] == right["mode"] == "cross_check"
    assert left["cross_check"]["round_id"] == right["cross_check"]["round_id"]
    assert left["cross_check"]["state"] == right["cross_check"]["state"] == "in_progress"
    assert sorted(item["resumed"] for item in payloads) == [False, True]

    rounds = _open_rounds()
    assignments = _assignments()
    assert len(rounds) == 1
    assert str(rounds[0][0]) == left["task_id"]
    assert rounds[0][1] == bob
    assert rounds[0][2] == "in_progress"
    assert len(assignments) == 1
    assert assignments[0][0] == bob
    assert str(assignments[0][1]) == left["task_id"]
    assert assignments[0][2] == "cross_check"


def test_two_users_cannot_both_get_in_progress_on_same_task(database, seed_tasks):
    task_id = seed_tasks(1)[0]
    source(task_id, "airport", "high")
    annotate_all("alice-cc-two", [task_id], prefix="alice", source_scene="airport")
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = user("bob-cc-two")
    carol = user("carol-cc-two")
    barrier = threading.Barrier(2)

    def claim_for(uid):
        barrier.wait(timeout=10)
        try:
            return "ok", repo.claim(fence_of(uid), source_scene="airport")
        except (repo.NoTaskAvailable, repo.TaskPoolBusy) as exc:
            return type(exc).__name__, exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim_for, bob), pool.submit(claim_for, carol)]
        outcomes = [future.result(timeout=15) for future in futures]

    wins = [item[1] for item in outcomes if item[0] == "ok"]
    losses = [item for item in outcomes if item[0] != "ok"]
    assert len(wins) == 1
    assert len(losses) == 1
    assert losses[0][0] in {"NoTaskAvailable", "TaskPoolBusy"}
    winner = wins[0]
    assert winner["mode"] == "cross_check"
    assert winner["task_id"] == str(task_id)
    assert winner["cross_check"]["state"] == "in_progress"

    rounds = _open_rounds()
    assignments = _assignments()
    assert [row[2] for row in rounds] == ["in_progress"]
    assert str(rounds[0][0]) == str(task_id)
    assert len(assignments) == 1
    assert str(assignments[0][1]) == str(task_id)
    in_progress_users = {row[1] for row in rounds}
    assigned_users = {row[0] for row in assignments}
    assert in_progress_users == assigned_users
    assert in_progress_users <= {bob, carol}


def test_twenty_users_claim_distinct_cross_check_tasks(database, seed_tasks):
    tasks = seed_tasks(20)
    for task_id in tasks:
        source(task_id, "airport", "high")
    annotate_all("alice-cc-20", tasks, prefix="alice", source_scene="airport")
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    users = [user(f"cc-fan-{index}") for index in range(20)]
    barrier = threading.Barrier(20)

    def claim_one(uid):
        barrier.wait(timeout=10)
        return repo.claim(fence_of(uid), source_scene="airport")

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(claim_one, uid) for uid in users]
        payloads = [future.result(timeout=30) for future in futures]

    task_ids = [item["task_id"] for item in payloads]
    round_ids = [item["cross_check"]["round_id"] for item in payloads]
    assert all(item["mode"] == "cross_check" for item in payloads)
    assert all(item["cross_check"]["state"] == "in_progress" for item in payloads)
    assert all(item["resumed"] is False for item in payloads)
    assert len(set(task_ids)) == 20
    assert set(task_ids) == set(tasks)
    assert len(set(round_ids)) == 20

    rounds = _open_rounds()
    assignments = _assignments()
    assert len(rounds) == 20
    assert len(assignments) == 20
    assert {str(row[0]) for row in rounds} == set(tasks)
    assert {row[2] for row in rounds} == {"in_progress"}
    assert {row[2] for row in assignments} == {"cross_check"}
    assert len({str(row[3]) for row in assignments}) == 20
