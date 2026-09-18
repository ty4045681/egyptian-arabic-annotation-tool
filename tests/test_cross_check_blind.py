from __future__ import annotations

import json
import uuid

from psycopg.types.json import Json

import annotation_repository as repo
import db
from annotation_metadata.repository import append_review, insert_prediction
from tests.test_api import login, segment_payload
from tests.test_cross_check_claim import (
    ScriptedRng, annotate_all, enable_cross_check, fence_of, user,
)
from tests.test_scene_claims import source


ORIGINAL_TEXT = "ALICE-SECRET-TRANSCRIPT"
REVIEW_NOTE = "ORIGINAL-REVIEW-SECRET"
PREDICTION_LABEL = "ORIGINAL-MODEL-LABEL"
EXTRA_TEXT = "EXTRA-ORIGINAL-TEXT"
EXTRA_AUTHOR = "alice-identity-leak"


def _blob(payload) -> str:
    return json.dumps(payload, default=str)


def _assert_blind(payload, *, original_version_id=None):
    blob = _blob(payload)
    assert payload["mode"] == "cross_check"
    assert payload["status"] == "annotated"
    assert payload["cross_check"]["state"] == "in_progress"
    assert payload["cross_check"]["round_id"]
    assert "original_version_id" not in payload
    assert "comparison" not in payload
    assert "decision" not in payload
    metadata = payload["metadata"]
    assert metadata["reference_review"] is None
    assert metadata["prediction"] is None
    assert REVIEW_NOTE not in blob
    assert PREDICTION_LABEL not in blob
    assert ORIGINAL_TEXT not in blob
    assert EXTRA_TEXT not in blob
    assert EXTRA_AUTHOR not in blob
    if original_version_id:
        assert str(original_version_id) not in blob
    for segment in payload["segments"]:
        assert segment["text"] == ""
        assert segment["exclude_from_training"] is False
        assert "annotator" not in segment
        assert EXTRA_TEXT not in str(segment)
    headline = metadata.get("headline") or ""
    assert REVIEW_NOTE not in headline
    assert "Confirmed" not in headline or "not submitted" in headline.lower() or "pending" in headline.lower()


def _plant_leaks(task_id, published_id, alice_id):
    with db.db_tx() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE segments
               SET text = %s, extra = %s
               WHERE version_id = %s""",
            (ORIGINAL_TEXT, Json({
                "text": EXTRA_TEXT,
                "annotator": EXTRA_AUTHOR,
                "original_text": EXTRA_TEXT,
                "user_id": str(alice_id),
            }), published_id),
        )
        baseline = cur.execute(
            "SELECT baseline_version_id FROM annotation_tasks WHERE id = %s",
            (task_id,),
        ).fetchone()[0]
        cur.execute(
            """UPDATE segments
               SET extra = %s
               WHERE version_id = %s""",
            (Json({
                "text": EXTRA_TEXT,
                "annotator": EXTRA_AUTHOR,
                "original_text": ORIGINAL_TEXT,
                "username": "alice",
            }), baseline),
        )
        cur.execute(
            """UPDATE annotation_versions
               SET extra = extra || %s::jsonb
               WHERE id IN (%s, %s)""",
            (json.dumps({
                "original_text": ORIGINAL_TEXT,
                "annotator": EXTRA_AUTHOR,
            }), published_id, baseline),
        )
        append_review(
            cur, version_id=published_id, status="confirmed",
            scene_codes=["airport"], note=REVIEW_NOTE,
            actor_kind="annotator", actor_user_id=alice_id,
        )
        insert_prediction(
            cur, task_id=task_id, predicted_label=PREDICTION_LABEL,
            model_name="leak-model", predicted_scene_code="airport",
            input_version_id=published_id, input_revision=1,
            input_digest="d" * 64, score=0.9,
        )


def test_claim_get_assignment_and_relogin_are_blind(client, seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "airport", "high")
    alice, completed = annotate_all(
        "alice-blind", [task], prefix=ORIGINAL_TEXT, source_scene="airport",
    )
    with db.db_conn() as conn:
        published = conn.execute(
            "SELECT current_published_version_id FROM annotation_tasks WHERE id = %s",
            (task,),
        ).fetchone()[0]
    _plant_leaks(task, published, alice)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)

    login(client, "bob-blind")
    with db.db_conn() as conn:
        bob_id = conn.execute(
            "SELECT id FROM annotators WHERE username = %s", ("bob-blind",),
        ).fetchone()[0]
    first = repo.claim(
        fence_of(bob_id), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    _assert_blind(first, original_version_id=published)
    assert first["task_id"] == task
    assert first["version_id"] != str(published)

    fetched = client.get("/api/assignment")
    assert fetched.status_code == 200
    _assert_blind(fetched.json, original_version_id=published)
    assert fetched.json["version_id"] == first["version_id"]
    assert fetched.json["resumed"] is True

    assert client.post("/api/logout", json={}).status_code == 200
    login(client, "bob-blind")
    relogin = client.get("/api/assignment")
    assert relogin.status_code == 200
    _assert_blind(relogin.json, original_version_id=published)
    assert relogin.json["task_id"] == task
    assert relogin.json["version_id"] == first["version_id"]


def test_extra_implant_does_not_leak_through_copy_or_response(client, seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "airport", "high")
    alice, _ = annotate_all(
        "alice-extra", [task], prefix=ORIGINAL_TEXT, source_scene="airport",
    )
    with db.db_conn() as conn:
        published, baseline = conn.execute(
            """SELECT current_published_version_id, baseline_version_id
               FROM annotation_tasks WHERE id = %s""",
            (task,),
        ).fetchone()
    _plant_leaks(task, published, alice)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = user("bob-extra")
    assignment = repo.claim(
        fence_of(bob), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    _assert_blind(assignment, original_version_id=published)
    with db.db_conn() as conn:
        extras = conn.execute(
            "SELECT extra FROM segments WHERE version_id = %s",
            (assignment["version_id"],),
        ).fetchall()
        version_extra = conn.execute(
            "SELECT extra FROM annotation_versions WHERE id = %s",
            (assignment["version_id"],),
        ).fetchone()[0]
    for (extra,) in extras:
        blob = json.dumps(extra or {})
        assert EXTRA_TEXT not in blob
        assert EXTRA_AUTHOR not in blob
        assert ORIGINAL_TEXT not in blob
    version_blob = json.dumps(version_extra or {})
    assert ORIGINAL_TEXT not in version_blob
    assert EXTRA_AUTHOR not in version_blob


def test_second_user_cannot_save_original_version(client, seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "airport", "high")
    alice, _ = annotate_all(
        "alice-patch", [task], prefix=ORIGINAL_TEXT, source_scene="airport",
    )
    with db.db_conn() as conn:
        published = conn.execute(
            "SELECT current_published_version_id FROM annotation_tasks WHERE id = %s",
            (task,),
        ).fetchone()[0]
        original_rows = conn.execute(
            "SELECT segment_id, text FROM segments WHERE version_id = %s ORDER BY segment_id",
            (published,),
        ).fetchall()
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob-patch")
    with db.db_conn() as conn:
        bob_id = conn.execute(
            "SELECT id FROM annotators WHERE username = %s", ("bob-patch",),
        ).fetchone()[0]
    assignment = repo.claim(
        fence_of(bob_id), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    body = {
        "lease_token": assignment["lease_token"],
        "expected_revision": 0,
        "operation_id": str(uuid.uuid4()),
        "version_id": str(published),
        "segments": segment_payload(assignment, "bob-new"),
    }
    saved = client.patch("/api/assignment/current", json=body)
    assert saved.status_code == 200
    assert saved.json["revision"] == 1
    with db.db_conn() as conn:
        after = conn.execute(
            "SELECT segment_id, text FROM segments WHERE version_id = %s ORDER BY segment_id",
            (published,),
        ).fetchall()
        draft_text = conn.execute(
            "SELECT text FROM segments WHERE version_id = %s ORDER BY segment_id",
            (assignment["version_id"],),
        ).fetchall()
    assert after == original_rows
    assert any("bob-new" in (row[0] or "") for row in draft_text)


def test_original_author_and_third_user_cannot_read_second_draft(client, seed_tasks):
    tasks = seed_tasks(2)
    source(tasks[0], "airport", "high")
    source(tasks[1], "airport", "high")
    alice, completed = annotate_all(
        "alice-priv", tasks, prefix=ORIGINAL_TEXT, source_scene="airport",
    )
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = user("bob-priv")
    assignment = repo.claim(
        fence_of(bob), source_scene="airport", rng=ScriptedRng(0, 0),
    )
    repo.save_draft(
        fence_of(bob), assignment["lease_token"], 0,
        segment_payload(assignment, "bob-private"),
        str(uuid.uuid4()), "bob-save",
    )

    assert repo.get_assignment(alice) is None
    history = repo.history_recent(alice)
    assert "bob-private" not in _blob(history)
    completed_list = repo.completed_list(alice)
    assert "bob-private" not in _blob(completed_list)
    detail = repo.completed_detail(alice, assignment["task_id"])
    assert "bob-private" not in _blob(detail)
    assert ORIGINAL_TEXT in _blob(detail)

    login(client, "carol-priv")
    assert client.get("/api/assignment").json["assigned"] is False
    assert client.get("/api/completed").json["items"] == []
    blocked = client.get(f"/api/completed/{assignment['task_id']}")
    assert blocked.status_code == 403
    assert "bob-private" not in _blob(blocked.json)
    assert client.get("/api/history/recent").json["items"] == []


def test_annotator_cannot_hit_admin_routes(client, seed_tasks):
    seed_tasks(1)
    login(client, "annotator-admin")
    for path in (
        "/api/admin/overview",
        "/api/admin/quality",
        "/api/admin/annotations",
        "/api/admin/tasks",
    ):
        response = client.get(path)
        assert response.status_code in (401, 403), (path, response.status_code)
        assert response.status_code != 200
