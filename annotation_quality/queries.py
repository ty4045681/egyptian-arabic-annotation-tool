"""Shared cross-check list filters, corpus stats, and training-export predicates."""

from __future__ import annotations

import hashlib
import json
import uuid

from annotation_metadata.contracts import TaskFilter, parse_strict
from annotation_metadata.queries import metadata_filter_sql
from annotation_quality.contracts import OPEN_ROUND_STATES, CrossCheckListQuery

# Current-result attribution. Admin-edited versions have NULL submitter.
CREDITED_ANNOTATOR_SQL = (
    "COALESCE({version}.credited_annotator_id, {version}.submitted_by_user_id)"
)
TRAINING_COMPLETION_EVENT_TYPES = ("completed", "cross_check_submitted")
TRAINING_BEGIN_EVENT_TYPES = ("claimed", "reopened", "cross_check_claimed")
UNIQUE_ANNOTATED_CORPUS_SQL = """
SELECT count(*) AS annotated_count,
       COALESCE(sum(t.duration), 0) AS annotated_duration_seconds
FROM annotation_tasks t
JOIN annotation_versions v ON v.id = t.current_published_version_id
WHERE t.status = 'annotated'
  AND v.lifecycle = 'published'
  AND v.target_status = 'annotated'
"""
CROSS_CHECK_SUBMITTED_WORKLOAD_SQL = """
SELECT count(*) AS cross_check_submitted_count,
       COALESCE(sum(t.duration), 0) AS cross_check_submitted_audio_seconds
FROM annotation_events e
JOIN annotation_tasks t ON t.id = e.task_id
WHERE e.event_type = 'cross_check_submitted'
"""


def applied_list_filters(query: CrossCheckListQuery, *, timezone_name: str) -> dict:
    return {
        "state": str(query.state),
        "source_scene": query.source_scene,
        "batch_code": query.batch_code,
        "original_annotator_id": query.original_annotator_id,
        "secondary_annotator_id": query.secondary_annotator_id,
        "reason_code": query.reason_code,
        "q": query.q,
        "from": query.created_from,
        "to": query.created_to,
        "timezone": timezone_name,
        "limit": int(query.limit),
    }


def list_filter_digest(query: CrossCheckListQuery, *, timezone_name: str) -> str:
    payload = dict(applied_list_filters(query, timezone_name=timezone_name))
    payload.pop("limit", None)
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:16]


def list_where_sql(query: CrossCheckListQuery) -> tuple[str, list]:
    clauses = ["r.state = %s"]
    params: list = [str(query.state)]
    if query.original_annotator_id:
        clauses.append("r.original_annotator_id = %s")
        params.append(uuid.UUID(query.original_annotator_id))
    if query.secondary_annotator_id:
        clauses.append("r.secondary_annotator_id = %s")
        params.append(uuid.UUID(query.secondary_annotator_id))
    if query.reason_code:
        clauses.append("%s = ANY(r.reason_codes)")
        params.append(query.reason_code)
    if query.created_from:
        clauses.append("r.created_at >= %s::timestamptz")
        params.append(query.created_from)
    if query.created_to:
        clauses.append("r.created_at < %s::timestamptz")
        params.append(query.created_to)
    if query.q:
        like = f"%{query.q}%"
        q_clause = "(t.filename ILIKE %s OR t.rel_path ILIKE %s)"
        q_params: list = [like, like]
        try:
            q_uuid = uuid.UUID(str(query.q))
        except (ValueError, TypeError, AttributeError):
            q_uuid = None
        if q_uuid is not None:
            q_clause = f"({q_clause} OR r.id = %s OR r.task_id = %s)"
            q_params.extend([q_uuid, q_uuid])
        clauses.append(q_clause)
        params.extend(q_params)
    metadata = parse_strict(TaskFilter, {
        "source_scene": query.source_scene,
        "batch_code": query.batch_code,
    })
    meta_sql, meta_params = metadata_filter_sql(metadata, task_alias="t")
    if meta_sql != "true":
        clauses.append(meta_sql)
        params.extend(meta_params)
    return " AND ".join(clauses), params


def cross_check_quality_summary(cur) -> dict:
    """Live queue metrics. Counts are rounds except blocked audio seconds."""
    counts = cur.execute(
        """SELECT
               count(*) FILTER (WHERE state = 'in_progress'),
               count(*) FILTER (WHERE state = 'awaiting_review'),
               count(*) FILTER (WHERE state = 'passed'),
               count(*) FILTER (WHERE state = 'adjudicated'),
               min(created_at) FILTER (WHERE state = 'awaiting_review')
           FROM cross_check_rounds""",
    ).fetchone()
    # Unique-task duration: never SUM(DISTINCT duration), which collapses
    # two different 60s files into 60 instead of 120.
    blocked = cur.execute(
        """SELECT COALESCE(SUM(t.duration), 0)
           FROM annotation_tasks t
           WHERE EXISTS (
               SELECT 1 FROM cross_check_rounds r
               WHERE r.task_id = t.id AND r.state = ANY(%s)
           )""",
        (list(OPEN_ROUND_STATES),),
    ).fetchone()[0]
    oldest = counts[4]
    return {
        "in_progress_count": int(counts[0] or 0),
        "pending_review_count": int(counts[1] or 0),
        "passed_count": int(counts[2] or 0),
        "adjudicated_count": int(counts[3] or 0),
        "blocked_audio_seconds": float(blocked or 0),
        "count_unit": "rounds",
        "blocked_unit": "unique_task_audio_seconds",
        "queue_path": "/api/admin/cross-checks",
        "default_state": "awaiting_review",
        "oldest_pending_created_at": oldest.isoformat() if oldest else None,
    }


def credited_annotator_sql(version_alias: str = "v") -> str:
    return CREDITED_ANNOTATOR_SQL.format(version=version_alias)


def unique_annotated_corpus(cur) -> tuple[int, float]:
    """Unique current published/annotated tasks. Never SUM(DISTINCT duration)."""
    row = cur.execute(UNIQUE_ANNOTATED_CORPUS_SQL).fetchone()
    return int(row[0] or 0), float(row[1] or 0)


def cross_check_submitted_workload(cur) -> tuple[int, float]:
    """Independent secondary-submit workload; not added into corpus duration."""
    row = cur.execute(CROSS_CHECK_SUBMITTED_WORKLOAD_SQL).fetchone()
    return int(row[0] or 0), float(row[1] or 0)


def assignment_queue_counts(cur) -> tuple[int, int]:
    """Pending-task assignments, then in-progress cross-check rounds."""
    assigned = cur.execute(
        """SELECT count(*)
           FROM assignments a
           JOIN annotation_tasks t ON t.id = a.task_id
           WHERE t.status = 'pending'""",
    ).fetchone()[0]
    in_progress = cur.execute(
        """SELECT count(*) FROM cross_check_rounds
           WHERE state = 'in_progress'""",
    ).fetchone()[0]
    return int(assigned or 0), int(in_progress or 0)


def current_published_annotated_sql(
    *, task_alias: str = "t", version_alias: str = "v",
) -> str:
    return (
        f"{task_alias}.status = 'annotated'"
        f" AND {version_alias}.lifecycle = 'published'"
        f" AND {version_alias}.target_status = 'annotated'"
    )


def open_quality_round_sql(
    *, task_alias: str = "t", exists: bool = True,
) -> tuple[str, list]:
    keyword = "EXISTS" if exists else "NOT EXISTS"
    sql = (
        f"{keyword} ("
        f" SELECT 1 FROM cross_check_rounds open_round"
        f" WHERE open_round.task_id = {task_alias}.id"
        f" AND open_round.state = ANY(%s))"
    )
    return sql, [list(OPEN_ROUND_STATES)]


def _training_current_sql(
    filters: TaskFilter | None,
    *,
    task_alias: str,
    version_alias: str,
    require_open_round: bool,
) -> tuple[str, list]:
    open_sql, params = open_quality_round_sql(
        task_alias=task_alias, exists=require_open_round,
    )
    sql = (
        f"{current_published_annotated_sql(task_alias=task_alias, version_alias=version_alias)}"
        f" AND {open_sql}"
    )
    if filters is not None and not filters.is_empty():
        meta_sql, meta_params = metadata_filter_sql(filters, task_alias=task_alias)
        if meta_sql != "true":
            sql = f"{sql} AND ({meta_sql})"
            params.extend(meta_params)
    return sql, params


def training_export_eligible_sql(
    *, task_alias: str = "t", version_alias: str = "v",
) -> tuple[str, list]:
    """Current annotated published version with no open quality round."""
    return training_export_where_sql(
        None, task_alias=task_alias, version_alias=version_alias,
    )


def training_export_where_sql(
    filters: TaskFilter | None = None,
    *,
    task_alias: str = "t",
    version_alias: str = "v",
) -> tuple[str, list]:
    return _training_current_sql(
        filters, task_alias=task_alias, version_alias=version_alias,
        require_open_round=False,
    )


def unique_open_quality_blocked_sql(
    filters: TaskFilter | None = None,
    *,
    task_alias: str = "t",
    version_alias: str = "v",
) -> tuple[str, list]:
    """Annotated published tasks excluded only because a quality round is open."""
    return _training_current_sql(
        filters, task_alias=task_alias, version_alias=version_alias,
        require_open_round=True,
    )


def training_timing_joins_sql(
    *, task_alias: str = "t", version_alias: str = "v",
) -> str:
    """Bind wall-clock timing to the content draft, not admin wait time."""
    completion = ", ".join(f"'{item}'" for item in TRAINING_COMPLETION_EVENT_TYPES)
    begin = ", ".join(f"'{item}'" for item in TRAINING_BEGIN_EVENT_TYPES)
    return f"""
        LEFT JOIN annotation_versions timing_content
          ON timing_content.id = CASE
              WHEN {version_alias}.purpose = 'adjudication'
              THEN {version_alias}.base_version_id
              ELSE {version_alias}.id
          END
        LEFT JOIN LATERAL (
            SELECT e.id, e.created_at, e.user_id
            FROM annotation_events e
            WHERE e.task_id = {task_alias}.id
              AND e.version_id = timing_content.id
              AND e.event_type IN ({completion})
              AND e.to_status = 'annotated'
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT 1
        ) completed ON TRUE
        LEFT JOIN LATERAL (
            SELECT max(begin_event.created_at) AS at
            FROM annotation_events begin_event
            WHERE begin_event.task_id = {task_alias}.id
              AND begin_event.user_id = COALESCE(
                  completed.user_id,
                  timing_content.submitted_by_user_id,
                  timing_content.credited_annotator_id
              )
              AND begin_event.event_type IN ({begin})
              AND begin_event.created_at <= COALESCE(
                  completed.created_at, timing_content.submitted_at
              )
        ) started ON TRUE
    """


def latest_quality_round_lateral_sql(
    *, task_alias: str = "t", alias: str = "q",
) -> str:
    open_states = ", ".join(f"'{item}'" for item in sorted(OPEN_ROUND_STATES))
    return f"""
        LEFT JOIN LATERAL (
            SELECT r.id, r.state
            FROM cross_check_rounds r
            WHERE r.task_id = {task_alias}.id
              AND r.state NOT IN ('cancelled', 'invalidated')
            ORDER BY CASE WHEN r.state IN ({open_states}) THEN 0 ELSE 1 END,
                     r.created_at DESC, r.id DESC
            LIMIT 1
        ) {alias} ON TRUE
    """


def exported_task_quality_sql() -> str:
    return """
        SELECT t.id::text,
               t.current_published_version_id::text,
               q.id::text,
               q.state,
               q.decision
        FROM annotation_tasks t
        LEFT JOIN LATERAL (
            SELECT r.id, r.state, r.decision
            FROM cross_check_rounds r
            WHERE r.task_id = t.id
              AND r.state IN ('passed', 'adjudicated')
            ORDER BY r.resolved_at DESC NULLS LAST, r.id DESC
            LIMIT 1
        ) q ON TRUE
        WHERE t.id = ANY(%s::uuid[])
    """

