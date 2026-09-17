from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import annotation_repository as repo


FROZEN_UTC = datetime(2026, 9, 17, 9, 30, tzinfo=timezone.utc)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _segments(assignment, prefix="text"):
    return [
        {
            "id": seg["id"], "start": seg["start"], "end": seg["end"],
            "duration": seg["duration"], "text": f"{prefix} {seg['id']}",
            "exclude_from_training": False,
        }
        for seg in assignment["segments"]
    ]


def _make_user(name):
    sid = str(uuid.uuid4())
    return repo.login(name, sid, 1800), sid


def _complete_next(user, *, status="annotated", prefix="text"):
    assignment = repo.claim(user["fence"])
    result = repo.complete(
        user["fence"],
        assignment["lease_token"],
        assignment["revision"],
        status,
        ["noisy"] if status == "skipped" else [],
        _segments(assignment, prefix) if status == "annotated" else [],
        str(uuid.uuid4()),
        f"complete-{uuid.uuid4()}",
    )
    return {
        **result,
        "version_id": assignment["version_id"],
        "task_id": assignment["task_id"],
    }


def _set_submitted_at(task_id, when):
    import db
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE annotation_versions AS version
                  SET submitted_at = %s
                 FROM annotation_tasks AS task
                WHERE task.id = %s
                  AND version.id = task.current_published_version_id""",
            (when, task_id),
        )
        conn.commit()


def _set_duration(task_id, duration):
    import db
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE annotation_tasks SET duration = %s WHERE id = %s",
            (duration, task_id),
        )
        conn.commit()


def _day_map(speed):
    return {item["date"]: item for item in speed["days"]}


@pytest.fixture
def freeze_shanghai_afternoon(monkeypatch):
    monkeypatch.setattr(repo, "utcnow", lambda: FROZEN_UTC)


def test_speed_returns_28_days_and_four_weeks(database, freeze_shanghai_afternoon):
    speed = repo.public_annotation_speed("Asia/Shanghai")
    assert speed["timezone"] == "Asia/Shanghai"
    assert speed["window_days"] == 28
    assert speed["from"] == "2026-08-21"
    assert speed["through"] == "2026-09-17"
    assert speed["generated_at"] == "2026-09-17T17:30:00+08:00"
    assert len(speed["days"]) == 28
    assert len(speed["weeks"]) == 4
    dates = [item["date"] for item in speed["days"]]
    assert dates[0] == "2026-08-21"
    assert dates[-1] == "2026-09-17"
    assert dates == sorted(set(dates))
    for index, item in enumerate(speed["days"]):
        expected = (
            datetime(2026, 8, 21, tzinfo=SHANGHAI).date() + timedelta(days=index)
        ).isoformat()
        assert item["date"] == expected
        assert item["duration_seconds"] == 0.0
        assert item["is_partial"] is (index == 27)
    assert [week["start_date"] for week in speed["weeks"]] == [
        "2026-08-21", "2026-08-28", "2026-09-04", "2026-09-11",
    ]
    assert [week["end_date"] for week in speed["weeks"]] == [
        "2026-08-27", "2026-09-03", "2026-09-10", "2026-09-17",
    ]


def test_missing_days_are_zero_filled(database, seed_tasks, freeze_shanghai_afternoon):
    seed_tasks(1, duration=1200)
    user, _ = _make_user("alice")
    completed = _complete_next(user)
    _set_submitted_at(
        completed["task_id"],
        datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc),
    )
    speed = repo.public_annotation_speed("Asia/Shanghai")
    days = _day_map(speed)
    assert days["2026-08-21"]["duration_seconds"] == 1200.0
    assert days["2026-08-22"]["duration_seconds"] == 0.0
    assert days["2026-09-17"]["duration_seconds"] == 0.0
    assert len(speed["days"]) == 28


def test_shanghai_midnight_boundary(database, seed_tasks, freeze_shanghai_afternoon):
    seed_tasks(2, duration=10)
    user, _ = _make_user("alice")
    before = _complete_next(user, prefix="before")
    after = _complete_next(user, prefix="after")
    _set_duration(before["task_id"], 111)
    _set_duration(after["task_id"], 222)
    _set_submitted_at(
        before["task_id"],
        datetime(2026, 9, 16, 15, 59, 59, tzinfo=timezone.utc),
    )
    _set_submitted_at(
        after["task_id"],
        datetime(2026, 9, 16, 16, 0, 0, tzinfo=timezone.utc),
    )
    speed = repo.public_annotation_speed("Asia/Shanghai")
    days = _day_map(speed)
    assert days["2026-09-16"]["duration_seconds"] == 111.0
    assert days["2026-09-17"]["duration_seconds"] == 222.0
    assert days["2026-09-17"]["is_partial"] is True
    assert days["2026-09-16"]["is_partial"] is False


def test_sums_multiple_tasks_on_the_same_day(
        database, seed_tasks, freeze_shanghai_afternoon):
    seed_tasks(1, folder="short", duration=10)
    seed_tasks(1, folder="long", duration=20)
    user, _ = _make_user("alice")
    first = _complete_next(user, prefix="one")
    second = _complete_next(user, prefix="two")
    when = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)
    _set_submitted_at(first["task_id"], when)
    _set_submitted_at(second["task_id"], when)
    speed = repo.public_annotation_speed("Asia/Shanghai")
    assert _day_map(speed)["2026-09-10"]["duration_seconds"] == 30.0


def test_skipped_tasks_are_excluded(database, seed_tasks, freeze_shanghai_afternoon):
    seed_tasks(2, duration=50)
    user, _ = _make_user("alice")
    skipped = _complete_next(user, status="skipped")
    kept = _complete_next(user, prefix="kept")
    when = datetime(2026, 9, 12, 4, 0, tzinfo=timezone.utc)
    _set_submitted_at(skipped["task_id"], when)
    _set_submitted_at(kept["task_id"], when)
    speed = repo.public_annotation_speed("Asia/Shanghai")
    assert _day_map(speed)["2026-09-12"]["duration_seconds"] == 50.0


def test_revoked_versions_are_excluded(database, seed_tasks, freeze_shanghai_afternoon):
    import db

    seed_tasks(1, duration=90)
    user, _ = _make_user("alice")
    completed = _complete_next(user)
    _set_submitted_at(
        completed["task_id"],
        datetime(2026, 9, 5, 4, 0, tzinfo=timezone.utc),
    )
    assert _day_map(repo.public_annotation_speed("Asia/Shanghai"))[
        "2026-09-05"
    ]["duration_seconds"] == 90.0
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE annotation_versions
                  SET lifecycle = 'revoked', revoked_at = now(),
                      revoked_reason = 'quality failure'
                WHERE id = %s""",
            (completed["version_id"],),
        )
        conn.execute(
            """UPDATE annotation_tasks
                  SET status = 'pending', current_published_version_id = NULL
                WHERE id = %s""",
            (completed["task_id"],),
        )
        conn.commit()
    speed = repo.public_annotation_speed("Asia/Shanghai")
    assert _day_map(speed)["2026-09-05"]["duration_seconds"] == 0.0
    assert sum(item["duration_seconds"] for item in speed["days"]) == 0.0


def test_reannotation_counts_only_the_current_version(
        database, seed_tasks, freeze_shanghai_afternoon):
    seed_tasks(1, duration=40)
    user, _ = _make_user("alice")
    first = _complete_next(user, prefix="original")
    _set_submitted_at(
        first["task_id"],
        datetime(2026, 8, 25, 4, 0, tzinfo=timezone.utc),
    )
    repo.reopen_completed(user["fence"], first["task_id"], str(uuid.uuid4()))
    draft = repo.get_assignment(user["id"])
    repo.complete(
        user["fence"],
        draft["lease_token"],
        draft["revision"],
        "annotated",
        [],
        _segments(draft, "corrected"),
        str(uuid.uuid4()),
        "reannotate",
    )
    _set_submitted_at(
        first["task_id"],
        datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc),
    )
    speed = repo.public_annotation_speed("Asia/Shanghai")
    days = _day_map(speed)
    assert days["2026-08-25"]["duration_seconds"] == 0.0
    assert days["2026-09-14"]["duration_seconds"] == 40.0


def test_weekly_average_divides_by_seven_including_zeros(
        database, seed_tasks, freeze_shanghai_afternoon):
    seed_tasks(1, duration=7000)
    user, _ = _make_user("alice")
    completed = _complete_next(user)
    _set_submitted_at(
        completed["task_id"],
        datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc),
    )
    speed = repo.public_annotation_speed("Asia/Shanghai")
    assert speed["weeks"][0]["average_daily_duration_seconds"] == pytest.approx(1000.0)
    assert speed["weeks"][1]["average_daily_duration_seconds"] == 0.0
    assert speed["weeks"][2]["average_daily_duration_seconds"] == 0.0
    assert speed["weeks"][3]["average_daily_duration_seconds"] == 0.0


def test_today_is_marked_partial(database, freeze_shanghai_afternoon):
    speed = repo.public_annotation_speed("Asia/Shanghai")
    assert speed["days"][-1]["is_partial"] is True
    assert all(not item["is_partial"] for item in speed["days"][:-1])


def test_timezones_do_not_duplicate_or_drop_days(database, freeze_shanghai_afternoon):
    shanghai = repo.public_annotation_speed("Asia/Shanghai")
    honolulu = repo.public_annotation_speed("Pacific/Honolulu")
    assert shanghai["through"] == "2026-09-17"
    assert honolulu["through"] == "2026-09-16"
    for speed in (shanghai, honolulu):
        dates = [item["date"] for item in speed["days"]]
        assert len(dates) == 28
        assert len(set(dates)) == 28
        start = datetime.fromisoformat(dates[0]).date()
        for index, value in enumerate(dates):
            assert value == (start + timedelta(days=index)).isoformat()


def test_invalid_timezone_is_rejected(database):
    with pytest.raises(repo.ValidationError, match="Invalid timezone"):
        repo.public_annotation_speed("Not/AZone")


SPEED_EXPLAIN_SQL = """
SELECT timezone(%s, v.submitted_at)::date AS day,
       COALESCE(SUM(t.duration), 0) AS duration_seconds
FROM annotation_versions v
JOIN annotation_tasks t
  ON t.current_published_version_id = v.id
 AND t.status = 'annotated'
WHERE v.lifecycle = 'published'
  AND v.target_status = 'annotated'
  AND v.submitted_at IS NOT NULL
  AND v.submitted_at >= %s
  AND v.submitted_at < %s
GROUP BY day
"""
SPEED_WINDOW_START_UTC = datetime(2026, 8, 20, 16, 0, tzinfo=timezone.utc)
SPEED_WINDOW_END_UTC = datetime(2026, 9, 17, 16, 0, tzinfo=timezone.utc)


def _plan_index_names(plan) -> set[str]:
    names: set[str] = set()
    stack = []
    nodes = plan if isinstance(plan, list) else [plan]
    for item in nodes:
        if isinstance(item, dict) and "Plan" in item:
            stack.append(item["Plan"])
        elif isinstance(item, dict):
            stack.append(item)
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        index_name = node.get("Index Name")
        if index_name:
            names.add(index_name)
        stack.extend(node.get("Plans") or [])
    return names


def _seed_selective_speed_rows(conn, *, total: int, recent: int) -> None:
    conn.execute(
        """INSERT INTO annotation_tasks (
               rel_path, filename, folder, duration, status, eligible,
               allocation_order
           )
           SELECT 'speed/' || i || '.wav',
                  i || '.wav',
                  'speed',
                  10.0,
                  'annotated',
                  true,
                  nextval('task_allocation_order_seq')
           FROM generate_series(1, %s) AS i""",
        (total,),
    )
    conn.execute(
        """INSERT INTO annotation_versions (
               task_id, version_no, lifecycle, target_status, submitted_at
           )
           SELECT id, 1, 'published', 'annotated',
                  CASE WHEN rn <= %s
                       THEN timestamptz '2026-08-25 00:00:00+00'
                            + ((rn %% 20) * interval '1 day')
                       ELSE timestamptz '2024-01-01 00:00:00+00'
                            + ((rn %% 300) * interval '1 day')
                  END
           FROM (
               SELECT id, row_number() OVER (ORDER BY id) AS rn
               FROM annotation_tasks
               WHERE folder = 'speed'
           ) numbered""",
        (recent,),
    )
    conn.execute(
        """UPDATE annotation_tasks AS task
              SET current_published_version_id = version.id
             FROM annotation_versions AS version
            WHERE version.task_id = task.id
              AND task.folder = 'speed'"""
    )
    conn.execute("ANALYZE annotation_tasks")
    conn.execute("ANALYZE annotation_versions")
    conn.commit()


def test_speed_query_uses_partial_indexes(database, freeze_shanghai_afternoon):
    import db

    with db.db_conn() as conn:
        _seed_selective_speed_rows(conn, total=8000, recent=40)
        plan = conn.execute(
            "EXPLAIN (FORMAT JSON) " + SPEED_EXPLAIN_SQL,
            ("Asia/Shanghai", SPEED_WINDOW_START_UTC, SPEED_WINDOW_END_UTC),
        ).fetchone()[0]
    names = _plan_index_names(plan)
    assert "idx_versions_published_annotated_submitted" in names, names
    assert "idx_tasks_current_published_annotated" in names, names


@pytest.mark.skipif(
    os.environ.get("ANNOTATION_SPEED_EXPLAIN") != "1",
    reason="100k EXPLAIN ANALYZE is opt-in (ANNOTATION_SPEED_EXPLAIN=1)",
)
def test_speed_query_plan_on_100k_current_versions(
        database, monkeypatch, tmp_path):
    """EXPLAIN ANALYZE on 100k rows with a selective 28-day slice."""
    import json
    import db

    monkeypatch.setattr(repo, "utcnow", lambda: FROZEN_UTC)
    with db.db_conn() as conn:
        _seed_selective_speed_rows(conn, total=100000, recent=200)
        plan = conn.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + SPEED_EXPLAIN_SQL,
            ("Asia/Shanghai", SPEED_WINDOW_START_UTC, SPEED_WINDOW_END_UTC),
        ).fetchone()[0]
    root = plan[0]["Plan"] if isinstance(plan, list) else plan["Plan"]
    elapsed_ms = float(
        plan[0]["Execution Time"] if isinstance(plan, list) else plan["Execution Time"]
    )
    names = _plan_index_names(plan)
    artifact = tmp_path / "explain-annotation-speed.json"
    artifact.write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    print(
        f"ANNOTATION_SPEED_EXPLAIN elapsed_ms={elapsed_ms:.2f} "
        f"node={root.get('Node Type')} indexes={sorted(names)} artifact={artifact}",
        flush=True,
    )
    speed = repo.public_annotation_speed("Asia/Shanghai")
    assert len(speed["days"]) == 28
    assert len(speed["weeks"]) == 4
    assert elapsed_ms < 1000.0
    assert "idx_versions_published_annotated_submitted" in names, names
    assert "idx_tasks_current_published_annotated" in names, names
