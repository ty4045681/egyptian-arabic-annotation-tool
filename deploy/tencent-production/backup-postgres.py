#!/usr/bin/env python3
"""Standalone PG backup; publish only after a restore in an isolated cluster.

Uses the standard library and matching PostgreSQL binaries, independent of
application releases. Never restores into or changes the production database.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone


JOB = "annotation-production-daily"
BACKUP_NAME = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}\Z")


def utc_now():
    return datetime.now(timezone.utc)


def log(message):
    print(f"{utc_now().isoformat()} {message}", flush=True)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def prune_backups(root, database, retention_days, current, now):
    """Delete only expired, verified backups produced by this job for this DB."""
    cutoff = now - timedelta(days=retention_days)
    for path in sorted(root.iterdir()):
        if path == current or path.is_symlink() or not path.is_dir():
            continue
        if not BACKUP_NAME.fullmatch(path.name):
            continue
        try:
            created = datetime.strptime(path.name[:16], "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
            report = json.loads((path / "verification.json").read_text())
        except (OSError, ValueError):
            continue
        if (isinstance(report, dict) and created < cutoff and report.get("job") == JOB
                and report.get("database") == database
                and report.get("restore_verified") is True):
            shutil.rmtree(path)
            log(f"removed expired backup: {path.name}")


def backup():
    os.umask(0o077)
    database = os.environ["ANNOTATION_BACKUP_DATABASE"]
    user = os.environ["ANNOTATION_BACKUP_USER"]
    host = os.environ.get("ANNOTATION_BACKUP_HOST", "/var/run/postgresql")
    port = os.environ.get("ANNOTATION_BACKUP_PORT", "5432")
    root = Path(os.environ["ANNOTATION_BACKUP_DIR"]).resolve()
    bindir = Path(os.environ["ANNOTATION_PG_BINDIR"])
    state = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/annotation-backup"))
    runtime = Path(os.environ.get("RUNTIME_DIRECTORY", "/run/annotation-backup"))
    retention = int(os.environ.get("ANNOTATION_BACKUP_RETENTION_DAYS", "14"))
    if retention < 2 or not host.startswith("/") or not port.isdigit():
        raise ValueError("require retention >= 2 days and a local Unix socket")
    for name in ("psql", "pg_dump", "pg_restore", "initdb", "pg_ctl", "createdb"):
        if not os.access(bindir / name, os.X_OK):
            raise RuntimeError(f"missing PostgreSQL binary: {bindir / name}")
    for path in (root, state, runtime):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Keep the lock open until all cleanup is complete.
    with (root / ".backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Clear libpq overrides inherited from a shell or service manager.
        env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
        env.update(PGHOST=host, PGPORT=port, PGUSER=user, PGDATABASE=database,
                   PGCONNECT_TIMEOUT="15", PGAPPNAME=JOB)

        def run(name, *args, connection=env, capture=False, output=None):
            result = subprocess.run(
                [str(bindir / name), *map(str, args)], env=connection, check=True,
                text=True, stdout=subprocess.PIPE if capture else output,
            )
            return result.stdout.strip() if capture else None

        source = json.loads(run(
            "psql", "-X", "--no-password", "-At", "-v", "ON_ERROR_STOP=1", "-c",
            "SELECT json_build_object('database', current_database(), "
            "'server_version_num', current_setting('server_version_num')::int, "
            "'database_bytes', pg_database_size(current_database()));", capture=True,
        ))
        client = run("pg_dump", "--version", capture=True)
        client_major = int(re.search(r"(\d+)\.", client).group(1))
        if source["database"] != database or source["server_version_num"] // 10000 != client_major:
            raise RuntimeError("source database or PostgreSQL client version mismatch")
        reserve = 512 * 1024 * 1024
        required = source["database_bytes"] * 3 + reserve
        if any(shutil.disk_usage(p).free < required for p in (root, state)):
            raise RuntimeError(f"insufficient free space; require {required} bytes per filesystem")

        started = utc_now()
        name = started.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=root))
        scratch = Path(tempfile.mkdtemp(prefix="verify-", dir=state))
        socket = Path(tempfile.mkdtemp(prefix="pg-", dir=runtime))
        pgdata = scratch / "pgdata"
        final = root / name

        def stop_cluster():
            if (pgdata / "postmaster.pid").exists():
                try:
                    run("pg_ctl", "-D", pgdata, "-m", "fast", "-w", "-t", "60", "stop")
                except subprocess.CalledProcessError:
                    run("pg_ctl", "-D", pgdata, "-m", "immediate", "-w", "-t", "30", "stop")

        try:
            log(f"backing up database {database}")
            dump = pending / "production.dump"
            run("pg_dump", "--no-password", "--format=custom", "--compress=6",
                "--lock-wait-timeout=60s", "--file", dump)
            with (pending / "restore.list").open("w") as output:
                run("pg_restore", "--list", dump, output=output)

            # The restore cluster has no TCP listener and a private Unix socket.
            # No production administrator credentials or CREATEDB grant needed.
            with (pending / "initdb.log").open("w") as output:
                run("initdb", "-D", pgdata, "--auth-local=peer", "--auth-host=reject",
                    "--no-locale", "--encoding=UTF8", "--username", user, output=output)
            run("pg_ctl", "-D", pgdata, "-w", "-t", "60", "-l", pending / "restore.log",
                "-o", f"-k {socket} -c listen_addresses='' -p 5432 "
                "-c max_connections=10 -c shared_buffers=32MB -c maintenance_work_mem=64MB", "start")
            restore_env = dict(env, PGHOST=str(socket), PGPORT="5432", PGDATABASE="postgres")
            run("createdb", "--no-password", "--template=template0", "backup_verify",
                connection=restore_env)
            restore_env["PGDATABASE"] = "backup_verify"
            log("restoring full dump in isolated verification cluster")
            run("pg_restore", "--no-password", "--dbname=backup_verify", "--no-owner",
                "--no-acl", "--exit-on-error", "--single-transaction", dump,
                connection=restore_env)
            # Count every restored user table, including future migrations.
            counts_sql = """
CREATE TEMP TABLE backup_table_counts (table_name text, row_count bigint);
DO $$ DECLARE t record; n bigint; BEGIN
  FOR t IN SELECT schemaname, tablename FROM pg_tables
           WHERE schemaname <> 'information_schema' AND schemaname !~ '^pg_'
           ORDER BY schemaname, tablename LOOP
    EXECUTE format('SELECT count(*) FROM %I.%I', t.schemaname, t.tablename) INTO n;
    INSERT INTO backup_table_counts VALUES (t.schemaname || '.' || t.tablename, n);
  END LOOP;
END $$;
SELECT coalesce(json_object_agg(table_name, row_count), '{}'::json) FROM backup_table_counts;
"""
            counts = json.loads(run("psql", "-X", "--no-password", "-qAt", "-v", "ON_ERROR_STOP=1",
                                    "-c", counts_sql, connection=restore_env, capture=True))
            if not {"public.annotation_tasks", "public.segments", "public.schema_migrations"} <= counts.keys():
                raise RuntimeError("restored database is missing required application tables")
            migrations = json.loads(run(
                "psql", "-X", "--no-password", "-At", "-v", "ON_ERROR_STOP=1", "-c",
                "SELECT coalesce(json_agg(version ORDER BY version), '[]'::json) FROM schema_migrations;",
                connection=restore_env, capture=True,
            ))
            stop_cluster()
            report = dict(job=JOB, database=database, started_at=started.isoformat(),
                          verified_at=utc_now().isoformat(), restore_verified=True,
                          source=source, pg_dump=client, restored_table_rows=counts,
                          schema_migrations=migrations, dump_bytes=dump.stat().st_size,
                          retention_days=retention)
            write_json(pending / "verification.json", report)
            with (pending / "SHA256SUMS").open("w") as sums:
                for path in sorted(pending.iterdir()):
                    if path.name != "SHA256SUMS":
                        with path.open("rb") as stream:
                            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
                        sums.write(f"{checksum}  {path.name}\n")
            # Flush artifacts before the atomic rename makes this backup visible.
            for path in pending.iterdir():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            directory_fd = os.open(pending, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            pending.rename(final)
            with (root / ".last-success.tmp").open("w") as stream:
                json.dump(dict(report, path=str(final)), stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            (root / ".last-success.tmp").replace(root / "last-success.json")
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            # Only a newly verified backup permits retention cleanup.
            prune_backups(root, database, retention, final, utc_now())
            log(f"backup complete and restore verified: {final}")
        finally:
            stop_cluster()
            shutil.rmtree(scratch)
            shutil.rmtree(socket)
            if pending.exists():
                shutil.rmtree(pending)


def interrupted(signum, frame):
    raise RuntimeError(f"backup interrupted by signal {signum}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    try:
        backup()
    except Exception as error:
        print(f"backup failed: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
