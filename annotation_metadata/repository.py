"""Persistence for sources, identities, predictions, reviews, and scopes.

Callers must supply an existing cursor; this module never opens connections
or commits. That keeps complete/save/review atomic with assignment release.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable

from psycopg.types.json import Json

from annotation_metadata.contracts import MediaIdentity, NormalizedSource, TaskFilter
from annotation_metadata.queries import SceneScope, load_scope, source_row_match_sql
from annotation_metadata.taxonomy import (
    CONFIDENCE_RANK,
    FALLBACK_SOURCE_SCENE,
    SCENE_BY_CODE,
    SCENE_CODES,
    SCENE_ORDER,
    effective_source_scene,
    ordered_scene_codes,
    scene_label,
    source_scene_label,
)
from annotation_repository import ConflictError, ValidationError


PUBLIC_SOURCE_FIELDS = (
    "id", "scene_code", "scene_label", "confidence", "confidence_basis",
    "provider", "video_id", "source_url", "batch_code", "source_type",
    "channel_title", "is_current", "revision",
)


def ensure_batch(cur, batch_code: str, *, name: str | None = None,
                 source_note: str = "") -> uuid.UUID:
    code = (batch_code or "").strip()
    if len(code) < 4 or code.isdigit():
        raise ValidationError(
            "batch_code must be a globally unique code such as crawler-2026-09-14-0914",
            code="invalid_batch_code",
            field="batch_code",
        )
    row = cur.execute(
        """INSERT INTO source_batches (id, batch_code, name, source_note)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (batch_code) DO UPDATE
             SET name = COALESCE(NULLIF(EXCLUDED.name, ''), source_batches.name)
           RETURNING id""",
        (uuid.uuid4(), code, name or code, source_note),
    ).fetchone()
    return row[0]


def ensure_default_scope(cur, user_id) -> None:
    cur.execute(
        """INSERT INTO annotator_scene_scopes (user_id, mode, allow_unknown, revision)
           VALUES (%s, 'all', false, 0)
           ON CONFLICT (user_id) DO NOTHING""",
        (user_id,),
    )


def lookup_identity(cur, identity: MediaIdentity | None) -> dict | None:
    if identity is None:
        return None
    row = cur.execute(
        """SELECT id, task_id, pcm_sha256
           FROM task_media_identities
           WHERE provider = %s AND external_id = %s AND variant = %s""",
        (identity.provider, identity.external_id, identity.variant),
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "task_id": row[1], "pcm_sha256": row[2]}


def identity_lock_key(identity: MediaIdentity) -> str:
    return f"scene-identity:{identity.provider}:{identity.external_id}:{identity.variant}"


def lock_identity(cur, identity: MediaIdentity) -> None:
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (identity_lock_key(identity),),
    )


def attach_identity(cur, task_id, identity: MediaIdentity) -> dict:
    existing = lookup_identity(cur, identity)
    if existing:
        if (identity.pcm_sha256 and existing["pcm_sha256"]
                and identity.pcm_sha256 != existing["pcm_sha256"]):
            raise ConflictError(
                "identity exists with a different PCM digest"
            )
        if existing["task_id"] != task_id:
            raise ConflictError(
                "identity is already bound to a different task"
            )
        if identity.pcm_sha256 and not existing["pcm_sha256"]:
            cur.execute(
                "UPDATE task_media_identities SET pcm_sha256 = %s WHERE id = %s",
                (identity.pcm_sha256, existing["id"]),
            )
        return existing
    new_id = uuid.uuid4()
    inserted = cur.execute(
        """INSERT INTO task_media_identities
               (id, task_id, provider, external_id, variant, pcm_sha256)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (provider, external_id, variant) DO NOTHING
           RETURNING id, task_id, pcm_sha256""",
        (new_id, task_id, identity.provider, identity.external_id,
         identity.variant, identity.pcm_sha256),
    ).fetchone()
    if inserted:
        return {"id": inserted[0], "task_id": inserted[1], "pcm_sha256": inserted[2]}
    raced = lookup_identity(cur, identity)
    if not raced:
        raise ConflictError("identity insert raced and then disappeared")
    if raced["task_id"] != task_id:
        raise ConflictError(
            "identity is already bound to a different task"
        )
    return raced


def find_pcm_duplicates(cur, pcm_sha256: str, *, exclude_task_id=None) -> list[dict]:
    if not pcm_sha256:
        return []
    rows = cur.execute(
        """SELECT i.task_id, i.provider, i.external_id, t.rel_path
           FROM task_media_identities i
           JOIN annotation_tasks t ON t.id = i.task_id
           WHERE i.pcm_sha256 = %s AND (%s::uuid IS NULL OR i.task_id <> %s)""",
        (pcm_sha256, exclude_task_id, exclude_task_id),
    ).fetchall()
    return [
        {"task_id": str(row[0]), "provider": row[1], "external_id": row[2],
         "rel_path": row[3]}
        for row in rows
    ]


def sync_source_record(cur, *, task_id, batch_id,
                       source: NormalizedSource) -> str:
    """Insert or no-op a current source row. Content changes append a revision.

    Ownership of (batch_id, record_key) is checked before any no-op or
    version change so a digest update cannot silently move evidence onto
    another task.
    """
    current = cur.execute(
        """SELECT id, content_digest, revision, task_id
           FROM task_sources
           WHERE batch_id = %s AND record_key = %s AND is_current
           FOR UPDATE""",
        (batch_id, source.record_key),
    ).fetchone()
    if current and current[3] != task_id:
        raise ConflictError(
            f"source record {source.record_key} belongs to another task"
        )
    if current and current[1] == source.content_digest:
        return "unchanged"
    if current:
        cur.execute(
            "UPDATE task_sources SET is_current = false WHERE id = %s",
            (current[0],),
        )
        revision = int(current[2]) + 1
        action = "revised"
    else:
        revision = 1
        action = "created"
    cur.execute(
        """INSERT INTO task_sources
               (id, task_id, batch_id, record_key, revision, is_current,
                scene_code, confidence, confidence_basis, source_type,
                source_url, video_id, channel_id, channel_title, provider,
                raw_record, content_digest)
           VALUES (%s, %s, %s, %s, %s, true, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            uuid.uuid4(), task_id, batch_id, source.record_key, revision,
            source.scene_code, source.confidence, source.confidence_basis,
            source.source_type, source.source_url, source.video_id,
            source.channel_id, source.channel_title, source.provider,
            Json(source.raw_record or {}), source.content_digest,
        ),
    )
    return action


_SOURCE_COLUMNS = """
s.id, s.scene_code, s.confidence, s.confidence_basis,
s.provider, s.video_id, s.source_url, b.batch_code,
s.source_type, s.channel_title, s.is_current, s.revision,
s.record_key, s.channel_id
""".strip()


def _source_from_row(row) -> dict:
    return {
        "id": str(row[0]),
        "scene_code": row[1],
        "scene_label": source_scene_label(row[1]),
        "confidence": row[2],
        "confidence_basis": row[3] or "",
        "provider": row[4],
        "video_id": row[5],
        "source_url": row[6],
        "batch_code": row[7],
        "source_type": row[8] or "",
        "channel_title": row[9],
        "is_current": bool(row[10]),
        "revision": int(row[11]),
        "record_key": row[12],
        "channel_id": row[13],
    }


def list_current_sources(cur, task_id) -> list[dict]:
    rows = cur.execute(
        f"""SELECT {_SOURCE_COLUMNS}
           FROM task_sources s
           JOIN source_batches b ON b.id = s.batch_id
           WHERE s.task_id = %s AND s.is_current
           ORDER BY s.scene_code NULLS LAST, s.id""",
        (task_id,),
    ).fetchall()
    return [_source_from_row(row) for row in rows]


def get_source(cur, source_id) -> dict | None:
    """Load one source row by id, including historical (non-current) evidence."""
    if source_id in (None, ""):
        return None
    row = cur.execute(
        f"""SELECT {_SOURCE_COLUMNS}
           FROM task_sources s
           JOIN source_batches b ON b.id = s.batch_id
           WHERE s.id = %s""",
        (source_id,),
    ).fetchone()
    return _source_from_row(row) if row else None


def list_source_history(cur, task_id) -> list[dict]:
    rows = cur.execute(
        """SELECT s.id, s.record_key, s.revision, s.is_current, s.scene_code,
                  s.confidence, s.confidence_basis, s.source_type, s.source_url,
                  s.video_id, s.channel_id, s.channel_title, s.provider,
                  s.raw_record, s.content_digest, s.created_at,
                  b.batch_code, s.batch_id
           FROM task_sources s
           JOIN source_batches b ON b.id = s.batch_id
           WHERE s.task_id = %s
           ORDER BY s.record_key, s.revision, s.id""",
        (task_id,),
    ).fetchall()
    return [_complete_source_from_row(row) for row in rows]


def _complete_source_from_row(row) -> dict:
    raw = row[13] if isinstance(row[13], dict) else dict(row[13] or {})
    return {
        "id": str(row[0]),
        "record_key": row[1],
        "revision": int(row[2]),
        "is_current": bool(row[3]),
        "scene_code": row[4],
        "scene_label": scene_label(row[4]),
        "confidence": row[5],
        "confidence_basis": row[6] or "",
        "source_type": row[7] or "",
        "source_url": row[8],
        "video_id": row[9],
        "channel_id": row[10],
        "channel_title": row[11],
        "provider": row[12],
        "raw_record": raw,
        "content_digest": row[14],
        "created_at": row[15].isoformat() if row[15] else None,
        "batch_code": row[16],
        "batch_id": str(row[17]) if row[17] else None,
    }


def latest_prediction(cur, task_id) -> dict | None:
    row = cur.execute(
        """SELECT id, predicted_label, predicted_scene_code, score, score_meaning,
                  model_name, prompt_version, input_version_id, input_revision,
                  input_digest, created_at
           FROM task_scene_predictions
           WHERE task_id = %s
           ORDER BY created_at DESC, id DESC
           LIMIT 1""",
        (task_id,),
    ).fetchone()
    if not row:
        return None
    return _prediction_from_row(row)


def list_predictions(cur, task_id) -> list[dict]:
    rows = cur.execute(
        """SELECT id, predicted_label, predicted_scene_code, score, score_meaning,
                  model_name, prompt_version, input_version_id, input_revision,
                  input_digest, created_at
           FROM task_scene_predictions
           WHERE task_id = %s
           ORDER BY created_at, id""",
        (task_id,),
    ).fetchall()
    return [_prediction_from_row(row) for row in rows]


def _prediction_from_row(row) -> dict:
    return {
        "id": str(row[0]),
        "predicted_label": row[1],
        "predicted_scene_code": row[2],
        "score": float(row[3]) if row[3] is not None else None,
        "score_meaning": row[4],
        "model_name": row[5],
        "prompt_version": row[6] or "",
        "input_version_id": str(row[7]) if row[7] else None,
        "input_revision": row[8],
        "input_digest": row[9],
        "created_at": row[10].isoformat() if row[10] else None,
    }


def insert_prediction(cur, *, task_id, predicted_label: str,
                      model_name: str, prompt_version: str = "",
                      input_version_id=None, input_revision: int | None = None,
                      input_digest: str = "", score=None,
                      score_meaning: str | None = None,
                      predicted_scene_code: str | None = None) -> uuid.UUID:
    pred_id = uuid.uuid4()
    cur.execute(
        """INSERT INTO task_scene_predictions
               (id, task_id, input_version_id, input_revision, input_digest,
                model_name, prompt_version, predicted_label, predicted_scene_code,
                score, score_meaning)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (pred_id, task_id, input_version_id, input_revision,
         input_digest, model_name, prompt_version, predicted_label,
         predicted_scene_code, score, score_meaning),
    )
    return pred_id


def latest_review(cur, version_id) -> dict | None:
    if version_id is None:
        return None
    row = cur.execute(
        """SELECT id, review_no, status, note, actor_kind, actor_user_id,
                  actor_admin_action_id, operation_id, previous_review_id,
                  created_at
           FROM scene_reviews
           WHERE version_id = %s AND NOT superseded
           ORDER BY review_no DESC
           LIMIT 1""",
        (version_id,),
    ).fetchone()
    if not row:
        return None
    labels = [
        r[0] for r in cur.execute(
            """SELECT scene_code FROM scene_review_labels
               WHERE review_id = %s ORDER BY scene_code""",
            (row[0],),
        ).fetchall()
    ]
    return {
        "id": str(row[0]),
        "review_no": int(row[1]),
        "status": row[2],
        "note": row[3] or "",
        "scene_codes": labels,
        "actor_kind": row[4],
        "actor_user_id": str(row[5]) if row[5] else None,
        "actor_admin_action_id": str(row[6]) if row[6] else None,
        "operation_id": str(row[7]) if row[7] else None,
        "previous_review_id": str(row[8]) if row[8] else None,
        "created_at": row[9].isoformat() if row[9] else None,
    }


def review_equal(current: dict | None, status: str, scene_codes: Iterable[str],
                 note: str) -> bool:
    wanted = sorted(scene_codes)
    if current is None:
        return status == "pending" and not wanted and not (note or "")
    return (
        current["status"] == status
        and sorted(current.get("scene_codes") or []) == wanted
        and (current.get("note") or "") == (note or "")
    )


def append_review(cur, *, version_id, status: str, scene_codes: list[str],
                  note: str = "", actor_kind: str, actor_user_id=None,
                  actor_admin_action_id=None, operation_id=None) -> tuple[dict, bool]:
    current = latest_review(cur, version_id)
    if review_equal(current, status, scene_codes, note):
        if current is None:
            # Materialise an explicit pending row only when something writes.
            if status == "pending" and not scene_codes and not note:
                return {
                    "id": None, "status": "pending", "scene_codes": [],
                    "note": "", "review_no": 0,
                }, False
        return current, False
    if current:
        cur.execute(
            "UPDATE scene_reviews SET superseded = true WHERE id = %s",
            (current["id"],),
        )
        review_no = int(current["review_no"]) + 1
        previous_id = current["id"]
    else:
        review_no = 1
        previous_id = None
    review_id = uuid.uuid4()
    cur.execute(
        """INSERT INTO scene_reviews
               (id, version_id, review_no, status, note, actor_user_id,
                actor_admin_action_id, actor_kind, operation_id,
                previous_review_id, superseded)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false)""",
        (review_id, version_id, review_no, status, note or "",
         actor_user_id, actor_admin_action_id, actor_kind, operation_id,
         previous_id),
    )
    for code in scene_codes:
        if code not in SCENE_CODES:
            raise ValidationError(f"unknown scene code: {code}")
        cur.execute(
            """INSERT INTO scene_review_labels (review_id, scene_code)
               VALUES (%s, %s)""",
            (review_id, code),
        )
    created = latest_review(cur, version_id)
    return created, True


def list_reviews_for_task(cur, task_id) -> list[dict]:
    rows = cur.execute(
        """SELECT r.id, r.version_id, r.review_no, r.status, r.note, r.actor_kind,
                  r.actor_user_id, r.actor_admin_action_id, r.operation_id,
                  r.previous_review_id, r.superseded, r.created_at,
                  v.version_no, v.lifecycle
           FROM scene_reviews r
           JOIN annotation_versions v ON v.id = r.version_id
           WHERE v.task_id = %s
           ORDER BY v.version_no, r.review_no, r.id""",
        (task_id,),
    ).fetchall()
    if not rows:
        return []
    review_ids = [row[0] for row in rows]
    label_rows = cur.execute(
        """SELECT review_id, scene_code FROM scene_review_labels
           WHERE review_id = ANY(%s)
           ORDER BY review_id, scene_code""",
        (review_ids,),
    ).fetchall()
    labels: dict[str, list[str]] = {}
    for review_id, code in label_rows:
        labels.setdefault(str(review_id), []).append(code)
    return [
        {
            "id": str(row[0]),
            "version_id": str(row[1]),
            "review_no": int(row[2]),
            "status": row[3],
            "note": row[4] or "",
            "actor_kind": row[5],
            "actor_user_id": str(row[6]) if row[6] else None,
            "actor_admin_action_id": str(row[7]) if row[7] else None,
            "operation_id": str(row[8]) if row[8] else None,
            "previous_review_id": str(row[9]) if row[9] else None,
            "superseded": bool(row[10]),
            "created_at": row[11].isoformat() if row[11] else None,
            "version_no": int(row[12]),
            "version_lifecycle": row[13],
            "scene_codes": labels.get(str(row[0]), []),
        }
        for row in rows
    ]


def list_media_identities(cur, task_id=None) -> list[dict]:
    sql = """SELECT id, task_id, provider, external_id, variant, pcm_sha256, created_at
             FROM task_media_identities"""
    params: list = []
    if task_id is not None:
        sql += " WHERE task_id = %s"
        params.append(task_id)
    sql += " ORDER BY provider, external_id, variant, id"
    rows = cur.execute(sql, params).fetchall()
    return [
        {
            "id": str(row[0]),
            "task_id": str(row[1]),
            "provider": row[2],
            "external_id": row[3],
            "variant": row[4],
            "pcm_sha256": row[5],
            "created_at": row[6].isoformat() if row[6] else None,
        }
        for row in rows
    ]


def list_import_runs(cur) -> list[dict]:
    rows = cur.execute(
        """SELECT r.id, r.batch_id, b.batch_code, r.snapshot_sha256, r.snapshot_bytes,
                  r.contract_version, r.status, r.processed_count, r.counts,
                  r.error_report, r.checkpoint, r.started_at, r.completed_at
           FROM source_import_runs r
           JOIN source_batches b ON b.id = r.batch_id
           ORDER BY r.started_at, r.id"""
    ).fetchall()
    return [
        {
            "id": str(row[0]),
            "batch_id": str(row[1]),
            "batch_code": row[2],
            "snapshot_sha256": row[3],
            "snapshot_bytes": row[4],
            "contract_version": int(row[5]),
            "status": row[6],
            "processed_count": int(row[7]),
            "counts": row[8] if isinstance(row[8], dict) else dict(row[8] or {}),
            "error_report": row[9] if isinstance(row[9], list) else list(row[9] or []),
            "checkpoint": row[10] if isinstance(row[10], dict) else dict(row[10] or {}),
            "started_at": row[11].isoformat() if row[11] else None,
            "completed_at": row[12].isoformat() if row[12] else None,
        }
        for row in rows
    ]


def path_aliases_for_task(cur, task_id) -> list[str]:
    row = cur.execute(
        "SELECT extra->'path_aliases' FROM annotation_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if not row or row[0] is None:
        return []
    aliases = row[0]
    if isinstance(aliases, list):
        return [str(item) for item in aliases]
    return []


def assignment_claim_context(cur, user_id) -> dict | None:
    row = cur.execute(
        """SELECT claim_scene_code, claim_source_id, claim_policy, claim_confidence
           FROM assignments WHERE user_id = %s""",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "scene_code": row[0],
        "source_id": str(row[1]) if row[1] else None,
        "policy": row[2],
        "confidence": row[3],
    }


def list_active_scenes(cur, *, active_only: bool = False) -> list[dict]:
    sql = """SELECT code, label_zh, label_en, active, sort_order
             FROM scenes"""
    if active_only:
        sql += " WHERE active"
    sql += " ORDER BY sort_order, code"
    rows = cur.execute(sql).fetchall()
    return [
        {"code": row[0], "label_zh": row[1], "label_en": row[2],
         "active": bool(row[3]), "sort_order": int(row[4])}
        for row in rows
    ]


def scope_payload(scope: SceneScope) -> dict:
    if scope.all_scenes:
        codes = list(SCENE_ORDER)
        payload_codes = list(SCENE_ORDER)
    else:
        codes = ordered_scene_codes(scope.effective_scene_codes())
        payload_codes = list(codes)
    scenes = [
        {
            "code": code,
            "label": scene_label(code),
            "label_zh": str(SCENE_BY_CODE[code]["label_zh"]),
            "label_en": str(SCENE_BY_CODE[code]["label_en"]),
        }
        for code in codes
    ]
    return {
        "mode": scope.mode,
        "allow_unknown": scope.allows_fallback_source() if scope.mode == "restricted" else scope.allow_unknown,
        "revision": scope.revision,
        "scene_codes": payload_codes if scope.mode == "restricted" else list(scope.scene_codes),
        "scenes": scenes,
    }


def replace_scope(cur, user_id, *, mode: str, scene_codes: list[str],
                  allow_unknown: bool, expected_revision: int) -> SceneScope:
    row = cur.execute(
        """SELECT revision FROM annotator_scene_scopes
           WHERE user_id = %s FOR UPDATE""",
        (user_id,),
    ).fetchone()
    current_revision = int(row[0]) if row else 0
    if current_revision != int(expected_revision):
        raise ConflictError(
            f"scene scope revision conflict, server revision is {current_revision}"
        )
    new_revision = current_revision + 1
    cur.execute(
        """INSERT INTO annotator_scene_scopes
               (user_id, mode, allow_unknown, revision, updated_at)
           VALUES (%s, %s, %s, %s, now())
           ON CONFLICT (user_id) DO UPDATE SET
               mode = EXCLUDED.mode,
               allow_unknown = EXCLUDED.allow_unknown,
               revision = EXCLUDED.revision,
               updated_at = now()""",
        (user_id, mode, allow_unknown, new_revision),
    )
    cur.execute("DELETE FROM annotator_scene_access WHERE user_id = %s", (user_id,))
    if mode == "restricted":
        for code in scene_codes:
            cur.execute(
                """INSERT INTO annotator_scene_access (user_id, scene_code)
                   VALUES (%s, %s)""",
                (user_id, code),
            )
    return load_scope(cur, user_id)


def metadata_summaries(cur, task_ids: list,
                       filters: TaskFilter | None = None) -> dict[str, dict]:
    """Matched source summary for list/stats rows.

    Scene, confidence, and batch come from the same matching evidence rows
    as the list filter. Full provenance stays on detailed task metadata.
    """
    if not task_ids:
        return {}
    task_filter = filters if filters is not None else TaskFilter()
    match_sql, match_params = source_row_match_sql(task_filter, src_alias="s")
    source_rows = cur.execute(
        f"""SELECT s.task_id, s.scene_code, s.confidence, b.batch_code
           FROM task_sources s
           JOIN source_batches b ON b.id = s.batch_id
           WHERE s.task_id = ANY(%s) AND {match_sql}
           ORDER BY s.task_id, s.scene_code NULLS LAST, b.batch_code, s.id""",
        (list(task_ids), *match_params),
    ).fetchall()
    grouped: dict[str, dict] = {}
    for task_id, scene_code, confidence, batch_code in source_rows:
        key = str(task_id)
        item = grouped.setdefault(key, {
            "source_scenes": [],
            "source_confidence": "unknown",
            "batch_codes": [],
            "scene_confidence_pairs": [],
        })
        effective = effective_source_scene(scene_code)
        if effective not in item["source_scenes"]:
            item["source_scenes"].append(effective)
        if batch_code and batch_code not in item["batch_codes"]:
            item["batch_codes"].append(batch_code)
        pair = f"{effective}:{confidence or 'unknown'}"
        if pair not in item["scene_confidence_pairs"]:
            item["scene_confidence_pairs"].append(pair)
        current = CONFIDENCE_RANK.get(item["source_confidence"], 4)
        candidate = CONFIDENCE_RANK.get(confidence or "unknown", 4)
        if candidate < current:
            item["source_confidence"] = confidence or "unknown"
    review_rows = cur.execute(
        """SELECT t.id, COALESCE(sr.status, 'pending')
           FROM annotation_tasks t
           LEFT JOIN LATERAL (
               SELECT status FROM scene_reviews
               WHERE version_id = t.current_published_version_id
                 AND NOT superseded
               ORDER BY review_no DESC LIMIT 1
           ) sr ON true
           WHERE t.id = ANY(%s)""",
        (list(task_ids),),
    ).fetchall()
    reviews = {str(row[0]): row[1] for row in review_rows}
    prediction_rows = cur.execute(
        """SELECT DISTINCT ON (task_id)
                  task_id, predicted_label, predicted_scene_code
           FROM task_scene_predictions
           WHERE task_id = ANY(%s)
           ORDER BY task_id, created_at DESC, id DESC""",
        (list(task_ids),),
    ).fetchall()
    predictions = {
        str(row[0]): {
            "prediction_label": row[1],
            "prediction_scene": row[2],
        }
        for row in prediction_rows
    }
    human_rows = cur.execute(
        """SELECT t.id, l.scene_code
           FROM annotation_tasks t
           JOIN LATERAL (
               SELECT id FROM scene_reviews
               WHERE version_id = t.current_published_version_id
                 AND NOT superseded
               ORDER BY review_no DESC LIMIT 1
           ) sr ON true
           JOIN scene_review_labels l ON l.review_id = sr.id
           WHERE t.id = ANY(%s)
           ORDER BY t.id, l.scene_code""",
        (list(task_ids),),
    ).fetchall()
    human_scenes: dict[str, list[str]] = {}
    for task_id, code in human_rows:
        human_scenes.setdefault(str(task_id), []).append(code)
    result = {}
    for task_id in task_ids:
        key = str(task_id)
        base = grouped.get(key, {
            "source_scenes": [],
            "source_confidence": "unknown",
            "batch_codes": [],
            "scene_confidence_pairs": [],
        })
        if not base["source_scenes"]:
            base = {
                **base,
                "source_scenes": [FALLBACK_SOURCE_SCENE],
            }
        prediction = predictions.get(key, {})
        result[key] = {
            **base,
            "review_status": reviews.get(key, "pending"),
            "prediction_label": prediction.get("prediction_label"),
            "prediction_scene": prediction.get("prediction_scene"),
            "human_scenes": human_scenes.get(key, []),
        }
    return result


def list_batches(cur) -> list[dict]:
    rows = cur.execute(
        """SELECT id, batch_code, name, source_note, created_at
           FROM source_batches ORDER BY created_at DESC, batch_code"""
    ).fetchall()
    return [
        {"id": str(row[0]), "batch_code": row[1], "name": row[2],
         "source_note": row[3], "created_at": row[4].isoformat() if row[4] else None}
        for row in rows
    ]
