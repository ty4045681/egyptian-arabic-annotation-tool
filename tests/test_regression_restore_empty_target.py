"""A metadata-free database may still contain unrelated user data."""
import uuid
from types import SimpleNamespace

import psycopg
import pytest

from annotation_metadata import postgres_backup as backup


def test_restore_refuses_existing_user_objects_outside_public(pg_server, tmp_path, monkeypatch):
    name = 'restore_guard_' + uuid.uuid4().hex
    admin = pg_server.get_uri(database='postgres')
    target = pg_server.get_uri(database=name)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    invoked = []
    def run(argv, **kwargs):
        invoked.append(argv)
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(backup.subprocess, 'run', run)
    dump = tmp_path / 'synthetic.dump'
    dump.write_bytes(b'synthetic archive; should never reach pg_restore')
    try:
        with psycopg.connect(target) as conn:
            conn.execute('CREATE SCHEMA customer_records')
            conn.execute('CREATE TABLE customer_records.existing_data (note text)')
            conn.execute("INSERT INTO customer_records.existing_data VALUES ('preserve me')")
        with pytest.raises(RuntimeError, match='empty|user schema|already'):
            backup.restore_database(dump, target)
        assert not invoked, 'Refuse an occupied restore target before starting pg_restore'
        with psycopg.connect(target) as conn:
            assert conn.execute('SELECT note FROM customer_records.existing_data').fetchone()[0] == 'preserve me'
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
