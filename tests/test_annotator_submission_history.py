"""Self-history combines submissions without exposing quality outcomes."""
from __future__ import annotations

import uuid

import pytest

from annotation_quality.contracts import CrossCheckDecisionCommand
from annotation_quality.service import decide_cross_check
from tests.test_api import login
from tests.test_cross_check_admin import _admin_session, queue_rounds, round_state
from tests.test_cross_check_claim import enable_cross_check
from tests.test_cross_check_submit import complete, text_segments
from tests.test_scene_claims import source


@pytest.mark.parametrize("decision", [None, "original", "secondary"])
def test_submission_history_hides_outcomes_and_counts_adopted_version_once(client, seed_tasks, decision):
    queued = queue_rounds(client, seed_tasks, 1)[0]
    if decision:
        state = round_state(queued["round_id"])
        decide_cross_check(str(_admin_session()["id"]), queued["round_id"],
            CrossCheckDecisionCommand.model_validate({
                "operation_id": str(uuid.uuid4()), "expected_revision": state[1],
                "expected_original_version_id": str(state[2]),
                "expected_secondary_version_id": str(state[3]),
                "decision": decision, "reason": "Private administrator reasoning",
            }))
    login(client, "bob")
    response = client.get("/api/completed?include_submissions=1")
    assert response.status_code == 200, response.json
    assert len(response.json["items"]) == 1
    item = response.json["items"][0]
    assert item["version_id"] == queued["secondary_version_id"]
    assert item["status"] == "annotated"
    assert response.json["summary"]["annotated"] == 1
    assert response.json["summary"]["duration_seconds"] == item["duration"]
    assert not {"mode", "round_id", "state", "reason_codes", "decision"} & item.keys()
    detail = client.get(f'/api/completed/{item["task_id"]}?version_id={item["version_id"]}')
    assert detail.status_code == 200, detail.json
    assert detail.json["segments"][0]["text"] == queued["secondary_text"]
    assert queued["original_text"] not in str(detail.json)
    assert "Private administrator reasoning" not in str(detail.json)


def test_mixed_history_filters_pages_and_preserves_published_summary(client, seed_tasks):
    queued = queue_rounds(client, seed_tasks, 3)
    enable_cross_check(enabled=False)
    task_id = seed_tasks(1, folder="own-skipped")[0]
    source(task_id, "airport", "high")
    login(client, "bob")
    assignment = client.post("/api/assignment/claim", json={"source_scene": "airport"}).json
    response, _ = complete(client, assignment, segments=text_segments(assignment, ""),
                           target_status="skipped", skip_reasons=["noisy"])
    assert response.status_code == 200, response.json
    published = client.get("/api/completed").json
    assert len(published["items"]) == 1
    assert published["summary"]["annotated"] == 0
    assert published["summary"]["skipped"] == 1
    items = []
    cursor = None
    while True:
        args = {"include_submissions": 1, "limit": 1}
        if cursor:
            args["cursor"] = cursor
        response = client.get("/api/completed", query_string=args)
        assert response.status_code == 200, response.json
        assert response.json["summary"]["annotated"] == 3
        assert response.json["summary"]["skipped"] == 1
        items.extend(response.json["items"])
        cursor = response.json["next_cursor"]
        if cursor is None:
            break
    assert len(items) == len({item["task_id"] for item in items}) == 4
    skipped = client.get("/api/completed?include_submissions=1&status=skipped&q=own-skipped").json
    assert [item["task_id"] for item in skipped["items"]] == [task_id]
    assert client.get("/api/completed?include_submissions=1&cursor=invalid").status_code == 400
    assert client.get("/api/completed?include_submissions=invalid").status_code == 400

    # Supplying a version ID never authorizes another person's transcript.
    state = round_state(queued[0]["round_id"])
    forbidden = client.get(f'/api/completed/{queued[0]["task_id"]}?version_id={state[2]}')
    assert forbidden.status_code == 403
    mismatch = client.get(f'/api/completed/{task_id}?version_id={queued[0]["secondary_version_id"]}')
    assert mismatch.status_code == 404
    client.post("/api/logout", json={})
    login(client, "stranger")
    assert client.get("/api/completed?include_submissions=1").json["items"] == []
    assert client.get(f'/api/completed/{queued[0]["task_id"]}?version_id={state[3]}').status_code == 403
