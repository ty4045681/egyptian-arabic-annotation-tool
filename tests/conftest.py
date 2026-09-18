from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from tests.pg_runtime import open_test_postgres


@pytest.fixture(scope="session")
def pg_admin(tmp_path_factory):
    admin = open_test_postgres(tmp_path_factory.mktemp("pgserver"))
    print(f"ACCEPTANCE DATABASE BINARY: {admin.version} ({admin.kind})", flush=True)
    if admin.bindir is not None:
        print(f"ACCEPTANCE DATABASE BINDIR: {admin.bindir}", flush=True)
    yield admin


@pytest.fixture(scope="session")
def pg_server(pg_admin):
    """Compatibility alias: pgserver handle, or a URI adapter for services."""
    if pg_admin.pg_server is not None:
        yield pg_admin.pg_server
        return

    class _ServiceAdapter:
        def get_uri(self, database="postgres"):
            return pg_admin.uri_for(database)

    yield _ServiceAdapter()


@pytest.fixture
def database(pg_admin, monkeypatch):
    import db

    db.close_pool()
    name = "test_" + uuid.uuid4().hex
    admin_dsn = pg_admin.uri_for("postgres")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_admin.uri_for(name)
    monkeypatch.setenv("ANNOTATION_DB_DSN", dsn)
    with psycopg.connect(dsn) as conn:
        assert db.apply_migrations(conn) == db.expected_versions()
    yield dsn
    db.close_pool()
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        try:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        except psycopg.Error:
            conn.execute(f'DROP DATABASE "{name}"')


@pytest.fixture
def seed_tasks(database):
    import db
    from preprocess_store import store_preprocessed_task

    def seed(count=3, folder="", duration=10.0):
        ids = []
        with db.db_conn() as conn:
            for index in range(count):
                filename = f"audio-{index:03d}.wav"
                rel_path = f"{folder}/{filename}" if folder else filename
                result = store_preprocessed_task(
                    conn,
                    rel_path=rel_path,
                    filename=filename,
                    folder=folder,
                    duration=duration,
                    segments=[
                        {"id": 1, "start": 0.0, "end": 5.0,
                         "duration": 5.0, "asr_text": "asr one", "text": "",
                         "exclude_from_training": False},
                        {"id": 2, "start": 5.0, "end": duration,
                         "duration": duration - 5.0, "asr_text": "asr two", "text": "",
                         "exclude_from_training": False},
                    ],
                    waveform_payload=b"\x01\x00\xff\xff" * 10,
                )
                ids.append(result["task_id"])
        return ids

    return seed


@pytest.fixture
def client(database, tmp_path, monkeypatch):
    import server

    audio = tmp_path / "audio"
    audio.mkdir()
    server.app.config.update(
        TESTING=True,
        AUDIO_DIR=str(audio),
        SESSION_TTL_SECONDS=1800,
        SESSION_ABSOLUTE_SECONDS=20 * 3600,
        SESSION_PRESENCE_HEARTBEAT_SECONDS=30,
        SESSION_PRESENCE_LEASE_SECONDS=150,
        SESSION_TAKEOVER_TOKEN_SECONDS=60,
        SESSION_ACTIVITY_THROTTLE_SECONDS=30,
        OFFLINE_DRAFT_RETENTION_DAYS=7,
        SESSION_IDLE_WARNING_SECONDS=120,
        PUBLIC_DASHBOARD_TIMEZONE="Asia/Shanghai",
        PERMANENT_SESSION_LIFETIME=20 * 3600,
        SESSION_REFRESH_EACH_REQUEST=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
        SECRET_KEY="test-secret",
    )
    return server.app.test_client()
