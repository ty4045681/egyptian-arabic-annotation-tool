"""Synthetic 100k-task PostgreSQL capacity check.

Creates 100,000 tasks with two segments each using COPY, then measures the
steady-state SQL paths used by the app and runs 20 concurrent claims.
Run: uv run python scripts/load_test_100k.py
"""

from __future__ import annotations

import concurrent.futures
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pgserver
import psycopg

TASKS = 100_000
ANNOTATED = 10_000
SKIPPED = 5_000
USERS = 20


def timed(label, fn):
    start = time.perf_counter()
    value = fn()
    elapsed = (time.perf_counter() - start) * 1000
    print(f"{label}: {elapsed:.1f} ms")
    return value, elapsed


def main():
    pg = pgserver.get_server(".pgdata/load-100k")
    admin = pg.get_uri(database="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS load_100k")
        conn.execute("CREATE DATABASE load_100k")
    os.environ["ANNOTATION_DB_DSN"] = pg.get_uri(database="load_100k")

    from db import apply_migrations, db_conn, close_pool
    import annotation_repository as repo

    with db_conn() as conn:
        apply_migrations(conn)
        user_ids = [uuid.uuid4() for _ in range(USERS)]
        with conn.cursor() as cur:
            with cur.copy("COPY annotators (id, username) FROM STDIN") as copy:
                for i, user_id in enumerate(user_ids):
                    copy.write_row((user_id, f"load-user-{i:02d}"))

        task_ids = [uuid.uuid4() for _ in range(TASKS)]
        version_ids = [uuid.uuid4() for _ in range(TASKS)]
        with conn.cursor() as cur:
            with cur.copy("""COPY annotation_tasks
                (id, rel_path, filename, folder, duration, status, eligible,
                 allocation_order, created_at, updated_at)
                FROM STDIN""") as copy:
                for i in range(TASKS):
                    status = "annotated" if i < ANNOTATED else "skipped" if i < ANNOTATED + SKIPPED else "pending"
                    copy.write_row((task_ids[i], f"batch/{i:06d}.wav", f"{i:06d}.wav", "batch", 60.0, status, True, i + 1, "2026-01-01", "2026-01-01"))
            with cur.copy("""COPY annotation_versions
                (id, task_id, version_no, lifecycle, target_status, revision,
                 submitted_by_user_id, submitted_at, created_at, updated_at)
                FROM STDIN""") as copy:
                for i in range(TASKS):
                    published = i < ANNOTATED + SKIPPED
                    status = "annotated" if i < ANNOTATED else "skipped" if published else "pending"
                    copy.write_row((version_ids[i], task_ids[i], 1, "published" if published else "draft", status, 0, user_ids[i % USERS] if published else None, "2026-01-02" if published else None, "2026-01-01", "2026-01-02"))
            with cur.copy("""COPY segments
                (version_id, segment_id, start_s, end_s, duration, asr_text,
                 text, exclude_from_training, extra) FROM STDIN""") as copy:
                for i in range(TASKS):
                    text = "confirmed" if i < ANNOTATED else ""
                    copy.write_row((version_ids[i], 1, 0.0, 30.0, 30.0, "asr 1", text, False, "{}"))
                    copy.write_row((version_ids[i], 2, 30.0, 60.0, 30.0, "asr 2", text, False, "{}"))
        conn.execute("""UPDATE annotation_tasks t SET current_published_version_id = v.id
                        FROM annotation_versions v
                        WHERE v.task_id = t.id AND v.lifecycle = 'published'""")
        conn.execute("ANALYZE")
        conn.commit()

    print(f"Loaded {TASKS:,} tasks / {TASKS*2:,} segments")
    dash, dash_ms = timed("dashboard", repo.dashboard)
    pool, pool_ms = timed("pool_state", repo.pool_state)
    completed, completed_ms = timed("completed_list page", lambda: repo.completed_list(user_ids[0], limit=25))
    assert dash["stats"] == {"total": TASKS, "annotated": ANNOTATED, "skipped": SKIPPED, "pending": TASKS - ANNOTATED - SKIPPED, "percent_complete": 15.0}
    assert pool["available"] == TASKS - ANNOTATED - SKIPPED
    assert len(completed["items"]) == 25

    def claim(uid):
        return repo.claim(uid)["task_id"]

    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=USERS) as executor:
        claimed = list(executor.map(claim, user_ids))
    claim_ms = (time.perf_counter() - start) * 1000
    print(f"20 concurrent claims: {claim_ms:.1f} ms")
    assert len(set(claimed)) == USERS

    with db_conn() as conn:
        db_size = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        plan = conn.execute("""EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            SELECT t.id FROM annotation_tasks t
            JOIN annotation_versions v ON v.task_id=t.id AND v.lifecycle='draft'
            WHERE t.status='pending' AND t.eligible
              AND t.reserved_for_user_id IS NULL
              AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.task_id=t.id)
            ORDER BY t.allocation_order LIMIT 1""").fetchone()[0][0]
    print(f"Database size: {db_size/1024/1024:.1f} MiB")
    print(f"Claim SQL execution: {plan['Execution Time']:.3f} ms")
    print("LOAD_TEST_100K_OK")
    close_pool()


if __name__ == "__main__":
    main()
