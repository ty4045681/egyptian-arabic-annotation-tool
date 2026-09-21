from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from psycopg.errors import IntegrityError

import db
from annotation_quality.contracts import (
    COMPARISON_VERSION,
    WORD_DIFFERENCE_THRESHOLD_BPS,
    CrossCheckDecision,
    CrossCheckDecisionCommand,
    CrossCheckSettingsUpdateCommand,
    parse_strict,
)
from annotation_repository import ValidationError


MIGRATIONS_THROUGH_007 = (
    (1, "001_initial.sql"),
    (2, "002_admin.sql"),
    (3, "003_scene_provenance.sql"),
    (4, "004_claim_capacity.sql"),
    (5, "005_ten_scene_catalog.sql"),
    (6, "006_session_takeover.sql"),
    (7, "007_annotation_speed_indexes.sql"),
)

PUBLISHED_TEXT = "keep this published text"


def _apply_sql_files(conn, files=MIGRATIONS_THROUGH_007):
    root = Path(__file__).resolve().parents[1] / "migrations"
    for version, filename in files:
        conn.execute((root / filename).read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
            (version, filename.split("_", 1)[1].removesuffix(".sql")),
        )


def _insert_annotator(conn, username):
    user_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO annotators (id, username) VALUES (%s, %s)",
        (user_id, username),
    )
    return user_id


def _insert_task(conn, rel_path, duration, status, allocation_order):
    task_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO annotation_tasks
               (id, rel_path, filename, folder, duration, status, eligible,
                allocation_order)
           VALUES (%s, %s, %s, '', %s, %s, true, %s)""",
        (task_id, rel_path, rel_path, duration, status, allocation_order),
    )
    return task_id


def _insert_version(conn, task_id, version_no, lifecycle, target_status,
                    submitted_by=None, created_by=None, modified_by=None,
                    purpose=None):
    version_id = uuid.uuid4()
    if purpose is None:
        conn.execute(
            """INSERT INTO annotation_versions
                   (id, task_id, version_no, lifecycle, target_status,
                    created_by_user_id, modified_by_user_id,
                    submitted_by_user_id, submitted_at, human_modified)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                       CASE WHEN %s::uuid IS NULL THEN NULL ELSE now() END,
                       %s)""",
            (version_id, task_id, version_no, lifecycle, target_status,
             created_by, modified_by, submitted_by, submitted_by,
             submitted_by is not None),
        )
    else:
        conn.execute(
            """INSERT INTO annotation_versions
                   (id, task_id, version_no, lifecycle, target_status,
                    purpose, created_by_user_id, modified_by_user_id,
                    submitted_by_user_id, submitted_at, human_modified)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                       CASE WHEN %s::uuid IS NULL THEN NULL ELSE now() END,
                       %s)""",
            (version_id, task_id, version_no, lifecycle, target_status, purpose,
             created_by, modified_by, submitted_by, submitted_by,
             submitted_by is not None),
        )
    return version_id


def _insert_segment(conn, version_id, text, asr_text="asr", exclude=False):
    conn.execute(
        """INSERT INTO segments
               (version_id, segment_id, start_s, end_s, duration,
                asr_text, text, exclude_from_training)
           VALUES (%s, 1, 0, 10, 10, %s, %s, %s)""",
        (version_id, asr_text, text, exclude),
    )


def _set_task_pointers(conn, task_id, *, baseline_id, published_id=None,
                       quality=None):
    conn.execute(
        """UPDATE annotation_tasks
           SET baseline_version_id = %s,
               current_published_version_id = %s,
               baseline_quality = %s
           WHERE id = %s""",
        (baseline_id, published_id, quality, task_id),
    )


def _snapshot(conn):
    rows = conn.execute(
        """SELECT rel_path, duration, status, current_published_version_id
           FROM annotation_tasks ORDER BY allocation_order"""
    ).fetchall()
    texts = {
        row[0]: conn.execute(
            """SELECT s.text FROM annotation_tasks t
               JOIN segments s ON s.version_id = t.current_published_version_id
               WHERE t.rel_path = %s ORDER BY s.segment_id""",
            (row[0],),
        ).fetchall()
        for row in rows
        if row[3] is not None
    }
    return {
        "task_count": len(rows),
        "total_duration": sum(float(row[1]) for row in rows),
        "rows": rows,
        "published_texts": texts,
    }


@pytest.fixture
def database_at_007(pg_server, monkeypatch):
    """007 schema plus representative historical rows, before 008."""
    db.close_pool()
    name = "test_pre_cross_check_" + uuid.uuid4().hex
    admin_dsn = pg_server.get_uri(database="postgres")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_server.get_uri(database=name)
    monkeypatch.setenv("ANNOTATION_DB_DSN", dsn)

    with psycopg.connect(dsn) as conn:
        _apply_sql_files(conn)

        alice = _insert_annotator(conn, "alice")
        bob = _insert_annotator(conn, "bob")
        cara = _insert_annotator(conn, "cara")
        dave = _insert_annotator(conn, "dave")
        eve = _insert_annotator(conn, "eve")

        pending = _insert_task(conn, "pending_exact.wav", 11, "pending", 1)
        pending_baseline = _insert_version(conn, pending, 1, "baseline", "pending")
        pending_draft = _insert_version(conn, pending, 2, "draft", "pending")
        _insert_segment(conn, pending_baseline, "", "pending asr")
        _insert_segment(conn, pending_draft, "", "pending asr")
        _set_task_pointers(
            conn, pending, baseline_id=pending_baseline, quality="exact",
        )

        published = _insert_task(conn, "published.wav", 13, "annotated", 2)
        published_baseline = _insert_version(
            conn, published, 1, "baseline", "pending",
        )
        published_version = _insert_version(
            conn, published, 2, "published", "annotated",
            submitted_by=alice, modified_by=alice,
        )
        _insert_segment(conn, published_baseline, "", "published asr")
        _insert_segment(
            conn, published_version, PUBLISHED_TEXT, "published asr", exclude=True,
        )
        _set_task_pointers(
            conn, published, baseline_id=published_baseline,
            published_id=published_version, quality="reconstructed",
        )
        conn.execute(
            """INSERT INTO annotation_events
                   (user_id, task_id, version_id, event_type, to_status)
               VALUES (%s, %s, %s, 'completed', 'annotated')""",
            (alice, published, published_version),
        )

        skipped = _insert_task(conn, "skipped.wav", 17, "skipped", 3)
        skipped_baseline = _insert_version(conn, skipped, 1, "baseline", "pending")
        skipped_version = _insert_version(
            conn, skipped, 2, "published", "skipped", submitted_by=eve,
        )
        _insert_segment(conn, skipped_baseline, "", "skipped asr")
        _insert_segment(conn, skipped_version, "", "skipped asr")
        _set_task_pointers(
            conn, skipped, baseline_id=skipped_baseline,
            published_id=skipped_version, quality="reconstructed",
        )

        revoked = _insert_task(conn, "revoked.wav", 19, "pending", 4)
        revoked_baseline = _insert_version(conn, revoked, 1, "baseline", "pending")
        revoked_version = _insert_version(
            conn, revoked, 2, "revoked", "annotated", submitted_by=alice,
        )
        revoked_draft = _insert_version(conn, revoked, 3, "draft", "pending")
        _insert_segment(conn, revoked_baseline, "", "revoked asr")
        _insert_segment(conn, revoked_version, "revoked human text", "revoked asr")
        _insert_segment(conn, revoked_draft, "", "revoked asr")
        _set_task_pointers(
            conn, revoked, baseline_id=revoked_baseline, quality="reconstructed",
        )

        assigned = _insert_task(conn, "assigned.wav", 23, "pending", 5)
        assigned_baseline = _insert_version(
            conn, assigned, 1, "baseline", "pending",
        )
        assigned_draft = _insert_version(
            conn, assigned, 2, "draft", "pending", created_by=bob, modified_by=bob,
        )
        _insert_segment(conn, assigned_baseline, "", "assigned asr")
        _insert_segment(conn, assigned_draft, "in progress draft", "assigned asr")
        _set_task_pointers(
            conn, assigned, baseline_id=assigned_baseline, quality="exact",
        )
        conn.execute(
            """INSERT INTO assignments
                   (user_id, task_id, working_version_id, mode, lease_token,
                    base_revision)
               VALUES (%s, %s, %s, 'annotation', %s, 0)""",
            (bob, assigned, assigned_draft, uuid.uuid4()),
        )
        conn.execute(
            """INSERT INTO annotation_events
                   (user_id, task_id, version_id, event_type, to_status)
               VALUES (%s, %s, %s, 'claimed', 'pending')""",
            (bob, assigned, assigned_draft),
        )

        revision = _insert_task(conn, "revision.wav", 29, "annotated", 6)
        revision_baseline = _insert_version(
            conn, revision, 1, "baseline", "pending",
        )
        revision_published = _insert_version(
            conn, revision, 2, "published", "annotated", submitted_by=cara,
        )
        revision_draft = _insert_version(
            conn, revision, 3, "draft", "annotated", created_by=cara,
        )
        _insert_segment(conn, revision_baseline, "", "revision asr")
        _insert_segment(conn, revision_published, "cara published", "revision asr")
        _insert_segment(conn, revision_draft, "cara revising", "revision asr")
        _set_task_pointers(
            conn, revision, baseline_id=revision_baseline,
            published_id=revision_published, quality="reconstructed",
        )
        conn.execute(
            """INSERT INTO assignments
                   (user_id, task_id, working_version_id, mode, lease_token,
                    base_revision)
               VALUES (%s, %s, %s, 'revision', %s, 0)""",
            (cara, revision, revision_draft, uuid.uuid4()),
        )
        conn.execute(
            """INSERT INTO annotation_events
                   (user_id, task_id, version_id, event_type, from_status)
               VALUES (%s, %s, %s, 'reopened', 'annotated')""",
            (cara, revision, revision_draft),
        )

        no_author = _insert_task(conn, "no_author.wav", 31, "annotated", 7)
        no_author_baseline = _insert_version(
            conn, no_author, 1, "baseline", "pending",
        )
        no_author_published = _insert_version(
            conn, no_author, 2, "published", "annotated",
        )
        _insert_segment(conn, no_author_baseline, "", "imported asr")
        _insert_segment(conn, no_author_published, "imported text", "imported asr")
        _set_task_pointers(
            conn, no_author, baseline_id=no_author_baseline,
            published_id=no_author_published, quality="reconstructed",
        )
        conn.execute(
            """INSERT INTO annotation_events
                   (user_id, task_id, version_id, event_type, to_status)
               VALUES (NULL, %s, %s, 'imported', 'annotated')""",
            (no_author, no_author_published),
        )
        conn.execute(
            """INSERT INTO annotation_events
                   (user_id, task_id, version_id, event_type, to_status)
               VALUES (%s, %s, %s, 'imported', 'annotated')""",
            (dave, no_author, no_author_published),
        )

        before = _snapshot(conn)
        conn.commit()

    yield {
        "dsn": dsn,
        "alice": alice,
        "bob": bob,
        "cara": cara,
        "dave": dave,
        "eve": eve,
        "published": published,
        "published_version": published_version,
        "assigned": assigned,
        "no_author": no_author,
        "no_author_published": no_author_published,
        "before": before,
    }

    db.close_pool()
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(f'DROP DATABASE "{name}"')


def test_008_upgrades_historical_rows_without_changing_content(database_at_007):
    with psycopg.connect(database_at_007["dsn"]) as conn:
        assert db.apply_migrations(conn) == [8]
        after = _snapshot(conn)
        assert after == database_at_007["before"]
        assert after["task_count"] == 7
        assert after["total_duration"] == 11 + 13 + 17 + 19 + 23 + 29 + 31
        assert after["published_texts"]["published.wav"] == [(PUBLISHED_TEXT,)]
        assert db.apply_migrations(conn) == []
        assert db.applied_versions(conn) == [1, 2, 3, 4, 5, 6, 7, 8]


def test_008_backfills_participants_without_inventing_credited_or_system(
        database_at_007):
    with psycopg.connect(database_at_007["dsn"]) as conn:
        db.apply_migrations(conn)
        participants = {
            (row[0], row[1])
            for row in conn.execute(
                """SELECT t.rel_path, a.username
                   FROM task_annotation_participants p
                   JOIN annotation_tasks t ON t.id = p.task_id
                   JOIN annotators a ON a.id = p.user_id"""
            ).fetchall()
        }
        assert ("published.wav", "alice") in participants
        assert ("assigned.wav", "bob") in participants
        assert ("revision.wav", "cara") in participants
        assert ("skipped.wav", "eve") in participants
        assert ("revoked.wav", "alice") in participants
        assert ("no_author.wav", "dave") not in participants
        assert not any(name == "dave" for _, name in participants)
        no_author_before = conn.execute(
            """SELECT submitted_by_user_id, credited_annotator_id
               FROM annotation_versions WHERE id = %s""",
            (database_at_007["no_author_published"],),
        ).fetchone()
        assert no_author_before == (None, None)

        conn.execute(
            """UPDATE annotation_versions
               SET credited_annotator_id = %s
               WHERE id = %s""",
            (database_at_007["dave"], database_at_007["no_author_published"]),
        )
        conn.commit()
        dave_rows = conn.execute(
            """SELECT count(*) FROM task_annotation_participants
               WHERE user_id = %s""",
            (database_at_007["dave"],),
        ).fetchone()[0]
        assert dave_rows == 0
        no_author_participants = conn.execute(
            """SELECT count(*) FROM task_annotation_participants
               WHERE task_id = %s""",
            (database_at_007["no_author"],),
        ).fetchone()[0]
        assert no_author_participants == 0


def test_008_backfills_credited_annotator_from_submitter(database_at_007):
    with psycopg.connect(database_at_007["dsn"]) as conn:
        db.apply_migrations(conn)
        rows = conn.execute(
            """SELECT credited_annotator_id, submitted_by_user_id
               FROM annotation_versions
               WHERE submitted_by_user_id IS NOT NULL"""
        ).fetchall()
        assert rows
        assert all(row[0] == row[1] for row in rows)
        nulls = conn.execute(
            """SELECT count(*) FROM annotation_versions
               WHERE submitted_by_user_id IS NULL
                 AND credited_annotator_id IS NOT NULL"""
        ).fetchone()[0]
        assert nulls == 0
        published = conn.execute(
            """SELECT credited_annotator_id FROM annotation_versions
               WHERE id = %s""",
            (database_at_007["published_version"],),
        ).fetchone()
        assert published[0] == database_at_007["alice"]


def test_008_inserts_disabled_settings_row(database_at_007):
    with psycopg.connect(database_at_007["dsn"]) as conn:
        db.apply_migrations(conn)
        row = conn.execute(
            """SELECT id, enabled, sampling_rate_bps, revision
               FROM cross_check_settings"""
        ).fetchall()
        assert row == [(1, False, 1000, 0)]


def test_fresh_database_includes_version_8(database):
    with db.db_conn() as conn:
        assert db.applied_versions(conn) == db.expected_versions() == [
            1, 2, 3, 4, 5, 6, 7, 8,
        ]
        assert db.apply_migrations(conn) == []
        settings = conn.execute(
            """SELECT enabled, sampling_rate_bps, revision
               FROM cross_check_settings WHERE id = 1"""
        ).fetchone()
        assert settings == (False, 1000, 0)
        indexes = {
            row[0] for row in conn.execute(
                """SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"""
            ).fetchall()
        }
        assert {
            "uniq_draft_per_task",
            "uniq_published_per_task",
            "uniq_open_cross_check_round_per_task",
            "uniq_active_cross_check_original_version",
            "idx_cross_check_rounds_final_version",
            "idx_cross_check_rounds_state_created",
            "idx_cross_check_rounds_secondary_submitted",
            "idx_cross_check_rounds_task_created",
        }.issubset(indexes)
        lifecycle = " ".join(
            row[0] for row in conn.execute(
                """SELECT pg_get_constraintdef(oid)
                   FROM pg_constraint
                   WHERE conrelid = 'annotation_versions'::regclass
                     AND contype = 'c'"""
            ).fetchall()
        )
        assert "cross_check_submitted" in lifecycle
        mode = " ".join(
            row[0] for row in conn.execute(
                """SELECT pg_get_constraintdef(oid)
                   FROM pg_constraint
                   WHERE conrelid = 'assignments'::regclass
                     AND contype = 'c'"""
            ).fetchall()
        )
        assert "cross_check" in mode


def _round_task(conn, original_user, secondary_user, rel_path,
                secondary_lifecycle="draft"):
    task_id = uuid.uuid4()
    allocation = conn.execute(
        "SELECT nextval('task_allocation_order_seq')"
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO annotation_tasks
               (id, rel_path, filename, folder, duration, status, eligible,
                allocation_order, baseline_quality)
           VALUES (%s, %s, %s, '', 10, 'annotated', true, %s, 'exact')""",
        (task_id, rel_path, rel_path, allocation),
    )
    baseline_id = _insert_version(
        conn, task_id, 1, "baseline", "pending", purpose="annotation",
    )
    original_id = _insert_version(
        conn, task_id, 2, "published", "annotated",
        submitted_by=original_user, purpose="annotation",
    )
    secondary_submitted = (
        secondary_user if secondary_lifecycle != "draft" else None
    )
    secondary_id = _insert_version(
        conn, task_id, 3, secondary_lifecycle, "pending",
        created_by=secondary_user, submitted_by=secondary_submitted,
        purpose="cross_check",
    )
    _set_task_pointers(
        conn, task_id, baseline_id=baseline_id, published_id=original_id,
        quality="exact",
    )
    return {
        "task_id": task_id,
        "baseline_id": baseline_id,
        "original_id": original_id,
        "secondary_id": secondary_id,
    }


def _insert_round(conn, ids, original_user, secondary_user, *,
                  state="in_progress", secondary_id=None, task_id=None,
                  original_id=None, original_user_id=None,
                  secondary_user_id=None, extra=None):
    round_id = uuid.uuid4()
    extra = extra or {}
    conn.execute(
        """INSERT INTO cross_check_rounds (
               id, task_id, revision,
               original_version_id, original_annotator_id,
               secondary_version_id, secondary_annotator_id,
               baseline_version_id, baseline_quality,
               state, settings_revision, sampling_rate_bps, claim_policy,
               submitted_at, compared_at, reason_codes,
               original_word_count, secondary_word_count, edit_distance,
               decision, final_version_id, decided_by_admin_action_id
           ) VALUES (
               %s, %s, 0,
               %s, %s,
               %s, %s,
               %s, 'exact',
               %s, 0, 1000, 'fifo',
               %s, %s, %s,
               %s, %s, %s,
               %s, %s, %s
           )""",
        (
            round_id,
            task_id or ids["task_id"],
            original_id or ids["original_id"],
            original_user_id if original_user_id is not None else original_user,
            secondary_id or ids["secondary_id"],
            secondary_user_id if secondary_user_id is not None else secondary_user,
            extra.get("baseline_version_id") or ids["baseline_id"],
            state,
            extra.get("submitted_at"),
            extra.get("compared_at"),
            extra.get("reason_codes", []),
            extra.get("original_word_count"),
            extra.get("secondary_word_count"),
            extra.get("edit_distance"),
            extra.get("decision"),
            extra.get("final_version_id"),
            extra.get("decided_by_admin_action_id"),
        ),
    )
    return round_id


def _reject(conn, callback):
    with pytest.raises(IntegrityError):
        with conn.transaction():
            callback()


def _insert_assignment(conn, user_id, task_id, version_id, mode,
                       round_id=None):
    conn.execute(
        """INSERT INTO assignments
               (user_id, task_id, working_version_id, mode, lease_token,
                base_revision, cross_check_round_id)
           VALUES (%s, %s, %s, %s, %s, 0, %s)""",
        (user_id, task_id, version_id, mode, uuid.uuid4(), round_id),
    )


def _insert_admin_action(conn):
    action_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO admin_actions
               (id, operation_id, action_type, reason, request_hash, status)
           VALUES (%s, %s, 'cross_check_decision', 'test', %s, 'completed')""",
        (action_id, uuid.uuid4(), "a" * 64),
    )
    return action_id


def test_database_rejects_invalid_cross_check_rows_and_rolls_back(database):
    with db.db_conn() as conn:
        original = _insert_annotator(conn, "cc-original")
        secondary = _insert_annotator(conn, "cc-secondary")
        other = _insert_annotator(conn, "cc-other")
        now = datetime.now(timezone.utc)

        def two_open_rounds():
            ids = _round_task(
                conn, original, secondary, "cc-open.wav",
                secondary_lifecycle="cross_check_submitted",
            )
            _insert_round(
                conn, ids, original, secondary,
                state="awaiting_review",
                extra={
                    "submitted_at": now,
                    "reason_codes": ["word_difference_exceeded"],
                },
            )
            superseded = _insert_version(
                conn, ids["task_id"], 4, "superseded", "annotated",
                submitted_by=original, purpose="annotation",
            )
            new_draft = _insert_version(
                conn, ids["task_id"], 5, "draft", "pending",
                created_by=other, purpose="cross_check",
            )
            _insert_round(
                conn, ids, original, other,
                state="in_progress",
                original_id=superseded,
                secondary_id=new_draft,
                secondary_user_id=other,
            )

        _reject(conn, two_open_rounds)
        assert conn.execute("SELECT count(*) FROM cross_check_rounds").fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM annotation_tasks WHERE rel_path = 'cc-open.wav'"
        ).fetchone()[0] == 0

        def two_assignments_one_user():
            ids = _round_task(conn, original, secondary, "cc-user-a.wav")
            other_ids = _round_task(conn, original, other, "cc-user-b.wav")
            _insert_assignment(
                conn, secondary, ids["task_id"], ids["secondary_id"], "annotation",
            )
            _insert_assignment(
                conn, secondary, other_ids["task_id"], other_ids["secondary_id"],
                "annotation",
            )

        _reject(conn, two_assignments_one_user)

        def two_assignments_one_task():
            ids = _round_task(conn, original, secondary, "cc-task-a.wav")
            other_ids = _round_task(conn, original, other, "cc-task-b.wav")
            _insert_assignment(
                conn, secondary, ids["task_id"], ids["secondary_id"], "annotation",
            )
            _insert_assignment(
                conn, other, ids["task_id"], other_ids["secondary_id"],
                "annotation",
            )

        _reject(conn, two_assignments_one_task)

        def round_version_other_task():
            ids = _round_task(conn, original, secondary, "cc-cross-a.wav")
            other_ids = _round_task(conn, original, other, "cc-cross-b.wav")
            _insert_round(
                conn, ids, original, secondary,
                original_id=other_ids["original_id"],
            )

        _reject(conn, round_version_other_task)

        def same_annotators():
            ids = _round_task(conn, original, secondary, "cc-same.wav")
            _insert_round(
                conn, ids, original, secondary,
                original_user_id=secondary,
                secondary_user_id=secondary,
            )

        _reject(conn, same_annotators)

        def mode_with_round_id():
            ids = _round_task(conn, original, secondary, "cc-mode.wav")
            round_id = _insert_round(conn, ids, original, secondary)
            _insert_assignment(
                conn, secondary, ids["task_id"], ids["secondary_id"],
                "annotation", round_id,
            )

        _reject(conn, mode_with_round_id)

        def cross_check_without_round():
            ids = _round_task(conn, original, secondary, "cc-none.wav")
            _insert_assignment(
                conn, secondary, ids["task_id"], ids["secondary_id"],
                "cross_check", None,
            )

        _reject(conn, cross_check_without_round)

        def assignment_user_mismatch():
            ids = _round_task(conn, original, secondary, "cc-mismatch.wav")
            round_id = _insert_round(conn, ids, original, secondary)
            _insert_assignment(
                conn, other, ids["task_id"], ids["secondary_id"],
                "cross_check", round_id,
            )

        _reject(conn, assignment_user_mismatch)

        def assignment_update_mismatch():
            ids = _round_task(conn, original, secondary, "cc-update.wav")
            round_id = _insert_round(conn, ids, original, secondary)
            _insert_assignment(
                conn, secondary, ids["task_id"], ids["secondary_id"],
                "cross_check", round_id,
            )
            conn.execute(
                "UPDATE assignments SET user_id = %s WHERE user_id = %s",
                (other, secondary),
            )

        _reject(conn, assignment_update_mismatch)

        def assignment_task_mismatch():
            ids = _round_task(conn, original, secondary, "cc-task-mis.wav")
            other_ids = _round_task(conn, original, other, "cc-task-mis-b.wav")
            round_id = _insert_round(conn, ids, original, secondary)
            _insert_assignment(
                conn, secondary, other_ids["task_id"], ids["secondary_id"],
                "cross_check", round_id,
            )

        _reject(conn, assignment_task_mismatch)

        def assignment_version_mismatch():
            ids = _round_task(conn, original, secondary, "cc-ver-mis.wav")
            round_id = _insert_round(conn, ids, original, secondary)
            _insert_assignment(
                conn, secondary, ids["task_id"], ids["original_id"],
                "cross_check", round_id,
            )

        _reject(conn, assignment_version_mismatch)

        def round_update_annotator_mismatch():
            ids = _round_task(conn, original, secondary, "cc-round-upd.wav")
            round_id = _insert_round(conn, ids, original, secondary)
            _insert_assignment(
                conn, secondary, ids["task_id"], ids["secondary_id"],
                "cross_check", round_id,
            )
            conn.execute(
                """UPDATE cross_check_rounds
                   SET secondary_annotator_id = %s WHERE id = %s""",
                (other, round_id),
            )

        _reject(conn, round_update_annotator_mismatch)

        def secondary_version_other_task():
            ids = _round_task(conn, original, secondary, "cc-sec-x.wav")
            other_ids = _round_task(conn, original, other, "cc-sec-y.wav")
            _insert_round(
                conn, ids, original, secondary,
                secondary_id=other_ids["secondary_id"],
            )

        _reject(conn, secondary_version_other_task)

        def baseline_version_other_task():
            ids = _round_task(conn, original, secondary, "cc-base-x.wav")
            other_ids = _round_task(conn, original, other, "cc-base-y.wav")
            _insert_round(
                conn, ids, original, secondary,
                extra={"baseline_version_id": other_ids["baseline_id"]},
            )

        _reject(conn, baseline_version_other_task)

        def final_version_other_task():
            ids = _round_task(conn, original, secondary, "cc-final-x.wav")
            other_ids = _round_task(conn, original, other, "cc-final-y.wav")
            action_id = _insert_admin_action(conn)
            _insert_round(
                conn, ids, original, secondary, state="adjudicated",
                extra={
                    "decision": "original",
                    "final_version_id": other_ids["original_id"],
                    "decided_by_admin_action_id": action_id,
                },
            )

        _reject(conn, final_version_other_task)
        assert conn.execute("SELECT count(*) FROM assignments").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM cross_check_rounds").fetchone()[0] == 0


def test_state_checks_reject_illegal_passed_and_review_rows(database):
    with db.db_conn() as conn:
        original = _insert_annotator(conn, "state-original")
        secondary = _insert_annotator(conn, "state-secondary")
        now = datetime.now(timezone.utc)

        def passed_null_distance():
            ids = _round_task(conn, original, secondary, "state.wav")
            _insert_round(
                conn, ids, original, secondary, state="passed",
                extra={
                    "submitted_at": now,
                    "compared_at": now,
                    "original_word_count": 10,
                    "secondary_word_count": 10,
                    "edit_distance": None,
                    "reason_codes": [],
                },
            )

        _reject(conn, passed_null_distance)

        def passed_with_exception_reason():
            ids = _round_task(conn, original, secondary, "state2.wav")
            _insert_round(
                conn, ids, original, secondary, state="passed",
                extra={
                    "submitted_at": now,
                    "compared_at": now,
                    "original_word_count": 10,
                    "secondary_word_count": 10,
                    "edit_distance": 0,
                    "reason_codes": ["word_difference_exceeded"],
                },
            )

        _reject(conn, passed_with_exception_reason)

        def awaiting_without_reason():
            ids = _round_task(conn, original, secondary, "state3.wav")
            _insert_round(
                conn, ids, original, secondary, state="awaiting_review",
                extra={"submitted_at": now, "reason_codes": []},
            )

        _reject(conn, awaiting_without_reason)

        def adjudicated_without_admin():
            ids = _round_task(conn, original, secondary, "state4.wav")
            _insert_round(
                conn, ids, original, secondary, state="adjudicated",
                extra={"decision": "original"},
            )

        _reject(conn, adjudicated_without_admin)

        def awaiting_without_submitted_at():
            ids = _round_task(conn, original, secondary, "state5.wav")
            _insert_round(
                conn, ids, original, secondary, state="awaiting_review",
                extra={
                    "submitted_at": None,
                    "reason_codes": ["word_difference_exceeded"],
                },
            )

        _reject(conn, awaiting_without_submitted_at)

        passed_ids = _round_task(conn, original, secondary, "legal-passed.wav")
        _insert_round(
            conn, passed_ids, original, secondary, state="passed",
            extra={
                "submitted_at": now,
                "compared_at": now,
                "original_word_count": 10,
                "secondary_word_count": 10,
                "edit_distance": 0,
                "reason_codes": [],
            },
        )
        review_ids = _round_task(conn, original, secondary, "legal-review.wav")
        _insert_round(
            conn, review_ids, original, secondary, state="awaiting_review",
            extra={
                "submitted_at": now,
                "reason_codes": ["word_difference_exceeded"],
            },
        )
        adjudicated_ids = _round_task(
            conn, original, secondary, "legal-adjudicated.wav",
        )
        action_id = _insert_admin_action(conn)
        _insert_round(
            conn, adjudicated_ids, original, secondary, state="adjudicated",
            extra={
                "decision": "original",
                "final_version_id": adjudicated_ids["original_id"],
                "decided_by_admin_action_id": action_id,
            },
        )
        states = {
            row[0] for row in conn.execute(
                "SELECT state FROM cross_check_rounds"
            ).fetchall()
        }
        assert states == {"passed", "awaiting_review", "adjudicated"}


def test_settings_singleton_and_sampling_bounds(database):
    with db.db_conn() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                """INSERT INTO cross_check_settings (id, enabled, sampling_rate_bps, revision)
                   VALUES (2, false, 1000, 0)"""
            )
        conn.rollback()
        with pytest.raises(IntegrityError):
            conn.execute(
                "UPDATE cross_check_settings SET sampling_rate_bps = 10001 WHERE id = 1"
            )
        conn.rollback()
        row = conn.execute(
            "SELECT enabled, sampling_rate_bps FROM cross_check_settings WHERE id = 1"
        ).fetchone()
        assert row == (False, 1000)


def test_contracts_reject_unknown_fields_and_bool_integers():
    operation_id = str(uuid.uuid4())
    with pytest.raises(ValidationError):
        parse_strict(CrossCheckSettingsUpdateCommand, {
            "operation_id": operation_id,
            "expected_revision": 0,
            "enabled": True,
            "sampling_rate_bps": 1000,
            "reason": "ok",
            "unknown": 1,
        })
    with pytest.raises(ValidationError):
        parse_strict(CrossCheckSettingsUpdateCommand, {
            "operation_id": operation_id,
            "expected_revision": False,
            "enabled": True,
            "sampling_rate_bps": 1000,
            "reason": "ok",
        })
    with pytest.raises(ValidationError):
        parse_strict(CrossCheckSettingsUpdateCommand, {
            "operation_id": operation_id,
            "expected_revision": 0,
            "enabled": True,
            "sampling_rate_bps": True,
            "reason": "ok",
        })
    parsed = parse_strict(CrossCheckSettingsUpdateCommand, {
        "operation_id": operation_id,
        "expected_revision": 0,
        "enabled": True,
        "sampling_rate_bps": 1000,
        "reason": "Enable cross-check claims at 10 percent",
    })
    assert parsed.enabled is True
    assert parsed.sampling_rate_bps == 1000
    assert WORD_DIFFERENCE_THRESHOLD_BPS == 1000
    assert COMPARISON_VERSION == "worddiff_v1"

    original_version_id = str(uuid.uuid4())
    secondary_version_id = str(uuid.uuid4())
    accepted = parse_strict(CrossCheckDecisionCommand, {
        "operation_id": operation_id,
        "expected_revision": 1,
        "expected_original_version_id": original_version_id,
        "expected_secondary_version_id": secondary_version_id,
        "decision": "original",
        "reason": "keep original",
    })
    assert accepted.decision == CrossCheckDecision.ORIGINAL
    with pytest.raises(ValidationError):
        parse_strict(CrossCheckDecisionCommand, {
            "operation_id": operation_id,
            "expected_revision": True,
            "expected_original_version_id": original_version_id,
            "expected_secondary_version_id": secondary_version_id,
            "decision": "original",
            "reason": "keep original",
        })
    with pytest.raises(ValidationError):
        parse_strict(CrossCheckDecisionCommand, {
            "operation_id": operation_id,
            "expected_revision": 1,
            "expected_original_version_id": original_version_id,
            "expected_secondary_version_id": secondary_version_id,
            "decision": "original",
            "reason": "keep original",
            "segments": [{"id": 1}],
        })
