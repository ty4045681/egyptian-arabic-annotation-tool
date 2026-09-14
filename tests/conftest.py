from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pgserver
import psycopg
import pytest


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("pgserver")
    server = pgserver.get_server(root / "data")
    yield server


@pytest.fixture
def database(pg_server, monkeypatch):
    import db

    db.close_pool()
    name = "test_" + uuid.uuid4().hex
    admin_dsn = pg_server.get_uri(database="postgres")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_server.get_uri(database=name)
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
    server.app.config.update(TESTING=True, AUDIO_DIR=str(audio),
                             SESSION_TTL_SECONDS=1800,
                             PERMANENT_SESSION_LIFETIME=1800,
                             SECRET_KEY="test-secret")
    return server.app.test_client()
