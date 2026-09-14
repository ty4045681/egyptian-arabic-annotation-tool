from __future__ import annotations

import uuid

import annotation_repository as repo
import db
from tests.test_scene_claims import source, user


def test_compound_filters_use_same_evidence_and_unique_counts(database, seed_tasks):
    mixed, other = seed_tasks(2)
    source(mixed, "airport", "medium", "batch-a-2026-09-14")
    source(mixed, "shopping", "high", "batch-b-2026-09-14")
    source(other, "airport", "high", "batch-a-2026-09-14")
    overview = repo.admin_overview({
        "source_scene": "airport",
        "source_confidence": "high",
        "batch_code": "batch-a-2026-09-14",
    })
    assert overview["totals"]["total_audio_count"] == 1
    assert overview["totals"]["total_audio_duration_seconds"] == pytest_duration(other)
    tasks = repo.admin_tasks({
        "source_scene": "airport",
        "source_confidence": "medium",
        "batch_code": "batch-a-2026-09-14",
    })
    assert [item["task_id"] for item in tasks["items"]] == [mixed]


def pytest_duration(task_id):
    with db.db_conn() as conn:
        return float(conn.execute(
            "SELECT duration FROM annotation_tasks WHERE id = %s", (task_id,),
        ).fetchone()[0])


def test_unknown_scene_includes_legacy_and_null_scene_rows(database, seed_tasks):
    legacy, explicit = seed_tasks(2)
    with db.db_conn() as conn:
        bid = conn.execute(
            "INSERT INTO source_batches(batch_code,name) VALUES(%s,%s) RETURNING id",
            ("null-scene-2026-09-14", "null-scene-2026-09-14"),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO task_sources(task_id,batch_id,record_key,scene_code,
                      confidence,confidence_basis,content_digest)
               VALUES(%s,%s,'null-row',NULL,'unknown','none',%s)""",
            (explicit, bid, "c" * 64),
        )
    claimed = repo.claim(user("unknown-scene"), source_scene="unknown")
    assert claimed["task_id"] in {legacy, explicit}
    pool = repo.pool_state(user("unknown-scene-2"), source_scene="unknown")
    assert pool["available"] == 1
