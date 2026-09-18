from __future__ import annotations

import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

import annotation_repository as repo
import db
import server
from annotation_quality.contracts import CrossCheckDecisionCommand
from annotation_quality.service import decide_cross_check
from tests.test_admin_api import ADMIN_KEY, ADMIN_KEY_DIGEST, _admin_login
from tests.test_api import login
from tests.test_cross_check_claim import enable_cross_check
from tests.test_cross_check_submit import (
    bob_claim,
    changed_words,
    complete,
    seed_annotated,
    text_segments,
    words,
)


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


def _admin_session() -> dict:
    nonce = uuid.uuid4().hex
    return repo.create_admin_session(
        key_id="primary",
        token_digest=hashlib.sha256(f"token-{nonce}".encode()).hexdigest(),
        csrf_digest=hashlib.sha256(f"csrf-{nonce}".encode()).hexdigest(),
        idle_seconds=1800,
        absolute_seconds=28800,
        ip_hash="test-ip",
        user_agent="pytest",
    )


def queue_rounds(client, seed_tasks, count, *, original_text=None,
                 secondary_text=None):
    original_text = original_text or words(100)
    secondary_text = secondary_text or changed_words(100, 11)
    seed_annotated(client, seed_tasks, count, text=original_text)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")
    rounds = []
    for _ in range(count):
        assignment = bob_claim(client)
        response, _ = complete(
            client, assignment,
            segments=text_segments(assignment, secondary_text),
        )
        assert response.status_code == 200, response.json
        rounds.append({
            "assignment": assignment,
            "body": response.json,
            "round_id": response.json["cross_check"]["round_id"],
            "task_id": assignment["task_id"],
            "secondary_version_id": assignment["version_id"],
            "original_text": original_text,
            "secondary_text": secondary_text,
        })
    client.post("/api/logout", json={})
    return rounds


def round_state(round_id):
    with db.db_conn() as conn:
        return conn.execute(
            """SELECT state, revision, original_version_id, secondary_version_id,
                      decision, final_version_id, edit_distance, reason_codes,
                      original_word_count, secondary_word_count
               FROM cross_check_rounds WHERE id = %s""",
            (round_id,),
        ).fetchone()


def version_info(version_id):
    with db.db_conn() as conn:
        return conn.execute(
            """SELECT lifecycle, purpose, submitted_by_user_id,
                      credited_annotator_id, published_by_admin_action_id,
                      submitted_at, target_status
               FROM annotation_versions WHERE id = %s""",
            (version_id,),
        ).fetchone()


def segment_texts(version_id):
    with db.db_conn() as conn:
        return [
            row[0] for row in conn.execute(
                """SELECT text FROM segments
                   WHERE version_id = %s ORDER BY segment_id""",
                (version_id,),
            ).fetchall()
        ]


def task_row(task_id):
    with db.db_conn() as conn:
        return conn.execute(
            """SELECT status, current_published_version_id
               FROM annotation_tasks WHERE id = %s""",
            (task_id,),
        ).fetchone()


def user_id(username):
    with db.db_conn() as conn:
        return conn.execute(
            "SELECT id FROM annotators WHERE username = %s",
            (username,),
        ).fetchone()[0]


def decision_body(detail, *, decision, reason="checked", **extra):
    body = {
        "operation_id": str(uuid.uuid4()),
        "expected_revision": detail["revision"],
        "expected_original_version_id": detail["original_version_id"],
        "expected_secondary_version_id": detail["secondary_version_id"],
        "decision": decision,
        "reason": reason,
    }
    body.update(extra)
    return body


def test_settings_get_put_stale_revision_and_idempotent_put(admin_client, database):
    _, headers = _admin_login(admin_client)
    got = admin_client.get("/api/admin/cross-check-settings")
    assert got.status_code == 200, got.json
    assert got.json["enabled"] is False
    assert got.json["sampling_rate_bps"] == 1000
    assert got.json["revision"] == 0
    assert got.json["word_difference_threshold_bps"] == 1000
    assert got.json["comparison_version"] == "worddiff_v1"

    operation_id = str(uuid.uuid4())
    payload = {
        "operation_id": operation_id,
        "expected_revision": 0,
        "enabled": True,
        "sampling_rate_bps": 2500,
        "reason": "Enable cross-check claims at 25 percent",
    }
    first = admin_client.put(
        "/api/admin/cross-check-settings", json=payload, headers=headers,
    )
    assert first.status_code == 200, first.json
    assert first.json["enabled"] is True
    assert first.json["sampling_rate_bps"] == 2500
    assert first.json["revision"] == 1
    assert first.json["action_id"]
    assert first.json["word_difference_threshold_bps"] == 1000

    replay = admin_client.put(
        "/api/admin/cross-check-settings", json=payload, headers=headers,
    )
    assert replay.status_code == 200, replay.json
    assert replay.json["idempotent_replay"] is True
    assert replay.json["revision"] == 1
    assert replay.json["action_id"] == first.json["action_id"]

    stale = admin_client.put(
        "/api/admin/cross-check-settings",
        json={
            "operation_id": str(uuid.uuid4()),
            "expected_revision": 0,
            "enabled": False,
            "sampling_rate_bps": 0,
            "reason": "stale write",
        },
        headers=headers,
    )
    assert stale.status_code == 409
    after = admin_client.get("/api/admin/cross-check-settings")
    assert after.json["revision"] == 1
    assert after.json["enabled"] is True


def test_disable_claims_does_not_cancel_open_rounds(
        admin_client, database, seed_tasks):
    seed_annotated(admin_client, seed_tasks, 1)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(admin_client, "bob")
    assignment = bob_claim(admin_client)
    round_id = assignment["cross_check"]["round_id"]
    admin_client.post("/api/logout", json={})

    _, headers = _admin_login(admin_client)
    disabled = admin_client.put(
        "/api/admin/cross-check-settings",
        json={
            "operation_id": str(uuid.uuid4()),
            "expected_revision": 0,
            "enabled": False,
            "sampling_rate_bps": 1000,
            "reason": "Disable new claims only",
        },
        headers=headers,
    )
    assert disabled.status_code == 200, disabled.json
    assert disabled.json["enabled"] is False
    assert round_state(round_id)[0] == "in_progress"

    login(admin_client, "bob")
    current = admin_client.get("/api/assignment")
    assert current.status_code == 200
    assert current.json["assigned"] is True
    assert current.json["cross_check"]["round_id"] == round_id
    assert current.json["task_id"] == assignment["task_id"]


def test_list_default_filters_cursor_and_no_full_text(
        admin_client, database, seed_tasks):
    seed_annotated(admin_client, seed_tasks, 3, text=SECRET_A)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(admin_client, "bob")
    queued = []
    for _ in range(2):
        assignment = bob_claim(admin_client)
        response, _ = complete(
            admin_client, assignment,
            segments=text_segments(assignment, SECRET_B),
        )
        assert response.status_code == 200, response.json
        assert response.json["cross_check"]["state"] == "awaiting_review"
        queued.append({
            "assignment": assignment,
            "round_id": response.json["cross_check"]["round_id"],
        })
    passed = bob_claim(admin_client)
    passed_complete, _ = complete(
        admin_client, passed, segments=text_segments(passed, SECRET_A),
    )
    assert passed_complete.json["cross_check"]["state"] == "passed"
    admin_client.post("/api/logout", json={})

    _, headers = _admin_login(admin_client)
    default = admin_client.get("/api/admin/cross-checks")
    assert default.status_code == 200, default.json
    assert default.json["applied_filters"]["state"] == "awaiting_review"
    ids = {item["round_id"] for item in default.json["items"]}
    assert {item["round_id"] for item in queued} <= ids
    assert passed_complete.json["cross_check"]["round_id"] not in ids
    blob = json.dumps(default.json)
    assert SECRET_A not in blob
    assert SECRET_B not in blob
    for item in default.json["items"]:
        assert "segments" not in item
        assert "diff_ops" not in item
        assert "original_normalized_summary" not in item
        assert item["training_export_blocked"] is True
        assert item["reason_codes"]

    alice = str(user_id("alice"))
    bob = str(user_id("bob"))
    filtered = admin_client.get(
        "/api/admin/cross-checks",
        query_string={
            "original_annotator_id": alice,
            "secondary_annotator_id": bob,
            "reason_code": "word_difference_exceeded",
            "source_scene": "airport",
            "q": queued[0]["assignment"]["filename"],
        },
    )
    assert filtered.status_code == 200, filtered.json
    assert filtered.json["items"]
    assert all(
        item["original_annotator_id"] == alice
        and item["secondary_annotator_id"] == bob
        for item in filtered.json["items"]
    )

    passed_list = admin_client.get(
        "/api/admin/cross-checks", query_string={"state": "passed"},
    )
    assert {
        item["round_id"] for item in passed_list.json["items"]
    } == {passed_complete.json["cross_check"]["round_id"]}

    page1 = admin_client.get("/api/admin/cross-checks", query_string={"limit": 1})
    assert page1.status_code == 200
    assert len(page1.json["items"]) == 1
    assert page1.json["next_cursor"]
    page2 = admin_client.get(
        "/api/admin/cross-checks",
        query_string={"limit": 1, "cursor": page1.json["next_cursor"]},
    )
    assert page2.status_code == 200
    assert len(page2.json["items"]) >= 1
    assert page2.json["items"][0]["round_id"] != page1.json["items"][0]["round_id"]


def test_detail_includes_both_texts_and_ops(admin_client, database, seed_tasks):
    queued = queue_rounds(
        admin_client, seed_tasks, 1,
        original_text=SECRET_A,
        secondary_text=SECRET_B,
    )
    _, headers = _admin_login(admin_client)
    detail = admin_client.get(
        f"/api/admin/cross-checks/{queued[0]['round_id']}",
    )
    assert detail.status_code == 200, detail.json
    blob = json.dumps(detail.json)
    assert SECRET_A in blob
    assert SECRET_B in blob
    assert detail.json["diff_ops"]
    assert detail.json["audio_url"] == (
        f"/api/admin/audio/{queued[0]['task_id']}"
    )
    assert detail.json["original_version_id"]
    assert detail.json["secondary_version_id"]
    assert detail.json["comparison_unavailable"] is False


def test_decision_original_keeps_published_text(admin_client, database, seed_tasks):
    queued = queue_rounds(admin_client, seed_tasks, 1)
    round_id = queued[0]["round_id"]
    before = round_state(round_id)
    published_before = task_row(queued[0]["task_id"])
    original_submitted = version_info(before[2])[5]
    original_texts = segment_texts(before[2])
    _, headers = _admin_login(admin_client)
    detail = admin_client.get(f"/api/admin/cross-checks/{round_id}").json
    body = decision_body(detail, decision="original", reason="keep original")
    result = admin_client.post(
        f"/api/admin/cross-checks/{round_id}/decision",
        json=body, headers=headers,
    )
    assert result.status_code == 200, result.json
    assert result.json["state"] == "adjudicated"
    assert result.json["final_version_id"] == str(before[2])
    assert result.json["final_status"] == "annotated"
    assert result.json["training_export_blocked"] is False
    after = round_state(round_id)
    assert after[0] == "adjudicated"
    assert after[4] == "original"
    assert after[6] == before[6]
    published = task_row(queued[0]["task_id"])
    assert published[1] == published_before[1]
    original = version_info(before[2])
    assert original[0] == "published"
    assert original[5] == original_submitted
    assert segment_texts(before[2]) == original_texts


def test_decision_secondary_publishes_frozen_b(admin_client, database, seed_tasks):
    queued = queue_rounds(admin_client, seed_tasks, 1)
    round_id = queued[0]["round_id"]
    before = round_state(round_id)
    secondary_submitted = version_info(before[3])[5]
    secondary_texts = segment_texts(before[3])
    _, headers = _admin_login(admin_client)
    detail = admin_client.get(f"/api/admin/cross-checks/{round_id}").json
    result = admin_client.post(
        f"/api/admin/cross-checks/{round_id}/decision",
        json=decision_body(detail, decision="secondary", reason="take B"),
        headers=headers,
    )
    assert result.status_code == 200, result.json
    assert result.json["final_version_id"] == str(before[3])
    published = task_row(queued[0]["task_id"])
    assert str(published[1]) == str(before[3])
    original = version_info(before[2])
    secondary = version_info(before[3])
    assert original[0] == "superseded"
    assert secondary[0] == "published"
    assert secondary[5] == secondary_submitted
    assert secondary[2] == user_id("bob")
    assert secondary[4] == uuid.UUID(result.json["action_id"])
    assert segment_texts(before[3]) == secondary_texts


def test_decision_edited_annotated_creates_adjudication_version(
        admin_client, database, seed_tasks):
    task_ids = seed_tasks(1)
    from tests.test_scene_claims import source
    source(task_ids[0], "airport", "high")
    login(admin_client, "alice")
    claimed = admin_client.post(
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
    assert admin_client.post(
        "/api/assignment/current/complete", json=complete_payload,
    ).status_code == 200
    admin_client.post("/api/logout", json={})
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(admin_client, "bob")
    bob_assignment = bob_claim(admin_client)
    queued, _ = complete(
        admin_client, bob_assignment,
        segments=text_segments(bob_assignment, SECRET_B),
    )
    assert queued.json["cross_check"]["state"] == "awaiting_review"
    round_id = queued.json["cross_check"]["round_id"]
    admin_client.post("/api/logout", json={})

    _, headers = _admin_login(admin_client)
    detail = admin_client.get(f"/api/admin/cross-checks/{round_id}").json
    original_review_id = detail["original_review_id"]
    before_distance = detail["edit_distance"]
    original_texts = segment_texts(detail["original_version_id"])
    edited_segments = []
    for segment in detail["original_segments"]:
        item = dict(segment)
        if not item.get("exclude_from_training"):
            item["text"] = "admin edited " + (item.get("text") or "")
        edited_segments.append(item)
    result = admin_client.post(
        f"/api/admin/cross-checks/{round_id}/decision",
        json=decision_body(
            detail, decision="edited", reason="admin rewrite",
            base="original", segments=edited_segments,
            target_status="annotated",
            scene_review={
                "status": "confirmed",
                "scene_codes": ["airport"],
                "note": "admin-final-note",
            },
        ),
        headers=headers,
    )
    assert result.status_code == 200, result.json
    final_id = result.json["final_version_id"]
    assert final_id not in {
        detail["original_version_id"], detail["secondary_version_id"],
    }
    final = version_info(final_id)
    assert final[0] == "published"
    assert final[1] == "adjudication"
    assert final[2] is None
    assert final[3] == user_id("alice")
    assert final[4] == uuid.UUID(result.json["action_id"])
    assert segment_texts(detail["original_version_id"]) == original_texts
    assert round_state(round_id)[6] == before_distance
    with db.db_conn() as conn:
        original_reviews = conn.execute(
            """SELECT id, superseded, note FROM scene_reviews
               WHERE version_id = %s ORDER BY review_no""",
            (detail["original_version_id"],),
        ).fetchall()
        final_notes = [
            row[0] for row in conn.execute(
                """SELECT note FROM scene_reviews
                   WHERE version_id = %s AND NOT superseded""",
                (final_id,),
            ).fetchall()
        ]
    assert str(original_reviews[0][0]) == original_review_id
    assert original_reviews[0][1] is False
    assert original_reviews[0][2] == "alice-original-note"
    assert "admin-final-note" in final_notes
    assert any("admin edited" in text for text in segment_texts(final_id))


def test_decision_edited_skipped(admin_client, database, seed_tasks):
    queued = queue_rounds(admin_client, seed_tasks, 1)
    round_id = queued[0]["round_id"]
    _, headers = _admin_login(admin_client)
    detail = admin_client.get(f"/api/admin/cross-checks/{round_id}").json
    result = admin_client.post(
        f"/api/admin/cross-checks/{round_id}/decision",
        json=decision_body(
            detail, decision="edited", reason="unusable audio",
            base="secondary", segments=detail["secondary_segments"],
            target_status="skipped", skip_reasons=["noisy"],
        ),
        headers=headers,
    )
    assert result.status_code == 200, result.json
    assert result.json["final_status"] == "skipped"
    assert result.json["training_export_blocked"] is False
    published = task_row(queued[0]["task_id"])
    assert published[0] == "skipped"
    final = version_info(result.json["final_version_id"])
    assert final[1] == "adjudication"
    assert final[2] is None
    assert final[3] == user_id("bob")
    assert final[6] == "skipped"


def test_two_admins_race_one_decision_succeeds(admin_client, database, seed_tasks):
    queued = queue_rounds(admin_client, seed_tasks, 1)
    round_id = queued[0]["round_id"]
    _, headers = _admin_login(admin_client)
    detail = admin_client.get(f"/api/admin/cross-checks/{round_id}").json
    admin_a = _admin_session()
    admin_b = _admin_session()
    commands = [
        CrossCheckDecisionCommand.model_validate(decision_body(
            detail, decision="original", reason="admin-a",
        )),
        CrossCheckDecisionCommand.model_validate(decision_body(
            detail, decision="original", reason="admin-b",
        )),
    ]
    barrier = threading.Barrier(2)
    outcomes: list = [None, None]

    def worker(index, session, command):
        barrier.wait()
        try:
            outcomes[index] = (
                "ok", decide_cross_check(session["id"], round_id, command),
            )
        except Exception as exc:  # noqa: BLE001 - race winner/loser
            outcomes[index] = ("err", exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker, 0, admin_a, commands[0])
        second = pool.submit(worker, 1, admin_b, commands[1])
        first.result()
        second.result()
    wins = [item for item in outcomes if item[0] == "ok"]
    losses = [item for item in outcomes if item[0] == "err"]
    assert len(wins) == 1
    assert len(losses) == 1
    assert isinstance(losses[0][1], repo.ConflictError)
    assert round_state(round_id)[0] == "adjudicated"


def test_decision_replay_and_finished_round_rejects_new_operation(
        admin_client, database, seed_tasks):
    queued = queue_rounds(admin_client, seed_tasks, 1)
    round_id = queued[0]["round_id"]
    _, headers = _admin_login(admin_client)
    detail = admin_client.get(f"/api/admin/cross-checks/{round_id}").json
    body = decision_body(detail, decision="original", reason="replay me")
    first = admin_client.post(
        f"/api/admin/cross-checks/{round_id}/decision",
        json=body, headers=headers,
    )
    assert first.status_code == 200, first.json
    replay = admin_client.post(
        f"/api/admin/cross-checks/{round_id}/decision",
        json=body, headers=headers,
    )
    assert replay.status_code == 200, replay.json
    assert replay.json["idempotent_replay"] is True
    assert replay.json["action_id"] == first.json["action_id"]
    other = decision_body(detail, decision="secondary", reason="too late")
    other["expected_revision"] = first.json.get("revision", detail["revision"])
    rejected = admin_client.post(
        f"/api/admin/cross-checks/{round_id}/decision",
        json=other, headers=headers,
    )
    assert rejected.status_code == 409
    assert rejected.json.get("code") == "cross_check_state_conflict"
    assert round_state(round_id)[4] == "original"


def test_cancel_in_progress_and_reject_awaiting_review(
        admin_client, database, seed_tasks):
    seed_annotated(admin_client, seed_tasks, 2, text=words(100))
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(admin_client, "bob")
    first = bob_claim(admin_client)
    queued, _ = complete(
        admin_client, first,
        segments=text_segments(first, changed_words(100, 11)),
    )
    assert queued.json["cross_check"]["state"] == "awaiting_review"
    awaiting = {"round_id": queued.json["cross_check"]["round_id"]}
    live = bob_claim(admin_client)
    live_id = live["cross_check"]["round_id"]
    live_task = live["task_id"]
    live_published = task_row(live_task)
    admin_client.post("/api/logout", json={})

    _, headers = _admin_login(admin_client)
    cancelled = admin_client.post(
        f"/api/admin/cross-checks/{live_id}/cancel",
        json={
            "operation_id": str(uuid.uuid4()),
            "expected_revision": 0,
            "reason": "annotator unavailable",
            "confirm": True,
        },
        headers=headers,
    )
    assert cancelled.status_code == 200, cancelled.json
    assert cancelled.json["state"] == "cancelled"
    assert round_state(live_id)[0] == "cancelled"
    assert task_row(live_task)[1] == live_published[1]
    login(admin_client, "bob")
    assert admin_client.get("/api/assignment").json["assigned"] is False
    admin_client.post("/api/logout", json={})

    blocked = admin_client.post(
        f"/api/admin/cross-checks/{awaiting['round_id']}/cancel",
        json={
            "operation_id": str(uuid.uuid4()),
            "expected_revision": round_state(awaiting["round_id"])[1],
            "reason": "cannot cancel queued review",
            "confirm": True,
        },
        headers=headers,
    )
    assert blocked.status_code == 409
    assert blocked.json.get("code") == "cross_check_state_conflict"
    assert round_state(awaiting["round_id"])[0] == "awaiting_review"


def test_annotator_cannot_get_admin_detail_and_csrf_required(
        admin_client, database, seed_tasks):
    queued = queue_rounds(admin_client, seed_tasks, 1)
    round_id = queued[0]["round_id"]
    annotator = server.app.test_client()
    login(annotator, "eve")
    denied = annotator.get(f"/api/admin/cross-checks/{round_id}")
    assert denied.status_code in (401, 403)
    assert denied.status_code != 200

    _admin_login(admin_client)
    missing_csrf = admin_client.post(
        f"/api/admin/cross-checks/{round_id}/decision",
        json=decision_body(
            admin_client.get(f"/api/admin/cross-checks/{round_id}").json,
            decision="original",
        ),
    )
    assert missing_csrf.status_code == 403
    missing = admin_client.get(
        "/api/admin/cross-checks/00000000-0000-4000-8000-000000000001",
    )
    assert missing.status_code == 404


def test_mine_and_submission_visibility(admin_client, database, seed_tasks):
    queued = queue_rounds(
        admin_client, seed_tasks, 1,
        original_text=SECRET_A,
        secondary_text=SECRET_B,
    )
    round_id = queued[0]["round_id"]
    login(admin_client, "bob")
    mine = admin_client.get("/api/cross-checks/mine")
    assert mine.status_code == 200, mine.json
    assert mine.json["items"]
    assert mine.json["items"][0]["round_id"] == round_id
    submission = admin_client.get(f"/api/cross-checks/{round_id}/submission")
    assert submission.status_code == 200, submission.json
    blob = json.dumps(submission.json)
    assert SECRET_B in blob
    assert SECRET_A not in blob
    assert "diff_ops" not in submission.json
    assert "original_annotator_id" not in submission.json
    assert "original_segments" not in submission.json
    admin_client.post("/api/logout", json={})

    login(admin_client, "alice")
    assert admin_client.get("/api/cross-checks/mine").json["items"] == []
    assert admin_client.get(
        f"/api/cross-checks/{round_id}/submission",
    ).status_code == 404


def test_quality_cross_check_counts(admin_client, database, seed_tasks):
    seed_annotated(admin_client, seed_tasks, 4, text=words(100))
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(admin_client, "bob")
    passed = bob_claim(admin_client)
    passed_body, _ = complete(
        admin_client, passed, segments=text_segments(passed, words(100)),
    )
    assert passed_body.json["cross_check"]["state"] == "passed"
    first_queue = bob_claim(admin_client)
    first_body, _ = complete(
        admin_client, first_queue,
        segments=text_segments(first_queue, changed_words(100, 11)),
    )
    second_queue = bob_claim(admin_client)
    second_body, _ = complete(
        admin_client, second_queue,
        segments=text_segments(second_queue, changed_words(100, 11)),
    )
    live = bob_claim(admin_client)
    admin_client.post("/api/logout", json={})

    _, headers = _admin_login(admin_client)
    decide_id = first_body.json["cross_check"]["round_id"]
    detail = admin_client.get(f"/api/admin/cross-checks/{decide_id}").json
    decided = admin_client.post(
        f"/api/admin/cross-checks/{decide_id}/decision",
        json=decision_body(detail, decision="original"),
        headers=headers,
    )
    assert decided.status_code == 200, decided.json

    quality = admin_client.get("/api/admin/quality")
    assert quality.status_code == 200, quality.json
    summary = quality.json["cross_check"]
    assert summary["in_progress_count"] == 1
    assert summary["pending_review_count"] == 1
    assert summary["passed_count"] == 1
    assert summary["adjudicated_count"] == 1
    assert summary["blocked_audio_seconds"] == 20.0
    assert quality.json["stats"]["unusually_fast"] >= 0
    assert "stale_assignments" in quality.json["stats"]
    assert live["cross_check"]["round_id"]
    assert second_body.json["cross_check"]["state"] == "awaiting_review"
