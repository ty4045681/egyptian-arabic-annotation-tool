"""Actual pg_dump/pg_restore into a disposable database."""

from __future__ import annotations

import uuid

import psycopg

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
