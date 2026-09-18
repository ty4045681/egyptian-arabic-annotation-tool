"""Shared cross-check list filters and quality-summary counts."""

from __future__ import annotations

import hashlib
import json
import uuid

from annotation_metadata.contracts import TaskFilter, parse_strict
from annotation_metadata.queries import metadata_filter_sql
from annotation_quality.contracts import OPEN_ROUND_STATES, CrossCheckListQuery


def list_filter_digest(query: CrossCheckListQuery, *, timezone_name: str) -> str:
    payload = {
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
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:16]


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


def word_difference_rate_sql(alias: str = "r") -> str:
    return (
        f"CASE WHEN {alias}.edit_distance IS NULL "
        f"OR {alias}.original_word_count IS NULL "
        f"OR {alias}.secondary_word_count IS NULL "
        f"OR GREATEST({alias}.original_word_count, "
        f"{alias}.secondary_word_count) <= 0 "
        f"THEN NULL ELSE {alias}.edit_distance::float "
        f"/ GREATEST({alias}.original_word_count, "
        f"{alias}.secondary_word_count) END"
    )


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
