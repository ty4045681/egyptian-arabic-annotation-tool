"""Scene-scoped pool counts and candidate selection. Callers own the transaction."""

from __future__ import annotations

from annotation_metadata.claim_policy import get_claim_policy
from annotation_metadata.contracts import TaskFilter
from annotation_metadata.queries import (
    SceneScope,
    best_source_lateral,
    build_claimable_from,
    empty_pool_reason,
    load_scope,
    order_sql,
)
from annotation_metadata.taxonomy import SCENE_BY_CODE, scene_label
from annotation_repository import ForbiddenError


def parse_claim_filters(*, source_scene=None, batch_code=None,
                        source_confidence=None) -> TaskFilter:
    return TaskFilter(
        selected_scene=source_scene,
        source_scene=source_scene,
        batch_code=batch_code,
        source_confidence=source_confidence,
    )


def assert_scope_allows(scope: SceneScope, filters: TaskFilter) -> None:
    selected = filters.source_scene_value()
    if selected is None:
        return
    if not scope.allows_scene(selected if selected != "unknown" else None):
        raise ForbiddenError("source_scene is outside the annotator's allowed scope")


def claimable_select_sql(scope: SceneScope, filters: TaskFilter, user_id,
                         policy: str) -> tuple[str, list]:
    from_sql, params = build_claimable_from(scope, filters, user_id)
    sql = (
        """SELECT t.id, t.rel_path, t.filename, t.folder, t.duration, t.status,
                  v.id, v.revision, t.reserved_for_user_id,
                  best.id, best.scene_code, best.confidence """
        + from_sql
        + " ORDER BY " + order_sql(policy)
        + " LIMIT 1 FOR UPDATE OF t, v SKIP LOCKED"
    )
    return sql, [*params, user_id]


def candidate_exists_sql(scope: SceneScope, filters: TaskFilter, user_id) -> tuple[str, list]:
    from_sql, params = build_claimable_from(scope, filters, user_id)
    return "SELECT EXISTS(SELECT 1 " + from_sql + ")", params


def count_claimable(cur, scope: SceneScope, filters: TaskFilter, user_id,
                    *, include_assigned: bool = False) -> int:
    from_sql, params = build_claimable_from(
        scope, filters, user_id, include_assigned=include_assigned,
    )
    return cur.execute("SELECT count(*) " + from_sql, params).fetchone()[0]


def scene_counts(cur, scope: SceneScope, filters: TaskFilter, user_id) -> list[dict]:
    """Available counts per allowed scene using the same matching rules."""
    items = []
    for code in (list(SCENE_BY_CODE) if scope.all_scenes else scope.scene_codes):
        scene_filters = TaskFilter(
            selected_scene=code,
            source_scene=code,
            batch_code=filters.batch_code,
            source_confidence=filters.source_confidence,
        )
        available = count_claimable(cur, scope, scene_filters, user_id)
        items.append({
            "scene_code": code,
            "label_zh": scene_label(code),
            "label_en": str(SCENE_BY_CODE[code]["label_en"]),
            "available": available,
        })
    unknown_filters = TaskFilter(
        selected_scene="unknown",
        source_scene="unknown",
        batch_code=filters.batch_code,
        source_confidence=filters.source_confidence,
    )
    unknown_available = 0
    if scope.all_scenes or scope.allow_unknown:
        unknown_available = count_claimable(cur, scope, unknown_filters, user_id)
    return items, unknown_available


def pool_snapshot(cur, user_id, filters: TaskFilter, *, policy: str | None = None) -> dict:
    from annotation_repository import _validate_uuid
    uid = _validate_uuid(str(user_id), "user_id")
    scope = load_scope(cur, uid)
    policy_name = get_claim_policy(policy).name
    counts = dict(
        cur.execute(
            "SELECT status, count(*) FROM annotation_tasks GROUP BY status"
        ).fetchall()
    )
    total = sum(counts.values())
    annotated = counts.get("annotated", 0)
    skipped = counts.get("skipped", 0)
    pending = counts.get("pending", 0)
    assigned = cur.execute("SELECT count(*) FROM assignments").fetchone()[0]
    eligible_pending = cur.execute(
        "SELECT count(*) FROM annotation_tasks WHERE status = 'pending' AND eligible"
    ).fetchone()[0]
    global_unassigned = cur.execute(
        """SELECT count(*) FROM annotation_tasks t
           JOIN annotation_versions v ON v.task_id = t.id AND v.lifecycle = 'draft'
           WHERE t.status = 'pending' AND t.eligible
             AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.task_id = t.id)"""
    ).fetchone()[0]
    matching_unassigned = 0
    matching = 0
    by_scene = []
    unknown_available = 0
    if filters.source_scene_value():
        assert_scope_allows(scope, filters)
    if scope.can_claim:
        matching = count_claimable(cur, scope, filters, uid, include_assigned=True)
        matching_unassigned = count_claimable(cur, scope, filters, uid)
        by_scene, unknown_available = scene_counts(cur, scope, filters, uid)
    available = matching_unassigned if scope.can_claim else 0
    reason = empty_pool_reason(
        scope=scope, filters=filters, total=total, pending=pending,
        eligible_pending=eligible_pending, matching=matching,
        matching_unassigned=matching_unassigned,
        global_unassigned=global_unassigned,
    )
    if available > 0:
        reason = "available"
    return {
        "total": total,
        "annotated": annotated,
        "skipped": skipped,
        "pending": pending,
        "assigned": assigned,
        "available": available,
        "reason": reason,
        "by_scene": by_scene,
        "unknown_available": unknown_available,
        "scope": {
            "mode": scope.mode,
            "allow_unknown": scope.allow_unknown,
            "revision": scope.revision,
            "scene_codes": list(scope.scene_codes),
        },
        "applied_filters": {
            "source_scene": filters.source_scene_value(),
            "batch_code": filters.batch_code,
            "source_confidence": filters.source_confidence,
        },
        "claim_policy": policy_name,
    }


def task_still_matches(cur, task_id, scope: SceneScope, filters: TaskFilter,
                       user_id) -> tuple | None:
    from_sql, params = build_claimable_from(scope, filters, user_id)
    row = cur.execute(
        """SELECT t.id, best.id, best.scene_code, best.confidence """
        + from_sql + " AND t.id = %s",
        (*params, task_id),
    ).fetchone()
    return row
