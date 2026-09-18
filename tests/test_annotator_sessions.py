"""Session takeover, idle/absolute timeouts, fencing, and heartbeat contracts."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

import annotation_repository as repo
import db
from tests.test_repository import full_segments


def _session_row(user_id):
    with db.db_conn() as conn:
        return conn.execute(
            """SELECT session_id, generation, expires_at, absolute_expires_at,
                      last_seen_at, last_activity_at, login_time
                 FROM active_sessions WHERE user_id = %s""",
            (user_id,),
        ).fetchone()


def _set_session(user_id, **assignments):
    if not assignments:
        return
    parts = []
    params = []
    for column, value in assignments.items():
        parts.append(f"{column} = {value}")
    params.append(user_id)
    with db.db_conn() as conn:
        conn.execute(
            f"UPDATE active_sessions SET {', '.join(parts)} WHERE user_id = %s",
            params,
        )
        conn.commit()


def _event_types(user_id):
    with db.db_conn() as conn:
        return [row[0] for row in conn.execute(
            """SELECT event_type FROM annotator_session_events
               WHERE user_id = %s ORDER BY occurred_at, id""",
            (user_id,),
        ).fetchall()]


def _event_reasons(user_id):
    with db.db_conn() as conn:
        return [row[0] for row in conn.execute(
            """SELECT details->>'reason' FROM annotator_session_events
               WHERE user_id = %s ORDER BY occurred_at, id""",
            (user_id,),
        ).fetchall()]


def test_first_login_generation_and_deadlines(database):
    actor = repo.login_actor("alice")
    assert actor["generation"] == 1
    assert actor["mode"] == "login"
    assert actor["fence"].generation == 1
    row = _session_row(actor["id"])
    assert int(row[1]) == 1
    with db.db_conn() as conn:
        idle, absolute = conn.execute(
            """SELECT expires_at - now(), absolute_expires_at - now()
               FROM active_sessions WHERE user_id = %s""",
            (actor["id"],),
        ).fetchone()
    assert 29 * 60 < idle.total_seconds() <= 30 * 60
    assert 19.5 * 3600 < absolute.total_seconds() <= 20 * 3600
    assert "login" in _event_types(actor["id"])


def test_resume_same_sid_does_not_bump_generation(database):
    first = repo.login("alice", str(uuid.uuid4()), 1800)
    resumed = repo.login(
        "alice", str(uuid.uuid4()), 1800,
        cookie_session_id=str(first["session_id"]),
        cookie_generation=first["generation"],
    )
    assert resumed["mode"] == "resume"
    assert resumed["generation"] == first["generation"]
    assert str(resumed["session_id"]) == str(first["session_id"])


def test_fresh_presence_conflict(database):
    first = repo.login_actor("alice")
    with pytest.raises(repo.ActiveSessionConflict) as caught:
        repo.login_actor("alice")
    conflict = caught.value
    assert conflict.code == "session_active"
    assert conflict.observed_generation == first["generation"]
    assert str(first["session_id"]) not in str(conflict)
    assert str(first["session_id"]) not in repr(conflict)


def test_stale_presence_auto_takeover_increments_generation(database):
    first = repo.login_actor("alice")
    _set_session(first["id"], **{"last_seen_at": "now() - interval '200 seconds'"})
    second = repo.login_actor("alice")
    assert second["mode"] == "stale_takeover"
    assert second["generation"] == first["generation"] + 1
    assert str(second["session_id"]) != str(first["session_id"])
    assert "stale_takeover" in _event_types(first["id"])


def test_idle_and_absolute_expiry_allow_new_login_with_distinct_reasons(database):
    idle_user = repo.login_actor("idle-user")
    _set_session(idle_user["id"], **{
        "expires_at": "now() - interval '1 second'",
        "last_seen_at": "now()",
    })
    idle_next = repo.login_actor("idle-user")
    assert idle_next["mode"] == "login"
    assert idle_next["reason"] == "idle_timeout"
    assert idle_next["generation"] == idle_user["generation"] + 1

    abs_user = repo.login_actor("abs-user")
    _set_session(abs_user["id"], **{
        "absolute_expires_at": "now() - interval '1 second'",
        "expires_at": "now() - interval '1 second'",
        "last_seen_at": "now()",
    })
    abs_next = repo.login_actor("abs-user")
    assert abs_next["mode"] == "login"
    assert abs_next["reason"] == "absolute_timeout"
    assert "idle_timeout" in _event_reasons(idle_user["id"])
    assert "absolute_timeout" in _event_reasons(abs_user["id"])


def test_forced_takeover_requires_observed_generation(database):
    first = repo.login_actor("alice")
    fingerprint = repo.session_fingerprint(first["session_id"])
    with pytest.raises(repo.SessionChangedConflict):
        repo.force_takeover(
            "alice", str(uuid.uuid4()),
            observed_generation=first["generation"] + 5,
            observed_session_fingerprint=fingerprint,
        )
    taken = repo.force_takeover(
        "alice", str(uuid.uuid4()),
        observed_generation=first["generation"],
        observed_session_fingerprint=fingerprint,
    )
    assert taken["mode"] == "forced_takeover"
    assert taken["generation"] == first["generation"] + 1


def test_forced_takeover_params_cannot_be_replayed(database):
    first = repo.login_actor("alice")
    fingerprint = repo.session_fingerprint(first["session_id"])
    generation = first["generation"]
    repo.force_takeover(
        "alice", str(uuid.uuid4()),
        observed_generation=generation,
        observed_session_fingerprint=fingerprint,
    )
    with pytest.raises(repo.SessionChangedConflict):
        repo.force_takeover(
            "alice", str(uuid.uuid4()),
            observed_generation=generation,
            observed_session_fingerprint=fingerprint,
        )


def test_takeover_token_bound_to_sid_fingerprint_after_logout_relogin(database):
    first = repo.login_actor("alice")
    fingerprint = repo.session_fingerprint(first["session_id"])
    generation = first["generation"]
    repo.logout("alice", first["session_id"], first["generation"])
    second = repo.login_actor("alice")
    assert second["generation"] == 1
    with pytest.raises(repo.SessionChangedConflict):
        repo.force_takeover(
            "alice", str(uuid.uuid4()),
            observed_generation=generation,
            observed_session_fingerprint=fingerprint,
        )


def test_concurrent_stale_takeover_has_one_winner(database):
    first = repo.login_actor("alice")
    _set_session(first["id"], **{"last_seen_at": "now() - interval '200 seconds'"})
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait(timeout=10)
        try:
            return ("ok", repo.login_actor("alice")["generation"])
        except repo.ActiveSessionConflict:
            return ("conflict", None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    outcomes = [item[0] for item in results]
    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 1


def test_concurrent_forced_takeover_has_one_winner(database):
    first = repo.login_actor("alice")
    fingerprint = repo.session_fingerprint(first["session_id"])
    generation = first["generation"]
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait(timeout=10)
        try:
            result = repo.force_takeover(
                "alice", str(uuid.uuid4()),
                observed_generation=generation,
                observed_session_fingerprint=fingerprint,
            )
            return "ok"
        except repo.SessionChangedConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["conflict", "ok"]


def test_old_sid_logout_does_not_delete_new_sid(database):
    first = repo.login_actor("alice")
    _set_session(first["id"], **{"last_seen_at": "now() - interval '200 seconds'"})
    second = repo.login_actor("alice")
    repo.logout("alice", first["session_id"], first["generation"])
    row = _session_row(first["id"])
    assert str(row[0]) == str(second["session_id"])
    assert int(row[1]) == second["generation"]


def test_presence_heartbeat_does_not_extend_idle(database):
    actor = repo.login_actor("alice")
    _set_session(actor["id"], **{
        "last_seen_at": "now() - interval '20 seconds'",
        "expires_at": "now() + interval '10 minutes'",
    })
    before = _session_row(actor["id"])
    repo.session_heartbeat(
        actor["username"], actor["session_id"], actor["generation"],
        activity=False,
    )
    after = _session_row(actor["id"])
    assert after[2] == before[2]
    assert after[3] == before[3]
    assert after[4] > before[4]


def test_activity_heartbeat_extends_idle_but_not_absolute(database):
    actor = repo.login_actor("alice")
    _set_session(actor["id"], **{
        "last_activity_at": "now() - interval '45 seconds'",
        "expires_at": "now() + interval '5 minutes'",
        "absolute_expires_at": "now() + interval '2 minutes'",
    })
    result = repo.session_heartbeat(
        actor["username"], actor["session_id"], actor["generation"],
        activity=True,
    )
    after = _session_row(actor["id"])
    assert after[2] <= after[3]
    assert result["idle_expires_at"] <= result["absolute_expires_at"]
    with db.db_conn() as conn:
        remaining = conn.execute(
            "SELECT absolute_expires_at - expires_at FROM active_sessions WHERE user_id = %s",
            (actor["id"],),
        ).fetchone()[0]
    assert remaining.total_seconds() >= 0


def test_absolute_expiry_cannot_be_resurrected_by_heartbeat(database):
    actor = repo.login_actor("alice")
    _set_session(actor["id"], **{
        "absolute_expires_at": "now() - interval '1 second'",
        "expires_at": "now() - interval '1 second'",
        "last_seen_at": "now()",
    })
    with pytest.raises(repo.SessionFenceError) as caught:
        repo.session_heartbeat(
            actor["username"], actor["session_id"], actor["generation"],
            activity=True,
        )
    assert caught.value.code == "absolute_timeout"


def test_deactivated_annotator_cannot_login_or_takeover(database):
    actor = repo.login_actor("alice")
    admin = repo.create_admin_session(
        "primary",
        hashlib.sha256(b"admin-session-token").hexdigest(),
        hashlib.sha256(b"admin-csrf").hexdigest(),
        1800, 28800,
    )
    repo.admin_deactivate(
        admin_session_id=admin["id"],
        operation_id=str(uuid.uuid4()),
        annotator_id=str(actor["id"]),
        reason="test deactivation",
        confirm=True,
        confirm_username="alice",
    )
    with pytest.raises(repo.ForbiddenError) as caught:
        repo.login_actor("alice")
    assert caught.value.code == "account_deactivated"
    with pytest.raises(repo.ForbiddenError) as takeover_error:
        repo.force_takeover(
            "alice", str(uuid.uuid4()),
            observed_generation=1,
            observed_session_fingerprint="0" * 64,
        )
    assert takeover_error.value.code == "account_deactivated"


def test_assignment_survives_logout_idle_stale_and_forced_takeover(database, seed_tasks):
    seed_tasks(1)
    actor = repo.login_actor("alice")
    assignment = repo.claim(actor["fence"])
    task_id = assignment["task_id"]
    token = assignment["lease_token"]

    repo.logout("alice", actor["session_id"], actor["generation"])
    assert repo.get_assignment(actor["id"])["task_id"] == task_id

    resumed = repo.login_actor("alice")
    assert repo.claim(resumed["fence"])["lease_token"] == token

    _set_session(actor["id"], **{"expires_at": "now() - interval '1 second'"})
    after_idle = repo.login_actor("alice")
    assert repo.get_assignment(actor["id"])["task_id"] == task_id

    _set_session(actor["id"], **{"last_seen_at": "now() - interval '200 seconds'"})
    after_stale = repo.login_actor("alice")
    assert after_stale["mode"] == "stale_takeover"
    assert repo.get_assignment(actor["id"])["lease_token"] == token

    forced = repo.force_takeover(
        "alice", str(uuid.uuid4()),
        observed_generation=after_stale["generation"],
        observed_session_fingerprint=repo.session_fingerprint(after_stale["session_id"]),
    )
    kept = repo.get_assignment(actor["id"])
    assert kept["task_id"] == task_id
    assert kept["lease_token"] == token
    assert forced["mode"] == "forced_takeover"


def test_inspect_session_status_order(database):
    actor = repo.login_actor("alice")
    replaced = repo.inspect_session("alice", str(uuid.uuid4()), 1)
    assert replaced.status == "session_replaced"
    _set_session(actor["id"], **{
        "absolute_expires_at": "now() - interval '1 second'",
        "expires_at": "now() - interval '5 minutes'",
    })
    abs_state = repo.inspect_session(
        "alice", str(actor["session_id"]), actor["generation"],
    )
    assert abs_state.status == "absolute_timeout"
    _set_session(actor["id"], **{
        "absolute_expires_at": "now() + interval '20 hours'",
        "expires_at": "now() - interval '1 second'",
    })
    idle_state = repo.inspect_session(
        "alice", str(actor["session_id"]), actor["generation"],
    )
    assert idle_state.status == "idle_timeout"
    repo.logout("alice", actor["session_id"], actor["generation"])
    logged_out = repo.inspect_session(
        "alice", str(actor["session_id"]), actor["generation"],
    )
    assert logged_out.status == "logged_out"


def test_legacy_cookie_without_generation_is_accepted_when_sid_matches(database):
    actor = repo.login_actor("alice")
    state = repo.inspect_session("alice", str(actor["session_id"]), None)
    assert state.status == "valid"
    assert state.fence.generation == actor["generation"]


def test_save_then_takeover_linearization(database, seed_tasks):
    seed_tasks(1)
    actor = repo.login_actor("alice")
    assignment = repo.claim(actor["fence"])
    acquired = threading.Event()
    original = repo._require_session_fence
    order = []

    def wrapped(cur, fence):
        original(cur, fence)
        acquired.set()
        time.sleep(0.3)

    save_holder = {}
    takeover_holder = {}

    def do_save():
        with patch.object(repo, "_require_session_fence", wrapped):
            segments = full_segments(assignment)
            body = {"segments": segments, "expected_revision": 0}
            req_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            save_holder["result"] = repo.save_draft(
                actor["fence"], assignment["lease_token"], 0,
                segments, str(uuid.uuid4()), req_hash,
            )
            order.append("save")

    def do_takeover():
        assert acquired.wait(10)
        takeover_holder["result"] = repo.force_takeover(
            "alice", str(uuid.uuid4()),
            observed_generation=actor["generation"],
            observed_session_fingerprint=repo.session_fingerprint(actor["session_id"]),
        )
        order.append("takeover")

    with ThreadPoolExecutor(max_workers=2) as pool:
        save_future = pool.submit(do_save)
        takeover_future = pool.submit(do_takeover)
        save_future.result(timeout=15)
        takeover_future.result(timeout=15)
    assert order == ["save", "takeover"]
    assert save_holder["result"]["revision"] == 1
    assert takeover_holder["result"]["generation"] == actor["generation"] + 1
    with db.db_conn() as conn:
        revision = conn.execute(
            "SELECT revision FROM annotation_versions WHERE id = %s",
            (assignment["version_id"],),
        ).fetchone()[0]
    assert revision == 1


def test_takeover_then_old_save_is_rejected_before_mutation(database, seed_tasks):
    seed_tasks(1)
    actor = repo.login_actor("alice")
    assignment = repo.claim(actor["fence"])
    repo.force_takeover(
        "alice", str(uuid.uuid4()),
        observed_generation=actor["generation"],
        observed_session_fingerprint=repo.session_fingerprint(actor["session_id"]),
    )
    segments = full_segments(assignment)
    body = {"segments": segments, "expected_revision": 0}
    req_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    with pytest.raises(repo.SessionFenceError) as caught:
        repo.save_draft(
            actor["fence"], assignment["lease_token"], 0,
            segments, str(uuid.uuid4()), req_hash,
        )
    assert caught.value.code == "session_replaced"
    with db.db_conn() as conn:
        revision = conn.execute(
            "SELECT revision FROM annotation_versions WHERE id = %s",
            (assignment["version_id"],),
        ).fetchone()[0]
        op_count = conn.execute("SELECT count(*) FROM operations").fetchone()[0]
        texts = [row[0] for row in conn.execute(
            "SELECT text FROM segments WHERE version_id = %s ORDER BY segment_id",
            (assignment["version_id"],),
        ).fetchall()]
    assert revision == 0
    assert op_count == 0
    assert texts == ["", ""]


def test_takeover_rejects_all_five_write_paths(database, seed_tasks):
    seed_tasks(2)
    actor = repo.login_actor("alice")
    assignment = repo.claim(actor["fence"])
    repo.force_takeover(
        "alice", str(uuid.uuid4()),
        observed_generation=actor["generation"],
        observed_session_fingerprint=repo.session_fingerprint(actor["session_id"]),
    )
    segments = full_segments(assignment)
    req_hash = hashlib.sha256(b"x").hexdigest()
    with pytest.raises(repo.SessionFenceError):
        repo.claim(actor["fence"])
    with pytest.raises(repo.SessionFenceError):
        repo.save_draft(
            actor["fence"], assignment["lease_token"], 0,
            segments, str(uuid.uuid4()), req_hash,
        )
    with pytest.raises(repo.SessionFenceError):
        repo.complete(
            actor["fence"], assignment["lease_token"], 0,
            "annotated", [], segments, str(uuid.uuid4()), req_hash,
        )
    with pytest.raises(repo.SessionFenceError):
        repo.abandon(actor["fence"], assignment["lease_token"], str(uuid.uuid4()), True)
    with pytest.raises(repo.SessionFenceError):
        repo.reopen_completed(actor["fence"], assignment["task_id"], str(uuid.uuid4()))


def test_fence_runs_before_operation_replay(database, seed_tasks):
    seed_tasks(1)
    actor = repo.login_actor("alice")
    assignment = repo.claim(actor["fence"])
    segments = full_segments(assignment)
    op = str(uuid.uuid4())
    body = {"segments": segments, "expected_revision": 0}
    req_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    saved = repo.save_draft(
        actor["fence"], assignment["lease_token"], 0, segments, op, req_hash,
    )
    assert saved["revision"] == 1
    repo.force_takeover(
        "alice", str(uuid.uuid4()),
        observed_generation=actor["generation"],
        observed_session_fingerprint=repo.session_fingerprint(actor["session_id"]),
    )
    with pytest.raises(repo.SessionFenceError) as caught:
        repo.save_draft(
            actor["fence"], assignment["lease_token"], 0, segments, op, req_hash,
        )
    assert caught.value.code == "session_replaced"
    with db.db_conn() as conn:
        revision = conn.execute(
            "SELECT revision FROM annotation_versions WHERE id = %s",
            (assignment["version_id"],),
        ).fetchone()[0]
        ops = conn.execute("SELECT count(*) FROM operations").fetchone()[0]
    assert revision == 1
    assert ops == 1


def test_write_methods_require_session_fence(database, seed_tasks):
    seed_tasks(1)
    actor = repo.login_actor("alice")
    assignment = repo.claim(actor["fence"])
    with pytest.raises(repo.ValidationError, match="session fence"):
        repo.claim(actor["id"])
    with pytest.raises(repo.ValidationError, match="session fence"):
        repo.save_draft(
            actor["id"], assignment["lease_token"], 0, [],
            str(uuid.uuid4()), "h",
        )
