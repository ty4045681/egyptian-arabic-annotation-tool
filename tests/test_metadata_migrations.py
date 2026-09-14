from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest

import db
from preprocess_store import store_preprocessed_task


def test_003_seeds_scenes_and_keeps_legacy_tasks_unknown(database, seed_tasks):
    seed_tasks(2)
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM scenes").fetchone()[0] == 9
        assert db.applied_versions(conn) == [1, 2, 3]
        assert conn.execute("SELECT count(*) FROM task_sources").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM annotation_tasks").fetchone()[0] == 2


def test_feature_flags_keep_scope_when_fifo(database, seed_tasks, monkeypatch):
    import annotation_repository as repo
    monkeypatch.setenv("ANNOTATION_CLAIM_POLICY", "fifo")
    monkeypatch.setenv("ANNOTATION_SCENE_REVIEW_WRITE", "0")
    monkeypatch.setenv("ANNOTATION_METADATA_UI", "0")
    seed_tasks(1)
    user = repo.login("flag-user", str(uuid.uuid4()), 1800)
    assignment = repo.claim(user["id"])
    assert assignment["assigned"] is True
    with pytest.raises(repo.ForbiddenError):
        repo.save_draft(
            user["id"], assignment["lease_token"], 0, [],
            str(uuid.uuid4()), "h",
            scene_review={"status": "confirmed", "scene_codes": ["airport"]},
        )


def test_metadata_export_round_trip(database, seed_tasks, tmp_path):
    from annotation_metadata.export_metadata import export_metadata, verify_metadata_file
    import annotation_repository as repo
    seed_tasks(1)
    user = repo.login("exp", str(uuid.uuid4()), 1800)
    assignment = repo.claim(user["id"])
    repo.complete(
        user["id"], assignment["lease_token"], 0, "annotated", [],
        [{"id": seg["id"], "start": seg["start"], "end": seg["end"],
          "duration": seg["duration"], "text": "t", "exclude_from_training": False}
         for seg in assignment["segments"]],
        str(uuid.uuid4()), "h",
        scene_review={"status": "confirmed", "scene_codes": ["hotel"]},
    )
    with db.db_conn() as conn:
        result = export_metadata(conn, tmp_path / "meta")
    verified = verify_metadata_file(Path(result["path"]))
    assert verified["ok"]
    assert verified["tasks"] >= 1
