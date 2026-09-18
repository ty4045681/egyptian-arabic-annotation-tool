"""Cross-check integration with reopen, revoke, restore, deactivate, media."""

from __future__ import annotations

import argparse
import concurrent.futures
import threading
import uuid
from pathlib import Path

import pytest

import annotation_repository as repo
import db
import server
from preprocess_store import TaskProtectedError, store_preprocessed_task
from tests.test_admin_api import ADMIN_KEY_DIGEST, _admin_login
from tests.test_admin_repository import _admin_session
from tests.test_api import login
from tests.test_cross_check_admin import (
    decision_body,
    queue_rounds,
    round_state,
    task_row,
    user_id,
    version_info,
)
from tests.test_cross_check_claim import (
    ScriptedRng,
    annotate_all,
    enable_cross_check,
    fence_of,
    user,
)
from tests.test_cross_check_submit import (
    bob_claim,
    changed_words,
    complete,
    seed_annotated,
    text_segments,
    words,
)
from tests.test_repository import full_segments
from tests.test_scene_claims import source


SECRET_A = "ALPHA-ORIGINAL-TRANSCRIPT"
SECRET_B = "BRAVO-SECONDARY-TRANSCRIPT"


@pytest.fixture
def admin_client(client, monkeypatch):
    monkeypatch.setenv("ANNOTATION_ADMIN_KEY_SHA256", ADMIN_KEY_DIGEST)
    monkeypatch.setitem(server.app.config, "ADMIN_KEY_SHA256", ADMIN_KEY_DIGEST)
    monkeypatch.setitem(server.app.config, "ADMIN_KEY_ID", "primary")
    monkeypatch.setitem(server.app.config, "ADMIN_SESSION_COOKIE_NAME", "admin_session")
    monkeypatch.setitem(server.app.config, "ADMIN_SESSION_COOKIE_SECURE", False)
    monkeypatch.setitem(server.app.config, "ADMIN_SESSION_IDLE_SECONDS", 1800)
    monkeypatch.setitem(server.app.config, "ADMIN_SESSION_ABSOLUTE_SECONDS", 28800)
    return client


def _round_row(task_id):
    with db.db_conn() as conn:
        return conn.execute(
            """SELECT id, state, termination_reason, secondary_version_id
               FROM cross_check_rounds WHERE task_id = %s
               ORDER BY created_at DESC LIMIT 1""",
            (task_id,),
        ).fetchone()


def _open_round(task_id):
    with db.db_conn() as conn:
        return conn.execute(
            """SELECT state FROM cross_check_rounds
               WHERE task_id = %s
                 AND state IN ('in_progress', 'awaiting_review')""",
            (task_id,),
        ).fetchone()


def _version_lifecycle(version_id):
    with db.db_conn() as conn:
        return conn.execute(
            "SELECT lifecycle FROM annotation_versions WHERE id = %s",
            (version_id,),
        ).fetchone()[0]


def _task_rel_path(task_id):
    with db.db_conn() as conn:
        return conn.execute(
            "SELECT rel_path, filename, folder FROM annotation_tasks WHERE id = %s",
            (task_id,),
        ).fetchone()


def _event_types(task_id):
    with db.db_conn() as conn:
        return [
            row[0] for row in conn.execute(
                """SELECT event_type FROM annotation_events
                   WHERE task_id = %s ORDER BY id""",
                (task_id,),
            ).fetchall()
        ]


def _write_audio(client, rel_path, payload=b"RIFFWAVE"):
    root = Path(server.app.config["AUDIO_DIR"])
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _admin_headers(admin_client):
    _, headers = _admin_login(admin_client)
    return headers


def _reopen_body():
    return {"operation_id": str(uuid.uuid4())}


def test_reopen_409_while_in_progress_and_awaiting_review(client, seed_tasks):
    task_ids, completed = seed_annotated(client, seed_tasks, 2, text=SECRET_A)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")
    live = bob_claim(client)
    assert live["mode"] == "cross_check"
    client.post("/api/logout", json={})
    login(client, "alice")
    blocked_live = client.post(
        f"/api/completed/{live['task_id']}/reopen", json=_reopen_body(),
    )
    assert blocked_live.status_code == 409
    assert blocked_live.json.get("code") == "cross_check_active"
    client.post("/api/logout", json={})

    login(client, "bob")
    queued, _ = complete(
        client, live, segments=text_segments(live, SECRET_B),
    )
    assert queued.status_code == 200, queued.json
    assert queued.json["cross_check"]["state"] == "awaiting_review"
    client.post("/api/logout", json={})
    login(client, "alice")
    blocked_first = client.post(
        f"/api/completed/{live['task_id']}/reopen", json=_reopen_body(),
    )
    assert blocked_first.status_code == 409
    assert blocked_first.json.get("code") == "cross_check_active"
    client.post("/api/logout", json={})

    login(client, "bob")
    other = bob_claim(client)
    queued_other, _ = complete(
        client, other, segments=text_segments(other, changed_words(20, 8)),
    )
    assert queued_other.json["cross_check"]["state"] == "awaiting_review"
    client.post("/api/logout", json={})
    login(client, "alice")
    blocked_queued = client.post(
        f"/api/completed/{other['task_id']}/reopen", json=_reopen_body(),
    )
    assert blocked_queued.status_code == 409
    assert blocked_queued.json.get("code") == "cross_check_active"
    assert {str(tid) for tid in task_ids} >= {live["task_id"], other["task_id"]}
    assert completed


def test_revoke_original_invalidates_open_round_and_unblocks(
        admin_client, seed_tasks):
    queued = queue_rounds(admin_client, seed_tasks, 1, original_text=SECRET_A,
                          secondary_text=SECRET_B)
    task_id = queued[0]["task_id"]
    round_id = queued[0]["round_id"]
    secondary_id = queued[0]["secondary_version_id"]
    original_id = str(round_state(round_id)[2])
    alice = str(user_id("alice"))
    headers = _admin_headers(admin_client)

    preview_blocked = admin_client.post(
        "/api/admin/annotations/revoke/preview",
        json={
            "annotator_id": alice,
            "items": [{"task_id": task_id, "expected_version_id": original_id}],
            "block_reclaim": True,
            "release_conflicts": False,
        },
        headers=headers,
    )
    assert preview_blocked.status_code == 200, preview_blocked.json
    assert preview_blocked.json["items"][0]["conflict"] == "active_revision"
    assert preview_blocked.json["items"][0]["open_cross_check_state"] == (
        "awaiting_review"
    )
    blocked = admin_client.post(
        "/api/admin/annotations/revoke",
        json={
            "operation_id": str(uuid.uuid4()),
            "annotator_id": alice,
            "items": [{"task_id": task_id, "expected_version_id": original_id}],
            "reason": "must fail while awaiting review",
            "block_reclaim": True,
            "confirm": True,
            "release_conflicts": False,
        },
        headers=headers,
    )
    assert blocked.status_code == 409
    assert round_state(round_id)[0] == "awaiting_review"

    preview = admin_client.post(
        "/api/admin/annotations/revoke/preview",
        json={
            "annotator_id": alice,
            "items": [{"task_id": task_id, "expected_version_id": original_id}],
            "block_reclaim": True,
            "release_conflicts": True,
        },
        headers=headers,
    )
    assert preview.status_code == 200, preview.json
    assert preview.json["items"][0]["revokeable"] is True
    assert preview.json["items"][0]["will_invalidate_cross_check"] is True
    revoked = admin_client.post(
        "/api/admin/annotations/revoke",
        json={
            "operation_id": str(uuid.uuid4()),
            "annotator_id": alice,
            "items": [{"task_id": task_id, "expected_version_id": original_id}],
            "reason": "invalidate open cross-check",
            "block_reclaim": True,
            "confirm": True,
            "release_conflicts": True,
        },
        headers=headers,
    )
    assert revoked.status_code == 200, revoked.json
    assert revoked.json["summary"]["invalidated"] == 1
    after = round_state(round_id)
    assert after[0] == "invalidated"
    assert task_row(task_id)[0] == "pending"
    assert task_row(task_id)[1] is None
    assert _version_lifecycle(original_id) == "revoked"
    assert _version_lifecycle(secondary_id) == "cross_check_submitted"
    assert "cross_check_invalidated" in _event_types(task_id)
    assert _open_round(task_id) is None


def test_revoke_in_progress_with_release_conflicts_and_cli_release(
        database, seed_tasks):
    task = seed_tasks(1, folder="life")[0]
    source(task, "airport", "high")
    alice, completed = annotate_all(
        "alice-life", [task], prefix="alice-keep", source_scene="airport",
    )
    published = completed[0]["version_id"]
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = user("bob-life")
    assignment = repo.claim(
        fence_of(bob), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    admin = _admin_session()
    with pytest.raises(repo.ConflictError, match="active revision"):
        repo.admin_revoke(
            admin_session_id=admin["id"],
            operation_id=str(uuid.uuid4()),
            annotator_id=alice,
            items=[{
                "task_id": assignment["task_id"],
                "expected_version_id": published,
            }],
            reason="in progress without release",
            block_reclaim=True,
            confirm=True,
            release_conflicts=False,
        )
    assert _round_row(task)[1] == "in_progress"

    revoked = repo.admin_revoke(
        admin_session_id=admin["id"],
        operation_id=str(uuid.uuid4()),
        annotator_id=alice,
        items=[{
            "task_id": assignment["task_id"],
            "expected_version_id": published,
        }],
        reason="release in-progress cross-check",
        block_reclaim=True,
        confirm=True,
        release_conflicts=True,
    )
    assert revoked["summary"]["invalidated"] == 1
    row = _round_row(task)
    assert row[1] == "invalidated"
    assert repo.get_assignment(bob) is None
    assert _version_lifecycle(assignment["version_id"]) == "abandoned"


def test_cli_assignment_release_cancels_in_progress_round(database, seed_tasks):
    task = seed_tasks(1, folder="cli")[0]
    source(task, "airport", "high")
    annotate_all("alice-cli", [task], prefix="alice-cli", source_scene="airport")
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = user("bob-cli")
    live = repo.claim(
        fence_of(bob), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    from manage_state import command_release
    command_release(argparse.Namespace(username="bob-cli", reason="stuck cli"))
    assert repo.get_assignment(bob) is None
    cli_round = _round_row(live["task_id"])
    assert cli_round[1] == "cancelled"
    assert cli_round[2] == "stuck cli"
    assert task_row(live["task_id"])[0] == "annotated"
    assert _version_lifecycle(live["version_id"]) == "abandoned"


def test_admin_edited_revoke_restore_and_null_annotator_guard(
        admin_client, seed_tasks):
    queued = queue_rounds(
        admin_client, seed_tasks, 1,
        original_text=SECRET_A, secondary_text=SECRET_B,
    )
    round_id = queued[0]["round_id"]
    headers = _admin_headers(admin_client)
    detail = admin_client.get(f"/api/admin/cross-checks/{round_id}").json
    ordinary_null = admin_client.post(
        "/api/admin/annotations/revoke/preview",
        json={
            "annotator_id": None,
            "items": [{
                "task_id": queued[0]["task_id"],
                "expected_version_id": detail["original_version_id"],
            }],
            "block_reclaim": False,
        },
        headers=headers,
    )
    assert ordinary_null.status_code == 200
    assert ordinary_null.json["items"][0]["conflict"] == "not_admin_adjudication"
    ordinary_exec = admin_client.post(
        "/api/admin/annotations/revoke",
        json={
            "operation_id": str(uuid.uuid4()),
            "annotator_id": None,
            "items": [{
                "task_id": queued[0]["task_id"],
                "expected_version_id": detail["original_version_id"],
            }],
            "reason": "must not treat ordinary as admin-edited",
            "block_reclaim": False,
            "confirm": True,
        },
        headers=headers,
    )
    assert ordinary_exec.status_code == 409
    assert task_row(queued[0]["task_id"])[0] == "annotated"
    assert round_state(round_id)[0] == "awaiting_review"

    edited_segments = []
    for segment in detail["original_segments"]:
        item = dict(segment)
        if not item.get("exclude_from_training"):
            item["text"] = "admin edited " + (item.get("text") or "")
        edited_segments.append(item)
    decided = admin_client.post(
        f"/api/admin/cross-checks/{round_id}/decision",
        json=decision_body(
            detail, decision="edited", reason="admin rewrite",
            base="original", segments=edited_segments,
            target_status="annotated",
        ),
        headers=headers,
    )
    assert decided.status_code == 200, decided.json
    final_id = decided.json["final_version_id"]
    task_id = queued[0]["task_id"]
    final = version_info(final_id)
    assert final[1] == "adjudication"
    assert final[2] is None
    assert final[3] == user_id("alice")
    assert final[4] is not None

    missing = admin_client.post(
        "/api/admin/annotations/revoke/preview",
        json={
            "items": [{"task_id": task_id, "expected_version_id": final_id}],
            "block_reclaim": False,
        },
        headers=headers,
    )
    assert missing.status_code == 400

    reclaim_true = admin_client.post(
        "/api/admin/annotations/revoke",
        json={
            "operation_id": str(uuid.uuid4()),
            "annotator_id": None,
            "items": [{"task_id": task_id, "expected_version_id": final_id}],
            "reason": "must force block_reclaim false",
            "block_reclaim": True,
            "confirm": True,
        },
        headers=headers,
    )
    assert reclaim_true.status_code == 400

    preview = admin_client.post(
        "/api/admin/annotations/revoke/preview",
        json={
            "annotator_id": None,
            "items": [{"task_id": task_id, "expected_version_id": final_id}],
            "block_reclaim": False,
        },
        headers=headers,
    )
    assert preview.status_code == 200, preview.json
    assert preview.json["items"][0]["revokeable"] is True
    assert preview.json["items"][0]["will_block_reclaim"] is False
    revoked = admin_client.post(
        "/api/admin/annotations/revoke",
        json={
            "operation_id": str(uuid.uuid4()),
            "annotator_id": None,
            "items": [{"task_id": task_id, "expected_version_id": final_id}],
            "reason": "admin-edited revoke",
            "block_reclaim": False,
            "confirm": True,
        },
        headers=headers,
    )
    assert revoked.status_code == 200, revoked.json
    assert revoked.json["summary"]["blocked"] == 0
    with db.db_conn() as conn:
        item = conn.execute(
            """SELECT annotator_id, details FROM admin_action_items
               WHERE admin_action_id = %s""",
            (revoked.json["action_id"],),
        ).fetchone()
        blocks = conn.execute(
            "SELECT count(*) FROM task_annotator_blocks WHERE task_id = %s",
            (task_id,),
        ).fetchone()[0]
    assert item[0] is None
    assert item[1]["actual_author"] == "admin"
    assert item[1]["source_publish_action_id"] == str(final[4])
    assert blocks == 0
    assert _version_lifecycle(final_id) == "revoked"

    alice = user_id("alice")
    deactivated = repo.admin_deactivate(
        admin_session_id=_admin_session()["id"],
        operation_id=str(uuid.uuid4()),
        annotator_id=alice,
        reason="credited user offboarding",
        confirm=True,
        confirm_username="alice",
    )
    assert deactivated["summary"]["revoked"] == 0
    restored = admin_client.post(
        "/api/admin/annotations/restore",
        json={
            "operation_id": str(uuid.uuid4()),
            "admin_action_id": revoked.json["action_id"],
            "task_ids": [task_id],
            "reason": "restore admin-edited",
            "confirm": True,
        },
        headers=headers,
    )
    assert restored.status_code == 200, restored.json
    assert task_row(task_id)[0] == "annotated"
    assert str(task_row(task_id)[1]) == final_id
    assert version_info(final_id)[0] == "published"
    assert version_info(final_id)[2] is None


def test_deactivate_does_not_revoke_admin_edited_credited_result(
        admin_client, seed_tasks):
    queued = queue_rounds(admin_client, seed_tasks, 1)
    round_id = queued[0]["round_id"]
    headers = _admin_headers(admin_client)
    detail = admin_client.get(f"/api/admin/cross-checks/{round_id}").json
    decided = admin_client.post(
        f"/api/admin/cross-checks/{round_id}/decision",
        json=decision_body(
            detail, decision="edited", reason="keep edited",
            base="original", segments=detail["original_segments"],
            target_status="annotated",
        ),
        headers=headers,
    )
    assert decided.status_code == 200, decided.json
    final_id = decided.json["final_version_id"]
    preview = repo.admin_deactivate_preview(user_id("alice"))
    assert preview["summary"]["published_to_revoke"] == 0
    result = repo.admin_deactivate(
        admin_session_id=_admin_session()["id"],
        operation_id=str(uuid.uuid4()),
        annotator_id=user_id("alice"),
        reason="offboard credited author",
        confirm=True,
        confirm_username="alice",
    )
    assert result["summary"]["revoked"] == 0
    assert str(task_row(queued[0]["task_id"])[1]) == final_id
    assert version_info(final_id)[0] == "published"


def test_deactivate_secondary_cancels_in_progress_keeps_awaiting_review(
        database, seed_tasks):
    live_task, queued_task = seed_tasks(2, folder="deact")
    source(live_task, "airport", "high")
    source(queued_task, "airport", "high")
    annotate_all(
        "alice-deact", [live_task, queued_task], prefix=SECRET_A,
        source_scene="airport",
    )
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = user("bob-deact")
    live = repo.claim(
        fence_of(bob), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    repo.complete(
        fence_of(bob), live["lease_token"], live["revision"],
        "annotated", [], full_segments(live, SECRET_B),
        str(uuid.uuid4()), "bob-queue",
    )
    queued_round = _round_row(live["task_id"])
    assert queued_round[1] == "awaiting_review"
    second = repo.claim(
        fence_of(bob), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    assert second["mode"] == "cross_check"
    original_live = task_row(second["task_id"])
    original_queued = task_row(live["task_id"])
    preview = repo.admin_deactivate_preview(bob)
    assert preview["summary"]["cross_check_in_progress_to_cancel"] == 1
    assert preview["summary"]["cross_check_awaiting_review_kept"] == 1
    result = repo.admin_deactivate(
        admin_session_id=_admin_session()["id"],
        operation_id=str(uuid.uuid4()),
        annotator_id=bob,
        reason="secondary offboarding",
        confirm=True,
        confirm_username="bob-deact",
    )
    assert result["summary"]["revoked"] == 0
    assert _round_row(second["task_id"])[1] == "cancelled"
    assert _version_lifecycle(second["version_id"]) == "abandoned"
    assert task_row(second["task_id"]) == original_live
    kept = _round_row(live["task_id"])
    assert kept[1] == "awaiting_review"
    assert _version_lifecycle(kept[3]) == "cross_check_submitted"
    assert task_row(live["task_id"]) == original_queued
    assert version_info(original_queued[1])[0] == "published"


def test_deactivate_original_invalidates_awaiting_review_409s_in_progress(
        database, seed_tasks):
    live_task, queued_task = seed_tasks(2, folder="orig-deact")
    source(live_task, "airport", "high")
    source(queued_task, "airport", "high")
    alice, completed = annotate_all(
        "alice-orig-deact", [live_task, queued_task], prefix=SECRET_A,
        source_scene="airport",
    )
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = user("bob-orig-deact")
    first = repo.claim(
        fence_of(bob), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    repo.complete(
        fence_of(bob), first["lease_token"], first["revision"],
        "annotated", [], full_segments(first, SECRET_B),
        str(uuid.uuid4()), "bob-first",
    )
    second = repo.claim(
        fence_of(bob), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    assert second["mode"] == "cross_check"
    with pytest.raises(repo.ConflictError, match="another annotator's active"):
        repo.admin_deactivate(
            admin_session_id=_admin_session()["id"],
            operation_id=str(uuid.uuid4()),
            annotator_id=alice,
            reason="cannot steal in-progress",
            confirm=True,
            confirm_username="alice-orig-deact",
        )
    with db.db_conn() as conn:
        status = conn.execute(
            "SELECT status FROM annotators WHERE id = %s", (alice,),
        ).fetchone()[0]
    assert status == "active"
    assert _round_row(second["task_id"])[1] == "in_progress"

    repo.abandon(
        fence_of(bob), second["lease_token"], str(uuid.uuid4()), True,
    )
    queued_id = first["task_id"]
    secondary_id = first["version_id"]
    result = repo.admin_deactivate(
        admin_session_id=_admin_session()["id"],
        operation_id=str(uuid.uuid4()),
        annotator_id=alice,
        reason="offboard original",
        confirm=True,
        confirm_username="alice-orig-deact",
    )
    assert result["summary"]["revoked"] >= 1
    assert _round_row(queued_id)[1] == "invalidated"
    assert _version_lifecycle(secondary_id) == "cross_check_submitted"
    assert task_row(queued_id)[0] == "pending"
    assert completed


def test_awaiting_review_not_unlocked_by_cancel_disable_logout(
        admin_client, seed_tasks):
    queued = queue_rounds(
        admin_client, seed_tasks, 1,
        original_text=words(100), secondary_text=changed_words(100, 11),
    )
    round_id = queued[0]["round_id"]
    login(admin_client, "bob")
    admin_client.post("/api/logout", json={})
    assert round_state(round_id)[0] == "awaiting_review"

    headers = _admin_headers(admin_client)
    disabled = admin_client.put(
        "/api/admin/cross-check-settings",
        json={
            "operation_id": str(uuid.uuid4()),
            "expected_revision": 0,
            "enabled": False,
            "sampling_rate_bps": 1000,
            "reason": "stop new claims only",
        },
        headers=headers,
    )
    assert disabled.status_code == 200, disabled.json
    assert round_state(round_id)[0] == "awaiting_review"
    cancelled = admin_client.post(
        f"/api/admin/cross-checks/{round_id}/cancel",
        json={
            "operation_id": str(uuid.uuid4()),
            "expected_revision": round_state(round_id)[1],
            "reason": "cannot cancel queued review",
            "confirm": True,
        },
        headers=headers,
    )
    assert cancelled.status_code == 409
    assert cancelled.json.get("code") == "cross_check_state_conflict"
    assert round_state(round_id)[0] == "awaiting_review"
    assert _open_round(queued[0]["task_id"]) is not None


def test_scene_review_correction_blocked_while_round_open(
        client, seed_tasks):
    task_ids = seed_tasks(1)
    source(task_ids[0], "airport", "high")
    login(client, "alice")
    claimed = client.post(
        "/api/assignment/claim", json={"source_scene": "airport"},
    )
    assignment = claimed.json
    complete_payload = {
        "lease_token": assignment["lease_token"],
        "expected_revision": assignment["revision"],
        "target_status": "annotated",
        "skip_reasons": [],
        "operation_id": str(uuid.uuid4()),
        "segments": text_segments(assignment, SECRET_A),
        "scene_review": {
            "status": "confirmed",
            "scene_codes": ["airport"],
            "note": "alice-original-note",
        },
    }
    assert client.post(
        "/api/assignment/current/complete", json=complete_payload,
    ).status_code == 200
    client.post("/api/logout", json={})
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")
    bob_assignment = bob_claim(client)
    client.post("/api/logout", json={})
    detail = repo.admin_annotation_detail(bob_assignment["task_id"])
    session = _admin_session()
    payload = {
        "operation_id": str(uuid.uuid4()),
        "expected_version_id": detail["current_version_id"],
        "expected_review_id": detail["metadata"]["scene_review"]["id"],
        "status": "mixed",
        "scene_codes": ["airport", "shopping"],
        "note": "must not change frozen evidence",
        "reason": "blocked by cross-check",
    }
    with pytest.raises(repo.ConflictError) as exc:
        repo.admin_correct_scene_review(
            session["id"], bob_assignment["task_id"], payload,
        )
    assert exc.value.code == "cross_check_active"
    assert _round_row(bob_assignment["task_id"])[1] == "in_progress"


def test_preprocess_cannot_wipe_cross_check_submitted(admin_client, seed_tasks):
    queued = queue_rounds(
        admin_client, seed_tasks, 1,
        original_text=SECRET_A, secondary_text=SECRET_B,
    )
    task_id = queued[0]["task_id"]
    round_id = queued[0]["round_id"]
    secondary_id = queued[0]["secondary_version_id"]
    original_id = str(round_state(round_id)[2])
    headers = _admin_headers(admin_client)
    revoked = admin_client.post(
        "/api/admin/annotations/revoke",
        json={
            "operation_id": str(uuid.uuid4()),
            "annotator_id": str(user_id("alice")),
            "items": [{"task_id": task_id, "expected_version_id": original_id}],
            "reason": "revoke then preprocess",
            "block_reclaim": True,
            "confirm": True,
            "release_conflicts": True,
        },
        headers=headers,
    )
    assert revoked.status_code == 200, revoked.json
    rel_path, filename, folder = _task_rel_path(task_id)
    with db.db_conn() as conn:
        baseline = conn.execute(
            """SELECT t.baseline_version_id, s.text
               FROM annotation_tasks t
               JOIN segments s ON s.version_id = t.baseline_version_id
               WHERE t.id = %s ORDER BY s.segment_id""",
            (task_id,),
        ).fetchall()
        submitted_before = conn.execute(
            """SELECT text FROM segments
               WHERE version_id = %s ORDER BY segment_id""",
            (secondary_id,),
        ).fetchall()
        result = store_preprocessed_task(
            conn, rel_path=rel_path, filename=filename, folder=folder,
            duration=10.0,
            segments=[
                {"id": 1, "start": 0.0, "end": 5.0, "duration": 5.0,
                 "asr_text": "NEW ASR", "text": "WIPED",
                 "exclude_from_training": False},
                {"id": 2, "start": 5.0, "end": 10.0, "duration": 5.0,
                 "asr_text": "NEW ASR 2", "text": "WIPED 2",
                 "exclude_from_training": False},
            ],
        )
        with pytest.raises(TaskProtectedError):
            store_preprocessed_task(
                conn, rel_path=rel_path, filename=filename, folder=folder,
                duration=10.0,
                segments=[
                    {"id": 1, "start": 0.0, "end": 5.0, "duration": 5.0,
                     "asr_text": "NEW ASR", "text": "WIPED",
                     "exclude_from_training": False},
                ],
                skip_if_human_modified=False,
            )
        submitted_after = conn.execute(
            """SELECT text FROM segments
               WHERE version_id = %s ORDER BY segment_id""",
            (secondary_id,),
        ).fetchall()
        baseline_after = conn.execute(
            """SELECT text FROM segments
               WHERE version_id = %s ORDER BY segment_id""",
            (baseline[0][0],),
        ).fetchall()
    assert result["action"] == "skipped_human"
    assert submitted_after == submitted_before
    assert SECRET_B in submitted_after[0][0]
    assert [row[0] for row in baseline_after] == [row[1] for row in baseline]
    assert _version_lifecycle(secondary_id) == "cross_check_submitted"


def test_media_after_submit_and_completed_does_not_leak_original(
        client, seed_tasks):
    seed_annotated(client, seed_tasks, 1, text=SECRET_A)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")
    assignment = bob_claim(client)
    task_id = assignment["task_id"]
    payload = _write_audio(client, assignment["rel_path"])
    live_audio = client.get(f"/api/audio/{task_id}")
    assert live_audio.status_code == 200
    assert live_audio.data == payload
    live_wave = client.get(f"/api/waveform/{task_id}")
    assert live_wave.status_code == 200
    assert live_wave.json["waveform_b64"]

    queued, _ = complete(
        client, assignment, segments=text_segments(assignment, SECRET_B),
    )
    assert queued.status_code == 200, queued.json
    assert queued.json["cross_check"]["state"] == "awaiting_review"
    round_id = queued.json["cross_check"]["round_id"]

    after_audio = client.get(f"/api/audio/{task_id}")
    assert after_audio.status_code == 200
    assert after_audio.data == payload
    after_wave = client.get(f"/api/waveform/{task_id}")
    assert after_wave.status_code == 200
    completed = client.get(f"/api/completed/{task_id}")
    assert completed.status_code == 403
    assert SECRET_A not in str(completed.json)
    assert SECRET_B not in str(completed.json)
    listed = client.get("/api/completed")
    assert listed.status_code == 200
    assert listed.json["items"] == []
    submission = client.get(f"/api/cross-checks/{round_id}/submission")
    assert submission.status_code == 200, submission.json
    blob = str(submission.json)
    assert SECRET_B in blob
    assert SECRET_A not in blob
    media = repo.authorized_media(user_id("bob"), task_id)
    assert media["rel_path"] == assignment["rel_path"]

    client.post("/api/logout", json={})
    login(client, "carol")
    assert client.get(f"/api/audio/{task_id}").status_code == 403
    assert client.get(f"/api/waveform/{task_id}").status_code == 403
    leaked = client.get(f"/api/completed/{task_id}")
    assert leaked.status_code == 403
    assert SECRET_A not in str(leaked.json)


def test_complete_racing_revoke_and_reopen_racing_claim(database, seed_tasks):
    live_task, other_task = seed_tasks(2, folder="race")
    source(live_task, "airport", "high")
    source(other_task, "airport", "high")
    alice, completed = annotate_all(
        "alice-race", [live_task, other_task], prefix=SECRET_A,
        source_scene="airport",
    )
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = user("bob-race")
    assignment = repo.claim(
        fence_of(bob), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    admin = _admin_session()
    expected_original = next(
        item["version_id"] for item in completed
        if item["task_id"] == assignment["task_id"]
    )
    barrier = threading.Barrier(2)

    def complete_secondary():
        barrier.wait(timeout=10)
        try:
            return "ok", repo.complete(
                fence_of(bob), assignment["lease_token"],
                assignment["revision"], "annotated", [],
                full_segments(assignment, SECRET_B),
                str(uuid.uuid4()), "race-complete",
            )
        except repo.ConflictError as exc:
            return "conflict", str(exc)

    def revoke_original():
        barrier.wait(timeout=10)
        try:
            return "ok", repo.admin_revoke(
                admin_session_id=admin["id"],
                operation_id=str(uuid.uuid4()),
                annotator_id=alice,
                items=[{
                    "task_id": assignment["task_id"],
                    "expected_version_id": expected_original,
                }],
                reason="race revoke",
                block_reclaim=True,
                confirm=True,
                release_conflicts=True,
            )
        except repo.ConflictError as exc:
            return "conflict", str(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        complete_future = pool.submit(complete_secondary)
        revoke_future = pool.submit(revoke_original)
        complete_outcome = complete_future.result(timeout=15)
        revoke_outcome = revoke_future.result(timeout=15)

    assert {complete_outcome[0], revoke_outcome[0]} <= {"ok", "conflict"}
    assert "ok" in {complete_outcome[0], revoke_outcome[0]}
    state = _round_row(assignment["task_id"])[1]
    assert state in {"awaiting_review", "invalidated"}
    if complete_outcome[0] == "ok" and revoke_outcome[0] == "ok":
        assert state == "invalidated"
    elif complete_outcome[0] == "ok":
        assert state == "awaiting_review"
    else:
        assert state == "invalidated"

    reopen_task = next(
        item["task_id"] for item in completed
        if item["task_id"] != assignment["task_id"]
    )
    if _open_round(reopen_task):
        return
    carol = user("carol-race")
    reopen_barrier = threading.Barrier(2)

    def reopen():
        reopen_barrier.wait(timeout=10)
        try:
            return "ok", repo.reopen_completed(
                fence_of(alice), reopen_task, str(uuid.uuid4()),
            )
        except repo.ConflictError as exc:
            return "conflict", exc

    def claim_cross_check():
        reopen_barrier.wait(timeout=10)
        try:
            return "ok", repo.claim(
                fence_of(carol), source_scene="airport",
                rng=ScriptedRng(0, 0),
            )
        except (repo.ConflictError, repo.NoTaskAvailable) as exc:
            return "conflict", exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        reopen_future = pool.submit(reopen)
        claim_future = pool.submit(claim_cross_check)
        reopen_outcome = reopen_future.result(timeout=15)
        claim_outcome = claim_future.result(timeout=15)

    if reopen_outcome[0] == "ok":
        assert reopen_outcome[1]["mode"] == "revision"
        if claim_outcome[0] == "ok":
            assert claim_outcome[1]["task_id"] != reopen_task
    else:
        assert getattr(reopen_outcome[1], "code", None) == "cross_check_active"
        assert claim_outcome[0] == "ok"
        assert claim_outcome[1]["task_id"] == reopen_task
        assert claim_outcome[1]["mode"] == "cross_check"
