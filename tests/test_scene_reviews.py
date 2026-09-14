from __future__ import annotations

import hashlib
import json
import uuid

import pytest

import annotation_repository as repo
import db
from tests.test_repository import full_segments, make_user


def test_legacy_complete_does_not_clear_review(database, seed_tasks):
    seed_tasks(1)
    user, _ = make_user("alice")
    assignment = repo.claim(user["id"])
    result = repo.complete(
        user["id"], assignment["lease_token"], assignment["revision"],
        "annotated", [], full_segments(assignment), str(uuid.uuid4()), "h",
    )
    assert result["success"]
    detail = repo.completed_detail(user["id"], assignment["task_id"])
    assert detail["metadata"]["scene_review"]["status"] == "pending"


def test_review_saves_with_complete_and_ignores_label_order(database, seed_tasks):
    seed_tasks(1)
    user, _ = make_user("alice")
    assignment = repo.claim(user["id"])
    review = {
        "status": "mixed",
        "scene_codes": ["shopping", "airport"],
        "note": "both",
    }
    saved = repo.save_draft(
        user["id"], assignment["lease_token"], 0, full_segments(assignment),
        str(uuid.uuid4()), "h1", scene_review=review,
    )
    assert saved["scene_review"]["scene_codes"] == ["airport", "shopping"]
    again = repo.save_draft(
        user["id"], assignment["lease_token"], saved["revision"],
        full_segments(assignment), str(uuid.uuid4()), "h2",
        scene_review={"status": "mixed", "scene_codes": ["airport", "shopping"], "note": "both"},
    )
    assert again["scene_review_changed"] is False
    completed = repo.complete(
        user["id"], assignment["lease_token"], again["revision"],
        "annotated", [], full_segments(assignment), str(uuid.uuid4()), "h3",
        scene_review=review,
    )
    assert completed["scene_review"]["status"] == "mixed"
    detail = repo.completed_detail(user["id"], assignment["task_id"])
    assert detail["metadata"]["scene_review"]["status"] == "mixed"


def test_unpublished_confirmed_headline_uses_human_labels_not_published_wording(
        database, seed_tasks):
    seed_tasks(1)
    user, _ = make_user("headline-unpublished")
    assignment = repo.claim(user["id"])
    saved = repo.save_draft(
        user["id"], assignment["lease_token"], 0, full_segments(assignment),
        str(uuid.uuid4()), "headline-save",
        scene_review={"status": "confirmed", "scene_codes": ["shopping"]},
    )
    assert saved["scene_review"]["status"] == "confirmed"
    current = repo.get_assignment(user["id"])
    headline = current["metadata"]["headline"]
    assert "未提交" in headline
    assert "购物" in headline
    assert current["metadata"]["scene_review"]["submitted"] is False


def test_correction_draft_review_is_pending_not_published(database, seed_tasks):
    seed_tasks(1)
    user, _ = make_user("alice")
    assignment = repo.claim(user["id"])
    repo.complete(
        user["id"], assignment["lease_token"], 0, "annotated", [],
        full_segments(assignment), str(uuid.uuid4()), "complete",
        scene_review={"status": "confirmed", "scene_codes": ["airport"]},
    )
    repo.reopen_completed(user["id"], assignment["task_id"], str(uuid.uuid4()))
    draft = repo.get_assignment(user["id"])
    assert draft["metadata"]["draft_review"]["status"] == "pending"
    assert draft["metadata"]["reference_review"]["status"] == "confirmed"
    assert draft["metadata"]["scene_review"]["status"] == "pending"


def test_admin_review_correction_and_replay(database, seed_tasks):
    first_id, other = seed_tasks(2)
    user, _ = make_user("alice")
    assignment = repo.claim(user["id"])
    assert assignment["task_id"] == first_id
    repo.complete(
        user["id"], assignment["lease_token"], 0, "annotated", [],
        full_segments(assignment), str(uuid.uuid4()), "complete",
        scene_review={"status": "confirmed", "scene_codes": ["airport"]},
    )
    detail = repo.admin_annotation_detail(assignment["task_id"])
    session = repo.create_admin_session(
        "primary", hashlib.sha256(b"token").hexdigest(),
        hashlib.sha256(b"csrf").hexdigest(), 1800, 3600,
    )
    op = str(uuid.uuid4())
    payload = {
        "operation_id": op,
        "expected_version_id": detail["current_version_id"],
        "expected_review_id": detail["metadata"]["scene_review"]["id"],
        "status": "mixed",
        "scene_codes": ["airport", "shopping"],
        "note": "admin",
        "reason": "correct mixed scenes",
    }
    first = repo.admin_correct_scene_review(session["id"], assignment["task_id"], payload)
    assert first["review"]["status"] == "mixed"
    replay = repo.admin_correct_scene_review(session["id"], assignment["task_id"], payload)
    assert replay.get("idempotent_replay") is True
    other_user, _ = make_user("bob")
    other_asg = repo.claim(other_user["id"])
    repo.complete(
        other_user["id"], other_asg["lease_token"], 0, "annotated", [],
        full_segments(other_asg), str(uuid.uuid4()), "complete2",
    )
    with pytest.raises(repo.ConflictError):
        repo.admin_correct_scene_review(session["id"], other, payload)
