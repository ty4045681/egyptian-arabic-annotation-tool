"""PostgreSQL custom dump/restore helpers for disposable verification.

Use same-major binaries as the running server (pgserver bundled 16 or
/usr/lib/postgresql/18/bin). Never point these at a live staging database.
DSN passwords are passed via PGPASSWORD, never on the process command line.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict


def running_postgres_bindir(explicit: str | Path | None = None) -> Path:
    if explicit:
        path = Path(explicit)
    elif os.environ.get("ANNOTATION_PG_BINDIR"):
        path = Path(os.environ["ANNOTATION_PG_BINDIR"])
    else:
        path = None
        try:
            import pgserver.postgres_server as postgres_server
            path = Path(postgres_server.POSTGRES_BIN_PATH)
        except ImportError:
            found = shutil.which("pg_dump")
            path = Path(found).parent if found else None
    if path is None or not (path / "pg_dump").is_file() or not (path / "pg_restore").is_file():
        raise RuntimeError(f"pg_dump/pg_restore not found in {path}")
    return path


def _client_command(bindir: Path, tool: str, dsn: str, extra: list[str]) -> tuple[list[str], dict]:
    info = conninfo_to_dict(dsn)
    password = info.get("password")
    argv = [str(bindir / tool)]
    if info.get("host"):
        argv.extend(["--host", str(info["host"])])
    if info.get("port"):
        argv.extend(["--port", str(info["port"])])
    if info.get("user"):
        argv.extend(["--username", str(info["user"])])
    if info.get("dbname"):
        argv.extend(["--dbname", str(info["dbname"])])
    argv.extend(extra)
    env = os.environ.copy()
    if password:
        env["PGPASSWORD"] = str(password)
    return argv, env


def dump_database(dsn: str, output: Path, *, bindir: Path | None = None) -> dict:
    bindir = running_postgres_bindir(bindir)
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    argv, env = _client_command(
        bindir, "pg_dump", dsn,
        ["--format=custom", "--compress=6", "--file", str(tmp)],
    )
    try:
        result = subprocess.run(
            argv, check=False, capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "pg_dump failed")
        os.replace(tmp, output)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    version = subprocess.check_output(
        [str(bindir / "postgres"), "--version"], text=True,
    ).strip()
    return {
        "path": str(output),
        "size": output.stat().st_size,
        "bindir": str(bindir),
        "postgres": version,
    }


def restore_database(dump: Path, target_dsn: str, *, bindir: Path | None = None) -> dict:
    bindir = running_postgres_bindir(bindir)
    dump = Path(dump).expanduser().resolve()
    if not dump.is_file():
        raise RuntimeError(f"dump not found: {dump}")
    with psycopg.connect(target_dsn) as conn:
        exists = conn.execute(
            """SELECT 1 FROM information_schema.tables
               WHERE table_schema = 'public' AND table_name = 'annotation_tasks'"""
        ).fetchone()
        if exists:
            count = conn.execute("SELECT count(*) FROM annotation_tasks").fetchone()[0]
            if count:
                raise RuntimeError(
                    "target database already has annotation_tasks; "
                    "restore only into a newly created empty database"
                )
    argv, env = _client_command(
        bindir, "pg_restore", target_dsn,
        ["--no-owner", "--no-acl", "--exit-on-error", str(dump)],
    )
    # pg_restore --dbname is the target database name; keep extra flags after.
    result = subprocess.run(
        argv, check=False, capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "pg_restore failed")
    return {"dump": str(dump), "bindir": str(bindir)}


def provenance_counts(conn) -> dict:
    def count(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "tasks": count("SELECT count(*) FROM annotation_tasks"),
        "versions": count("SELECT count(*) FROM annotation_versions"),
        "segments": count("SELECT count(*) FROM segments"),
        "assignments": count("SELECT count(*) FROM assignments"),
        "sources": count("SELECT count(*) FROM task_sources"),
        "source_current": count("SELECT count(*) FROM task_sources WHERE is_current"),
        "predictions": count("SELECT count(*) FROM task_scene_predictions"),
        "reviews": count("SELECT count(*) FROM scene_reviews"),
        "review_labels": count("SELECT count(*) FROM scene_review_labels"),
        "media_identities": count("SELECT count(*) FROM task_media_identities"),
        "import_runs": count("SELECT count(*) FROM source_import_runs"),
        "scopes": count("SELECT count(*) FROM annotator_scene_scopes"),
        "events": count("SELECT count(*) FROM annotation_events"),
        "admin_actions": count("SELECT count(*) FROM admin_actions"),
    }


def provenance_relationships(conn) -> dict:
    dangling = {
        "sources_without_task": int(conn.execute(
            """SELECT count(*) FROM task_sources s
               LEFT JOIN annotation_tasks t ON t.id = s.task_id
               WHERE t.id IS NULL"""
        ).fetchone()[0]),
        "reviews_without_version": int(conn.execute(
            """SELECT count(*) FROM scene_reviews r
               LEFT JOIN annotation_versions v ON v.id = r.version_id
               WHERE v.id IS NULL"""
        ).fetchone()[0]),
        "predictions_without_task": int(conn.execute(
            """SELECT count(*) FROM task_scene_predictions p
               LEFT JOIN annotation_tasks t ON t.id = p.task_id
               WHERE t.id IS NULL"""
        ).fetchone()[0]),
        "labels_without_review": int(conn.execute(
            """SELECT count(*) FROM scene_review_labels l
               LEFT JOIN scene_reviews r ON r.id = l.review_id
               WHERE r.id IS NULL"""
        ).fetchone()[0]),
        "identities_without_task": int(conn.execute(
            """SELECT count(*) FROM task_media_identities i
               LEFT JOIN annotation_tasks t ON t.id = i.task_id
               WHERE t.id IS NULL"""
        ).fetchone()[0]),
    }
    return {"ok": all(value == 0 for value in dangling.values()), "dangling": dangling}
