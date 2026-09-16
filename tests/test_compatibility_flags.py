"""Same-schema compatibility rollback via feature flags, not git checkout."""

from __future__ import annotations

import uuid

import pytest

import annotation_repository as repo
import db
from annotation_metadata.export_metadata import export_metadata
from annotation_metadata.ingestion import import_one_record
from annotation_repository import ForbiddenError
from tests.provenance_fixtures import build_rich_provenance
from tests.test_repository import full_segments, make_user


def _disable_new_writes(monkeypatch):
    monkeypatch.setenv("ANNOTATION_CLAIM_POLICY", "fifo")
    monkeypatch.setenv("ANNOTATION_SCENE_REVIEW_WRITE", "0")
    monkeypatch.setenv("ANNOTATION_METADATA_UI", "0")
    monkeypatch.setenv("ANNOTATION_METADATA_WRITE", "0")


def test_flags_off_fifo_reads_old_and_new_and_refuses_writes(
        database, seed_tasks, client, tmp_path, monkeypatch):
    fixture = build_rich_provenance(seed_tasks)
    with db.db_conn() as conn:
        before_reviews = conn.execute("SELECT count(*) FROM scene_reviews").fetchone()[0]
        before_sources = conn.execute("SELECT count(*) FROM task_sources").fetchone()[0]
        extra_user_id = conn.execute("SELECT user_id FROM assignments LIMIT 1").fetchone()[0]
    extra_assignment = repo.get_assignment(extra_user_id)
    _disable_new_writes(monkeypatch)

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json["ok"] is True
    assert health.json["schema_versions"] == db.expected_versions()

    with db.db_conn() as conn:
        tables = {
            row[0] for row in conn.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema = 'public'"""
            ).fetchall()
        }
        assert {
            "task_sources", "scene_reviews", "task_scene_predictions",
            "annotator_scene_scopes", "task_media_identities",
        }.issubset(tables)
        conn.commit()
        exported = export_metadata(conn, tmp_path / "flag-export")
        assert exported["tasks"] == 3
        assert conn.execute("SELECT count(*) FROM scene_reviews").fetchone()[0] == before_reviews
        assert conn.execute("SELECT count(*) FROM task_sources").fetchone()[0] == before_sources

    resumed = repo.claim(extra_user_id)
    assert resumed["resumed"] is True
    assert resumed["task_id"] == extra_assignment["task_id"]
    assert resumed["lease_token"] == extra_assignment["lease_token"]

    old_detail = repo.completed_detail(fixture["annotator_id"], fixture["legacy_id"])
    new_detail = repo.completed_detail(fixture["annotator_id"], fixture["rich_id"])
    assert old_detail["metadata"]["sources"] == []
    assert any(item.get("scene_code") == "airport" for item in new_detail["metadata"]["sources"])

    shopping_user, _ = make_user("flag-shopping-only")
    with db.db_conn() as conn:
        conn.execute(
            """INSERT INTO annotator_scene_scopes (user_id, mode, revision)
               VALUES (%s, 'restricted', 0)
               ON CONFLICT (user_id) DO UPDATE SET mode = 'restricted', revision = 0""",
            (shopping_user["id"],),
        )
        conn.execute("DELETE FROM annotator_scene_access WHERE user_id = %s", (shopping_user["id"],))
        conn.execute(
            "INSERT INTO annotator_scene_access (user_id, scene_code) VALUES (%s, 'shopping')",
            (shopping_user["id"],),
        )
        conn.commit()
    with pytest.raises(ForbiddenError):
        repo.claim(shopping_user["id"], source_scene="airport")

    with pytest.raises(ForbiddenError, match="Scene review editing is disabled"):
        repo.save_draft(
            extra_user_id, extra_assignment["lease_token"],
            extra_assignment["revision"], [], str(uuid.uuid4()), "flag-review",
            scene_review={"status": "confirmed", "scene_codes": ["airport"]},
        )
    saved = repo.save_draft(
        extra_user_id, extra_assignment["lease_token"],
        extra_assignment["revision"],
        full_segments(extra_assignment, "flag-text"),
        str(uuid.uuid4()), "flag-text",
    )
    assert saved["revision"] >= extra_assignment["revision"]

    with pytest.raises(ForbiddenError, match="Metadata writes are disabled"):
        with db.db_conn() as conn:
            import_one_record(
                conn, raw={"scene": "01_机场", "status": "ready"},
                batch_code="flag-disabled-batch", source_root=tmp_path,
                audio_root=tmp_path,
            )

    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM scene_reviews").fetchone()[0] == before_reviews
        assert conn.execute("SELECT count(*) FROM task_sources").fetchone()[0] == before_sources


def test_old_client_omitting_review_preserves_history(database, seed_tasks, monkeypatch):
    seed_tasks(1)
    user, _ = make_user("omit-review")
    assignment = repo.claim(user["id"])
    completed = repo.complete(
        user["id"], assignment["lease_token"], 0, "annotated", [],
        full_segments(assignment), str(uuid.uuid4()), "with-review",
        scene_review={"status": "confirmed", "scene_codes": ["hotel"]},
    )
    review_id = completed["scene_review"]["id"]
    repo.reopen_completed(user["id"], assignment["task_id"], str(uuid.uuid4()))
    draft = repo.get_assignment(user["id"])
    _disable_new_writes(monkeypatch)
    saved = repo.save_draft(
        user["id"], draft["lease_token"], 0, full_segments(draft, "omit"),
        str(uuid.uuid4()), "omit-save",
    )
    assert "scene_review" in saved
    repo.complete(
        user["id"], draft["lease_token"], saved["revision"], "annotated", [],
        full_segments(draft, "omit"), str(uuid.uuid4()), "omit-complete",
    )
    with db.db_conn() as conn:
        rows = conn.execute(
            """SELECT id, status FROM scene_reviews ORDER BY created_at, review_no"""
        ).fetchall()
        tables = conn.execute(
            "SELECT count(*) FROM scene_reviews"
        ).fetchone()[0]
    assert any(str(row[0]) == str(review_id) and row[1] == "confirmed" for row in rows)
    assert tables >= 1
    detail = repo.completed_detail(user["id"], assignment["task_id"])
    assert detail["segments"][0]["text"].startswith("omit")
