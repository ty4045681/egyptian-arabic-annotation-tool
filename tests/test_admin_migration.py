from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest


@pytest.fixture
def database_before_admin(pg_server, monkeypatch):
    """A real database with 001 applied and representative legacy rows."""
    import db

    db.close_pool()
    name = "test_pre_admin_" + uuid.uuid4().hex
    admin_dsn = pg_server.get_uri(database="postgres")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_server.get_uri(database=name)
    monkeypatch.setenv("ANNOTATION_DB_DSN", dsn)

    with psycopg.connect(dsn) as conn:
        migration = Path(__file__).parents[1] / "migrations" / "001_initial.sql"
        conn.execute(migration.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (1, 'initial')"
        )

        user_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO annotators (id, username) VALUES (%s, 'legacy-user')",
            (user_id,),
        )

        pending_task = uuid.uuid4()
        pending_draft = uuid.uuid4()
        conn.execute(
            """INSERT INTO annotation_tasks
                   (id, rel_path, filename, folder, duration, status, eligible,
                    allocation_order)
               VALUES (%s, 'pending.wav', 'pending.wav', '', 10, 'pending',
                       true, 1)""",
            (pending_task,),
        )
        conn.execute(
            """INSERT INTO annotation_versions
                   (id, task_id, version_no, lifecycle, target_status,
                    human_modified)
               VALUES (%s, %s, 1, 'draft', 'pending', false)""",
            (pending_draft, pending_task),
        )
        conn.execute(
            """INSERT INTO segments
                   (version_id, segment_id, start_s, end_s, duration,
                    asr_text, text, exclude_from_training)
               VALUES (%s, 1, 0, 10, 10, 'pending asr', '', false)""",
            (pending_draft,),
        )

        published_task = uuid.uuid4()
        published_version = uuid.uuid4()
        conn.execute(
            """INSERT INTO annotation_tasks
                   (id, rel_path, filename, folder, duration, status, eligible,
                    allocation_order)
               VALUES (%s, 'published.wav', 'published.wav', '', 10,
                       'annotated', true, 2)""",
            (published_task,),
        )
        conn.execute(
            """INSERT INTO annotation_versions
                   (id, task_id, version_no, lifecycle, target_status,
                    human_modified, modified_by_user_id, submitted_by_user_id,
                    submitted_at, skip_reasons)
               VALUES (%s, %s, 1, 'published', 'annotated', true, %s, %s,
                       now(), ARRAY['noisy'])""",
            (published_version, published_task, user_id, user_id),
        )
        conn.execute(
            """INSERT INTO segments
                   (version_id, segment_id, start_s, end_s, duration,
                    asr_text, text, exclude_from_training)
               VALUES (%s, 1, 0, 10, 10, 'published asr',
                       'legacy human secret', true)""",
            (published_version,),
        )
        conn.execute(
            """UPDATE annotation_tasks SET current_published_version_id = %s
               WHERE id = %s""",
            (published_version, published_task),
        )
        conn.commit()

    yield {
        "dsn": dsn,
        "pending_task": pending_task,
        "pending_draft": pending_draft,
        "published_task": published_task,
        "published_version": published_version,
    }

    db.close_pool()
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(f'DROP DATABASE "{name}"')


def test_admin_migration_schema_and_indexes(database):
    import db

    with db.db_conn() as conn:
        assert db.applied_versions(conn) == db.expected_versions()
        assert db.apply_migrations(conn) == []

        columns = {
            (table, column)
            for table, column in conn.execute(
                """SELECT table_name, column_name
                   FROM information_schema.columns
                   WHERE table_schema = 'public'
                     AND table_name IN (
                       'annotators', 'annotation_tasks', 'annotation_versions',
                       'annotation_events', 'admin_sessions', 'admin_actions',
                       'admin_action_items', 'task_annotator_blocks'
                     )"""
            ).fetchall()
        }
        for expected in {
            ("annotators", "status"),
            ("annotators", "deactivated_at"),
            ("annotators", "deactivated_reason"),
            ("annotation_tasks", "baseline_version_id"),
            ("annotation_tasks", "baseline_quality"),
            ("annotation_versions", "revoked_at"),
            ("annotation_versions", "revoked_reason"),
            ("annotation_versions", "revoked_by_admin_action_id"),
            ("annotation_events", "admin_action_id"),
            ("admin_sessions", "token_digest"),
            ("admin_sessions", "csrf_digest"),
            ("admin_actions", "operation_id"),
            ("admin_action_items", "admin_action_id"),
            ("task_annotator_blocks", "task_id"),
            ("task_annotator_blocks", "user_id"),
        }:
            assert expected in columns

        tables = {
            row[0] for row in conn.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema = 'public'"""
            ).fetchall()
        }
        assert {
            "admin_sessions", "admin_actions", "admin_action_items",
            "task_annotator_blocks",
        }.issubset(tables)

        lifecycle_constraint = " ".join(
            row[0] for row in conn.execute(
                """SELECT pg_get_constraintdef(oid)
                   FROM pg_constraint
                   WHERE conrelid = 'annotation_versions'::regclass
                     AND contype = 'c'"""
            ).fetchall()
        )
        assert "baseline" in lifecycle_constraint
        assert "revoked" in lifecycle_constraint

        indexes = {
            row[0] for row in conn.execute(
                """SELECT indexname FROM pg_indexes
                   WHERE schemaname = 'public'"""
            ).fetchall()
        }
        assert {
            "uniq_baseline_per_task",
            "idx_admin_sessions_token",
            "idx_admin_actions_created",
            "idx_admin_action_items_task",
            "idx_task_annotator_blocks_user",
        }.issubset(indexes)


def test_002_backfills_exact_and_reconstructed_baselines(database_before_admin):
    import db

    with psycopg.connect(database_before_admin["dsn"]) as conn:
        assert db.apply_migrations(conn) == [2, 3, 4, 5, 6, 7, 8]
        tasks = {
            row[0]: row[1:]
            for row in conn.execute(
                """SELECT rel_path, baseline_version_id, baseline_quality,
                          current_published_version_id, status
                   FROM annotation_tasks ORDER BY allocation_order"""
            ).fetchall()
        }

        pending_baseline = tasks["pending.wav"][0]
        assert tasks["pending.wav"][1:] == ("exact", None, "pending")
        assert pending_baseline != database_before_admin["pending_draft"]
        baseline_segment = conn.execute(
            """SELECT lifecycle, asr_text, text, exclude_from_training
               FROM annotation_versions v
               JOIN segments s ON s.version_id = v.id
               WHERE v.id = %s""",
            (pending_baseline,),
        ).fetchone()
        draft_segment = conn.execute(
            """SELECT lifecycle, asr_text, text, exclude_from_training
               FROM annotation_versions v
               JOIN segments s ON s.version_id = v.id
               WHERE v.id = %s""",
            (database_before_admin["pending_draft"],),
        ).fetchone()
        assert baseline_segment == ("baseline", "pending asr", "", False)
        assert draft_segment == ("draft", "pending asr", "", False)

        published_baseline = tasks["published.wav"][0]
        assert tasks["published.wav"][1] == "reconstructed"
        assert tasks["published.wav"][2] == database_before_admin["published_version"]
        assert tasks["published.wav"][3] == "annotated"
        reconstructed = conn.execute(
            """SELECT v.lifecycle, v.skip_reasons, s.asr_text, s.text,
                      s.exclude_from_training
               FROM annotation_versions v
               JOIN segments s ON s.version_id = v.id
               WHERE v.id = %s""",
            (published_baseline,),
        ).fetchone()
        current = conn.execute(
            """SELECT v.lifecycle, s.text, s.exclude_from_training
               FROM annotation_versions v
               JOIN segments s ON s.version_id = v.id
               WHERE v.id = %s""",
            (database_before_admin["published_version"],),
        ).fetchone()
        assert reconstructed == (
            "baseline", [], "published asr", "", False,
        )
        assert current == ("published", "legacy human secret", True)
        assert db.apply_migrations(conn) == []


def test_new_preprocessed_task_has_separate_exact_immutable_baseline(
        database, seed_tasks):
    import db

    task_id = seed_tasks(1)[0]
    with db.db_conn() as conn:
        task = conn.execute(
            """SELECT baseline_version_id, baseline_quality
               FROM annotation_tasks WHERE id = %s""",
            (task_id,),
        ).fetchone()
        versions = conn.execute(
            """SELECT id, lifecycle, version_no, human_modified, skip_reasons
               FROM annotation_versions WHERE task_id = %s
               ORDER BY version_no""",
            (task_id,),
        ).fetchall()
        segment_sets = {
            str(version_id): conn.execute(
                """SELECT segment_id, start_s, end_s, duration, asr_text, text,
                          exclude_from_training
                   FROM segments WHERE version_id = %s ORDER BY segment_id""",
                (version_id,),
            ).fetchall()
            for version_id, *_ in versions
        }

    assert task[1] == "exact"
    baseline = next(row for row in versions if row[1] == "baseline")
    draft = next(row for row in versions if row[1] == "draft")
    assert baseline[0] == task[0]
    assert baseline[0] != draft[0]
    assert baseline[3] is False and draft[3] is False
    assert baseline[4] == draft[4] == []
    assert segment_sets[str(baseline[0])] == segment_sets[str(draft[0])]
    assert all(
        segment[5] == "" and segment[6] is False
        for segment in segment_sets[str(baseline[0])]
    )


def test_database_rejects_duplicate_baseline_and_plaintext_admin_token(
        database, seed_tasks):
    """The uniqueness constraint is structural; secrets are digest-sized values."""
    import db
    import psycopg

    task_id = seed_tasks(1)[0]
    with db.db_conn() as conn:
        next_no = conn.execute(
            """SELECT max(version_no) + 1 FROM annotation_versions
               WHERE task_id = %s""",
            (task_id,),
        ).fetchone()[0]
        try:
            conn.execute(
                """INSERT INTO annotation_versions
                       (id, task_id, version_no, lifecycle, target_status)
                   VALUES (%s, %s, %s, 'baseline', 'pending')""",
                (uuid.uuid4(), task_id, next_no),
            )
        except psycopg.errors.UniqueViolation:
            conn.rollback()
        else:  # pragma: no cover - makes a missing partial unique index explicit
            conn.rollback()
            raise AssertionError("database accepted a second baseline for one task")

        # Digests are fixed hexadecimal SHA-256 values; raw session tokens cannot
        # accidentally be inserted by a future caller.
        try:
            conn.execute(
                """INSERT INTO admin_sessions
                       (key_id, token_digest, csrf_digest, idle_expires_at,
                        absolute_expires_at)
                   VALUES ('primary', 'plaintext-token', 'plaintext-csrf',
                           now() + interval '1 hour', now() + interval '8 hours')"""
            )
        except psycopg.errors.CheckViolation:
            conn.rollback()
        else:  # pragma: no cover
            conn.rollback()
            raise AssertionError("database accepted non-digest admin credentials")
