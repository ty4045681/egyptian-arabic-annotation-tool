"""Independent public repository acceptance for filtered metadata summaries."""
from __future__ import annotations

import uuid

import pytest

import annotation_repository as repo
import db
from tests.test_repository import full_segments, make_user


def source(task_id, scene, confidence, batch):
    with db.db_conn() as conn:
        batch_id = conn.execute(
            "INSERT INTO source_batches(batch_code,name) VALUES(%s,%s) "
            "ON CONFLICT(batch_code) DO UPDATE SET name=EXCLUDED.name RETURNING id",
            (batch, batch),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO task_sources(task_id,batch_id,record_key,scene_code,"
            "confidence,confidence_basis,content_digest) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (task_id, batch_id, str(uuid.uuid4()), scene, confidence,
             "independent acceptance evidence", "0" * 64),
        )


def test_filtered_task_summary_keeps_scene_confidence_association(seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "airport", "medium", "acceptance-airport")
    source(task, "shopping", "high", "acceptance-shopping")
    result = repo.admin_tasks({"source_scene": "airport"})
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["source_confidence"] == "medium", item
    assert item["batch_codes"] == ["acceptance-airport"], item


def test_overview_source_groups_apply_same_evidence_filters(seed_tasks):
    first, second = seed_tasks(2, duration=10.0)
    source(first, "airport", "medium", "acceptance-airport")
    source(first, "shopping", "high", "acceptance-shopping")
    source(second, "airport", "medium", "acceptance-airport")
    source(second, "airport", "medium", "acceptance-airport-repeat")
    result = repo.admin_overview({"source_scene": "airport", "source_confidence": "medium"})
    assert result["totals"]["total_audio_count"] == 2
    assert result["totals"]["total_audio_duration_seconds"] == 20.0
    groups = {row["scene_code"]: row for row in result["source_scenes"]}
    assert set(groups) == {"airport"}, groups
    assert groups["airport"]["task_count"] == 2
    assert groups["airport"]["duration_seconds"] == 20.0
    confidence = {row["confidence"]: row for row in result["confidence_buckets"]}
    assert confidence["medium"]["task_count"] == 2
    assert confidence["medium"]["duration_seconds"] == 20.0


def test_unknown_scene_group_counts_legacy_and_explicit_unknown_once(seed_tasks):
    legacy, explicit = seed_tasks(2)
    source(explicit, None, "unknown", "acceptance-explicit-unknown")
    result = repo.admin_overview({"source_scene": "unknown"})
    groups = {row["scene_code"]: row for row in result["source_scenes"]}
    assert groups["unknown"]["task_count"] == 2, groups
    assert groups["unknown"]["duration_seconds"] == 20.0


def test_corpus_cursor_cannot_cross_search_filter_context(seed_tasks):
    seed_tasks(3)
    first = repo.admin_tasks({"q": "audio-"}, limit=1)
    assert first["next_cursor"]
    with pytest.raises(repo.ValidationError):
        repo.admin_tasks({"q": "audio-002"}, limit=1, cursor=first["next_cursor"])


def test_claim_headline_uses_selected_evidence_not_first_same_scene(seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "airport", "low", "acceptance-airport-low")
    source(task, "airport", "high", "acceptance-airport-high")
    with db.db_conn() as conn:
        conn.execute("UPDATE task_sources SET id='00000000-0000-4000-8000-000000000001' WHERE task_id=%s AND confidence='low'", (task,))
        conn.execute("UPDATE task_sources SET id='00000000-0000-4000-8000-000000000002' WHERE task_id=%s AND confidence='high'", (task,))
    user = repo.login("acceptance-headline-evidence", str(uuid.uuid4()), 1800)
    claimed = repo.claim(user["id"], source_scene="airport", batch_code="acceptance-airport-high")
    metadata = claimed["metadata"]
    assert metadata["claim_context"]["confidence"] == "high"
    assert "来源置信度高" in metadata["headline"], metadata["headline"]
    assert "2 个来源场景" not in metadata["headline"], "Two airport sources represent one distinct scene"


def test_repeated_sources_do_not_double_count_task_or_duration(seed_tasks):
    task = seed_tasks(1, duration=12.0)[0]
    source(task, "airport", "medium", "repeat-batch-one")
    source(task, "airport", "medium", "repeat-batch-two")
    result = repo.admin_overview({"source_scene": "airport"})
    groups = {row["scene_code"]: row for row in result["source_scenes"]}
    assert groups["airport"]["task_count"] == 1
    assert groups["airport"]["duration_seconds"] == 12.0
    batches = {row["batch_code"]: row for row in result["source_batches"]}
    assert set(batches) == {"repeat-batch-one", "repeat-batch-two"}
    assert batches["repeat-batch-one"]["task_count"] == 1
    assert batches["repeat-batch-two"]["task_count"] == 1
    assert batches["repeat-batch-one"]["duration_seconds"] == 12.0
    assert batches["repeat-batch-two"]["duration_seconds"] == 12.0


def test_equal_durations_are_summed_not_collapsed(seed_tasks):
    first, second = seed_tasks(2, duration=7.5)
    source(first, "airport", "high", "equal-duration-batch")
    source(second, "airport", "high", "equal-duration-batch")
    result = repo.admin_overview({"source_scene": "airport"})
    groups = {row["scene_code"]: row for row in result["source_scenes"]}
    assert groups["airport"]["task_count"] == 2
    assert groups["airport"]["duration_seconds"] == 15.0
    batches = {row["batch_code"]: row for row in result["source_batches"]}
    assert batches["equal-duration-batch"]["task_count"] == 2
    assert batches["equal-duration-batch"]["duration_seconds"] == 15.0


def test_overview_batch_groups_overlap_and_definitions_are_explicit(seed_tasks):
    first, second = seed_tasks(2, duration=10.0)
    source(first, "airport", "medium", "overlap-airport")
    source(first, "shopping", "high", "overlap-shopping")
    source(second, "airport", "medium", "overlap-airport")
    result = repo.admin_overview({})
    scenes = {row["scene_code"]: row for row in result["source_scenes"]}
    assert scenes["airport"]["task_count"] == 2
    assert scenes["shopping"]["task_count"] == 1
    assert scenes["airport"]["overlapping"] is True
    batches = {row["batch_code"]: row for row in result["source_batches"]}
    assert batches["overlap-airport"]["task_count"] == 2
    assert batches["overlap-shopping"]["task_count"] == 1
    assert result["definitions"]["scene_groups_overlap"] is True
    assert result["definitions"]["batch_groups_overlap"] is True
    assert result["as_of"] == result["updated_at"]
    assert "REPEATABLE READ" in result["definitions"]["snapshot"]


def test_draft_review_is_not_published_verification(seed_tasks):
    seed_tasks(1)
    user, _ = make_user("draft-review-stats")
    assignment = repo.claim(user["id"])
    repo.save_draft(
        user["id"], assignment["lease_token"], 0, full_segments(assignment),
        str(uuid.uuid4()), "draft-review",
        scene_review={"status": "mixed", "scene_codes": ["airport", "shopping"]},
    )
    result = repo.admin_overview({})
    statuses = {row["status"]: row for row in result["review_statuses"]}
    assert "mixed" not in statuses
    assert statuses["unreviewed_unpublished"]["task_count"] == 1
    assert result["review_stats"]["mixed"] == 0


def test_published_review_groups_include_duration(seed_tasks):
    seed_tasks(1)
    user, _ = make_user("published-review-stats")
    assignment = repo.claim(user["id"])
    repo.complete(
        user["id"], assignment["lease_token"], 0, "annotated", [],
        full_segments(assignment), str(uuid.uuid4()), "complete-review",
        scene_review={"status": "confirmed", "scene_codes": ["airport"]},
    )
    result = repo.admin_overview({})
    statuses = {row["status"]: row for row in result["review_statuses"]}
    assert statuses["confirmed"]["task_count"] == 1
    assert statuses["confirmed"]["duration_seconds"] == 10.0
    assert statuses["confirmed"]["overlapping"] is False
    assert result["review_stats"]["confirmed"] == 1
    assert result["review_stats"]["unreviewed_unpublished"] == 0


def test_corpus_cursor_continues_same_search_and_rejects_status_change(seed_tasks):
    seed_tasks(3)
    first = repo.admin_tasks({"q": "audio-"}, limit=1)
    second = repo.admin_tasks({"q": "audio-"}, limit=1, cursor=first["next_cursor"])
    assert first["items"][0]["task_id"] != second["items"][0]["task_id"]
    pending = repo.admin_tasks({"status": "pending"}, limit=1)
    with pytest.raises(repo.ValidationError):
        repo.admin_tasks(
            {"status": "annotated"}, limit=1, cursor=pending["next_cursor"],
        )


def test_applied_filters_include_search_and_source(seed_tasks):
    seed_tasks(1)
    result = repo.admin_tasks({"q": "audio-000", "source_scene": "airport"})
    assert result["applied_filters"]["q"] == "audio-000"
    assert result["applied_filters"]["source_scene"] == "airport"
    overview = repo.admin_overview({
        "q": "audio-000", "from": "2026-01-01T00:00:00+00:00",
    })
    assert overview["applied_filters"]["q"] == "audio-000"
    assert overview["applied_filters"]["from"]


def test_task_detail_keeps_full_source_provenance(seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "airport", "medium", "detail-airport")
    source(task, "shopping", "high", "detail-shopping")
    listed = repo.admin_tasks({"source_scene": "airport"})["items"][0]
    assert listed["source_confidence"] == "medium"
    assert listed["batch_codes"] == ["detail-airport"]
    detail = repo.admin_annotation_detail(task)
    scenes = {item["scene_code"] for item in detail["metadata"]["sources"]}
    batches = {item["batch_code"] for item in detail["metadata"]["sources"]}
    assert scenes == {"airport", "shopping"}
    assert batches == {"detail-airport", "detail-shopping"}


def test_claim_headline_keeps_historical_evidence_after_revision(seed_tasks):
    task = seed_tasks(1)[0]
    source(task, "airport", "high", "headline-historical-high")
    user = repo.login("headline-historical", str(uuid.uuid4()), 1800)
    claimed = repo.claim(user["id"], source_scene="airport")
    source_id = claimed["metadata"]["claim_context"]["source_id"]
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE task_sources SET is_current = false WHERE id = %s",
            (source_id,),
        )
    source(task, "airport", "low", "headline-historical-low")
    resumed = repo.claim(user["id"])
    metadata = resumed["metadata"]
    assert metadata["claim_context"]["confidence"] == "high"
    assert metadata["claim_context"]["source_is_current"] is False
    assert metadata["claim_context"]["historical"] is True
    assert "来源置信度高" in metadata["headline"]
    current = metadata["claim_context"]["current_evidence"]
    assert current
    assert current[0]["confidence"] == "low"
