"""psycopg 3 connection pool + migration runner.

DSN comes from the ANNOTATION_DB_DSN environment variable (systemd
EnvironmentFile in production, test fixtures in CI). Never stored in
config.json or the repository.

One pool per process, created lazily AFTER Gunicorn forks workers.
DDL is never auto-applied here — use `manage_state.py apply-migrations`.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()

# 4 Gunicorn workers x max 16 conns = 64 app conns; PostgreSQL runs with
# max_connections=150, leaving >=50 for migration/preprocess/backup/admin.
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 16
POOL_TIMEOUT = 10.0  # seconds waiting for a free connection


def get_dsn() -> str:
    dsn = os.environ.get("ANNOTATION_DB_DSN", "").strip()
    if not dsn:
        raise RuntimeError(
            "ANNOTATION_DB_DSN is not set. Provide it via environment "
            "(systemd EnvironmentFile in production)."
        )
    return dsn


def get_pool() -> ConnectionPool:
    """Process-wide lazy pool. Safe to call from any request thread."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(
                conninfo=get_dsn(),
                min_size=POOL_MIN_SIZE,
                max_size=POOL_MAX_SIZE,
                timeout=POOL_TIMEOUT,
                max_lifetime=60 * 60,          # recycle connections hourly
                max_idle=10 * 60,
                reconnect_timeout=30,
                open=True,
                check=ConnectionPool.check_connection,
            )
        return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


@contextmanager
def db_conn():
    """Borrow a connection; autocommit off — caller manages the transaction."""
    pool = get_pool()
    with pool.connection() as conn:
        yield conn


@contextmanager
def db_tx():
    """Explicit transaction: commits on success, rolls back on exception."""
    with db_conn() as conn:
        with conn.transaction():
            yield conn


def apply_migrations(conn: psycopg.Connection) -> list[int]:
    """Apply migrations serially under a PostgreSQL advisory lock."""
    lock_key = 7_116_001_005  # stable project-specific session lock
    conn.commit()
    conn.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
    conn.commit()
    try:
        with conn.transaction():
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
                " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        applied = {
            r[0]
            for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        conn.commit()

        files: list[tuple[int, str, Path]] = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            stem = path.stem.split("_", 1)
            version = int(stem[0])
            name = stem[1] if len(stem) > 1 else path.stem
            files.append((version, name, path))
        files.sort(key=lambda t: t[0])

        newly: list[int] = []
        for version, name, path in files:
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                    (version, name),
                )
            newly.append(version)
        return newly
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
        conn.commit()


def expected_versions() -> list[int]:
    return sorted(
        int(path.stem.split("_", 1)[0])
        for path in MIGRATIONS_DIR.glob("*.sql")
    )


def applied_versions(conn: psycopg.Connection) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()]


def assert_schema_current(conn: psycopg.Connection) -> list[int]:
    applied = applied_versions(conn)
    expected = expected_versions()
    if applied != expected:
        raise RuntimeError(
            f"database schema mismatch: applied={applied}, expected={expected}; "
            "run `uv run python manage_state.py apply-migrations` with the migration role"
        )
    return applied
