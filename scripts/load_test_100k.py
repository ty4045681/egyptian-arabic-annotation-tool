"""Synthetic 100k-task PostgreSQL capacity check.

Creates a uniquely named disposable database (never drops an arbitrary
configured DSN), seeds 100,000 tasks with baseline + draft/published
versions, approximately 300,000 current source associations, and measures
HTTP p50/p95/p99 through Gunicorn. Default workers/threads are 2x4, matching
the isolated 4-core/~8GB preview. Authentication is outside the timers.
New-claim samples require distinct newly assigned task IDs (not resumes).

Run from the repository root after other pytest/load jobs have finished:

    UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run python \\
      scripts/load_test_100k.py --artifact-dir docs/plans/load-test-100k

    UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run python \\
      scripts/load_test_100k.py --pg-bindir /usr/lib/postgresql/18/bin

Artifacts (reviewable): results.json, samples.jsonl, dataset.json,
explain-claim.json, explain-list.json, explain-overview.json.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TASKS = 100_000
ANNOTATED = 10_000
SKIPPED = 5_000
SCOPE_USERS = 20
CLAIM_USERS = 250
WARMUP_USERS = 4
TARGET_CLAIM_P95_MS = 500
TARGET_LIST_P95_MS = 500
TARGET_OVERVIEW_P95_MS = 2000
ADMIN_KEY = "load-test-admin-key-not-for-production"
NORMAL_SCENES = (
    "tourism_information", "clinic", "business_negotiation", "restaurant", "hotel",
)


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (p / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[low])
    return float(ordered[low] * (high - rank) + ordered[high] * (rank - low))


def summarize(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50_ms": percentile(values, 50),
        "p95_ms": percentile(values, 95),
        "p99_ms": percentile(values, 99),
        "max_ms": max(values) if values else None,
        "min_ms": min(values) if values else None,
        "mean_ms": (sum(values) / len(values)) if values else None,
    }


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def airport_confidence(index: int) -> str:
    remainder = index % 100
    if remainder < 90:
        return "high"
    if remainder < 98:
        return "medium"
    return "low"


def shopping_confidence(index: int) -> str:
    remainder = index % 100
    if remainder < 5:
        return "high"
    if remainder < 55:
        return "medium"
    if remainder < 95:
        return "low"
    return "unknown"


def mixed_confidence(index: int) -> str:
    remainder = index % 10
    if remainder < 4:
        return "high"
    if remainder < 7:
        return "medium"
    if remainder < 9:
        return "low"
    return "unknown"


class ApiClient:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
        )

    def request(self, method: str, path: str, body=None, headers=None,
                timeout: float = 120.0) -> tuple[int, dict, float]:
        payload = None
        hdrs = {"Accept": "application/json"}
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        request = urllib.request.Request(
            self.base + path, data=payload, headers=hdrs, method=method,
        )
        started = time.perf_counter()
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read()
                elapsed = (time.perf_counter() - started) * 1000
                parsed = json.loads(raw.decode("utf-8") or "{}")
                return int(response.status), parsed, elapsed
        except urllib.error.HTTPError as error:
            elapsed = (time.perf_counter() - started) * 1000
            raw = error.read()
            try:
                parsed = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                parsed = {"error": raw.decode("utf-8", errors="replace")}
            return int(error.code), parsed, elapsed


def copy_rows(cur, sql: str, rows) -> int:
    count = 0
    with cur.copy(sql) as copy:
        for row in rows:
            copy.write_row(row)
            count += 1
    return count


def seed(conn, *, task_ids, baseline_ids, working_ids, scope_user_ids,
         claim_user_ids, warmup_user_ids, batch_ids) -> dict:
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        user_rows = []
        for index, user_id in enumerate(scope_user_ids):
            user_rows.append((user_id, f"load-user-{index:02d}"))
        for index, user_id in enumerate(claim_user_ids):
            user_rows.append((user_id, f"load-claim-{index:03d}"))
        for index, user_id in enumerate(warmup_user_ids):
            user_rows.append((user_id, f"load-warm-{index:02d}"))
        copy_rows(cur, "COPY annotators (id, username) FROM STDIN", user_rows)

        task_rows = []
        for index in range(TASKS):
            if index < ANNOTATED:
                status = "annotated"
            elif index < ANNOTATED + SKIPPED:
                status = "skipped"
            else:
                status = "pending"
            created = origin + timedelta(seconds=index)
            task_rows.append((
                task_ids[index], f"load/{index:06d}.wav", f"{index:06d}.wav",
                "load", 12.0, status, True, index + 1, created, created,
            ))
        copy_rows(cur, """COPY annotation_tasks
            (id, rel_path, filename, folder, duration, status, eligible,
             allocation_order, created_at, updated_at) FROM STDIN""", task_rows)

        version_rows = []
        for index in range(TASKS):
            created = origin + timedelta(seconds=index)
            version_rows.append((
                baseline_ids[index], task_ids[index], 1, "baseline", "pending",
                0, None, None, created, created,
            ))
            published = index < ANNOTATED + SKIPPED
            target = "annotated" if index < ANNOTATED else (
                "skipped" if published else "pending"
            )
            submitter = scope_user_ids[index % SCOPE_USERS] if published else None
            submitted_at = created + timedelta(hours=1) if published else None
            version_rows.append((
                working_ids[index], task_ids[index], 2,
                "published" if published else "draft", target,
                0, submitter, submitted_at, created, created,
            ))
        copy_rows(cur, """COPY annotation_versions
            (id, task_id, version_no, lifecycle, target_status, revision,
             submitted_by_user_id, submitted_at, created_at, updated_at)
            FROM STDIN""", version_rows)

        conn.execute(
            """UPDATE annotation_tasks t
               SET baseline_version_id = v.id, baseline_quality = 'exact'
               FROM annotation_versions v
               WHERE v.task_id = t.id AND v.lifecycle = 'baseline'"""
        )
        conn.execute(
            """UPDATE annotation_tasks t
               SET current_published_version_id = v.id
               FROM annotation_versions v
               WHERE v.task_id = t.id AND v.lifecycle = 'published'"""
        )

        segment_rows = []
        for index in range(TASKS):
            text = "confirmed" if index < ANNOTATED else ""
            for version_id in (baseline_ids[index], working_ids[index]):
                segment_rows.append(
                    (version_id, 1, 0.0, 6.0, 6.0, "asr 1", text, False)
                )
                segment_rows.append(
                    (version_id, 2, 6.0, 12.0, 6.0, "asr 2", text, False)
                )
        copy_rows(cur, """COPY segments
            (version_id, segment_id, start_s, end_s, duration, asr_text, text,
             exclude_from_training) FROM STDIN""", segment_rows)

        def source_row(task_id, batch_id, scene, confidence, key, current=True):
            return (
                uuid.uuid4(), task_id, batch_id, key, 1 if current else 1,
                current, scene, confidence, "synthetic load evidence",
                "https://example.invalid/watch", f"vid-{key[:8]}", "youtube",
                "0" * 64,
            )

        source_rows = []
        historical = 0
        for index, task_id in enumerate(task_ids):
            if index % 20 == 0:
                continue
            batch_main, batch_air, batch_shop = batch_ids
            source_rows.append(source_row(
                task_id, batch_air, "airport", airport_confidence(index),
                f"{task_id}:airport",
            ))
            source_rows.append(source_row(
                task_id, batch_shop, "shopping", shopping_confidence(index),
                f"{task_id}:shopping",
            ))
            if index % 40 == 1:
                source_rows.append(source_row(
                    task_id, batch_main, None, "unknown", f"{task_id}:unknown",
                ))
            else:
                scene = NORMAL_SCENES[index % len(NORMAL_SCENES)]
                source_rows.append(source_row(
                    task_id, batch_main, scene, mixed_confidence(index),
                    f"{task_id}:{scene}",
                ))
            if index % 170 == 1:
                source_rows.append(source_row(
                    task_id, batch_main, "taxi", mixed_confidence(index + 3),
                    f"{task_id}:taxi",
                ))
            if index % 6 == 0:
                extra = NORMAL_SCENES[(index + 2) % len(NORMAL_SCENES)]
                source_rows.append(source_row(
                    task_id, batch_main, extra, mixed_confidence(index + 1),
                    f"{task_id}:{extra}:extra",
                ))
            if index < 8000:
                source_rows.append(source_row(
                    task_id, batch_main, "hotel", "low",
                    f"{task_id}:hotel:old", current=False,
                ))
                historical += 1
            if len(source_rows) >= 20_000:
                copy_rows(cur, """COPY task_sources
                    (id, task_id, batch_id, record_key, revision, is_current,
                     scene_code, confidence, confidence_basis, source_url,
                     video_id, provider, content_digest) FROM STDIN""", source_rows)
                source_rows = []
        if source_rows:
            copy_rows(cur, """COPY task_sources
                (id, task_id, batch_id, record_key, revision, is_current,
                 scene_code, confidence, confidence_basis, source_url,
                 video_id, provider, content_digest) FROM STDIN""", source_rows)

        prediction_rows = []
        for index in range(0, ANNOTATED, 1):
            if index >= 10_000:
                break
            prediction_rows.append((
                uuid.uuid4(), task_ids[index], working_ids[index], 0,
                "load-digest", "fixture-model", "v1",
                "Airport" if index % 2 == 0 else "Shopping",
                "airport" if index % 2 == 0 else "shopping",
            ))
        copy_rows(cur, """COPY task_scene_predictions
            (id, task_id, input_version_id, input_revision, input_digest,
             model_name, prompt_version, predicted_label, predicted_scene_code)
            FROM STDIN""", prediction_rows)

        review_rows = []
        label_rows = []
        for index in range(0, ANNOTATED, 2):
            review_id = uuid.uuid4()
            review_rows.append((
                review_id, working_ids[index], 1, "confirmed",
                scope_user_ids[index % SCOPE_USERS], "annotator", False,
            ))
            label_rows.append((review_id, "airport"))
        copy_rows(cur, """COPY scene_reviews
            (id, version_id, review_no, status, actor_user_id, actor_kind,
             superseded) FROM STDIN""", review_rows)
        copy_rows(cur, """COPY scene_review_labels (review_id, scene_code)
            FROM STDIN""", label_rows)

        for index, user_id in enumerate(scope_user_ids):
            if index <= 9:
                continue
            if index <= 14:
                mode, codes, allow_unknown = "restricted", ["airport"], False
            elif index <= 17:
                mode, codes, allow_unknown = "restricted", ["shopping"], False
            elif index == 18:
                mode, codes, allow_unknown = "restricted", ["taxi"], False
            else:
                mode, codes, allow_unknown = "none", [], False
            cur.execute(
                """INSERT INTO annotator_scene_scopes
                       (user_id, mode, allow_unknown, revision)
                   VALUES (%s, %s, %s, 0)
                   ON CONFLICT (user_id) DO UPDATE
                     SET mode = EXCLUDED.mode,
                         allow_unknown = EXCLUDED.allow_unknown""",
                (user_id, mode, allow_unknown),
            )
            for code in codes:
                cur.execute(
                    """INSERT INTO annotator_scene_access (user_id, scene_code)
                       VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                    (user_id, code),
                )
        if SCOPE_USERS > 9:
            cur.execute(
                """UPDATE annotator_scene_scopes SET allow_unknown = true
                   WHERE user_id = %s""",
                (scope_user_ids[9],),
            )
        cur.execute("ANALYZE")
    conn.commit()
    return {"historical_sources": historical}


def dataset_invariants(conn) -> dict:
    def count(sql: str, params=None) -> int:
        return int(conn.execute(sql, params or ()).fetchone()[0])

    payload = {
        "tasks": count("SELECT count(*) FROM annotation_tasks"),
        "pending": count(
            "SELECT count(*) FROM annotation_tasks WHERE status = 'pending'"
        ),
        "annotated": count(
            "SELECT count(*) FROM annotation_tasks WHERE status = 'annotated'"
        ),
        "skipped": count(
            "SELECT count(*) FROM annotation_tasks WHERE status = 'skipped'"
        ),
        "baselines": count(
            "SELECT count(*) FROM annotation_versions WHERE lifecycle = 'baseline'"
        ),
        "drafts": count(
            "SELECT count(*) FROM annotation_versions WHERE lifecycle = 'draft'"
        ),
        "published": count(
            "SELECT count(*) FROM annotation_versions WHERE lifecycle = 'published'"
        ),
        "pending_without_draft": count(
            """SELECT count(*) FROM annotation_tasks t
               WHERE t.status = 'pending' AND NOT EXISTS (
                   SELECT 1 FROM annotation_versions v
                   WHERE v.task_id = t.id AND v.lifecycle = 'draft')"""
        ),
        "tasks_without_baseline": count(
            "SELECT count(*) FROM annotation_tasks WHERE baseline_version_id IS NULL"
        ),
        "segments": count("SELECT count(*) FROM segments"),
        "sources": count("SELECT count(*) FROM task_sources"),
        "sources_current": count(
            "SELECT count(*) FROM task_sources WHERE is_current"
        ),
        "emergencies_current": count(
            """SELECT count(*) FROM task_sources
               WHERE is_current AND scene_code = 'emergencies'"""
        ),
        "taxi_current_tasks": count(
            """SELECT count(DISTINCT task_id) FROM task_sources
               WHERE is_current AND scene_code = 'taxi'"""
        ),
        "airport_high_current": count(
            """SELECT count(*) FROM task_sources
               WHERE is_current AND scene_code = 'airport' AND confidence = 'high'"""
        ),
        "shopping_high_current": count(
            """SELECT count(*) FROM task_sources
               WHERE is_current AND scene_code = 'shopping' AND confidence = 'high'"""
        ),
        "predictions": count("SELECT count(*) FROM task_scene_predictions"),
        "reviews": count("SELECT count(*) FROM scene_reviews"),
        "users": count("SELECT count(*) FROM annotators"),
        "database_bytes": count("SELECT pg_database_size(current_database())"),
    }
    if payload["tasks"] != TASKS:
        raise RuntimeError(f"expected {TASKS} tasks, got {payload['tasks']}")
    if payload["tasks_without_baseline"] != 0:
        raise RuntimeError("every task must have a baseline version")
    if payload["pending_without_draft"] != 0:
        raise RuntimeError("every pending task must have a draft version")
    if payload["emergencies_current"] != 0:
        raise RuntimeError("emergencies must stay empty for the empty-scene case")
    if not (280_000 <= payload["sources_current"] <= 330_000):
        raise RuntimeError(
            f"current sources {payload['sources_current']} not ~300000"
        )
    if not (400 <= payload["taxi_current_tasks"] <= 900):
        raise RuntimeError(
            f"taxi task count {payload['taxi_current_tasks']} is not a narrow scene"
        )
    return payload


def wait_healthy(client: ApiClient) -> None:
    last = None
    for _ in range(80):
        try:
            status, body, _ = client.request("GET", "/api/health", timeout=5)
            last = (status, body)
            if status == 200 and body.get("ok"):
                return
        except Exception as exc:  # noqa: BLE001 — startup probe
            last = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"gunicorn did not become healthy: {last}")


def login_annotator(base: str, username: str) -> ApiClient:
    client = ApiClient(base)
    status, body, _ = client.request("POST", "/api/login", {"username": username})
    if status != 200 or not body.get("success"):
        raise RuntimeError(f"login failed for {username}: {status} {body}")
    return client


def login_admin(base: str, key: str) -> ApiClient:
    client = ApiClient(base)
    status, body, _ = client.request("POST", "/api/admin/login", {"key": key})
    if status != 200 or not body.get("authenticated"):
        raise RuntimeError(f"admin login failed: {status} {body}")
    return client


def timed_claim(client: ApiClient, filters: dict | None = None) -> dict:
    status, body, ms = client.request(
        "POST", "/api/assignment/claim", filters or {},
    )
    return {
        "http_status": status,
        "ms": ms,
        "assigned": bool(body.get("assigned")),
        "resumed": bool(body.get("resumed")),
        "task_id": body.get("task_id"),
        "pool_reason": (body.get("pool") or {}).get("reason"),
        "error": body.get("error"),
    }


def record_samples(path: Path, scenario: str, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({"scenario": scenario, **row}, default=str) + "\n")


def explain_to_file(conn, path: Path, title: str, sql: str, params) -> dict:
    plan = conn.execute(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params,
    ).fetchone()[0]
    payload = {"title": title, "sql": sql, "plan": plan}
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    execution = None
    if isinstance(plan, list) and plan:
        execution = plan[0].get("Execution Time")
    return {"title": title, "execution_ms": execution, "path": str(path)}


def db_lock_snapshot(conn) -> dict:
    row = conn.execute(
        """SELECT deadlocks, conflicts, tup_fetched, tup_returned,
                  blks_read, blks_hit
           FROM pg_stat_database WHERE datname = current_database()"""
    ).fetchone()
    waits = conn.execute(
        """SELECT wait_event_type, wait_event, count(*)
           FROM pg_stat_activity
           WHERE datname = current_database()
           GROUP BY 1, 2 ORDER BY 3 DESC"""
    ).fetchall()
    return {
        "deadlocks": row[0],
        "conflicts": row[1],
        "tup_fetched": row[2],
        "tup_returned": row[3],
        "blks_read": row[4],
        "blks_hit": row[5],
        "wait_events": [
            {"wait_event_type": item[0], "wait_event": item[1], "count": int(item[2])}
            for item in waits
        ],
    }


def run_new_claim_scenario(base: str, usernames: list[str], filters: dict,
                           samples_path: Path, scenario: str) -> dict:
    workers = max(1, min(20, len(usernames)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        clients = list(pool.map(lambda name: login_annotator(base, name), usernames))
    start_barrier = threading.Barrier(workers, timeout=30)

    def _one(index: int) -> dict:
        client = clients[index]
        # Synchronize the first wave; later partial waves must not wait for
        # nonexistent peers when --claim-samples isn't divisible by 20.
        if index < workers:
            start_barrier.wait()
        sample = timed_claim(client, filters)
        sample["username"] = usernames[index]
        return sample

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(_one, range(len(usernames))))
    wall_ms = (time.perf_counter() - started) * 1000
    errors = 0
    resumed = 0
    task_ids = []
    for sample in rows:
        if sample["http_status"] != 200 or not sample["assigned"]:
            errors += 1
        if sample["resumed"]:
            resumed += 1
        if sample["task_id"]:
            task_ids.append(sample["task_id"])
    record_samples(samples_path, scenario, rows)
    unique = len(set(task_ids))
    latencies = [row["ms"] for row in rows if row["http_status"] == 200 and row["assigned"]]
    return {
        "scenario": scenario,
        "filters": filters,
        "concurrent_clients": workers,
        "authentication": "completed before timed concurrent workload",
        "wall_ms": wall_ms,
        "throughput_per_s": unique / (wall_ms / 1000) if wall_ms else None,
        "error_count": errors,
        "error_rate": errors / len(rows) if rows else None,
        "resumed_count": resumed,
        "assigned_task_ids": unique,
        "repeat_task_ids": len(task_ids) - unique,
        "latency": summarize(latencies),
        "http_statuses": sorted({row["http_status"] for row in rows}),
        "ok": (
            errors == 0 and resumed == 0 and unique == len(usernames)
            and unique == len(task_ids)
        ),
    }


class LockWaitSampler:
    """Observe waits during HTTP load; short waits can fall between samples."""

    def __init__(self, dsn: str, interval: float = 0.1):
        self.dsn, self.interval = dsn, interval
        self.rows, self.errors = [], []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self):
        import psycopg
        try:
            with psycopg.connect(self.dsn, autocommit=True,
                                 application_name='capacity-wait-monitor') as conn:
                while not self.stop_event.is_set():
                    rows = conn.execute(
                        """SELECT state, wait_event_type, wait_event, count(*)
                           FROM pg_stat_activity
                           WHERE datname = current_database()
                             AND pid <> pg_backend_pid()
                           GROUP BY state, wait_event_type, wait_event"""
                    ).fetchall()
                    self.rows.append({
                        'at': datetime.now(timezone.utc).isoformat(),
                        'events': [dict(zip(('state', 'type', 'event', 'count'), r))
                                   for r in rows],
                        'lock_waiters': sum(r[3] for r in rows if r[1] == 'Lock'),
                    })
                    self.stop_event.wait(self.interval)
        except Exception as exc:
            self.errors.append(type(exc).__name__)

    def start(self):
        self.thread.start()

    def finish(self, artifact_dir: Path) -> dict:
        self.stop_event.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            self.errors.append('sampler_thread_did_not_stop')
        report = {
            'interval_seconds': self.interval,
            'sample_count': len(self.rows),
            'samples_with_lock_waiters': sum(r['lock_waiters'] > 0 for r in self.rows),
            'max_observed_lock_waiters': max((r['lock_waiters'] for r in self.rows), default=0),
            'errors': self.errors,
            'note': 'Samples taken during HTTP workload; waits shorter than the sampling interval can be missed. Counts do not measure exact wait durations.',
        }
        (artifact_dir / 'lock-waits.json').write_text(
            json.dumps({**report, 'samples': self.rows}, indent=2) + '\n')
        return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Isolated 100k-task capacity measurement. Creates a unique "
            "disposable database; never drops an arbitrary configured DSN. "
            "DSN passwords belong in PGPASSWORD/~/.pgpass, not argv."
        )
    )
    parser.add_argument("--pg-bindir", help="PostgreSQL binary directory (16 or 18)")
    parser.add_argument(
        "--artifact-dir",
        default=str(ROOT / "docs/plans/load-test-100k"),
        help="Directory for results.json, samples.jsonl, EXPLAIN plans",
    )
    parser.add_argument("--keep", action="store_true",
                        help="Retain the throwaway cluster/database and print paths")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--claim-samples", type=int, default=80)
    parser.add_argument("--list-samples", type=int, default=40)
    parser.add_argument("--overview-samples", type=int, default=40)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--profile-overview",
        action="store_true",
        help="After seed, time one admin_overview in-process and exit",
    )
    args = parser.parse_args()
    if min(args.claim_samples, args.list_samples, args.overview_samples,
           args.workers, args.threads) < 1:
        parser.error('sample counts, workers and threads must be positive')
    needed = args.claim_samples + 40 + 40 + 20 + 1 + 20 + 1
    if needed > CLAIM_USERS:
        raise SystemExit("claim sample total exceeds dedicated claim users")

    artifact_dir = Path(args.artifact_dir).expanduser().resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    lock_path = artifact_dir / ".load_test_100k.lock"
    lock_handle = lock_path.open("w")
    try:
        import fcntl
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise SystemExit(
            f"another load_test_100k.py run holds {lock_path}: {exc}"
        ) from exc

    os.environ.pop("ANNOTATION_DB_DSN", None)

    if args.pg_bindir:
        from tests.pg_runtime import apply_pgserver_bindir
        apply_pgserver_bindir(args.pg_bindir)

    import pgserver
    import pgserver.postgres_server as postgres_server
    import psycopg

    parent = Path(tempfile.mkdtemp(prefix="annotation-load-100k-"))
    pgdata = parent / "pgdata"
    cluster = pgserver.get_server(
        pgdata, cleanup_mode=None if args.keep else "delete",
    )
    db_name = "load_e_" + uuid.uuid4().hex
    admin_dsn = cluster.get_uri(database="postgres")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    dsn = cluster.get_uri(database=db_name)
    os.environ["ANNOTATION_DB_DSN"] = dsn
    os.environ["ANNOTATION_ADMIN_KEY_SHA256"] = hashlib.sha256(
        ADMIN_KEY.encode()
    ).hexdigest()
    os.environ["ANNOTATION_ADMIN_COOKIE_SECURE"] = "false"
    os.environ["GUNICORN_WORKERS"] = str(args.workers)
    os.environ["GUNICORN_THREADS"] = str(args.threads)

    from db import apply_migrations, close_pool, db_conn

    task_ids = [uuid.uuid4() for _ in range(TASKS)]
    baseline_ids = [uuid.uuid4() for _ in range(TASKS)]
    working_ids = [uuid.uuid4() for _ in range(TASKS)]
    scope_user_ids = [uuid.uuid4() for _ in range(SCOPE_USERS)]
    claim_user_ids = [uuid.uuid4() for _ in range(CLAIM_USERS)]
    warmup_user_ids = [uuid.uuid4() for _ in range(WARMUP_USERS)]
    batch_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]

    print(f"Seeding {TASKS:,} tasks into {db_name} ...", flush=True)
    seed_started = time.perf_counter()
    with db_conn() as conn:
        apply_migrations(conn)
        conn.commit()
        conn.execute(
            """INSERT INTO source_batches (id, batch_code, name)
               VALUES (%s, 'crawler-2026-09-14-0914', 'load main'),
                      (%s, 'crawler-2026-09-14-airport', 'load airport'),
                      (%s, 'crawler-2026-09-14-shopping', 'load shopping')""",
            batch_ids,
        )
        seed(conn, task_ids=task_ids, baseline_ids=baseline_ids,
             working_ids=working_ids, scope_user_ids=scope_user_ids,
             claim_user_ids=claim_user_ids, warmup_user_ids=warmup_user_ids,
             batch_ids=batch_ids)
        dataset = dataset_invariants(conn)
    seed_ms = (time.perf_counter() - seed_started) * 1000
    print(
        f"Seeded in {seed_ms/1000:.1f}s; current sources="
        f"{dataset['sources_current']:,}",
        flush=True,
    )
    (artifact_dir / "dataset.json").write_text(
        json.dumps(dataset, indent=2), encoding="utf-8",
    )

    if args.profile_overview:
        import annotation_repository as repo
        os.environ["ANNOTATION_OVERVIEW_TRACE"] = "1"
        started = time.perf_counter()
        payload = repo.admin_overview({})
        print(
            json.dumps({
                "overview_ms": (time.perf_counter() - started) * 1000,
                "total_audio_count": payload["totals"]["total_audio_count"],
                "source_scenes": len(payload.get("source_scenes") or []),
                "confidence_buckets": payload.get("confidence_buckets"),
            }, indent=2, default=str),
            flush=True,
        )
        close_pool()
        if not args.keep:
            with psycopg.connect(admin_dsn, autocommit=True) as conn:
                try:
                    conn.execute(f'DROP DATABASE "{db_name}" WITH (FORCE)')
                except psycopg.Error:
                    conn.execute(f'DROP DATABASE "{db_name}"')
            cluster.cleanup()
        return 0

    pg_version = subprocess.check_output(
        [str(Path(postgres_server.POSTGRES_BIN_PATH) / "postgres"), "--version"],
        text=True,
    ).strip()

    with db_conn() as conn:
        from annotation_metadata.claiming import (
            claim_lock_queries, parse_claim_filters,
        )
        from annotation_metadata.queries import load_scope
        from annotation_repository import (
            _admin_task_filter_sql, _normalize_admin_filters,
        )
        explain_meta = []
        with conn.cursor() as cur:
            scope = load_scope(cur, claim_user_ids[0])
            filters = parse_claim_filters()
            for label, sql, params in claim_lock_queries(
                scope, filters, claim_user_ids[0], "source_confidence",
            )[:3]:
                explain_meta.append(explain_to_file(
                    conn,
                    artifact_dir / f"explain-claim-{label.replace(':', '_')}.json",
                    f"claim {label}",
                    sql,
                    params,
                ))
            airport_filters = parse_claim_filters(source_scene="airport")
            for label, sql, params in claim_lock_queries(
                scope, airport_filters, claim_user_ids[0], "source_confidence",
            ):
                if label == "band:high":
                    explain_meta.append(explain_to_file(
                        conn, artifact_dir / "explain-claim-airport-high.json",
                        "claim airport high band", sql, params,
                    ))
            empty_filters = parse_claim_filters(source_scene="emergencies")
            for label, sql, params in claim_lock_queries(
                scope, empty_filters, claim_user_ids[0], "source_confidence",
            ):
                if label == "band:high":
                    explain_meta.append(explain_to_file(
                        conn, artifact_dir / "explain-claim-emergencies-high.json",
                        "claim empty scene high band", sql, params,
                    ))
            normalized = _normalize_admin_filters({})
            where, params = _admin_task_filter_sql(normalized)
            explain_meta.append(explain_to_file(
                conn, artifact_dir / "explain-list.json",
                "admin 50-row list",
                f"""SELECT t.id FROM annotation_tasks t
                    LEFT JOIN annotation_versions v
                      ON v.id = t.current_published_version_id
                    WHERE {where}
                    ORDER BY t.created_at DESC, t.id DESC LIMIT 51""",
                params,
            ))
            explain_meta.append(explain_to_file(
                conn, artifact_dir / "explain-overview-matched.json",
                "overview matched snapshot",
                f"""SELECT t.id, t.duration, t.status
                    FROM annotation_tasks t
                    LEFT JOIN annotation_versions v
                      ON v.id = t.current_published_version_id
                    WHERE {where}""",
                params,
            ))
        conn.rollback()

    port = args.port or free_port()
    base = f"http://127.0.0.1:{port}"
    gunicorn = subprocess.Popen(
        [
            sys.executable, "-m", "gunicorn",
            "-c", str(ROOT / "gunicorn_config.py"),
            "-w", str(args.workers),
            "--threads", str(args.threads),
            "-b", f"127.0.0.1:{port}",
            "--access-logfile", str(artifact_dir / "gunicorn-access.log"),
            "--error-logfile", str(artifact_dir / "gunicorn-error.log"),
            "--capture-output",
            "server:app",
        ],
        cwd=str(ROOT),
        env=os.environ.copy(),
    )
    samples_path = artifact_dir / "samples.jsonl"
    if samples_path.exists():
        samples_path.unlink()
    results = {
        "environment": {
            "postgres": pg_version,
            "bindir": str(postgres_server.POSTGRES_BIN_PATH),
            "database": db_name,
            "pgdata": str(pgdata),
            "workers": args.workers,
            "threads": args.threads,
            "gunicorn_config": "gunicorn_config.py",
            "bind": f"127.0.0.1:{port}",
            "note": (
                "Measured with 2 workers x 4 threads unless overridden; "
                "that matches the isolated 4-core/~8GB preview. "
                "gunicorn_config.py defaults to 4 workers on larger hosts. "
                "Authentication is outside the request timer. "
                "HTTP timings include application work (claim SQL, metadata, JSON)."
            ),
        },
        "dataset": dataset,
        "seed_ms": seed_ms,
        "explain": explain_meta,
        "targets": {
            "claim_new_p95_ms": TARGET_CLAIM_P95_MS,
            "list_50_p95_ms": TARGET_LIST_P95_MS,
            "overview_100k_p95_ms": TARGET_OVERVIEW_P95_MS,
        },
    }
    wait_sampler = LockWaitSampler(dsn)
    wait_sampler.start()
    try:
        probe = ApiClient(base)
        wait_healthy(probe)
        for username in [f"load-warm-{index:02d}" for index in range(WARMUP_USERS)]:
            client = login_annotator(base, username)
            timed_claim(client, {})

        claim_cursor = 0

        def take_users(count: int) -> list[str]:
            nonlocal claim_cursor
            names = [
                f"load-claim-{index:03d}"
                for index in range(claim_cursor, claim_cursor + count)
            ]
            claim_cursor += count
            return names

        results["claim_normal"] = run_new_claim_scenario(
            base, take_users(args.claim_samples), {}, samples_path, "claim_new_normal",
        )
        results["claim_high_heavy"] = run_new_claim_scenario(
            base, take_users(40), {"source_scene": "airport"},
            samples_path, "claim_new_airport_high_heavy",
        )
        results["claim_high_rare"] = run_new_claim_scenario(
            base, take_users(40), {"source_scene": "shopping"},
            samples_path, "claim_new_shopping_high_rare",
        )
        results["claim_narrow"] = run_new_claim_scenario(
            base, take_users(20), {"source_scene": "taxi"},
            samples_path, "claim_new_taxi_narrow",
        )

        empty_client = login_annotator(base, take_users(1)[0])
        empty_sample = timed_claim(empty_client, {"source_scene": "emergencies"})
        record_samples(samples_path, "claim_empty_emergencies", [empty_sample])
        results["claim_empty"] = {
            "sample": empty_sample,
            "ok": (
                empty_sample["http_status"] == 409
                and empty_sample["pool_reason"] == "no_matching_scene"
                and not empty_sample["assigned"]
            ),
        }

        with db_conn() as conn:
            before_locks = db_lock_snapshot(conn)
            locker = conn
            with locker.cursor() as cur:
                cur.execute(
                    """SELECT t.id FROM annotation_tasks t
                       JOIN annotation_versions v
                         ON v.task_id = t.id AND v.lifecycle = 'draft'
                       JOIN task_sources src
                         ON src.task_id = t.id AND src.is_current
                        AND src.scene_code = 'taxi'
                       WHERE t.status = 'pending' AND t.eligible
                       FOR UPDATE OF t"""
                )
                locked = len(cur.fetchall())
            busy_client = login_annotator(base, "load-user-18")
            busy_sample = timed_claim(
                busy_client, {"source_scene": "taxi"},
            )
            record_samples(samples_path, "claim_lock_busy", [busy_sample])
            locker.rollback()
            after_locks = db_lock_snapshot(conn)
        results["claim_busy"] = {
            "locked_rows": locked,
            "sample": busy_sample,
            "ok": (
                busy_sample["http_status"] == 409
                and busy_sample["pool_reason"] == "temporarily_busy"
                and not busy_sample["assigned"]
            ),
            "locks_before": before_locks,
            "locks_after": after_locks,
        }

        results["claim_concurrent"] = run_new_claim_scenario(
            base, take_users(20), {}, samples_path, "claim_concurrent_20",
        )
        scoped_names = [f'load-user-{i:02d}' for i in range(19)] + take_users(1)
        results['claim_mixed_scopes'] = run_new_claim_scenario(
            base, scoped_names, {}, samples_path, 'claim_mixed_scopes_20',
        )
        with db_conn() as conn:
            scope_rows = conn.execute(
                """SELECT u.username, a.task_id, COALESCE(sc.mode, 'all'),
                          CASE WHEN COALESCE(sc.mode, 'all') = 'all' THEN true
                               WHEN sc.mode = 'none' THEN false
                               WHEN src.scene_code IS NULL THEN sc.allow_unknown
                               ELSE EXISTS (SELECT 1 FROM annotator_scene_access sa
                                   WHERE sa.user_id=u.id AND sa.scene_code=src.scene_code)
                          END AS allowed
                   FROM annotators u
                   LEFT JOIN assignments a ON a.user_id=u.id
                   LEFT JOIN annotator_scene_scopes sc ON sc.user_id=u.id
                   LEFT JOIN task_sources src ON src.id=a.claim_source_id
                   WHERE u.username = ANY(%s)""", (scoped_names,),
            ).fetchall()
        scope_ok = len(scope_rows) == 20 and all(r[1] is not None and r[3] for r in scope_rows)
        results['claim_mixed_scopes']['scope_validated'] = scope_ok
        results['claim_mixed_scopes']['scope_modes'] = {
            mode: sum(r[2] == mode for r in scope_rows) for mode in ('all', 'restricted', 'none')
        }
        results['claim_mixed_scopes']['ok'] &= scope_ok
        none_sample = timed_claim(login_annotator(base, 'load-user-19'), {})
        record_samples(samples_path, 'claim_scope_none', [none_sample])
        results['claim_scope_none'] = {
            'sample': none_sample,
            'ok': (none_sample['http_status'] == 409 and not none_sample['assigned']
                   and none_sample['pool_reason'] == 'no_scene_access'),
        }

        admin = login_admin(base, ADMIN_KEY)
        for _ in range(8):
            admin.request("GET", "/api/admin/overview")
            admin.request("GET", "/api/admin/tasks?limit=50")
        list_rows = []
        for _ in range(args.list_samples):
            status, body, ms = admin.request("GET", "/api/admin/tasks?limit=50")
            list_rows.append({
                "http_status": status, "ms": ms,
                "item_count": len((body or {}).get("items") or []),
                "matched_count": (body or {}).get("matched_count"),
            })
        record_samples(samples_path, "admin_list_50", list_rows)
        results["list_50"] = {
            "latency": summarize([row["ms"] for row in list_rows if row["http_status"] == 200]),
            "error_count": sum(1 for row in list_rows if row["http_status"] != 200),
            "payload_error_count": sum(1 for row in list_rows
                                       if row['item_count'] != 50 or row['matched_count'] != TASKS),
            "item_count": list_rows[0]["item_count"] if list_rows else 0,
            "matched_count": list_rows[0]["matched_count"] if list_rows else None,
        }
        overview_rows = []
        for _ in range(args.overview_samples):
            status, body, ms = admin.request("GET", "/api/admin/overview")
            overview_rows.append({
                "http_status": status, "ms": ms,
                "total": ((body or {}).get("totals") or {}).get("total_audio_count"),
            })
        record_samples(samples_path, "admin_overview_100k", overview_rows)
        results["overview_100k"] = {
            "latency": summarize(
                [row["ms"] for row in overview_rows if row["http_status"] == 200]
            ),
            "error_count": sum(1 for row in overview_rows if row["http_status"] != 200),
            "payload_error_count": sum(1 for row in overview_rows if row['total'] != TASKS),
            "total_audio_count": overview_rows[0]["total"] if overview_rows else None,
        }

        with db_conn() as conn:
            results["final_invariants"] = dataset_invariants(conn)
            results["final_invariants"]["assignments"] = int(
                conn.execute("SELECT count(*) FROM assignments").fetchone()[0]
            )
            results["lock_snapshot_final"] = db_lock_snapshot(conn)
    finally:
        results['wait_sampling'] = wait_sampler.finish(artifact_dir)
        gunicorn.terminate()
        try:
            gunicorn.wait(timeout=20)
        except subprocess.TimeoutExpired:
            gunicorn.kill()
            gunicorn.wait(timeout=10)
        close_pool()
        if not args.keep:
            with psycopg.connect(admin_dsn, autocommit=True) as conn:
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (db_name,),
                )
                try:
                    conn.execute(f'DROP DATABASE "{db_name}" WITH (FORCE)')
                except psycopg.Error:
                    conn.execute(f'DROP DATABASE "{db_name}"')
            cluster.cleanup()
        else:
            results["retained"] = {"pgdata": str(pgdata), "database": db_name, "dsn": dsn}
            print(
                f"RETAINED artifacts: pgdata={pgdata} database={db_name}",
                flush=True,
            )

    claim_p95 = (results.get("claim_normal") or {}).get("latency", {}).get("p95_ms")
    list_p95 = (results.get("list_50") or {}).get("latency", {}).get("p95_ms")
    overview_p95 = (results.get("overview_100k") or {}).get("latency", {}).get("p95_ms")
    results["pass"] = {
        "claim_new_p95": (
            claim_p95 is not None and claim_p95 <= TARGET_CLAIM_P95_MS
            and results["claim_normal"]["ok"]
        ),
        "list_50_p95": (list_p95 is not None and list_p95 <= TARGET_LIST_P95_MS
                        and results['list_50']['error_count'] == 0
                        and results['list_50']['payload_error_count'] == 0),
        "overview_100k_p95": (
            overview_p95 is not None and overview_p95 <= TARGET_OVERVIEW_P95_MS
            and results['overview_100k']['error_count'] == 0
            and results['overview_100k']['payload_error_count'] == 0
        ),
        "empty_scene": results.get("claim_empty", {}).get("ok"),
        "busy": results.get("claim_busy", {}).get("ok"),
        "concurrent_unique": results.get("claim_concurrent", {}).get("ok"),
        "high_heavy_unique": results.get("claim_high_heavy", {}).get("ok"),
        "high_rare_unique": results.get("claim_high_rare", {}).get("ok"),
        "narrow_unique": results.get("claim_narrow", {}).get("ok"),
        "mixed_scopes": results.get('claim_mixed_scopes', {}).get('ok'),
        "scope_none": results.get('claim_scope_none', {}).get('ok'),
        "wait_sampling": (results.get('wait_sampling', {}).get('sample_count', 0) > 0
                          and not results.get('wait_sampling', {}).get('errors')),
    }
    results["pass"]["all_gates"] = all(bool(value) for value in results["pass"].values())
    (artifact_dir / "results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8",
    )
    print(json.dumps({
        "artifacts": str(artifact_dir),
        "claim_normal_p95_ms": claim_p95,
        "list_50_p95_ms": list_p95,
        "overview_100k_p95_ms": overview_p95,
        "pass": results["pass"],
    }, indent=2), flush=True)
    if results["pass"]["all_gates"]:
        print("LOAD_TEST_100K_OK", flush=True)
        return 0
    print("LOAD_TEST_100K_FAILED", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
