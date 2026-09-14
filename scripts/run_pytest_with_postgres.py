"""Run pytest against a requested PostgreSQL binary via pgserver.

Usage (from the repository root):

    UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \\
      scripts/run_pytest_with_postgres.py /usr/lib/postgresql/18/bin \\
      -q --ignore=tests/browser
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pgserver._commands as commands
import pgserver.postgres_server as postgres_server
import pytest

if len(sys.argv) < 2:
    raise SystemExit("usage: run_pytest_with_postgres.py <postgres-bin-dir> [pytest args]")

binary_root = Path(sys.argv[1]).resolve()
if not (binary_root / "postgres").is_file():
    raise SystemExit(f"Missing postgres executable: {binary_root}")
commands.POSTGRES_BIN_PATH = binary_root
postgres_server.POSTGRES_BIN_PATH = binary_root
os.environ["PATH"] = str(binary_root) + os.pathsep + os.environ.get("PATH", "")
version = subprocess.check_output(
    [str(binary_root / "postgres"), "--version"], text=True,
).strip()
print("ACCEPTANCE DATABASE BINARY:", version, flush=True)
sys.path.insert(0, str(Path.cwd()))
raise SystemExit(pytest.main(sys.argv[2:]))
