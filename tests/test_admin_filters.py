"""Exercise filter chips through the public admin APIs and actual PostgreSQL data."""
import uuid

import annotation_repository as repo
import db
from tests.test_admin_api import admin_client, _admin_login
from tests.test_cross_check_admin import queue_rounds, decision_body
from tests.test_regression_admin_metadata import source


def test_scene_or_filters_keep_same_evidence_and_counts_before_pagination(admin_client, seed_tasks):
    a, b, c, d, legacy, unknown = seed_tasks(6)
    source(a, "airport", "high", "east")
    source(a, "airport", "high", "repeat")
    source(b, "clinic", "medium", "west")
    source(c, "airport", "medium", "east")
    source(c, "clinic", "high", "west")
    source(d, "clinic", "low", "west")
    source(unknown, None, "unknown", "west")
    _admin_login(admin_client)
    params = {"source_scene": "airport,clinic", "source_confidence": "high", "limit": 1}
    response = admin_client.get("/api/admin/tasks", query_string=params)
    assert response.status_code == 200, response.json
    body = response.json
    assert body["matched_count"] == 2
    assert body["matched_duration_seconds"] == 20
    assert body["filter_counts"]["status"] == {"all": 2, "pending": 2, "assigned": 0, "annotated": 0, "skipped": 0}
    assert body["filter_counts"]["confidence"] == {"high": 2, "medium": 2, "low": 1}
    assert body["filter_counts"]["scenes"] == {"airport": 1, "clinic": 1}
    assert len(body["items"]) == 1
    second = admin_client.get("/api/admin/tasks", query_string={**params, "cursor": body["next_cursor"]})
    assert second.status_code == 200, second.json
    assert {body["items"][0]["task_id"], second.json["items"][0]["task_id"]} == {str(a), str(c)}
    assert second.json["next_cursor"] is None
    wrong = admin_client.get("/api/admin/tasks", query_string={**params, "source_scene": "airport", "cursor": body["next_cursor"]})
    assert wrong.status_code == 400
    # Scene and confidence must belong to one source row.
    airport = admin_client.get("/api/admin/tasks", query_string={**params, "source_scene": "airport"}).json
    assert airport["matched_count"] == 1
    assert airport["items"][0]["task_id"] == str(a)
    combined = admin_client.get("/api/admin/tasks", query_string={"source_scene": "airport,spoken_languages", "source_confidence": "unknown"}).json
    assert {item["task_id"] for item in combined["items"]} == {str(legacy), str(unknown)}
    batch = admin_client.get("/api/admin/tasks", query_string={"source_scene": "airport,spoken_languages", "source_confidence": "unknown", "batch_code": "west"}).json
    assert [item["task_id"] for item in batch["items"]] == [str(unknown)]
    overview = admin_client.get("/api/admin/overview", query_string=params).json
    assert overview["totals"]["total_audio_count"] == 2
    assert {item["scene_code"] for item in overview["source_scenes"]} == {"airport", "clinic"}
    assert admin_client.get("/api/admin/tasks", query_string={"source_scene": "airport,invalid"}).status_code == 400
    # Multi-select remains an admin feature; claiming still accepts one scene.
    assert admin_client.get("/api/admin/tasks", query_string={"source_confidence": "invalid"}).status_code == 400


def test_assignee_filters_dates_and_zero_results_have_honest_counts(admin_client, seed_tasks):
    task_ids = seed_tasks(3)
    user = repo.login("assigned-filter", str(uuid.uuid4()), 1800)
    assignment = repo.claim(user["fence"])
    _admin_login(admin_client)
    who = str(user["id"])
    assigned = admin_client.get("/api/admin/tasks", query_string={"assignee_id": who}).json
    assert assigned["matched_count"] == 1
    assert assigned["items"][0]["task_id"] == assignment["task_id"]
    assert assigned["filter_counts"]["status"]["assigned"] == 1
    free = admin_client.get("/api/admin/tasks", query_string={"assignee_id": "unassigned"}).json
    assert free["matched_count"] == 2
    assert all(item["assignment"] is None for item in free["items"])
    with db.db_conn() as conn:
        conn.execute("UPDATE annotation_tasks SET created_at='2020-01-01T01:00:00Z' WHERE id = %s", (task_ids[0],))
    dated = admin_client.get("/api/admin/tasks", query_string={"from": "2020-01-01", "to": "2020-01-02", "timezone": "Asia/Shanghai"}).json
    assert dated["matched_count"] == 1
    assert dated["filter_counts"]["status"]["all"] == 1
    none = admin_client.get("/api/admin/tasks", query_string={"q": "no-matching-audio"}).json
    assert none["matched_count"] == 0
    assert none["filter_counts"]["status"]["all"] == 0
    assert none["filter_counts"]["scenes"] == {}
    assert admin_client.get("/api/admin/tasks", query_string={"assignee_id": "invalid"}).status_code == 400


def test_crosscheck_scene_confidence_state_counts_and_filter_bound_cursor(admin_client, seed_tasks):
    rounds = queue_rounds(admin_client, seed_tasks, 3)
    with db.db_conn() as conn:
        conn.execute("UPDATE task_sources SET is_current=false WHERE task_id = ANY(%s)", ([uuid.UUID(item["task_id"]) for item in rounds],))
    source(rounds[0]["task_id"], "airport", "high", "east")
    source(rounds[1]["task_id"], "clinic", "high", "west")
    source(rounds[2]["task_id"], "clinic", "low", "west")
    _, headers = _admin_login(admin_client)
    rid = rounds[1]["round_id"]
    detail = admin_client.get(f"/api/admin/cross-checks/{rid}").json
    decided = admin_client.post(f"/api/admin/cross-checks/{rid}/decision", headers=headers,
                               json=decision_body(detail, decision="original", reason="filter fixture"))
    assert decided.status_code == 200, decided.json
    params = {"source_scene": "airport,clinic", "source_confidence": "high", "state": "all", "limit": 1}
    response = admin_client.get("/api/admin/cross-checks", query_string=params)
    assert response.status_code == 200, response.json
    body = response.json
    assert body["matched_count"] == 2
    assert body["matched_duration_seconds"] == 20
    assert body["state_counts"] == {"awaiting_review": 1, "adjudicated": 1}
    assert body["next_cursor"]
    second = admin_client.get("/api/admin/cross-checks", query_string={**params, "cursor": body["next_cursor"]})
    assert second.status_code == 200
    assert {body["items"][0]["round_id"], second.json["items"][0]["round_id"]} == {rounds[0]["round_id"], rid}
    filtered = admin_client.get("/api/admin/cross-checks", query_string={**params, "state": "awaiting_review"}).json
    assert filtered["matched_count"] == 1
    assert filtered["state_counts"] == body["state_counts"]
    assert admin_client.get("/api/admin/cross-checks", query_string={**params, "source_confidence": "low", "cursor": body["next_cursor"]}).status_code == 400
