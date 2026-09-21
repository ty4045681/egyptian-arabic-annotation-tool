#!/usr/bin/env python3
"""Reproduce frontend P0 evidence against disposable PostgreSQL only.

Usage (from the repository root):
    uv run --no-sync python scripts/frontend_p0.py regression
    uv run --no-sync python scripts/frontend_p0.py capture

This runner does not change application code or reuse a configured database.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs/plans/frontend-rebuild-p0"
sys.path.insert(0, str(ROOT))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class EvidencePlugin:
    def __init__(self, output, mode):
        self.output = output
        self.mode = mode
        self.reports = []
        self.database = None
        self.started = time.monotonic()

    def pytest_configure(self, config):
        # Existing tests write screenshots to an earlier acceptance folder.
        # Redirect that helper without modifying tests or historical evidence.
        import tests.browser.cross_check_helpers as helpers
        helpers.ACCEPT_DIR = self.output / "regression-screenshots"

    def pytest_collection_finish(self, session):
        write_json(self.output / f"{self.mode}-collected.json", [
            item.nodeid for item in session.items
        ])

    def pytest_runtest_call(self, item):
        admin = item.funcargs.get("pg_admin")
        if admin:
            self.database = {"kind": admin.kind, "version": admin.version}

    def pytest_runtest_logreport(self, report):
        if report.when == "call" or report.failed or report.skipped:
            record = {"nodeid": report.nodeid, "phase": report.when,
                      "outcome": report.outcome, "seconds": round(report.duration, 4)}
            if report.skipped:
                record["reason"] = str(report.longrepr)
            if report.failed:
                record["failure"] = str(report.longrepr)
            self.reports.append(record)

    def pytest_sessionfinish(self, session, exitstatus):
        counts = {status: sum(row["outcome"] == status for row in self.reports)
                  for status in ("passed", "failed", "skipped")}
        write_json(self.output / f"{self.mode}-results.json", {
            "source_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "exit_status": int(exitstatus), "database": self.database,
            "seconds": round(time.monotonic() - self.started, 2),
            "counts": counts, "reports": self.reports,
        })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["regression", "capture"])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--only", help="Optional pytest -k expression for a focused evidence rerun")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)
    # Do not inherit a live service or an external test service. pgserver owns
    # its cluster; tests/conftest.py creates and drops uniquely named databases.
    for key in ("ANNOTATION_DB_DSN", "ANNOTATION_TEST_PG_ADMIN_DSN",
                "ANNOTATION_PG_BINDIR", "ANNOTATION_SPEED_EXPLAIN"):
        os.environ.pop(key, None)
    os.environ["FRONTEND_P0_OUTPUT"] = str(output)
    packages = {name: importlib.metadata.version(name) for name in
                ("pytest", "pytest-playwright", "playwright", "pgserver", "flask", "psycopg")}
    sources = ["index.html", "login.html", "completed.html", "admin.html", "admin.js",
               "admin.css", "server.py", "annotation_metadata/routes.py", "annotation_quality/routes.py"]
    sources += [str(p.relative_to(ROOT)) for p in sorted((ROOT / "static").glob("*.js"))]
    sources += ["static/metadata.css"]
    write_json(output / "source-manifest.json", {
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "python": sys.version.split()[0], "packages": packages,
        "database_policy": "disposable pgserver; inherited DSNs removed",
        "files": {p: {"sha256": hashlib.sha256((ROOT / p).read_bytes()).hexdigest(),
                      "lines": len((ROOT / p).read_text().splitlines())} for p in sources},
    })
    import server
    routes = []
    for rule in sorted(server.app.url_map.iter_rules(), key=lambda r: (r.rule, r.endpoint)):
        fn = inspect.unwrap(server.app.view_functions[rule.endpoint])
        routes.append({"path": rule.rule, "methods": sorted(rule.methods - {"HEAD", "OPTIONS"}),
                       "endpoint": rule.endpoint,
                       "source": str(Path(inspect.getsourcefile(fn)).relative_to(ROOT)),
                       "line": inspect.getsourcelines(fn)[1]})
    write_json(output / "route-inventory.json", routes)
    import pytest
    target = "tests" if args.mode == "regression" else "scripts/frontend_p0_capture.py"
    log_path = output / f"{args.mode}.log"
    print(f"Running {args.mode}; output: {output}", flush=True)
    with log_path.open("w") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        pytest_args = [
            target, "-q", "--tb=short", "-o", "addopts=",
            f"--junitxml={output / (args.mode + '-junit.xml')}",
        ]
        if args.only:
            pytest_args += ["-k", args.only]
        result = pytest.main(pytest_args, plugins=[EvidencePlugin(output, args.mode)])
    print((output / f"{args.mode}-results.json").read_text()[:650], flush=True)
    print(f"Detailed output: {log_path}", flush=True)
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
