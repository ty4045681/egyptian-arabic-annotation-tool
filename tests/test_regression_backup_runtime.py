"""Backup commands must work in production installs and keep DSN secrets private."""
import builtins
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_backup_module_does_not_require_dev_only_pgserver(monkeypatch, tmp_path):
    original = builtins.__import__
    def production_import(name, *args, **kwargs):
        if name == 'pgserver' or name.startswith('pgserver.'):
            raise ModuleNotFoundError('pgserver is a development-only dependency')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', production_import)
    source = Path(__file__).resolve().parents[1] / 'annotation_metadata/postgres_backup.py'
    spec = importlib.util.spec_from_file_location('backup_production_runtime_check', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for binary in ('pg_dump', 'pg_restore', 'postgres'):
        (tmp_path / binary).write_text('fixture')
    assert module.running_postgres_bindir(tmp_path) == tmp_path


@pytest.mark.parametrize('operation,channel', [('dump','process'), ('restore','process'), ('restore','report')])
def test_backup_password_is_not_in_process_args_or_report(monkeypatch, tmp_path, operation, channel):
    from annotation_metadata import postgres_backup as backup
    secret = 'synthetic-secret-must-never-be-printed'
    dsn = f'postgresql://fixture_owner:{secret}@127.0.0.1:5432/fixture_database'
    calls = []
    def run(argv, **kwargs):
        calls.append(list(argv))
        if '--file' in argv:
            Path(argv[argv.index('--file') + 1]).write_bytes(b'fixture-custom-dump')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    class EmptyTarget:
        info = SimpleNamespace(server_version=180006)
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, *args, **kwargs): return self
        def fetchone(self): return None
        def fetchall(self): return []
    monkeypatch.setattr(backup, 'running_postgres_bindir', lambda explicit=None: tmp_path)
    monkeypatch.setattr(backup.subprocess, 'run', run)
    monkeypatch.setattr(backup.subprocess, 'check_output', lambda *args, **kwargs: 'postgres (PostgreSQL) 18.6')
    monkeypatch.setattr(backup.psycopg, 'connect', lambda *args, **kwargs: EmptyTarget())
    output = tmp_path / 'fixture.dump'
    if operation == 'dump':
        report = backup.dump_database(dsn, output, bindir=tmp_path)
    else:
        output.write_bytes(b'fixture-custom-dump')
        report = backup.restore_database(output, dsn, bindir=tmp_path)
    if channel == 'process':
        assert all(secret not in str(arg) for argv in calls for arg in argv), 'Database passwords must not be visible in child-process command lines'
    else:
        assert secret not in json.dumps(report), 'CLI reports must not print the target DSN password'
