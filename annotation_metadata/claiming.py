"""Scene-scoped pool counts and candidate selection. Callers own the transaction."""

from __future__ import annotations

from annotation_metadata.claim_policy import get_claim_policy
from annotation_metadata.contracts import TaskFilter
from annotation_metadata.queries import (
    CONFIDENCE_CASE,
    SceneScope,
    _scope_source_clause,
    best_source_lateral,
    build_claimable_from,
    claim_match_predicate,
    claim_source_exists_sql,
    empty_pool_reason,
    load_scope,
    matching_source_task_ids_sql,
    order_sql,
)
from annotation_metadata.taxonomy import (
    FALLBACK_SOURCE_SCENE,
    SCENE_BY_CODE,
    SCENE_CODES,
    SCENE_ORDER,
    is_source_fallback_filter,
    ordered_scene_codes,
    scene_label,
)
from annotation_repository import ForbiddenError

CONFIDENCE_BANDS = ("high", "medium", "low", "unknown")


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
    if not scope.allows_scene(selected):
        raise ForbiddenError("source_scene is outside the annotator's allowed scope")


def _filters_with_confidence(filters: TaskFilter, confidence: str) -> TaskFilter:
    payload = filters.model_dump()
    payload["source_confidence"] = confidence
    return TaskFilter(**payload)


def claimable_select_sql(scope: SceneScope, filters: TaskFilter, user_id,
                         policy: str) -> tuple[str, list]:
    """Logical one-shot candidate query (EXPLAIN / compatibility).

    Live claiming uses ``fetch_claim_candidate``, which locks one band at a
    time so PostgreSQL does not sort the entire pending set.
    """
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
    match_sql, match_params = claim_source_exists_sql(scope, filters)
    sql = (
        """SELECT EXISTS(
               SELECT 1 FROM annotation_tasks t
               JOIN annotation_versions v
                 ON v.task_id = t.id AND v.lifecycle = 'draft'
               WHERE t.status = 'pending' AND t.eligible
                 AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.task_id = t.id)
                 AND NOT EXISTS (
                       SELECT 1 FROM task_annotator_blocks b
                       WHERE b.task_id = t.id AND b.user_id = %s)
                 AND (t.reserved_for_user_id = %s OR t.reserved_for_user_id IS NULL)
                 AND (""" + match_sql + """))"""
    )
    return sql, [user_id, user_id, *match_params]


def _claimable_base_where(*, include_assigned: bool) -> str:
    assignment_clause = (
        "" if include_assigned
        else "AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.task_id = t.id)"
    )
    return f"""
FROM annotation_tasks t
JOIN annotation_versions v
  ON v.task_id = t.id AND v.lifecycle = 'draft'
WHERE t.status = 'pending' AND t.eligible
  {assignment_clause}
  AND NOT EXISTS (
        SELECT 1 FROM task_annotator_blocks b
        WHERE b.task_id = t.id AND b.user_id = %s
      )
  AND (t.reserved_for_user_id = %s OR t.reserved_for_user_id IS NULL)
"""


def count_claimable(cur, scope: SceneScope, filters: TaskFilter, user_id,
                    *, include_assigned: bool = False) -> int:
    match_sql, match_params = claim_source_exists_sql(scope, filters)
    sql = (
        "SELECT count(*) " + _claimable_base_where(include_assigned=include_assigned)
        + " AND (" + match_sql + ")"
    )
    return cur.execute(sql, (user_id, user_id, *match_params)).fetchone()[0]


def count_claimable_pair(cur, scope: SceneScope, filters: TaskFilter, user_id) -> tuple[int, int]:
    """``(matching including assigned, matching unassigned)`` in one scan."""
    match_sql, match_params = claim_source_exists_sql(scope, filters)
    sql = (
        """SELECT count(*),
                  count(*) FILTER (
                      WHERE NOT EXISTS (
                          SELECT 1 FROM assignments a WHERE a.task_id = t.id))
           """
        + _claimable_base_where(include_assigned=True)
        + " AND (" + match_sql + ")"
    )
    row = cur.execute(sql, (user_id, user_id, *match_params)).fetchone()
    return int(row[0]), int(row[1])


def scene_counts(cur, scope: SceneScope, filters: TaskFilter, user_id) -> tuple[list[dict], int]:
    """Available counts per allowed scene using the same matching rules."""
    codes = list(SCENE_ORDER) if scope.all_scenes else ordered_scene_codes(
        scope.effective_scene_codes(),
    )
    unknown_available = 0
    by_code: dict[str | None, int] = {}
    if scope.can_claim:
        extra_clauses = []
        extra_params: list = []
        if filters.source_confidence:
            extra_clauses.append("src.confidence = %s")
            extra_params.append(filters.source_confidence)
        if filters.batch_code:
            extra_clauses.append(
                "EXISTS (SELECT 1 FROM source_batches sb "
                "WHERE sb.id = src.batch_id AND sb.batch_code = %s)"
            )
            extra_params.append(filters.batch_code)
        extra_sql = (" AND " + " AND ".join(extra_clauses)) if extra_clauses else ""
        scope_sql, scope_params = _scope_source_clause(scope, src_alias="src")
        effective = f"COALESCE(src.scene_code, '{FALLBACK_SOURCE_SCENE}')"
        rows = cur.execute(
            f"""SELECT {effective}, count(DISTINCT t.id)
                FROM task_sources src
                JOIN annotation_tasks t ON t.id = src.task_id
                JOIN annotation_versions v
                  ON v.task_id = t.id AND v.lifecycle = 'draft'
                WHERE src.is_current
                  AND t.status = 'pending' AND t.eligible
                  AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.task_id = t.id)
                  AND NOT EXISTS (
                        SELECT 1 FROM task_annotator_blocks b
                        WHERE b.task_id = t.id AND b.user_id = %s)
                  AND (t.reserved_for_user_id = %s OR t.reserved_for_user_id IS NULL)
                  AND ({scope_sql})
                  {extra_sql}
                GROUP BY {effective}""",
            (user_id, user_id, *scope_params, *extra_params),
        ).fetchall()
        for scene_code, count in rows:
            by_code[scene_code] = int(count)
        if scope.allows_fallback_source():
            fallback_filters = TaskFilter(
                selected_scene=FALLBACK_SOURCE_SCENE,
                source_scene=FALLBACK_SOURCE_SCENE,
                batch_code=filters.batch_code,
                source_confidence=filters.source_confidence,
            )
            by_code[FALLBACK_SOURCE_SCENE] = count_claimable(
                cur, scope, fallback_filters, user_id,
            )
            unknown_available = int(by_code[FALLBACK_SOURCE_SCENE])
    items = []
    for code in codes:
        if code not in SCENE_CODES:
            continue
        label = scene_label(code)
        items.append({
            "scene_code": code,
            "label": label,
            "label_zh": label,
            "label_en": str(SCENE_BY_CODE[code]["label_en"]),
            "available": int(by_code.get(code, 0)),
        })
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
        matching, matching_unassigned = count_claimable_pair(cur, scope, filters, uid)
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


def _use_source_first(scope: SceneScope, filters: TaskFilter, *,
                      reserved_only: bool, fifo: bool) -> bool:
    """Named-scene lookups start from task_sources so empty scenes are O(1).

    Unfiltered high/medium bands walk pending allocation_order with EXISTS;
    combining that with IN (SELECT DISTINCT ...) made PostgreSQL hash 80k
    ids and join-filter 68k rows (~200ms) after the EXISTS already had a hit.
    """
    if reserved_only:
        return False
    selected = filters.source_scene_value()
    # Spoken languages includes virtual no-source tasks; keep the pending walk.
    return bool(selected and not is_source_fallback_filter(selected))


def claim_lock_query(scope: SceneScope, filters: TaskFilter, user_id, policy: str,
                     *, reserved_only: bool = False) -> tuple[str, list]:
    """One SKIP LOCKED LIMIT 1 query. Band filters already applied."""
    fifo = policy == "fifo"
    source_first = _use_source_first(
        scope, filters, reserved_only=reserved_only, fifo=fifo,
    )
    extra = []
    extra_params: list = []
    if reserved_only:
        extra.append("AND t.reserved_for_user_id = %s")
        extra_params.append(user_id)
    else:
        extra.append("AND t.reserved_for_user_id IS NULL")
    join_sql = ""
    join_params: list = []
    order = "t.allocation_order, t.id"
    order_params: list = []
    match_params: list = []
    if reserved_only and not fifo:
        best_sql, best_params = best_source_lateral(scope=scope, filters=filters)
        join_sql = best_sql
        join_params = best_params
        order = (
            CONFIDENCE_CASE.format(expr="COALESCE(best.confidence, 'unknown')")
            + ", t.allocation_order, t.id"
        )
        select_best = "best.id, best.scene_code, best.confidence"
        match_pred = claim_match_predicate(scope, filters)
    else:
        select_best = "NULL, NULL, NULL"
        if source_first:
            match_pred = "true"
            match_params = []
            ids_sql, ids_params = matching_source_task_ids_sql(scope, filters)
            extra.append("AND t.id IN (" + ids_sql + ")")
            extra_params.extend(ids_params)
        else:
            match_pred, match_params = claim_source_exists_sql(scope, filters)
    sql = f"""
SELECT t.id, t.rel_path, t.filename, t.folder, t.duration, t.status,
       v.id, v.revision, t.reserved_for_user_id,
       {select_best}
FROM annotation_tasks t
JOIN annotation_versions v
  ON v.task_id = t.id AND v.lifecycle = 'draft'
{join_sql}
WHERE t.status = 'pending' AND t.eligible
  AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.task_id = t.id)
  AND NOT EXISTS (
        SELECT 1 FROM task_annotator_blocks b
        WHERE b.task_id = t.id AND b.user_id = %s)
  AND (t.reserved_for_user_id = %s OR t.reserved_for_user_id IS NULL)
  AND ({match_pred})
  {' '.join(extra)}
ORDER BY {order}
LIMIT 1
FOR UPDATE OF t, v SKIP LOCKED
"""
    params = [
        *join_params,
        user_id, user_id,
        *match_params,
        *extra_params,
        *order_params,
    ]
    return sql, params


def claim_lock_queries(scope: SceneScope, filters: TaskFilter, user_id,
                       policy: str) -> list[tuple[str, str, list]]:
    """Production lock queries in execution order, for EXPLAIN."""
    plans = [("reserved", *claim_lock_query(
        scope, filters, user_id, policy, reserved_only=True,
    ))]
    if policy == "fifo":
        plans.append(("fifo", *claim_lock_query(
            scope, filters, user_id, policy, reserved_only=False,
        )))
        return plans
    requested = filters.source_confidence
    bands = (requested,) if requested else CONFIDENCE_BANDS
    for band in bands:
        band_filters = _filters_with_confidence(filters, band)
        plans.append((f"band:{band}", *claim_lock_query(
            scope, band_filters, user_id, policy, reserved_only=False,
        )))
    return plans


def fetch_claim_candidate(cur, scope: SceneScope, filters: TaskFilter, user_id,
                          policy: str):
    """Lock one matching draft using reservation-then-confidence bands."""
    if not scope.can_claim:
        return None
    sql, params = claim_lock_query(
        scope, filters, user_id, policy, reserved_only=True,
    )
    row = cur.execute(sql, params).fetchone()
    if row:
        return row
    if policy == "fifo":
        sql, params = claim_lock_query(
            scope, filters, user_id, policy, reserved_only=False,
        )
        return cur.execute(sql, params).fetchone()
    requested = filters.source_confidence
    bands = (requested,) if requested else CONFIDENCE_BANDS
    for band in bands:
        band_filters = _filters_with_confidence(filters, band)
        sql, params = claim_lock_query(
            scope, band_filters, user_id, policy, reserved_only=False,
        )
        row = cur.execute(sql, params).fetchone()
        if row:
            return row
    return None
