"""Reusable PostgreSQL target selection for tests and capacity scripts.

Environment:

- ``ANNOTATION_PG_BINDIR``: directory containing ``postgres``, ``pg_dump``,
  and ``pg_restore``. When set, pgserver is pointed at those binaries so a
  native 18 cluster is not confused with bundled 16.
- ``ANNOTATION_TEST_PG_ADMIN_DSN``: if set, tests create uniquely named
  disposable databases on this server instead of starting pgserver. Passwords
  belong in ``PGPASSWORD`` / ``~/.pgpass`` / a libpq service file, not in
  argv. Never point this at a live staging DSN.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict, make_conninfo


def apply_client_bindir(bindir: str | Path) -> Path:
    """Record matching pg_dump/pg_restore for dump/restore tests."""
    binary_root = Path(bindir).resolve()
    if not (binary_root / "pg_dump").is_file() or not (binary_root / "pg_restore").is_file():
        raise RuntimeError(f"Missing pg_dump/pg_restore in {binary_root}")
    os.environ["ANNOTATION_PG_BINDIR"] = str(binary_root)
    os.environ["PATH"] = str(binary_root) + os.pathsep + os.environ.get("PATH", "")
    return binary_root


def apply_pgserver_bindir(bindir: str | Path) -> Path:
    """Point pgserver at an explicit PostgreSQL binary directory."""
    binary_root = apply_client_bindir(bindir)
    if not (binary_root / "postgres").is_file():
        raise RuntimeError(f"Missing postgres executable: {binary_root}")
    import pgserver._commands as commands
    import pgserver.postgres_server as postgres_server
    commands.POSTGRES_BIN_PATH = binary_root
    postgres_server.POSTGRES_BIN_PATH = binary_root
    return binary_root


def postgres_version_from_bindir(bindir: Path) -> str:
    postgres = Path(bindir) / "postgres"
    tool = postgres if postgres.is_file() else Path(bindir) / "pg_dump"
    return subprocess.check_output([str(tool), "--version"], text=True).strip()


def configure_from_env() -> Path | None:
    """Apply ``ANNOTATION_PG_BINDIR`` if present. Returns the bindir or None."""
    raw = os.environ.get("ANNOTATION_PG_BINDIR", "").strip()
    if not raw:
        return None
    return apply_pgserver_bindir(raw)


@dataclass
class PostgresAdmin:
    """CREATE/DROP DATABASE target used by the per-test fixture."""

    kind: str
    admin_dsn: str
    bindir: Path | None
    version: str
    pg_server: object | None = None

    def uri_for(self, database: str) -> str:
        if self.pg_server is not None:
            return self.pg_server.get_uri(database=database)
        info = conninfo_to_dict(self.admin_dsn)
        info["dbname"] = database
        cleaned = {key: value for key, value in info.items() if value is not None}
        return make_conninfo(**cleaned)


def open_test_postgres(tmp_path) -> PostgresAdmin:
    """Session-scoped cluster: external DSN, or pgserver with optional bindir."""
    admin_dsn = os.environ.get("ANNOTATION_TEST_PG_ADMIN_DSN", "").strip()
    if admin_dsn:
        bindir = None
        raw = os.environ.get("ANNOTATION_PG_BINDIR", "").strip()
        if raw:
            bindir = Path(raw).resolve()
            os.environ["ANNOTATION_PG_BINDIR"] = str(bindir)
        import psycopg
        with psycopg.connect(admin_dsn) as conn:
            version = conn.execute("SHOW server_version").fetchone()[0]
            full = f"PostgreSQL {version}"
        return PostgresAdmin(
            kind="service",
            admin_dsn=admin_dsn,
            bindir=bindir,
            version=full,
        )

    bindir = configure_from_env()
    import pgserver
    import pgserver.postgres_server as postgres_server
    root = tmp_path / "data"
    server = pgserver.get_server(root)
    version = postgres_version_from_bindir(Path(postgres_server.POSTGRES_BIN_PATH))
    os.environ.setdefault(
        "ANNOTATION_PG_BINDIR", str(Path(postgres_server.POSTGRES_BIN_PATH)),
    )
    return PostgresAdmin(
        kind="pgserver",
        admin_dsn=server.get_uri(database="postgres"),
        bindir=Path(postgres_server.POSTGRES_BIN_PATH),
        version=version,
        pg_server=server,
    )
