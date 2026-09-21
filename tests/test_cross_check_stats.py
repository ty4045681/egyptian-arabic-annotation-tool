"""Corpus duration is unique-task; cross-check workload stays independent."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

import annotation_repository as repo
import db
from annotation_quality.contracts import CrossCheckDecisionCommand
from annotation_quality.queries import (
    cross_check_submitted_workload,
    unique_annotated_corpus,
)
from annotation_quality.service import decide_cross_check
from tests.test_api import login
from tests.test_cross_check_admin import _admin_session, queue_rounds
from tests.test_cross_check_claim import enable_cross_check
from tests.test_cross_check_submit import (
    IDENTICAL,
    bob_claim,
    complete,
    text_segments,
)
from tests.test_scene_claims import source


def _seed_annotated_duration(client, seed_tasks, count, duration, *, text=IDENTICAL):
    task_ids = seed_tasks(count, duration=duration)
    for task_id in task_ids:
        source(task_id, "airport", "high")
    login(client, "alice")
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
    client.post("/api/logout", json={})
    return task_ids


def _corpus():
    with db.db_conn() as conn, conn.cursor() as cur:
        return unique_annotated_corpus(cur)


def _workload():
    with db.db_conn() as conn, conn.cursor() as cur:
        return cross_check_submitted_workload(cur)


def _round_versions(round_id):
    with db.db_conn() as conn:
        return conn.execute(
            """SELECT original_version_id, secondary_version_id, revision
               FROM cross_check_rounds WHERE id = %s""",
            (round_id,),
        ).fetchone()


def test_one_60s_audio_stays_60_while_b_workload_is_independent(
        client, seed_tasks):
    task_ids = _seed_annotated_duration(client, seed_tasks, 1, 60.0)
    assert _corpus() == (1, 60.0)
    assert _workload() == (0, 0.0)
    dash = repo.dashboard()
    assert dash["stats"]["annotated_duration_seconds"] == pytest.approx(60.0)
    assert dash["stats"]["cross_check_submitted_count"] == 0

    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")
    assignment = bob_claim(client)
    assert assignment["task_id"] == task_ids[0]
    assert _corpus() == (1, 60.0)
    assert _workload() == (0, 0.0)
    pool = repo.pool_state()
    assert pool["assigned"] == 0
    assert pool["cross_check_in_progress"] == 1
    dash = repo.dashboard()
    assert dash["stats"]["annotated_duration_seconds"] == pytest.approx(60.0)

    response, _ = complete(
        client, assignment, segments=text_segments(assignment, IDENTICAL),
    )
    assert response.status_code == 200, response.json
    assert response.json["cross_check"]["state"] == "passed"
    assert _corpus() == (1, 60.0)
    count, seconds = _workload()
    assert count == 1
    assert seconds == pytest.approx(60.0)
    dash = repo.dashboard()
    assert dash["stats"]["annotated_duration_seconds"] == pytest.approx(60.0)
    assert dash["stats"]["cross_check_submitted_count"] == 1
    assert dash["stats"]["cross_check_submitted_audio_seconds"] == pytest.approx(60.0)


def test_two_60s_audios_sum_to_120_not_distinct_duration(client, seed_tasks):
    _seed_annotated_duration(client, seed_tasks, 2, 60.0)
    count, seconds = _corpus()
    assert count == 2
    assert seconds == pytest.approx(120.0)
    dash = repo.dashboard()
    assert dash["stats"]["annotated_duration_seconds"] == pytest.approx(120.0)

    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")
    first = bob_claim(client)
    complete(client, first, segments=text_segments(first, IDENTICAL))
    second = bob_claim(client)
    complete(client, second, segments=text_segments(second, IDENTICAL))
    assert _corpus() == (2, 120.0)
    workload_count, workload_seconds = _workload()
    assert workload_count == 2
    assert workload_seconds == pytest.approx(120.0)


def test_awaiting_review_keeps_corpus_duration_and_adds_workload(
        client, seed_tasks):
    queued = queue_rounds(client, seed_tasks, 1)
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE annotation_tasks SET duration = 60 WHERE id = %s",
            (queued[0]["task_id"],),
        )
        conn.commit()
    assert _corpus()[1] == pytest.approx(60.0)
    assert _workload()[1] == pytest.approx(60.0)
    overview = repo.admin_overview({})
    assert overview["totals"]["annotated_duration_seconds"] == pytest.approx(60.0)
    assert overview["totals"]["cross_check_submitted_count"] == 1
    assert overview["pending"]["assigned_count"] == 0
    assert overview["pending"]["cross_check_in_progress_count"] == 0
    assert overview["cross_check"]["pending_review_count"] == 1


def test_pool_splits_normal_and_cross_check_available(client, seed_tasks):
    task_ids = seed_tasks(2, duration=60.0)
    for task_id in task_ids:
        source(task_id, "airport", "high")
    login(client, "alice")
    claimed = client.post(
        "/api/assignment/claim", json={"source_scene": "airport"},
    )
    complete(
        client, claimed.json,
        segments=text_segments(claimed.json, IDENTICAL),
    )
    client.post("/api/logout", json={})

    bob = repo.login("bob", str(uuid.uuid4()), 1800)
    enable_cross_check(enabled=False, sampling_rate_bps=10000)
    disabled = repo.pool_state(bob["id"], source_scene="airport")
    assert disabled["normal_available"] == 1
    assert disabled["cross_check_available"] == 0
    assert disabled["available"] == 1

    enable_cross_check(enabled=True, sampling_rate_bps=0)
    zero = repo.pool_state(bob["id"], source_scene="airport")
    assert zero["cross_check_available"] == 0
    assert zero["available"] == 1

    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    enabled = repo.pool_state(bob["id"], source_scene="airport")
    assert enabled["normal_available"] == 1
    assert enabled["cross_check_available"] == 1
    assert enabled["available"] == 2
    assert enabled["reason"] == "available"
    assert enabled["cross_check_in_progress"] == 0


def test_cross_check_by_scene_matches_claim_source_row(client, seed_tasks):
    task_id = seed_tasks(1)[0]
    source(task_id, "airport", "high")
    source(task_id, "shopping", "low")
    login(client, "alice")
    claimed = client.post("/api/assignment/claim", json={})
    assert claimed.status_code == 200, claimed.json
    complete(
        client, claimed.json,
        segments=text_segments(claimed.json, IDENTICAL),
    )
    client.post("/api/logout", json={})
    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    bob = repo.login("bob", str(uuid.uuid4()), 1800)
    high = repo.pool_state(bob["id"], source_confidence="high")
    by_scene = {item["scene_code"]: item["available"] for item in high["by_scene"]}
    assert high["cross_check_available"] == 1
    assert by_scene["airport"] == 1
    assert by_scene["shopping"] == 0
    shopping_high = repo.pool_state(
        bob["id"], source_scene="shopping", source_confidence="high",
    )
    assert shopping_high["cross_check_available"] == 0
    assert shopping_high["available"] == 0


def test_auto_pass_does_not_move_speed_bucket(client, seed_tasks, monkeypatch):
    monkeypatch.setattr(
        repo, "utcnow",
        lambda: datetime(2026, 9, 17, 9, 30, tzinfo=timezone.utc),
    )
    task_ids = _seed_annotated_duration(client, seed_tasks, 1, 60.0)
    original_day = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE annotation_versions AS version
                  SET submitted_at = %s
                 FROM annotation_tasks AS task
                WHERE task.id = %s
                  AND version.id = task.current_published_version_id""",
            (original_day, task_ids[0]),
        )
        conn.commit()
    before = repo.public_annotation_speed("UTC")
    before_days = {item["date"]: item["duration_seconds"] for item in before["days"]}
    assert before_days["2026-09-10"] == pytest.approx(60.0)

    enable_cross_check(enabled=True, sampling_rate_bps=10000)
    login(client, "bob")
    assignment = bob_claim(client)
    complete(client, assignment, segments=text_segments(assignment, IDENTICAL))
    after = repo.public_annotation_speed("UTC")
    after_days = {item["date"]: item["duration_seconds"] for item in after["days"]}
    assert after_days["2026-09-10"] == pytest.approx(60.0)
    assert sum(after_days.values()) == pytest.approx(60.0)


def test_adopting_secondary_reattributes_speed_to_b_submit_time(
        client, seed_tasks):
    queued = queue_rounds(client, seed_tasks, 1)
    round_id = queued[0]["round_id"]
    original_id, secondary_id, revision = _round_versions(round_id)
    original_at = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)
    secondary_at = datetime.now(timezone.utc).replace(
        hour=8, minute=0, second=0, microsecond=0,
    )
    secondary_day = secondary_at.date().isoformat()
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE annotation_tasks SET duration = 60 WHERE id = %s",
            (queued[0]["task_id"],),
        )
        conn.execute(
            "UPDATE annotation_versions SET submitted_at = %s WHERE id = %s",
            (original_at, original_id),
        )
        conn.execute(
            "UPDATE annotation_versions SET submitted_at = %s WHERE id = %s",
            (secondary_at, secondary_id),
        )
        conn.commit()
    command = CrossCheckDecisionCommand.model_validate({
        "operation_id": str(uuid.uuid4()),
        "expected_revision": int(revision),
        "expected_original_version_id": str(original_id),
        "expected_secondary_version_id": str(secondary_id),
        "decision": "secondary",
        "reason": "take B for speed reattribution",
    })
    result = decide_cross_check(str(_admin_session()["id"]), str(round_id), command)
    assert result["state"] == "adjudicated"
    speed = repo.public_annotation_speed("UTC")
    days = {item["date"]: item["duration_seconds"] for item in speed["days"]}
    assert days.get("2026-09-01", 0.0) == pytest.approx(0.0)
    assert days[secondary_day] == pytest.approx(60.0)
    assert sum(days.values()) == pytest.approx(60.0)
    assert _corpus()[1] == pytest.approx(60.0)
