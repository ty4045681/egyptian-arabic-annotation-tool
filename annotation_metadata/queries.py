"""Shared SQL fragments for source matching, reviews, and pool/claim filters.

Admin lists, pool counts, claim selection, and grouped statistics must all
use these helpers so compound source filters apply to one evidence row.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from annotation_metadata.contracts import TaskFilter
from annotation_metadata.taxonomy import SCENE_CODES


@dataclass
class SceneScope:
    mode: str = "all"
    allow_unknown: bool = False
    revision: int = 0
    scene_codes: list[str] = field(default_factory=list)

    @property
    def all_scenes(self) -> bool:
        return self.mode == "all"

    @property
    def can_claim(self) -> bool:
        if self.mode == "none":
            return False
        if self.mode == "restricted" and not self.scene_codes and not self.allow_unknown:
            return False
        return True

    def allows_scene(self, scene_code: str | None) -> bool:
        if self.mode == "none":
            return False
        if scene_code in (None, "", "unknown"):
            return self.mode == "all" or self.allow_unknown
        if self.mode == "all":
            return scene_code in SCENE_CODES
        return scene_code in self.scene_codes


CONFIDENCE_CASE = """
CASE {expr}
  WHEN 'high' THEN 1
  WHEN 'medium' THEN 2
  WHEN 'low' THEN 3
  ELSE 4
END
""".strip()

NO_CURRENT_SOURCES = (
    "NOT EXISTS (SELECT 1 FROM task_sources u "
    "WHERE u.task_id = {task}.id AND u.is_current)"
)


def load_scope(cur, user_id) -> SceneScope:
    row = cur.execute(
        """SELECT mode, allow_unknown, revision
           FROM annotator_scene_scopes WHERE user_id = %s""",
        (user_id,),
    ).fetchone()
    if not row:
        return SceneScope(mode="all", allow_unknown=False, revision=0, scene_codes=[])
    codes = [
        r[0] for r in cur.execute(
            """SELECT scene_code FROM annotator_scene_access
               WHERE user_id = %s ORDER BY scene_code""",
            (user_id,),
        ).fetchall()
    ]
    return SceneScope(
        mode=row[0], allow_unknown=bool(row[1]), revision=int(row[2]),
        scene_codes=codes,
    )


def _source_row_clauses(filters: TaskFilter, *, src_alias: str = "src") -> tuple[list[str], list]:
    scene = filters.source_scene_value()
    clauses: list[str] = [f"{src_alias}.is_current"]
    params: list = []
    if scene == "unknown":
        clauses.append(f"{src_alias}.scene_code IS NULL")
    elif scene:
        clauses.append(f"{src_alias}.scene_code = %s")
        params.append(scene)
    if filters.source_confidence:
        clauses.append(f"{src_alias}.confidence = %s")
        params.append(filters.source_confidence)
    if filters.batch_code:
        clauses.append(
            f"EXISTS (SELECT 1 FROM source_batches sb "
            f"WHERE sb.id = {src_alias}.batch_id AND sb.batch_code = %s)"
        )
        params.append(filters.batch_code)
    return clauses, params


def source_exists_sql(filters: TaskFilter, *, task_alias: str = "t") -> tuple[str, list]:
    """EXISTS predicate: every source condition hits the same current row.

    ``unknown`` covers both legacy tasks with no current sources and current
    evidence rows whose scene_code is NULL. A batch filter never matches a
    virtual (no-row) unknown task, because that task has no batch evidence.
    """
    scene = filters.source_scene_value()
    if not scene and not filters.source_confidence and not filters.batch_code:
        return "true", []
    clauses, params = _source_row_clauses(filters, src_alias="src")
    exists_row = (
        f"EXISTS (SELECT 1 FROM task_sources src WHERE src.task_id = {task_alias}.id"
        f" AND " + " AND ".join(clauses) + ")"
    )
    if filters.allows_virtual_unknown():
        virtual = NO_CURRENT_SOURCES.format(task=task_alias)
        return f"({exists_row} OR {virtual})", params
    return exists_row, params


def review_filter_sql(filters: TaskFilter, *, task_alias: str = "t",
                      published_alias: str | None = None) -> tuple[str, list]:
    status = filters.review_status
    if not status:
        return "true", []
    published = published_alias or f"{task_alias}.current_published_version_id"
    latest = (
        "(SELECT sr.status FROM scene_reviews sr "
        f"WHERE sr.version_id = {published} AND NOT sr.superseded "
        "ORDER BY sr.review_no DESC LIMIT 1)"
    )
    if status == "unreviewed_unpublished":
        return f"{published} IS NULL", []
    if status == "unreviewed_published":
        return (
            f"{published} IS NOT NULL AND COALESCE({latest}, 'pending') = 'pending'",
            [],
        )
    if status == "pending":
        return f"COALESCE({latest}, 'pending') = 'pending'", []
    return f"{latest} = %s", [status]


def prediction_filter_sql(filters: TaskFilter, *, task_alias: str = "t") -> tuple[str, list]:
    if not filters.prediction_scene:
        return "true", []
    if filters.prediction_scene == "unknown":
        return (
            f"NOT EXISTS (SELECT 1 FROM task_scene_predictions p "
            f"WHERE p.task_id = {task_alias}.id)",
            [],
        )
    return (
        f"EXISTS (SELECT 1 FROM task_scene_predictions p "
        f"WHERE p.task_id = {task_alias}.id AND p.id = ("
        f"SELECT p2.id FROM task_scene_predictions p2 "
        f"WHERE p2.task_id = {task_alias}.id ORDER BY p2.created_at DESC, p2.id DESC "
        f"LIMIT 1) AND p.predicted_scene_code = %s)",
        [filters.prediction_scene],
    )


def human_scene_filter_sql(filters: TaskFilter, *, task_alias: str = "t") -> tuple[str, list]:
    if not filters.human_scene:
        return "true", []
    published = f"{task_alias}.current_published_version_id"
    latest_id = (
        "(SELECT sr.id FROM scene_reviews sr "
        f"WHERE sr.version_id = {published} AND NOT sr.superseded "
        "ORDER BY sr.review_no DESC LIMIT 1)"
    )
    if filters.human_scene == "unknown":
        return (
            f"({published} IS NULL OR NOT EXISTS ("
            f"SELECT 1 FROM scene_review_labels rl WHERE rl.review_id = {latest_id}))",
            [],
        )
    return (
        f"EXISTS (SELECT 1 FROM scene_review_labels rl "
        f"WHERE rl.review_id = {latest_id} AND rl.scene_code = %s)",
        [filters.human_scene],
    )


def metadata_filter_sql(filters: TaskFilter, *, task_alias: str = "t") -> tuple[str, list]:
    parts: list[str] = []
    params: list = []
    for builder in (
        source_exists_sql, review_filter_sql,
        prediction_filter_sql, human_scene_filter_sql,
    ):
        sql, extra = builder(filters, task_alias=task_alias)
        if sql != "true":
            parts.append(sql)
            params.extend(extra)
    if not parts:
        return "true", []
    return " AND ".join(parts), params


def best_source_lateral(*, scope: SceneScope, filters: TaskFilter,
                        alias: str = "best") -> tuple[str, list]:
    """LATERAL that picks the highest-priority matching current source.

    Tie-breakers among equivalent evidence stay inside this subquery
    (confidence, scene_code, source id). Outer claim ORDER BY does not
    use those columns.
    """
    selected = filters.source_scene_value()
    clauses = ["ts.task_id = t.id", "ts.is_current"]
    params: list = []
    if selected == "unknown":
        clauses.append("ts.scene_code IS NULL")
    elif selected:
        clauses.append("ts.scene_code = %s")
        params.append(selected)
    if filters.source_confidence:
        clauses.append("ts.confidence = %s")
        params.append(filters.source_confidence)
    if filters.batch_code:
        clauses.append(
            "EXISTS (SELECT 1 FROM source_batches sb "
            "WHERE sb.id = ts.batch_id AND sb.batch_code = %s)"
        )
        params.append(filters.batch_code)
    if not scope.all_scenes:
        scope_parts: list[str] = []
        if scope.scene_codes:
            scope_parts.append("ts.scene_code = ANY(%s)")
            params.append(scope.scene_codes)
        if scope.allow_unknown:
            scope_parts.append("ts.scene_code IS NULL")
        clauses.append("(" + " OR ".join(scope_parts) + ")" if scope_parts else "false")
    conf_rank = CONFIDENCE_CASE.format(expr="ts.confidence")
    sql = f"""
LEFT JOIN LATERAL (
    SELECT ts.id, ts.scene_code, ts.confidence, ts.batch_id, ts.source_url,
           ts.confidence_basis, ts.video_id, ts.provider
    FROM task_sources ts
    WHERE {' AND '.join(clauses)}
    ORDER BY
      {conf_rank},
      COALESCE(ts.scene_code, ''),
      ts.id
    LIMIT 1
) {alias} ON true
"""
    return sql, params


def claim_match_predicate(scope: SceneScope, filters: TaskFilter,
                          *, best_alias: str = "best") -> str:
    """A candidate must have matching same-evidence ``best`` when filters
    require a source row (named scene, batch, or non-unknown confidence).

    ``source_confidence=unknown`` is not a no-op: it matches explicit unknown
    evidence or legacy tasks with no current sources, never high/medium/low
    evidence.
    """
    if not scope.can_claim:
        return "false"
    no_sources = NO_CURRENT_SOURCES.format(task="t")
    if filters.requires_source_row():
        return f"{best_alias}.id IS NOT NULL"
    if (filters.source_scene_value() == "unknown"
            or filters.source_confidence == "unknown"):
        return f"({best_alias}.id IS NOT NULL OR {no_sources})"
    if scope.all_scenes:
        return "true"
    if scope.allow_unknown:
        return f"({best_alias}.id IS NOT NULL OR {no_sources})"
    return f"{best_alias}.id IS NOT NULL"


CLAIMABLE_BASE = """
FROM annotation_tasks t
JOIN annotation_versions v
  ON v.task_id = t.id AND v.lifecycle = 'draft'
{best_join}
WHERE t.status = 'pending' AND t.eligible
  {assignment_clause}
  AND NOT EXISTS (
        SELECT 1 FROM task_annotator_blocks b
        WHERE b.task_id = t.id AND b.user_id = %s
      )
  AND (t.reserved_for_user_id = %s OR t.reserved_for_user_id IS NULL)
  AND ({match_pred})
"""


def build_claimable_from(scope: SceneScope, filters: TaskFilter,
                         user_id, *, include_assigned: bool = False) -> tuple[str, list]:
    """Return FROM/WHERE SQL plus positional params (best-source params + uid, uid)."""
    best_sql, best_params = best_source_lateral(scope=scope, filters=filters)
    match_pred = claim_match_predicate(scope, filters)
    assignment_clause = (
        "" if include_assigned
        else "AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.task_id = t.id)"
    )
    sql = CLAIMABLE_BASE.format(
        best_join=best_sql, match_pred=match_pred,
        assignment_clause=assignment_clause,
    )
    return sql, [*best_params, user_id, user_id]


def order_sql(policy: str) -> str:
    """Reservation, then confidence (unless fifo), then allocation_order, task_id."""
    reserved = "(COALESCE(t.reserved_for_user_id = %s, false)) DESC"
    if policy == "fifo":
        return f"{reserved}, t.allocation_order, t.id"
    conf = CONFIDENCE_CASE.format(expr="COALESCE(best.confidence, 'unknown')")
    return f"{reserved}, {conf}, t.allocation_order, t.id"


def empty_pool_reason(*, scope: SceneScope, filters: TaskFilter,
                      total: int, pending: int, eligible_pending: int,
                      matching: int, matching_unassigned: int,
                      global_unassigned: int) -> str:
    if not scope.can_claim:
        return "no_scene_access"
    selected = filters.source_scene_value()
    if total == 0 or (pending > 0 and eligible_pending == 0):
        return "no_preprocessed"
    if eligible_pending == 0:
        return "all_completed"
    if matching == 0 and (selected or not scope.all_scenes or filters.batch_code
                          or filters.source_confidence):
        return "no_matching_scene"
    if matching_unassigned == 0 and eligible_pending > 0:
        return "temporarily_all_assigned"
    if matching_unassigned > 0:
        return "available"
    return "no_matching_scene"
