"""Genuine metadata export/import round-trip, mapping, and rollback tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

import annotation_repository as repo
import db
from annotation_metadata.export_metadata import (
    IDENTITY_HELP, MetadataImportError, comparable_document, export_metadata,
    import_metadata, verify_metadata_file,
)
from annotation_metadata.postgres_backup import provenance_counts
from tests.provenance_fixtures import (
    build_rich_provenance, clone_annotation_skeleton, wipe_metadata,
)
from tests.test_repository import full_segments, make_user


def _load_export(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _histories(document: dict) -> dict:
    tasks = {task["task_id"]: task for task in document["tasks"]}
    return {
        "sources": {
            task_id: [
                {
                    "id": item["id"], "record_key": item["record_key"],
                    "revision": item["revision"], "is_current": item["is_current"],
                    "scene_code": item["scene_code"], "confidence": item["confidence"],
                    "confidence_basis": item["confidence_basis"],
                    "source_url": item.get("source_url"),
                    "video_id": item.get("video_id"),
                    "raw_record": item.get("raw_record"),
                    "batch_code": item.get("batch_code"),
                    "content_digest": item.get("content_digest"),
                }
                for item in task.get("source_history") or []
            ]
            for task_id, task in tasks.items()
        },
        "predictions": {
            task_id: [
                {
                    "id": item["id"], "model_name": item["model_name"],
                    "prompt_version": item.get("prompt_version"),
                    "score": item.get("score"),
                    "input_version_id": item.get("input_version_id"),
                    "input_revision": item.get("input_revision"),
                    "input_digest": item.get("input_digest"),
                    "predicted_label": item.get("predicted_label"),
                }
                for item in task.get("predictions") or []
            ]
            for task_id, task in tasks.items()
        },
        "reviews": {
            task_id: [
                {
                    "id": item["id"], "version_id": item["version_id"],
                    "review_no": item["review_no"], "status": item["status"],
                    "note": item.get("note"),
                    "scene_codes": item.get("scene_codes"),
                    "actor_kind": item.get("actor_kind"),
                    "previous_review_id": item.get("previous_review_id"),
                    "superseded": item.get("superseded"),
                }
                for item in task.get("reviews") or []
            ]
            for task_id, task in tasks.items()
        },
        "identities": document.get("media_identities"),
        "import_runs": [
            {key: item[key] for key in ("id", "batch_code", "snapshot_sha256", "status")}
            for item in document.get("import_runs") or []
        ],
        "aliases": {
            task["task_id"]: task.get("path_aliases") for task in document["tasks"]
        },
        "publication_reviews": [
            event.get("publication_review_id")
            for event in document.get("audit_events") or []
            if event.get("publication_review_id")
        ],
        "scopes": [
            {
                "user_id": item["user_id"], "mode": item["mode"],
                "scene_codes": item.get("scene_codes"),
            }
            for item in document.get("scopes") or []
        ],
    }


def test_export_metadata_help_documents_identity_rules():
    result = subprocess.run(
        [sys.executable, "manage_state.py", "export-metadata", "-h"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, check=True,
    )
    assert "exact UUID" in result.stdout
    assert "Usernames" in result.stdout
    assert "--dry-run" in subprocess.run(
        [sys.executable, "manage_state.py", "import-metadata", "-h"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "mapping.tasks" in IDENTITY_HELP


def test_metadata_round_trip_exact_ids_and_idempotent(database, seed_tasks, tmp_path):
    fixture = build_rich_provenance(seed_tasks)
    with db.db_conn() as conn:
        first = export_metadata(conn, tmp_path / "first")
        original = _load_export(first["path"])
        text_before = conn.execute(
            """SELECT s.text FROM segments s
               JOIN annotation_versions v ON v.id = s.version_id
               JOIN annotation_tasks t ON t.current_published_version_id = v.id
               WHERE t.id = %s ORDER BY s.segment_id""",
            (fixture["rich_id"],),
        ).fetchall()
        status_before = conn.execute(
            "SELECT status FROM annotation_tasks WHERE id = %s",
            (fixture["rich_id"],),
        ).fetchone()[0]
        counts_before = provenance_counts(conn)
        wipe_metadata(conn)
        dry = import_metadata(conn, first["path"], dry_run=True)
        assert dry["dry_run"] is True
        assert conn.execute("SELECT count(*) FROM task_sources").fetchone()[0] == 0
        applied = import_metadata(conn, first["path"])
        assert applied["applied"] is True
        repeat = import_metadata(conn, first["path"])
        assert repeat["inserted"]["sources"] == 0
        second = export_metadata(conn, tmp_path / "second")
        restored = _load_export(second["path"])
        text_after = conn.execute(
            """SELECT s.text FROM segments s
               JOIN annotation_versions v ON v.id = s.version_id
               JOIN annotation_tasks t ON t.current_published_version_id = v.id
               WHERE t.id = %s ORDER BY s.segment_id""",
            (fixture["rich_id"],),
        ).fetchall()
        status_after = conn.execute(
            "SELECT status FROM annotation_tasks WHERE id = %s",
            (fixture["rich_id"],),
        ).fetchone()[0]
    assert text_before == text_after
    assert status_before == status_after == "annotated"
    assert _histories(original) == _histories(restored)
    assert comparable_document(original)["tasks"]
    rich = next(task for task in restored["tasks"] if task["task_id"] == fixture["rich_id"])
    history = rich["source_history"]
    assert len(history) >= 3
    revisions = [
        item for item in history if item["record_key"] == "youtube:vid-rich-1:airport"
    ]
    assert [item["revision"] for item in revisions] == [1, 2]
    assert revisions[0]["confidence"] == "low"
    assert revisions[1]["confidence"] == "high"
    assert revisions[0]["raw_record"]["url"] == "https://example.com/airport-v1"
    assert {item["scene_code"] for item in history if item["is_current"]} == {
        "airport", "shopping",
    }
    assert len(rich["predictions"]) == 2
    scores = [item["score"] for item in rich["predictions"]]
    assert None in scores and 0.42 in scores
    assert all(item.get("input_digest") for item in rich["predictions"])
    assert all(item.get("model_name") for item in rich["predictions"])
    statuses = [item["status"] for item in rich["reviews"]]
    assert "confirmed" in statuses and "mixed" in statuses and "uncertain" in statuses
    assert fixture["publication_review_id"] in {
        event.get("publication_review_id") for event in restored["audit_events"]
    }
    assert "alias/folder/rich.wav" in rich["path_aliases"]
    scoped = next(
        item for item in restored["scopes"] if item["user_id"] == fixture["scoped_id"]
    )
    assert scoped["mode"] == "restricted"
    assert scoped["scene_codes"] == ["shopping"]
    assert counts_before["sources"] == provenance_counts_from_doc(restored)
    verified = verify_metadata_file(Path(first["path"]))
    assert verified["ok"]
    assert verified["tasks"] == 3


def provenance_counts_from_doc(document: dict) -> int:
    return sum(len(task.get("source_history") or []) for task in document["tasks"])


def test_metadata_restore_into_cloned_dataset(database, seed_tasks, pg_server, tmp_path):
    build_rich_provenance(seed_tasks)
    with db.db_conn() as conn:
        exported = export_metadata(conn, tmp_path / "src")
        original = _load_export(exported["path"])
        src_counts = provenance_counts(conn)
    name = "skel_" + uuid.uuid4().hex
    admin = pg_server.get_uri(database="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dest_dsn = pg_server.get_uri(database=name)
    try:
        with psycopg.connect(dest_dsn) as conn:
            assert db.apply_migrations(conn) == db.expected_versions()
        clone_annotation_skeleton(database, dest_dsn)
        with psycopg.connect(dest_dsn) as conn:
            result = import_metadata(conn, exported["path"])
            assert result["applied"]
            restored_export = export_metadata(conn, tmp_path / "dest")
            restored = _load_export(restored_export["path"])
            dest_counts = provenance_counts(conn)
            relations_ok = dest_counts["sources"] == src_counts["sources"]
        assert relations_ok
        assert _histories(original) == _histories(restored)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            conn.execute(f'DROP DATABASE "{name}"')


def test_explicit_mapping_and_conflict_rollback(database, seed_tasks, pg_server, tmp_path):
    fixture = build_rich_provenance(seed_tasks)
    with db.db_conn() as conn:
        exported = export_metadata(conn, tmp_path / "src")
        original = _load_export(exported["path"])
        src_tasks = conn.execute("SELECT id FROM annotation_tasks").fetchall()
        src_versions = conn.execute("SELECT id FROM annotation_versions").fetchall()
        src_users = conn.execute("SELECT id FROM annotators").fetchall()
    id_map = {
        "tasks": {str(row[0]): str(uuid.uuid4()) for row in src_tasks},
        "versions": {str(row[0]): str(uuid.uuid4()) for row in src_versions},
        "users": {str(row[0]): str(uuid.uuid4()) for row in src_users},
    }
    name = "map_" + uuid.uuid4().hex
    admin = pg_server.get_uri(database="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dest_dsn = pg_server.get_uri(database=name)
    try:
        with psycopg.connect(dest_dsn) as conn:
            assert db.apply_migrations(conn) == db.expected_versions()
        clone_annotation_skeleton(database, dest_dsn, id_map=id_map)
        mapping_path = tmp_path / "mapping.json"
        mapping_path.write_text(json.dumps(id_map), encoding="utf-8")
        with psycopg.connect(dest_dsn) as conn:
            imported = import_metadata(conn, exported["path"], mapping_path=mapping_path)
            assert imported["applied"]
            restored = export_metadata(conn, tmp_path / "mapped")
            mapped_doc = _load_export(restored["path"])
            dest_rich = id_map["tasks"][fixture["rich_id"]]
            rich = next(task for task in mapped_doc["tasks"] if task["task_id"] == dest_rich)
            assert len(rich["source_history"]) >= 3
            assert len(rich["predictions"]) == 2
            assert any(item["status"] == "mixed" for item in rich["reviews"])
            assert rich["path_aliases"] == ["alias/folder/rich.wav"]
            with pytest.raises(MetadataImportError, match="username|pathname"):
                import_metadata(
                    conn, exported["path"],
                    mapping_path=_write(tmp_path / "bad-user.json", {"usernames": {"a": "b"}}),
                )
            before = conn.execute("SELECT count(*) FROM task_sources").fetchone()[0]
            bad_map = tmp_path / "dangling.json"
            bad_map.write_text(json.dumps({
                "tasks": {fixture["rich_id"]: str(uuid.uuid4())},
            }), encoding="utf-8")
            with pytest.raises(MetadataImportError, match="dangling"):
                import_metadata(conn, exported["path"], mapping_path=bad_map)
            after = conn.execute("SELECT count(*) FROM task_sources").fetchone()[0]
            assert after == before

        with psycopg.connect(database) as conn:
            wipe_metadata(conn)
            rich_task = next(
                task for task in original["tasks"] if task["task_id"] == fixture["rich_id"]
            )
            source_id = rich_task["source_history"][0]["id"]
            conn.execute(
                """INSERT INTO source_batches (batch_code, name)
                   VALUES ('conflict-batch-zzzz', 'conflict')"""
            )
            batch_id = conn.execute(
                "SELECT id FROM source_batches WHERE batch_code = 'conflict-batch-zzzz'"
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO task_sources
                       (id, task_id, batch_id, record_key, revision, is_current,
                        confidence, content_digest)
                   VALUES (%s, %s, %s, 'other', 1, true, 'low', %s)""",
                (source_id, fixture["legacy_id"], batch_id, "ee" * 32),
            )
            conn.commit()
            with pytest.raises(MetadataImportError, match="conflict"):
                import_metadata(conn, exported["path"])
            assert conn.execute("SELECT count(*) FROM scene_reviews").fetchone()[0] == 0
            assert conn.execute(
                "SELECT count(*) FROM task_sources WHERE content_digest = %s",
                ("ee" * 32,),
            ).fetchone()[0] == 1
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            conn.execute(f'DROP DATABASE "{name}"')


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_unknown_format_and_filter_context(database, seed_tasks, tmp_path):
    build_rich_provenance(seed_tasks)
    bogus = tmp_path / "bad.json"
    bogus.write_text(json.dumps({
        "format": "not-metadata", "schema_version": 1, "tasks": [], "scopes": [],
    }), encoding="utf-8")
    with pytest.raises(MetadataImportError, match="unknown metadata format"):
        verify_metadata_file(bogus)
    from annotation_metadata.contracts import TaskFilter
    with db.db_conn() as conn:
        result = export_metadata(
            conn, tmp_path / "filtered",
            filters=TaskFilter(source_scene="airport"),
        )
        document = _load_export(result["path"])
    assert document["applied_filters"]["mode"] == "task_filter"
    assert document["applied_filters"]["task_filter"]["source_scene"] == "airport"
    task_ids = {task["task_id"] for task in document["tasks"]}
    assert len(task_ids) == 1
    rich = document["tasks"][0]
    assert any(item.get("scene_code") == "airport" for item in rich["sources"])


def test_cli_export_import_round_trip(database, seed_tasks, tmp_path, monkeypatch):
    build_rich_provenance(seed_tasks)
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    out = tmp_path / "cli-meta"
    subprocess.run(
        [sys.executable, "manage_state.py", "export-metadata", "--output", str(out)],
        check=True, env={**os.environ, "ANNOTATION_DB_DSN": database},
    )
    verify = subprocess.run(
        [sys.executable, "manage_state.py", "verify-metadata", "--input", str(out)],
        check=True, capture_output=True, text=True,
        env={**os.environ, "ANNOTATION_DB_DSN": database},
    )
    assert '"ok": true' in verify.stdout
    with db.db_conn() as conn:
        wipe_metadata(conn)
    subprocess.run(
        [sys.executable, "manage_state.py", "import-metadata", "--input", str(out),
         "--dry-run"],
        check=True, env={**os.environ, "ANNOTATION_DB_DSN": database},
    )
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM task_sources").fetchone()[0] == 0
    subprocess.run(
        [sys.executable, "manage_state.py", "import-metadata", "--input", str(out)],
        check=True, env={**os.environ, "ANNOTATION_DB_DSN": database},
    )
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM task_sources").fetchone()[0] >= 3


def test_legacy_export_json_still_round_trips(database, seed_tasks, tmp_path):
    from manage_state import export_json
    seed_tasks(1)
    user, _ = make_user("json-compat")
    assignment = repo.claim(user["id"])
    repo.complete(
        user["id"], assignment["lease_token"], 0, "annotated", [],
        full_segments(assignment), str(uuid.uuid4()), "json-complete",
        scene_review={"status": "confirmed", "scene_codes": ["hotel"]},
    )
    out = tmp_path / "legacy-json"
    result = export_json(out)
    assert result["count"] == 1
    payload = json.loads((out / "audio-000.json").read_text())
    assert "segments" in payload
    assert "audio" in payload
    assert "metadata" not in payload
    assert payload["status"] == "annotated"
