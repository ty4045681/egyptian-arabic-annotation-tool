"""Actual pg_dump/pg_restore into a disposable database."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

import db
from annotation_metadata.postgres_backup import (
    dump_database, provenance_counts, provenance_relationships, restore_database,
    running_postgres_bindir,
)
from tests.provenance_fixtures import build_rich_provenance


def test_pg_dump_restore_preserves_provenance_graph(database, seed_tasks, pg_server, tmp_path):
    build_rich_provenance(seed_tasks)
    with db.db_conn() as conn:
        before = provenance_counts(conn)
        before_rel = provenance_relationships(conn)
        source_pairs = conn.execute(
            """SELECT s.id, s.task_id, s.revision, s.is_current FROM task_sources s
               ORDER BY s.task_id, s.record_key, s.revision"""
        ).fetchall()
        review_pairs = conn.execute(
            """SELECT r.id, r.version_id, r.review_no, r.status
               FROM scene_reviews r ORDER BY r.version_id, r.review_no"""
        ).fetchall()
        assignment_pairs = conn.execute(
            """SELECT user_id, task_id FROM assignments ORDER BY user_id"""
        ).fetchall()
    assert before_rel["ok"]
    assert before["sources"] >= 3
    assert before["reviews"] >= 3
    assert before["assignments"] >= 1

    dump_path = tmp_path / "annotation_tool.dump"
    dumped = dump_database(database, dump_path)
    assert dump_path.is_file() and dump_path.stat().st_size > 0
    bindir = running_postgres_bindir()
    assert dumped["bindir"] == str(bindir)

    name = "restore_" + uuid.uuid4().hex
    admin = pg_server.get_uri(database="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dest = pg_server.get_uri(database=name)
    try:
        restore_database(dump_path, dest)
        with psycopg.connect(dest) as conn:
            after = provenance_counts(conn)
            after_rel = provenance_relationships(conn)
            source_after = conn.execute(
                """SELECT s.id, s.task_id, s.revision, s.is_current FROM task_sources s
                   ORDER BY s.task_id, s.record_key, s.revision"""
            ).fetchall()
            review_after = conn.execute(
                """SELECT r.id, r.version_id, r.review_no, r.status
                   FROM scene_reviews r ORDER BY r.version_id, r.review_no"""
            ).fetchall()
            assignment_after = conn.execute(
                """SELECT user_id, task_id FROM assignments ORDER BY user_id"""
            ).fetchall()
            schema = db.applied_versions(conn)
        assert schema == db.expected_versions()
        assert after == before
        assert after_rel == before_rel
        assert source_after == source_pairs
        assert review_after == review_pairs
        assert assignment_after == assignment_pairs
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            conn.execute(f'DROP DATABASE "{name}"')


def test_dump_preserves_non_password_libpq_options(monkeypatch, tmp_path):
    from annotation_metadata import postgres_backup as backup
    secret = "synthetic-secret-must-never-be-printed"
    dsn = (
        f"postgresql://fixture_owner:{secret}@127.0.0.1:5432/fixture_database"
        "?sslmode=require&options=-csearch_path%3Dpublic"
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if "--file" in argv:
            Path(argv[argv.index("--file") + 1]).write_bytes(b"fixture")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(backup, "running_postgres_bindir", lambda explicit=None: tmp_path)
    monkeypatch.setattr(backup, "assert_client_matches_server", lambda *args, **kwargs: None)
    monkeypatch.setattr(backup.subprocess, "run", run)
    monkeypatch.setattr(
        backup.subprocess, "check_output",
        lambda *args, **kwargs: "pg_dump (PostgreSQL) 16.2",
    )
    backup.dump_database(dsn, tmp_path / "fixture.dump", bindir=tmp_path)
    joined = " ".join(calls[0])
    assert secret not in joined
    assert "sslmode=require" in joined or "sslmode" in joined
    assert "search_path" in joined or "options" in joined


def test_restore_refuses_existing_user_schema(database, tmp_path):
    dump_path = tmp_path / "empty.dump"
    dump_path.write_bytes(b"not-a-real-dump")
    with pytest.raises(RuntimeError, match="user schema objects"):
        restore_database(dump_path, database)


def test_dump_refuses_major_version_mismatch(database, monkeypatch, tmp_path):
    from annotation_metadata import postgres_backup as backup
    monkeypatch.setattr(backup, "running_postgres_bindir", lambda explicit=None: tmp_path)
    monkeypatch.setattr(backup, "client_major_version", lambda bindir: 14)
    monkeypatch.setattr(backup, "server_major_version", lambda dsn: 18)
    with pytest.raises(RuntimeError, match="does not match server major"):
        backup.dump_database(database, tmp_path / "mismatch.dump", bindir=tmp_path)
