"""Flask-client tests for cross-check complete, auto-pass, and review queue."""

from __future__ import annotations

import inspect
import uuid

import annotation_quality.service as quality_service
import annotation_repository as repo
import db
from annotation_quality.comparison import compare_transcripts as real_compare
from tests.test_api import login, segment_payload
from tests.test_cross_check_claim import enable_cross_check
from tests.test_scene_claims import source


IDENTICAL = "hello world"


def words(n, prefix="w"):
    return " ".join(f"{prefix}{i}" for i in range(n))


def changed_words(n, changed, prefix="w"):
    return " ".join(
        f"x{i}" if i < changed else f"{prefix}{i}" for i in range(n)
    )


def text_segments(assignment, text, *, bq_rest=True):
    segs = []
    for index, seg in enumerate(assignment["segments"]):
        segs.append({
            "id": seg["id"],
            "start": seg["start"],
            "end": seg["end"],
            "duration": seg["duration"],
            "text": text if index == 0 or not bq_rest else "",
            "exclude_from_training": bool(index > 0 and bq_rest),
        })
    return segs


def complete_body(assignment, *, segments, target_status="annotated",
                  skip_reasons=None, expected_revision=None, operation_id=None):
    return {
        "lease_token": assignment["lease_token"],
        "expected_revision": (
            assignment["revision"] if expected_revision is None
            else expected_revision
        ),
        "target_status": target_status,
        "skip_reasons": list(skip_reasons or []),
        "operation_id": operation_id or str(uuid.uuid4()),
        "segments": segments,
    }


def complete(client, assignment, **kwargs):
    body = complete_body(assignment, **kwargs)
    response = client.post("/api/assignment/current/complete", json=body)
    return response, body


def seed_annotated(client, seed_tasks, count, *, text=IDENTICAL):
    task_ids = seed_tasks(count)
    for task_id in task_ids:
        source(task_id, "airport", "high")
    login(client, "alice")
    completed = []
    for _ in task_ids:
        claimed = client.post(
            "/api/assignment/claim", json={"source_scene": "airport"},
        )
        assert claimed.status_code == 200, claimed.json
        assignment = claimed.json
        response, _ = complete(
            client, assignment, segments=text_segments(assignment, text),
        )
        assert response.status_code == 200, response.json
        completed.append(assignment)
    client.post("/api/logout", json={})
    return task_ids, completed


def bob_claim(client):
    claimed = client.post(
        "/api/assignment/claim", json={"source_scene": "airport"},
    )
    assert claimed.status_code == 200, claimed.json
    assert claimed.json["mode"] == "cross_check"
    return claimed.json


def round_row(round_id):
    with db.db_conn() as conn:
        return conn.execute(
            """SELECT state, reason_codes, edit_distance, original_word_count,
                      secondary_word_count, original_version_id,
                      secondary_version_id, submitted_at
               FROM cross_check_rounds WHERE id = %s""",
            (round_id,),
        ).fetchone()


def task_published(task_id):
    with db.db_conn() as conn:
        return conn.execute(
            """SELECT t.current_published_version_id, t.status, v.submitted_at,
                      v.lifecycle
               FROM annotation_tasks t
               JOIN annotation_versions v
                 ON v.id = t.current_published_version_id
               WHERE t.id = %s""",
            (task_id,),
        ).fetchone()


def version_row(version_id):
    with db.db_conn() as conn:
        return conn.execute(
            """SELECT lifecycle, submitted_by_user_id, credited_annotator_id,
                      target_status
               FROM annotation_versions WHERE id = %s""",
            (version_id,),
        ).fetchone()


def event_types(task_id):
    with db.db_conn() as conn:
        return conn.execute(
            """SELECT event_type, operation_id
               FROM annotation_events
               WHERE task_id = %s
               ORDER BY id""",
            (task_id,),
        ).fetchall()


def user_id(username):
    with db.db_conn() as conn:
        return conn.execute(
            "SELECT id FROM annotators WHERE username = %s", (username,),
        ).fetchone()[0]


def test_identical_texts_auto_pass_keeps_original_and_duration(client, seed_tasks):
    task_ids, _ = seed_annotated(client, seed_tasks, 1)
    task_id = task_ids[0]
    before = task_published(task_id)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)

    login(client, "bob")
    assignment = bob_claim(client)
    response, _ = complete(
        client, assignment, segments=text_segments(assignment, IDENTICAL),
    )
    assert response.status_code == 200, response.json
    body = response.json
    assert body["success"] is True
    assert body["task_id"] == task_id
    assert body["status"] == "annotated"
    assert body["published"] is False
    assert body["cross_check"]["state"] == "passed"
    assert body["cross_check"]["training_export_blocked"] is False
    assert body["cross_check"]["round_id"] == assignment["cross_check"]["round_id"]

    after = task_published(task_id)
    assert after[0] == before[0]
    assert after[1] == "annotated"
    assert after[2] == before[2]
    assert after[3] == "published"

    secondary = version_row(assignment["version_id"])
    assert secondary[0] == "cross_check_submitted"
    assert secondary[1] == user_id("bob")
    assert secondary[2] == user_id("bob")
    assert secondary[0] != "published"

    rnd = round_row(body["cross_check"]["round_id"])
    assert rnd[0] == "passed"
    assert list(rnd[1] or []) == []
    assert rnd[2] == 0

    dash = client.get("/api/dashboard")
    assert dash.status_code == 200
    assert dash.json["stats"]["annotated"] == 1
    assert dash.json["stats"]["annotated_duration_seconds"] == 10.0

    types = event_types(task_id)
    names = [row[0] for row in types]
    assert names.count("completed") == 1
    assert "cross_check_submitted" in names
    assert "cross_check_passed" in names
    submitted = [row for row in types if row[0] == "cross_check_submitted"]
    passed = [row for row in types if row[0] == "cross_check_passed"]
    assert submitted[0][1] is not None
    assert passed[0][1] is None
    assert submitted[0][1] != passed[0][1]

    current = client.get("/api/assignment")
    assert current.json["assigned"] is False


def test_word_diff_exactly_ten_percent_passes_eleven_queues(client, seed_tasks):
    seed_annotated(client, seed_tasks, 2, text=words(100))
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")

    first = bob_claim(client)
    ten, _ = complete(
        client, first, segments=text_segments(first, changed_words(100, 10)),
    )
    assert ten.status_code == 200, ten.json
    assert ten.json["cross_check"]["state"] == "passed"
    assert ten.json["published"] is False
    ten_row = round_row(ten.json["cross_check"]["round_id"])
    assert ten_row[0] == "passed"
    assert ten_row[2] == 10
    assert "word_difference_exceeded" not in list(ten_row[1] or [])

    second = bob_claim(client)
    eleven, _ = complete(
        client, second, segments=text_segments(second, changed_words(100, 11)),
    )
    assert eleven.status_code == 200, eleven.json
    assert eleven.json["cross_check"]["state"] == "awaiting_review"
    assert eleven.json["cross_check"]["training_export_blocked"] is True
    eleven_row = round_row(eleven.json["cross_check"]["round_id"])
    assert eleven_row[0] == "awaiting_review"
    assert eleven_row[2] == 11
    assert "word_difference_exceeded" in list(eleven_row[1] or [])


def test_skipped_empty_all_bq_and_bq_conflict_queue(client, seed_tasks):
    task_ids = seed_tasks(4)
    for task_id in task_ids:
        source(task_id, "airport", "high")
    login(client, "alice")

    skipped_task = None
    empty_task = None
    all_bq_task = None
    conflict_task = None
    for index in range(4):
        claimed = client.post(
            "/api/assignment/claim", json={"source_scene": "airport"},
        )
        assignment = claimed.json
        if index < 3:
            response, _ = complete(
                client, assignment,
                segments=text_segments(assignment, IDENTICAL),
            )
            assert response.status_code == 200, response.json
            if index == 0:
                skipped_task = assignment
            elif index == 1:
                empty_task = assignment
            else:
                all_bq_task = assignment
        else:
            segs = []
            for pos, seg in enumerate(assignment["segments"]):
                segs.append({
                    "id": seg["id"],
                    "start": seg["start"],
                    "end": seg["end"],
                    "duration": seg["duration"],
                    "text": "" if pos == 0 else IDENTICAL,
                    "exclude_from_training": pos == 0,
                })
            response, _ = complete(client, assignment, segments=segs)
            assert response.status_code == 200, response.json
            conflict_task = assignment
    client.post("/api/logout", json={})
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")

    cases = []
    for _ in range(4):
        assignment = bob_claim(client)
        task_id = assignment["task_id"]
        if task_id == skipped_task["task_id"]:
            response, _ = complete(
                client, assignment,
                segments=text_segments(assignment, IDENTICAL),
                target_status="skipped", skip_reasons=["noisy"],
            )
            assert response.status_code == 200, response.json
            assert response.json["status"] == "skipped"
            assert response.json["published"] is False
            row = round_row(response.json["cross_check"]["round_id"])
            assert row[0] == "awaiting_review"
            assert "submission_status_conflict" in list(row[1] or [])
            cases.append("skipped")
        elif task_id == empty_task["task_id"]:
            empty = text_segments(assignment, "", bq_rest=False)
            response, _ = complete(client, assignment, segments=empty)
            assert response.status_code == 200, response.json
            row = round_row(response.json["cross_check"]["round_id"])
            assert row[0] == "awaiting_review"
            assert "empty_secondary_text" in list(row[1] or [])
            cases.append("empty")
        elif task_id == all_bq_task["task_id"]:
            all_bq = []
            for seg in assignment["segments"]:
                all_bq.append({
                    "id": seg["id"],
                    "start": seg["start"],
                    "end": seg["end"],
                    "duration": seg["duration"],
                    "text": IDENTICAL,
                    "exclude_from_training": True,
                })
            response, _ = complete(client, assignment, segments=all_bq)
            assert response.status_code == 200, response.json
            row = round_row(response.json["cross_check"]["round_id"])
            assert row[0] == "awaiting_review"
            assert "empty_secondary_text" in list(row[1] or [])
            cases.append("all_bq")
        else:
            assert task_id == conflict_task["task_id"]
            segs = []
            for pos, seg in enumerate(assignment["segments"]):
                segs.append({
                    "id": seg["id"],
                    "start": seg["start"],
                    "end": seg["end"],
                    "duration": seg["duration"],
                    "text": IDENTICAL if pos == 0 else "",
                    "exclude_from_training": pos != 0,
                })
            response, _ = complete(client, assignment, segments=segs)
            assert response.status_code == 200, response.json
            row = round_row(response.json["cross_check"]["round_id"])
            assert row[0] == "awaiting_review"
            assert "bad_quality_conflict" in list(row[1] or [])
            cases.append("bq_conflict")
        assert response.json["cross_check"]["training_export_blocked"] is True
    assert sorted(cases) == ["all_bq", "bq_conflict", "empty", "skipped"]


def test_complete_merges_dirty_segments_before_compare(client, seed_tasks):
    seed_annotated(client, seed_tasks, 1, text=words(100))
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")
    assignment = bob_claim(client)

    saved_text = changed_words(100, 11)
    save_body = {
        "lease_token": assignment["lease_token"],
        "expected_revision": 0,
        "operation_id": str(uuid.uuid4()),
        "segments": text_segments(assignment, saved_text),
    }
    saved = client.patch("/api/assignment/current", json=save_body)
    assert saved.status_code == 200, saved.json
    assert saved.json["revision"] == 1

    overlap = text_segments(assignment, words(100))
    if len(overlap) > 1:
        overlap[1]["start"] = overlap[0]["start"]
        overlap[1]["exclude_from_training"] = False
        overlap[1]["text"] = "overlap"
        bad, _ = complete(
            client, assignment, segments=overlap, expected_revision=1,
        )
        assert bad.status_code == 400
        assert round_row(assignment["cross_check"]["round_id"])[0] == "in_progress"

    response, _ = complete(
        client, assignment,
        segments=text_segments(assignment, words(100)),
        expected_revision=1,
    )
    assert response.status_code == 200, response.json
    assert response.json["cross_check"]["state"] == "passed"
    row = round_row(response.json["cross_check"]["round_id"])
    assert row[2] == 0


def test_snapshot_replays_before_locking_assignment():
    src = inspect.getsource(quality_service._load_submit_snapshot)
    assert src.index("_operation_replay") < src.index("_lock_assignment")


def test_snapshot_replays_if_winner_released_assignment(
    client, seed_tasks, monkeypatch,
):
    seed_annotated(client, seed_tasks, 1)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")
    assignment = bob_claim(client)
    stored = {
        "success": True,
        "task_id": assignment["task_id"],
        "status": "annotated",
        "published": False,
        "skip_reasons": [],
        "cross_check": {
            "round_id": assignment["cross_check"]["round_id"],
            "state": "passed",
            "training_export_blocked": False,
        },
    }
    real_peek = quality_service._peek_complete_replay

    def peek_then_winner(uid, operation_id, request_hash):
        prior = real_peek(uid, operation_id, request_hash)
        assert prior is None
        with db.db_tx() as conn, conn.cursor() as cur:
            repo._store_operation(
                cur, operation_id, uid, "complete", request_hash, stored,
            )
            cur.execute("DELETE FROM assignments WHERE user_id = %s", (uid,))
        return None

    monkeypatch.setattr(
        quality_service, "_peek_complete_replay", peek_then_winner,
    )
    response, _ = complete(
        client, assignment, segments=text_segments(assignment, IDENTICAL),
    )
    assert response.status_code == 200, response.json
    assert response.json == stored
    assert client.get("/api/assignment").json["assigned"] is False


def test_over_budget_queues_without_zero_distance(client, seed_tasks, monkeypatch):
    seed_annotated(client, seed_tasks, 1, text=words(20))
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    monkeypatch.setattr("annotation_quality.comparison.MAX_COMPARISON_CELLS", 0)
    login(client, "bob")
    assignment = bob_claim(client)
    response, _ = complete(
        client, assignment, segments=text_segments(assignment, words(20)),
    )
    assert response.status_code == 200, response.json
    assert response.json["cross_check"]["state"] == "awaiting_review"
    assert response.json["cross_check"]["training_export_blocked"] is True
    row = round_row(response.json["cross_check"]["round_id"])
    assert row[0] == "awaiting_review"
    assert "comparison_unavailable" in list(row[1] or [])
    assert row[2] is None
    assert row[2] != 0
    secondary = version_row(assignment["version_id"])
    assert secondary[0] == "cross_check_submitted"
    after = task_published(assignment["task_id"])
    assert after[3] == "published"


def test_compare_exception_stores_null_word_counts(
    client, seed_tasks, monkeypatch,
):
    seed_annotated(client, seed_tasks, 1)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")
    assignment = bob_claim(client)

    def explode(*_args, **_kwargs):
        raise RuntimeError("worddiff failed")

    monkeypatch.setattr(quality_service, "compare_transcripts", explode)
    response, _ = complete(
        client, assignment, segments=text_segments(assignment, IDENTICAL),
    )
    assert response.status_code == 200, response.json
    assert response.json["cross_check"]["state"] == "awaiting_review"
    assert response.json["cross_check"]["training_export_blocked"] is True
    row = round_row(response.json["cross_check"]["round_id"])
    assert row[0] == "awaiting_review"
    assert "comparison_unavailable" in list(row[1] or [])
    assert row[2] is None
    assert row[3] is None
    assert row[4] is None
    with db.db_conn() as conn:
        summaries = conn.execute(
            """SELECT original_normalized_summary, secondary_normalized_summary
               FROM cross_check_rounds WHERE id = %s""",
            (response.json["cross_check"]["round_id"],),
        ).fetchone()
    assert summaries[0] is None
    assert summaries[1] is None
    secondary = version_row(assignment["version_id"])
    assert secondary[0] == "cross_check_submitted"


def test_assignment_released_but_audio_stays_blocked(client, seed_tasks):
    task_ids, _ = seed_annotated(client, seed_tasks, 2, text=changed_words(20, 8))
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")
    first = bob_claim(client)
    queued, _ = complete(
        client, first, segments=text_segments(first, words(20)),
    )
    assert queued.status_code == 200, queued.json
    assert queued.json["cross_check"]["state"] == "awaiting_review"
    assert queued.json["cross_check"]["training_export_blocked"] is True

    current = client.get("/api/assignment")
    assert current.json["assigned"] is False

    second = bob_claim(client)
    assert second["task_id"] != first["task_id"]
    assert second["task_id"] in {str(tid) for tid in task_ids}

    client.post("/api/logout", json={})
    login(client, "alice")
    reopen = client.post(
        f"/api/completed/{first['task_id']}/reopen",
        json={"operation_id": str(uuid.uuid4())},
    )
    assert reopen.status_code == 409
    assert reopen.json.get("code") == "cross_check_active"

    client.post("/api/logout", json={})
    login(client, "carol")
    claim = client.post(
        "/api/assignment/claim", json={"source_scene": "airport"},
    )
    assert claim.status_code == 409


def test_complete_idempotent_replay_and_operation_conflict(client, seed_tasks):
    seed_annotated(client, seed_tasks, 1)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")
    assignment = bob_claim(client)
    first, body = complete(
        client, assignment, segments=text_segments(assignment, IDENTICAL),
    )
    assert first.status_code == 200, first.json
    replay = client.post("/api/assignment/current/complete", json=body)
    assert replay.status_code == 200
    assert replay.json == first.json

    events_after = event_types(assignment["task_id"])
    assert [row[0] for row in events_after].count("cross_check_submitted") == 1
    with db.db_conn() as conn:
        rounds = conn.execute(
            "SELECT count(*) FROM cross_check_rounds WHERE task_id = %s",
            (assignment["task_id"],),
        ).fetchone()[0]
        submitted = conn.execute(
            """SELECT count(*) FROM annotation_versions
               WHERE task_id = %s AND lifecycle = 'cross_check_submitted'""",
            (assignment["task_id"],),
        ).fetchone()[0]
    assert rounds == 1
    assert submitted == 1

    other = dict(body)
    other["segments"] = text_segments(assignment, "totally different text")
    conflict = client.post("/api/assignment/current/complete", json=other)
    assert conflict.status_code == 409


def test_stale_revision_and_original_pointer_conflict(client, seed_tasks, monkeypatch):
    task_ids, _ = seed_annotated(client, seed_tasks, 2)
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")

    stale = bob_claim(client)
    stale_resp, _ = complete(
        client, stale,
        segments=text_segments(stale, IDENTICAL),
        expected_revision=stale["revision"] + 3,
    )
    assert stale_resp.status_code == 409
    assert stale_resp.json.get("conflict") == "revision"
    assert round_row(stale["cross_check"]["round_id"])[0] == "in_progress"

    def bump_revision(*args, **kwargs):
        with db.db_conn() as conn:
            conn.execute(
                "UPDATE annotation_versions SET revision = revision + 1 WHERE id = %s",
                (stale["version_id"],),
            )
        return real_compare(*args, **kwargs)

    monkeypatch.setattr(quality_service, "compare_transcripts", bump_revision)
    raced, _ = complete(
        client, stale, segments=text_segments(stale, IDENTICAL),
    )
    assert raced.status_code == 409
    monkeypatch.undo()
    assert round_row(stale["cross_check"]["round_id"])[0] == "in_progress"

    client.post(
        "/api/assignment/current/abandon",
        json={
            "lease_token": stale["lease_token"],
            "operation_id": str(uuid.uuid4()),
            "confirm": True,
        },
    )

    other = bob_claim(client)
    task_id = other["task_id"]

    def move_pointer(*args, **kwargs):
        with db.db_conn() as conn:
            conn.execute(
                """UPDATE annotation_tasks
                   SET current_published_version_id = baseline_version_id
                   WHERE id = %s""",
                (task_id,),
            )
        return real_compare(*args, **kwargs)

    monkeypatch.setattr(quality_service, "compare_transcripts", move_pointer)
    pointer, _ = complete(
        client, other, segments=text_segments(other, IDENTICAL),
    )
    assert pointer.status_code == 409
    assert round_row(other["cross_check"]["round_id"])[0] == "in_progress"
    secondary = version_row(other["version_id"])
    assert secondary[0] == "draft"


def test_normal_complete_still_rejects_empty_non_bq(client, seed_tasks):
    seed_tasks(1)
    login(client, "alice")
    claimed = client.post("/api/assignment/claim", json={})
    assignment = claimed.json
    assert assignment["mode"] != "cross_check"
    empty = segment_payload(assignment)
    empty[0]["text"] = ""
    response, _ = complete(client, assignment, segments=empty)
    assert response.status_code == 400
    assert "without text" in response.json["error"]
