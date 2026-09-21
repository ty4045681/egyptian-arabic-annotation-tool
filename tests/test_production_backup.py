"""Safety checks for unattended retention and failed restore verification."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/tencent-production/backup-postgres.py"
SPEC = importlib.util.spec_from_file_location("production_backup", SCRIPT)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class ProductionBackupSafetyTests(unittest.TestCase):
    def test_retention_preserves_other_databases_manual_unverified_and_linked_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def make(name, **overrides):
                path = root / name
                path.mkdir()
                report = dict(job=backup.JOB, database="production", restore_verified=True)
                report.update(overrides)
                (path / "verification.json").write_text(json.dumps(report))
                return path

            expired = make("20260901T031700Z-00000001")
            protected = [
                make("20260901T031700Z-00000002", database="another_database"),
                make("20260901T031700Z-00000003", restore_verified=False),
                make("20260901T031700Z-00000004", job="manual"),
                make("20260901T031700Z-00000005"),
                make("20260908T031700Z-00000006"),
                make("20260901T031700Z-00000009"),
                make("manual-before-release"),
            ]
            (protected[3] / "verification.json").write_text("incomplete JSON")
            (protected[5] / "verification.json").write_text("[]")
            current = make("20260921T031700Z-00000007")
            link = root / "20260901T031700Z-00000008"
            link.symlink_to(protected[-1], target_is_directory=True)
            backup.prune_backups(root, "production", 14, current,
                                 datetime(2026, 9, 21, tzinfo=timezone.utc))
            self.assertFalse(expired.exists())
            self.assertTrue(all(path.exists() for path in [*protected, current, link]))

    def test_failed_restore_does_not_publish_or_prune(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "backups"
            root.mkdir()
            old = root / "20200101T031700Z-00000001"
            old.mkdir()
            (old / "verification.json").write_text(json.dumps(dict(
                job=backup.JOB, database="production", restore_verified=True)))
            bindir = base / "bin"
            bindir.mkdir()
            for name in ("psql", "pg_dump", "pg_restore", "initdb", "pg_ctl", "createdb"):
                tool = bindir / name
                tool.touch(mode=0o700)
            env = dict(ANNOTATION_BACKUP_DATABASE="production", ANNOTATION_BACKUP_USER="backup",
                       ANNOTATION_BACKUP_DIR=str(root), ANNOTATION_PG_BINDIR=str(bindir),
                       STATE_DIRECTORY=str(base / "state"), RUNTIME_DIRECTORY=str(base / "runtime"))

            def execute(argv, **kwargs):
                tool = Path(argv[0]).name
                if tool == "psql":
                    output = json.dumps(dict(database="production", server_version_num=180006,
                                             database_bytes=1024))
                elif tool == "pg_dump" and "--version" in argv:
                    output = "pg_dump (PostgreSQL) 18.6"
                elif tool == "pg_dump":
                    Path(argv[argv.index("--file") + 1]).write_bytes(b"test dump")
                    output = ""
                elif tool == "pg_restore" and "--list" not in argv:
                    raise subprocess.CalledProcessError(1, argv, stderr="restore failed")
                else:
                    output = ""
                return subprocess.CompletedProcess(argv, 0, stdout=output)

            original_umask = os.umask(0o077)
            try:
                with patch.dict(os.environ, env, clear=True), patch.object(
                    backup.subprocess, "run", side_effect=execute
                ), patch.object(backup, "prune_backups") as prune:
                    with self.assertRaises(subprocess.CalledProcessError):
                        backup.backup()
                    prune.assert_not_called()
            finally:
                os.umask(original_umask)
            self.assertTrue(old.exists())
            self.assertFalse((root / "last-success.json").exists())
            self.assertEqual(sorted(p.name for p in root.iterdir()), [".backup.lock", old.name])
            self.assertEqual(list((base / "state").iterdir()), [])
            self.assertEqual(list((base / "runtime").iterdir()), [])
