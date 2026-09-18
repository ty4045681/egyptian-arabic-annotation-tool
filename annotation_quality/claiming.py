"""Cross-check candidate predicates, offset sampling, and claim-type split."""

from __future__ import annotations

import secrets

from psycopg.errors import IntegrityError

from annotation_metadata.contracts import TaskFilter
from annotation_metadata.queries import (
    SceneScope,
    best_source_lateral,
    claim_match_predicate,
    claim_source_exists_sql,
)
from annotation_quality.repository import (
    create_cross_check_claim,
    cross_check_allowed,
)


CLAIM_TYPE_RANGE = 10000
MAX_LOCK_ATTEMPTS = 8


def default_claim_rng():
    return secrets.SystemRandom()


def draw_claim_type_hit(rng, sampling_rate_bps: int) -> bool:
    """One draw in [0, 10000). Does not resample on lock retries."""
    return int(rng.randrange(CLAIM_TYPE_RANGE)) < int(sampling_rate_bps)


def _candidate_sql(scope: SceneScope, filters: TaskFilter, user_id):
    """Shared FROM/WHERE for count, offset fetch, and post-lock revalidate.

    Distinct task rows only: source matching is EXISTS, so multi-source
    evidence cannot increase draw weight.
    """
    match_sql, match_params = claim_source_exists_sql(scope, filters, task_alias="t")
    from_sql = """
FROM annotation_tasks t
JOIN annotation_versions pv
  ON pv.id = t.current_published_version_id
JOIN annotators orig
  ON orig.id = pv.submitted_by_user_id
JOIN annotation_versions bv
  ON bv.id = t.baseline_version_id
"""
    where_sql = f"""
t.eligible
AND t.status = 'annotated'
AND t.reserved_for_user_id IS NULL
AND (t.processing_token IS NULL
     OR t.processing_lease_until IS NULL
     OR t.processing_lease_until <= now())
AND t.baseline_quality IN ('exact', 'reconstructed', 'reprocessed')
AND pv.lifecycle = 'published'
AND pv.target_status = 'annotated'
AND pv.submitted_by_user_id IS NOT NULL
AND pv.submitted_by_user_id IS DISTINCT FROM %s
AND bv.lifecycle = 'baseline'
AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.task_id = t.id)
AND NOT EXISTS (
      SELECT 1 FROM annotation_versions d
      WHERE d.task_id = t.id AND d.lifecycle = 'draft')
AND NOT EXISTS (
      SELECT 1 FROM cross_check_rounds r
      WHERE r.task_id = t.id
        AND r.state IN ('in_progress', 'awaiting_review'))
AND NOT EXISTS (
      SELECT 1 FROM task_annotation_participants p
      WHERE p.task_id = t.id AND p.user_id = %s)
AND NOT EXISTS (
      SELECT 1 FROM task_annotator_blocks b
      WHERE b.task_id = t.id AND b.user_id = %s)
AND NOT EXISTS (
      SELECT 1 FROM cross_check_rounds r
      WHERE r.original_version_id = t.current_published_version_id
        AND r.state NOT IN ('cancelled', 'invalidated'))
AND NOT EXISTS (
      SELECT 1 FROM cross_check_rounds r
      WHERE r.final_version_id = t.current_published_version_id
        AND r.state IN ('passed', 'adjudicated'))
AND ({match_sql})
"""
    params = [user_id, user_id, user_id, *match_params]
    return from_sql, where_sql, params


def count_cross_check_candidates(cur, scope: SceneScope, filters: TaskFilter,
                                 user_id) -> int:
    if not scope.can_claim:
        return 0
    from_sql, where_sql, params = _candidate_sql(scope, filters, user_id)
    row = cur.execute(
        "SELECT count(*) " + from_sql + " WHERE " + where_sql,
        params,
    ).fetchone()
    return int(row[0] if row else 0)


def fetch_cross_check_task_id(cur, scope: SceneScope, filters: TaskFilter,
                              user_id, offset: int):
    from_sql, where_sql, params = _candidate_sql(scope, filters, user_id)
    row = cur.execute(
        """SELECT t.id """ + from_sql + " WHERE " + where_sql + """
           ORDER BY t.allocation_order, t.id
           OFFSET %s LIMIT 1""",
        (*params, int(offset)),
    ).fetchone()
    return row[0] if row else None


def _abandon_savepoint(cur) -> None:
    cur.execute("ROLLBACK TO SAVEPOINT cc_claim_lock")
    cur.execute("RELEASE SAVEPOINT cc_claim_lock")


def _lock_task(cur, task_id):
    row = cur.execute(
        """SELECT id FROM annotation_tasks
           WHERE id = %s
           FOR UPDATE SKIP LOCKED""",
        (task_id,),
    ).fetchone()
    return row[0] if row else None


def _revalidate_locked_candidate(cur, task_id, scope: SceneScope,
                                 filters: TaskFilter, user_id):
    from_sql, where_sql, params = _candidate_sql(scope, filters, user_id)
    return cur.execute(
        """SELECT t.id, t.rel_path, t.filename, t.folder, t.duration, t.status,
                  t.current_published_version_id, t.baseline_version_id,
                  t.baseline_quality, pv.submitted_by_user_id
           """ + from_sql + " WHERE t.id = %s AND " + where_sql,
        (task_id, *params),
    ).fetchone()


def matching_best_source(cur, task_id, scope: SceneScope, filters: TaskFilter):
    best_sql, best_params = best_source_lateral(scope=scope, filters=filters)
    pred = claim_match_predicate(scope, filters)
    return cur.execute(
        f"""SELECT t.id, best.id, best.scene_code, best.confidence
            FROM annotation_tasks t
            {best_sql}
            WHERE t.id = %s AND ({pred})""",
        (*best_params, task_id),
    ).fetchone()


def try_claim_cross_check(
    cur, *, user_id, scope: SceneScope, filters: TaskFilter,
    claim_policy: str, settings: dict, rng,
) -> tuple[dict | None, str]:
    """Lock one published candidate and create a round.

    Returns (created-row-or-None, 'claimed'|'empty'|'busy').
    Missing baseline on one row is excluded from the shared predicate so it
    cannot 500 the whole claim.
    """
    if not scope.can_claim or not cross_check_allowed(settings):
        return None, "empty"

    had_candidates = False
    for _attempt in range(MAX_LOCK_ATTEMPTS):
        count = count_cross_check_candidates(cur, scope, filters, user_id)
        if count <= 0:
            break
        had_candidates = True
        offset = int(rng.randrange(count))
        task_id = fetch_cross_check_task_id(
            cur, scope, filters, user_id, offset,
        )
        if task_id is None:
            continue
        cur.execute("SAVEPOINT cc_claim_lock")
        try:
            locked = _lock_task(cur, task_id)
            if locked is None:
                _abandon_savepoint(cur)
                continue
            row = _revalidate_locked_candidate(
                cur, task_id, scope, filters, user_id,
            )
            if row is None:
                _abandon_savepoint(cur)
                continue
            best = matching_best_source(cur, task_id, scope, filters)
            if best is None:
                _abandon_savepoint(cur)
                continue
            _task_id, best_id, best_scene, best_confidence = best
            created = create_cross_check_claim(
                cur,
                user_id=user_id,
                task_id=row[0],
                rel_path=row[1],
                filename=row[2],
                folder=row[3],
                duration=row[4],
                status=row[5],
                original_version_id=row[6],
                original_annotator_id=row[9],
                baseline_version_id=row[7],
                baseline_quality=row[8],
                filters=filters,
                claim_policy=claim_policy,
                settings=settings,
                best_id=best_id,
                best_scene=best_scene,
                best_confidence=best_confidence,
            )
            if created is None:
                _abandon_savepoint(cur)
                continue
            cur.execute("RELEASE SAVEPOINT cc_claim_lock")
            return created, "claimed"
        except IntegrityError:
            _abandon_savepoint(cur)
            continue
    return None, "busy" if had_candidates else "empty"


def build_cross_check_assignment_payload(created: dict) -> dict:
    return {
        "assigned": True,
        "task_id": str(created["task_id"]),
        "mode": "cross_check",
        "lease_token": str(created["lease_token"]),
        "status": created["status"],
        "version_id": str(created["draft_id"]),
        "revision": created["revision"],
        "rel_path": created["rel_path"],
        "filename": created["filename"],
        "folder": created["folder"],
        "duration": created["duration"],
        "skip_reasons": [],
        "resumed": False,
        "cross_check": {
            "round_id": str(created["round_id"]),
            "state": "in_progress",
        },
    }
