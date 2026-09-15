"""Reopening creates a revision assignment, not an unknown-source claim."""
import uuid

import pytest

import annotation_repository as repo
from tests.test_regression_admin_metadata import source
from tests.test_repository import full_segments, make_user


@pytest.mark.parametrize("multiple_sources", [False, True])
def test_reopened_task_keeps_known_source_information(seed_tasks, multiple_sources):
    task = seed_tasks(1)[0]
    source(task, "airport", "high", "reopen-airport")
    if multiple_sources:
        source(task, "shopping", "low", "reopen-shopping")
    user, _ = make_user("reviewer-reopen-source")
    assignment = repo.claim(user["id"], source_scene="airport")
    repo.complete(
        user["id"], assignment["lease_token"], 0, "annotated", [],
        full_segments(assignment), str(uuid.uuid4()), "reviewer-reopen",
        scene_review={"status": "confirmed", "scene_codes": ["airport"]},
    )
    repo.reopen_completed(user["id"], task, str(uuid.uuid4()))
    metadata = repo.get_assignment(user["id"])["metadata"]
    assert "Airport" in metadata["headline"], metadata
    assert "Source confidence High" in metadata["headline"], metadata
    assert "Unknown source" not in metadata["headline"], metadata
    assert "Source confidence Unknown" not in metadata["headline"], metadata
    assert metadata["draft_review"]["status"] == "pending"
