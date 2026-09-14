"""The explicit PostgreSQL runner must preserve requested pytest selection."""
from pathlib import Path
import importlib.util

import pytest


@pytest.mark.parametrize('flag_form', [False, True])
@pytest.mark.parametrize('selection', [
    ['tests/test_metadata_queries.py', '-q', '--tb=line'],
    ['-k', 'metadata and not slow', 'tests/test_metadata_queries.py', '-q'],
])
def test_explicit_pg_runner_keeps_first_test_selection(monkeypatch, flag_form, selection):
    script = Path(__file__).resolve().parents[1] / 'scripts/run_pytest_with_postgres.py'
    spec = importlib.util.spec_from_file_location('reviewed_pg_runner', script)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    selected = []
    monkeypatch.setattr(runner, 'apply_pgserver_bindir', lambda value: Path(value))
    monkeypatch.setattr(runner, 'postgres_version_from_bindir', lambda _: 'PostgreSQL 18.6 test stub')
    monkeypatch.setattr(runner.os, 'chdir', lambda _: None)
    monkeypatch.setattr(runner.pytest, 'main', lambda values: selected.extend(values) or 0)
    args = (['--bin-dir', '/synthetic/pg18/bin'] if flag_form else ['/synthetic/pg18/bin'])
    args += selection
    assert runner.main(args) == 0
    assert selected == selection
