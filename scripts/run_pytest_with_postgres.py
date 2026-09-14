"""Run pytest against an explicit PostgreSQL binary and/or service.

Usage (from the repository root):

    # Bundled pgserver PostgreSQL 16 (default pytest, no wrapper required):
    UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q

    # Native PostgreSQL 18 binaries via pgserver (throwaway cluster):
    UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \\
      scripts/run_pytest_with_postgres.py /usr/lib/postgresql/18/bin -q

    # External server (GitHub Actions service). Disposable DBs only:
    UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \\
      scripts/run_pytest_with_postgres.py --bin-dir /usr/lib/postgresql/16/bin \\
      --admin-dsn "$ANNOTATION_TEST_PG_ADMIN_DSN" -q

Never pass a live staging DSN. Passwords belong in PGPASSWORD, ~/.pgpass, or
a libpq service file — not on the command line.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.pg_runtime import (
    apply_client_bindir, apply_pgserver_bindir, postgres_version_from_bindir,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run pytest against an explicit PostgreSQL 16 or 18 target.",
    )
    parser.add_argument(
        "--bin-dir",
        dest="bin_dir",
        help="PostgreSQL binary directory (postgres/pg_dump/pg_restore)",
    )
    parser.add_argument(
        "--admin-dsn",
        dest="admin_dsn",
        help=(
            "Admin libpq DSN used only to CREATE/DROP disposable test databases. "
            "Omit the password; libpq reads PGPASSWORD, ~/.pgpass, or PGSERVICEFILE."
        ),
    )
    args, pytest_args = parser.parse_known_args(argv)
    bindir_raw = args.bin_dir
    # Parse only our named options so argparse never consumes a pytest file
    # or a value belonging to an unknown pytest option (for example, -k).
    # Keep the documented legacy form: the binary directory comes first.
    if (not bindir_raw and pytest_args and not pytest_args[0].startswith("-")
            and (not args.admin_dsn or Path(pytest_args[0]).is_dir())):
        bindir_raw = pytest_args.pop(0)
    if not bindir_raw and not args.admin_dsn:
        parser.error("pass a PostgreSQL binary directory and/or --admin-dsn")
    if bindir_raw:
        bindir_path = Path(bindir_raw)
        if args.admin_dsn and not (bindir_path / "postgres").is_file():
            bindir = apply_client_bindir(bindir_path)
        else:
            bindir = apply_pgserver_bindir(bindir_path)
        print("ACCEPTANCE DATABASE BINARY:", postgres_version_from_bindir(bindir), flush=True)
        print("ACCEPTANCE DATABASE BINDIR:", bindir, flush=True)
    if args.admin_dsn:
        os.environ["ANNOTATION_TEST_PG_ADMIN_DSN"] = args.admin_dsn.strip()
        print("ACCEPTANCE DATABASE TARGET: service DSN (disposable test databases)", flush=True)
    os.chdir(ROOT)
    return pytest.main(pytest_args)


if __name__ == "__main__":
    raise SystemExit(main())
