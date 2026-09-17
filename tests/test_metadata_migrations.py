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
        assert conn.execute("SELECT count(*) FROM scenes").fetchone()[0] == 10
        assert db.applied_versions(conn) == db.expected_versions()
        assert conn.execute("SELECT count(*) FROM task_sources").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM annotation_tasks").fetchone()[0] == 2


def test_feature_flags_keep_scope_when_fifo(database, seed_tasks, monkeypatch):
    import annotation_repository as repo
    monkeypatch.setenv("ANNOTATION_CLAIM_POLICY", "fifo")
    monkeypatch.setenv("ANNOTATION_SCENE_REVIEW_WRITE", "0")
    monkeypatch.setenv("ANNOTATION_METADATA_UI", "0")
    seed_tasks(1)
    user = repo.login("flag-user", str(uuid.uuid4()), 1800)
    assignment = repo.claim(user["fence"])
    assert assignment["assigned"] is True
    with pytest.raises(repo.ForbiddenError):
        repo.save_draft(
            user["fence"], assignment["lease_token"], 0, [],
            str(uuid.uuid4()), "h",
            scene_review={"status": "confirmed", "scene_codes": ["airport"]},
        )


def test_metadata_export_round_trip(database, seed_tasks, tmp_path):
    from annotation_metadata.export_metadata import export_metadata, verify_metadata_file
    import annotation_repository as repo
    seed_tasks(1)
    user = repo.login("exp", str(uuid.uuid4()), 1800)
    assignment = repo.claim(user["fence"])
    repo.complete(
        user["fence"], assignment["lease_token"], 0, "annotated", [],
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


EXPECTED_SCENE_ORDER = [
    "restaurant", "hotel", "taxi", "airport", "clinic",
    "tourism_information", "emergencies", "spoken_languages",
    "business_negotiation", "shopping",
]
EXPECTED_SCENE_LABELS = [
    "Restaurant", "Hotel", "Taxi", "Airport", "Clinic",
    "Tourism information", "Emergencies", "Spoken languages",
    "Business negotiation", "Shopping",
]


def test_003_file_does_not_include_spoken_languages():
    root = Path(__file__).resolve().parents[1] / "migrations"
    third = (root / "003_scene_provenance.sql").read_text(encoding="utf-8")
    fifth = (root / "005_ten_scene_catalog.sql").read_text(encoding="utf-8")
    assert "spoken_languages" not in third
    assert third.count("INSERT INTO scenes") == 1
    assert "spoken_languages" in fifth
    for name in ("001_initial.sql", "002_admin.sql", "003_scene_provenance.sql",
                 "004_claim_capacity.sql", "006_session_takeover.sql"):
        assert (root / name).is_file()


def test_005_is_idempotent_and_preserves_null_source_rows(database, seed_tasks):
    task = seed_tasks(1)[0]
    sql = (
        Path(__file__).resolve().parents[1] / "migrations" / "005_ten_scene_catalog.sql"
    ).read_text(encoding="utf-8")
    with db.db_conn() as conn:
        batch_id = conn.execute(
            "INSERT INTO source_batches(batch_code, name) VALUES (%s, %s) RETURNING id",
            ("preserve-null-005", "preserve-null-005"),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO task_sources(
                   task_id, batch_id, record_key, scene_code, confidence,
                   confidence_basis, content_digest, raw_record)
               VALUES (%s, %s, 'preserve-null', NULL, 'unknown', 'none', %s,
                       '{"keep":"raw"}'::jsonb)""",
            (task, batch_id, "d" * 64),
        )
        conn.execute(
            """INSERT INTO task_scene_predictions(
                   task_id, input_digest, model_name, predicted_label, predicted_scene_code)
               VALUES (%s, 'preserve-pred', 'fixture-model', 'Spoken languages', NULL)""",
            (task,),
        )
        before = conn.execute(
            """SELECT scene_code, raw_record, content_digest
               FROM task_sources WHERE task_id = %s""",
            (task,),
        ).fetchone()
        pred_before = conn.execute(
            "SELECT predicted_scene_code, predicted_label FROM task_scene_predictions WHERE task_id = %s",
            (task,),
        ).fetchone()
        conn.execute(sql)
        conn.execute(sql)
        rows = conn.execute(
            "SELECT code, label_en, sort_order FROM scenes ORDER BY sort_order, code"
        ).fetchall()
        assert [row[0] for row in rows] == EXPECTED_SCENE_ORDER
        assert [row[1] for row in rows] == EXPECTED_SCENE_LABELS
        assert [row[2] for row in rows] == list(range(1, 11))
        after = conn.execute(
            """SELECT scene_code, raw_record, content_digest
               FROM task_sources WHERE task_id = %s""",
            (task,),
        ).fetchone()
        pred_after = conn.execute(
            "SELECT predicted_scene_code, predicted_label FROM task_scene_predictions WHERE task_id = %s",
            (task,),
        ).fetchone()
        assert after[0] is None
        assert after[1] == {"keep": "raw"}
        assert after[2] == "d" * 64
        assert pred_after == pred_before == (None, "Spoken languages")
        assert db.applied_versions(conn) == db.expected_versions() == [1, 2, 3, 4, 5, 6]


def test_005_applies_after_004_without_rewriting_prior_migrations(
        pg_server, monkeypatch):
    import db

    db.close_pool()
    name = "test_005_after_004_" + uuid.uuid4().hex
    admin_dsn = pg_server.get_uri(database="postgres")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_server.get_uri(database=name)
    monkeypatch.setenv("ANNOTATION_DB_DSN", dsn)
    root = Path(__file__).resolve().parents[1] / "migrations"
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
                " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            for version, filename in (
                (1, "001_initial.sql"),
                (2, "002_admin.sql"),
                (3, "003_scene_provenance.sql"),
                (4, "004_claim_capacity.sql"),
            ):
                conn.execute((root / filename).read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                    (version, filename.split("_", 1)[1].removesuffix(".sql")),
                )
            conn.commit()
            assert conn.execute("SELECT count(*) FROM scenes").fetchone()[0] == 9
            assert "spoken_languages" not in {
                row[0] for row in conn.execute("SELECT code FROM scenes").fetchall()
            }
            newly = db.apply_migrations(conn)
            assert newly == [5, 6]
            rows = conn.execute(
                "SELECT code, label_en FROM scenes ORDER BY sort_order, code"
            ).fetchall()
            assert [row[0] for row in rows] == EXPECTED_SCENE_ORDER
            assert [row[1] for row in rows] == EXPECTED_SCENE_LABELS
            assert db.apply_migrations(conn) == []
    finally:
        db.close_pool()
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            conn.execute(f'DROP DATABASE "{name}"')


def test_006_adds_session_columns_and_old_insert_shape_still_works(database):
    import annotation_repository as repo

    with db.db_conn() as conn:
        assert db.applied_versions(conn) == [1, 2, 3, 4, 5, 6]
        columns = {
            row[0]
            for row in conn.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_name = 'active_sessions'"""
            ).fetchall()
        }
        assert {
            "generation", "last_activity_at", "absolute_expires_at", "expires_at",
        }.issubset(columns)
        checks = {
            row[0]
            for row in conn.execute(
                """SELECT conname FROM pg_constraint
                   WHERE conrelid = 'active_sessions'::regclass"""
            ).fetchall()
        }
        assert "active_sessions_generation_positive" in checks
        # Same-schema rollback: old code inserts without the new columns.
        with conn.cursor() as cur:
            user = repo.ensure_user(cur, "legacy-insert")
        conn.execute("DELETE FROM active_sessions WHERE user_id = %s", (user["id"],))
        conn.execute(
            """INSERT INTO active_sessions
                   (user_id, session_id, login_time, last_seen_at, expires_at)
               VALUES (%s, %s, now(), now(), now() + interval '30 minutes')""",
            (user["id"], uuid.uuid4()),
        )
        row = conn.execute(
            """SELECT generation, last_activity_at, absolute_expires_at
               FROM active_sessions WHERE user_id = %s""",
            (user["id"],),
        ).fetchone()
        assert int(row[0]) == 1
        assert row[1] is not None
        assert row[2] is not None
        conn.commit()
