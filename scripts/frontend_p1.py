#!/usr/bin/env python3
"""Run frontend P1 acceptance against disposable PostgreSQL only.

Usage (from the repository root):

    uv run --no-sync python scripts/frontend_p1.py acceptance
    uv run --no-sync python scripts/frontend_p1.py capture
    uv run --no-sync python scripts/frontend_p1.py combos
    uv run --no-sync python scripts/frontend_p1.py smoke

    uv run --no-sync python scripts/frontend_p1.py acceptance --only "test_p1_input_scale"

Modes:

* acceptance -- tests/test_frontend_delivery.py + tests/browser/test_frontend_p1_*.py
* capture    -- scripts/frontend_p1_capture.py (screenshots, geometry, perf tiers)
* combos     -- preview default-off / standard / p1 combination matrix only
* smoke      -- harness self-check: dist status + collect-only, no DB/browser run

A missing frontend dist is an explicit FAILURE (exit != 0 in
acceptance/capture/combos), never a skip. Output always goes to
docs/plans/frontend-rebuild-p1/; pointing --output at the P0 directory is
refused so P0 evidence can never be overwritten.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs/plans/frontend-rebuild-p1"
P0_OUTPUT = ROOT / "docs/plans/frontend-rebuild-p0"
sys.path.insert(0, str(ROOT))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def dist_status():
    info = {}
    for which in ("dist", "dist-p1"):
        d = ROOT / "frontend" / which
        info[which] = {
            "path": str(d.relative_to(ROOT)),
            "exists": d.is_dir(),
            "has_index": (d / "index.html").is_file() if d.is_dir() else False,
        }
    info["summary"] = "missing" if not all(
        v["exists"] and v["has_index"] for v in
        [info["dist"], info["dist-p1"]]) else "present"
    return info


class EvidencePlugin:
    def __init__(self, output, mode):
        self.output = output
        self.mode = mode
        self.reports = []
        self.database = None
        self.started = time.monotonic()

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
            "dist": dist_status(),
            "seconds": round(time.monotonic() - self.started, 2),
            "counts": counts, "reports": self.reports,
        })


def resolve_output(raw: Path) -> Path:
    output = raw.resolve()
    if output == P0_OUTPUT.resolve() or str(output).startswith(str(P0_OUTPUT.resolve()) + os.sep):
        print(f"ERROR: refusing to write P1 evidence into the P0 directory: {output}",
              file=sys.stderr)
        raise SystemExit(2)
    output.mkdir(parents=True, exist_ok=True)
    (output / "screenshots").mkdir(parents=True, exist_ok=True)
    (output / "traces").mkdir(parents=True, exist_ok=True)
    return output


def check_dist_or_fail(output: Path) -> int:
    status = dist_status()
    write_json(output / "dist-status.json", {
        "recorded_at": datetime.now(timezone.utc).isoformat(), **status})
    if status["summary"] == "missing":
        print("ERROR: frontend dist missing (frontend/dist/ and/or frontend/dist-p1/ "
              "with index.html not found).", flush=True)
        print("Build first: npm --prefix frontend run build && "
              "npm --prefix frontend run build:p1", flush=True)
        print("Missing dist is a FAILURE in CI; it is never skipped. "
              "Status recorded in dist-status.json.", flush=True)
        return 2
    return 0


def run_pytest(output, mode, target, only, extra_args=()):
    os.chdir(ROOT)
    for key in ("ANNOTATION_DB_DSN", "ANNOTATION_TEST_PG_ADMIN_DSN",
                "ANNOTATION_PG_BINDIR", "ANNOTATION_SPEED_EXPLAIN"):
        os.environ.pop(key, None)
    os.environ["FRONTEND_P1_OUTPUT"] = str(output)
    log_path = output / f"{mode}.log"
    print(f"Running {mode}; target: {target}; output: {output}", flush=True)
    import pytest
    with log_path.open("w") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        pytest_args = [target, "-q", "--tb=short", "-o", "addopts=",
                       f"--junitxml={output / (mode + '-junit.xml')}"]
        if only:
            pytest_args += ["-k", only]
        pytest_args += list(extra_args)
        result = pytest.main(pytest_args, plugins=[EvidencePlugin(output, mode)])
    results_path = output / f"{mode}-results.json"
    if results_path.is_file():
        print(results_path.read_text()[:650], flush=True)
    print(f"Detailed output: {log_path}", flush=True)
    return int(result)


def cmd_smoke(output) -> int:
    """Harness self-check without DB/browser: dist status + collect-only."""
    status = dist_status()
    write_json(output / "dist-status.json", {
        "recorded_at": datetime.now(timezone.utc).isoformat(), **status})
    os.chdir(ROOT)
    os.environ["FRONTEND_P1_OUTPUT"] = str(output)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "scripts/frontend_p1_capture.py",
         "--collect-only", "-q", "-o", "addopts="],
        capture_output=True, text=True, cwd=ROOT)
    collected = [ln for ln in proc.stdout.splitlines() if ln.startswith("scripts/")]
    # pytest --collect-only lists "<file>::<test>" lines; count test node ids.
    smoke = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "dist": status,
        "collect_only_exit": proc.returncode,
        "collected_tests": len(collected),
        "collect_output_tail": proc.stdout[-1500:],
        "collect_stderr_tail": proc.stderr[-500:] if proc.stderr else "",
    }
    write_json(output / "smoke.json", smoke)
    print(json.dumps({"dist": status["summary"],
                      "collect_only_exit": proc.returncode,
                      "collected_tests": len(collected)}), flush=True)
    if proc.returncode != 0:
        print("ERROR: capture file does not even collect; fix imports/selectors.", flush=True)
        print(proc.stdout[-2000:], flush=True)
        return 1
    if status["summary"] == "missing":
        print("SMOKE OK (harness collects); frontend dist absent as expected "
              "before work packages A-F land -- recorded, not hidden.", flush=True)
    else:
        print("SMOKE OK (harness collects, dist present).", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["acceptance", "capture", "combos", "smoke"])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--only", help="Optional pytest -k expression for a focused rerun")
    args = parser.parse_args()
    output = resolve_output(args.output)
    if args.mode == "smoke":
        return cmd_smoke(output)
    if args.mode == "acceptance":
        import glob as _glob
        targets = sorted(_glob.glob("tests/test_frontend_delivery.py") +
                         _glob.glob("tests/browser/test_frontend_p1_*.py"))
        if not targets:
            print("ERROR: no P1 test files yet (tests/test_frontend_delivery.py and "
                  "tests/browser/test_frontend_p1_*.py are work packages B-G deliverables).",
                  flush=True)
            print("This is an explicit failure, not a skip. Harness ready; "
                  "re-run after those files land.", flush=True)
            return 2
        dist_rc = check_dist_or_fail(output)
        if dist_rc:
            return dist_rc
        return run_pytest(output, "acceptance", " ".join(targets), args.only)
    if args.mode == "capture":
        dist_rc = check_dist_or_fail(output)
        if dist_rc:
            return dist_rc
        return run_pytest(output, "capture", "scripts/frontend_p1_capture.py", args.only)
    if args.mode == "combos":
        dist_rc = check_dist_or_fail(output)
        if dist_rc:
            return dist_rc
        return run_pytest(output, "combos", "scripts/frontend_p1_capture.py::test_p1_preview_combos",
                          args.only)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
