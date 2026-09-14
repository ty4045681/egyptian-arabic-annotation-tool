"""Versioned metadata sidecar export/import public facade.

PostgreSQL custom dump remains the complete backup. This JSON file is a
supported metadata round trip alongside legacy ``export-json``. Credentials,
session cookies, and active lease tokens are never exported.

Identity matching is exact UUID restore against already-present annotation
rows, or an explicit mapping file. Usernames and unrelated pathnames are
never used as silent remaps.

export/import require an idle connection (they start REPEATABLE READ) or an
already-open REPEATABLE READ/SERIALIZABLE transaction. They never commit a
caller's unrelated work.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError
from psycopg import pq

from annotation_metadata import CONTRACT_VERSION, SCHEMA_VERSION
from annotation_metadata.contracts import TaskFilter
from annotation_metadata.export_contract import (
    AUDIO_LEVEL_NOTICE, EXCEL_HEADERS, EXCLUDED_EXPORT_FIELDS, FORMAT_NAME,
    GENERATED_METADATA_KEYS, IDENTITY_HELP, IDENTITY_MODEL, METADATA_FILENAME,
    IdentityMapping, MetadataImportError, comparable_document, dumps_canonical,
    json_ready, load_identity_mapping, load_metadata_path, parse_metadata_document,
    write_json_atomic,
)
from annotation_metadata.export_import import apply_import, plan_import
from annotation_metadata.features import metadata_write_enabled
from annotation_metadata.queries import load_scope, metadata_filter_sql
from annotation_metadata.repository import (
    latest_prediction, latest_review, list_active_scenes, list_import_runs,
    list_media_identities, list_predictions, list_reviews_for_task,
    list_source_history, path_aliases_for_task, scope_payload,
)
from annotation_repository import ForbiddenError, ValidationError

__all__ = [
    "AUDIO_LEVEL_NOTICE", "EXCEL_HEADERS", "EXCLUDED_EXPORT_FIELDS",
    "FORMAT_NAME", "GENERATED_METADATA_KEYS", "IDENTITY_HELP", "IDENTITY_MODEL",
    "METADATA_FILENAME", "IdentityMapping", "MetadataImportError",
    "applied_filter_context", "build_metadata_document",
    "build_training_scene_sidecar", "comparable_document", "excel_column_values",
    "export_metadata", "import_metadata", "json_ready", "parse_task_filter",
    "task_provenance_bundle", "verify_metadata_file",
    "verify_training_scene_sidecar", "write_json_atomic",
]


def parse_task_filter(values: dict | None) -> TaskFilter | None:
    if not values:
        return None
    cleaned = {
        key: value for key, value in values.items()
        if value not in (None, "", "all")
    }
    if not cleaned:
        return None
    try:
        parsed = TaskFilter.model_validate(cleaned)
    except PydanticValidationError as exc:
        raise ValidationError(str(exc), code="invalid_field", field="filter") from exc
    return None if parsed.is_empty() else parsed


def applied_filter_context(filters: TaskFilter | None) -> dict:
    if filters is None or filters.is_empty():
        return {
            "mode": "all_data",
            "task_filter": None,
            "semantics": "default: every task; same-evidence TaskFilter when set",
        }
    dumped = {
        key: value for key, value in filters.model_dump().items()
        if value is not None
    }
    return {
        "mode": "task_filter",
        "task_filter": dumped,
        "semantics": (
            "same-evidence TaskFilter: source scene/confidence/batch must match "
            "one current source row; review/model/human filters stay separate"
        ),
    }


def excel_column_values(summary: dict) -> list:
    return [
        ",".join(summary.get("source_scenes") or []),
        summary.get("source_confidence") or "unknown",
        ";".join(summary.get("scene_confidence_pairs") or []),
        ",".join(summary.get("batch_codes") or []),
        summary.get("review_status") or "pending",
        ",".join(summary.get("human_scenes") or []),
        summary.get("prediction_label") or "",
    ]


def snapshot_connection_mode(conn) -> str:
    """Idle connections start REPEATABLE READ. Open RC transactions are refused."""
    if conn.info.transaction_status == pq.TransactionStatus.IDLE:
        return "idle"
    isolation = (conn.execute("SHOW transaction_isolation").fetchone()[0] or "").lower()
    isolation = isolation.replace("_", " ")
    if isolation in {"repeatable read", "serializable"}:
        return isolation.replace(" ", "_")
    raise RuntimeError(
        "metadata export/import requires an idle connection or an already-open "
        "REPEATABLE READ/SERIALIZABLE transaction; "
        f"found {isolation}. Refusing to treat READ COMMITTED as a snapshot."
    )


@contextmanager
def metadata_transaction(conn, *, read_only: bool = False):
    mode = snapshot_connection_mode(conn)
    with conn.transaction():
        if mode == "idle":
            sql = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
            if read_only:
                sql += ", READ ONLY"
            conn.execute(sql)
            yield "repeatable_read"
        else:
            yield mode


def _filtered_task_rows(cur, filters: TaskFilter | None) -> list[tuple]:
    where = "true"
    params: list = []
    if filters is not None and not filters.is_empty():
        where, params = metadata_filter_sql(filters, task_alias="t")
    return cur.execute(
        f"""SELECT t.id, t.rel_path, t.legacy_audio_key, t.allocation_order,
                   t.current_published_version_id, t.extra
            FROM annotation_tasks t
            WHERE {where}
            ORDER BY t.allocation_order, t.id""",
        params,
    ).fetchall()


def build_metadata_document(conn, *, filters: TaskFilter | None = None) -> dict:
    """Consistent snapshot with canonical ordering."""
    with metadata_transaction(conn, read_only=True) as isolation:
        exported_at = conn.execute("SELECT now()").fetchone()[0]
        with conn.cursor() as cur:
            return _build_document(
                cur, filters=filters, exported_at=exported_at, isolation=isolation,
            )


def _build_document(cur, *, filters: TaskFilter | None, exported_at, isolation: str) -> dict:
    scenes = list_active_scenes(cur)
    scenes.sort(key=lambda item: (item.get("sort_order", 0), item.get("code") or ""))
    batches = [
        {
            "id": str(row[0]),
            "batch_code": row[1],
            "name": row[2],
            "source_note": row[3] or "",
            "created_at": row[4].isoformat() if row[4] else None,
        }
        for row in cur.execute(
            """SELECT id, batch_code, name, source_note, created_at
               FROM source_batches ORDER BY batch_code, id"""
        ).fetchall()
    ]
    task_rows = _filtered_task_rows(cur, filters)
    task_ids = [row[0] for row in task_rows]
    versions = [
        {
            "id": str(row[0]),
            "task_id": str(row[1]),
            "version_no": int(row[2]),
            "lifecycle": row[3],
            "revision": int(row[4]),
            "created_at": row[5].isoformat() if row[5] else None,
            "submitted_at": row[6].isoformat() if row[6] else None,
        }
        for row in (
            cur.execute(
                """SELECT id, task_id, version_no, lifecycle, revision,
                          created_at, submitted_at
                   FROM annotation_versions
                   WHERE task_id = ANY(%s)
                   ORDER BY task_id, version_no, id""",
                (task_ids,),
            ).fetchall()
            if task_ids else []
        )
    ]
    users = [
        {
            "id": str(row[0]),
            "username": row[1],
            "created_at": row[2].isoformat() if row[2] else None,
            "status": row[3],
        }
        for row in cur.execute(
            """SELECT id, username, created_at, status
               FROM annotators ORDER BY username, id"""
        ).fetchall()
    ]
    scopes = []
    for user in users:
        payload = scope_payload(load_scope(cur, user["id"]))
        scopes.append({
            "user_id": user["id"],
            "username": user["username"],
            **payload,
        })
    import_runs = list_import_runs(cur)
    media_identities = [
        item for item in list_media_identities(cur)
        if not task_ids or uuid.UUID(item["task_id"]) in {row[0] for row in task_rows}
    ]
    tasks = []
    used_batch_codes: set[str] = set()
    for task_id, rel_path, legacy_key, _order, published_id, extra in task_rows:
        history = list_source_history(cur, task_id)
        current = [item for item in history if item.get("is_current")]
        current.sort(key=lambda item: (item.get("scene_code") or "", item.get("id") or ""))
        predictions = list_predictions(cur, task_id)
        reviews = list_reviews_for_task(cur, task_id)
        aliases = path_aliases_for_task(cur, task_id)
        extra_aliases = []
        if isinstance(extra, dict) and isinstance(extra.get("path_aliases"), list):
            extra_aliases = [str(item) for item in extra["path_aliases"]]
        aliases = aliases or extra_aliases
        for item in history:
            if item.get("batch_code"):
                used_batch_codes.add(item["batch_code"])
        tasks.append({
            "task_id": str(task_id),
            "rel_path": rel_path,
            "legacy_audio_key": legacy_key,
            "path_aliases": aliases,
            "sources": current,
            "source_history": history,
            "prediction": latest_prediction(cur, task_id),
            "predictions": predictions,
            "published_review": latest_review(cur, published_id),
            "reviews": reviews,
        })
    if filters is not None and not filters.is_empty():
        import_runs = [
            item for item in import_runs if item.get("batch_code") in used_batch_codes
        ]
        batches = [item for item in batches if item["batch_code"] in used_batch_codes]
        wanted_tasks = {item["task_id"] for item in tasks}
        versions = [item for item in versions if item["task_id"] in wanted_tasks]
        media_identities = [
            item for item in media_identities if item["task_id"] in wanted_tasks
        ]
    review_admin_ids = {
        review["actor_admin_action_id"]
        for task in tasks
        for review in task["reviews"]
        if review.get("actor_admin_action_id")
    }
    admin_actions = []
    if review_admin_ids:
        admin_actions = [
            {
                "id": str(row[0]),
                "operation_id": str(row[1]) if row[1] else None,
                "action_type": row[2],
                "reason": row[3] or "",
                "request_hash": row[4],
                "status": row[5],
                "admin_key_id": row[6],
                "summary": row[7] if isinstance(row[7], dict) else dict(row[7] or {}),
                "created_at": row[8].isoformat() if row[8] else None,
                "completed_at": row[9].isoformat() if row[9] else None,
            }
            for row in cur.execute(
                """SELECT id, operation_id, action_type, reason, request_hash, status,
                          admin_key_id, summary, created_at, completed_at
                   FROM admin_actions
                   WHERE id = ANY(%s)
                   ORDER BY created_at, id""",
                ([uuid.UUID(item) for item in review_admin_ids],),
            ).fetchall()
        ]
    audit_events = []
    if task_ids:
        event_rows = cur.execute(
            """SELECT id, user_id, task_id, version_id, event_type, from_status,
                      to_status, details, created_at, admin_action_id
               FROM annotation_events
               WHERE task_id = ANY(%s)
                 AND (
                   event_type IN ('scene_review_corrected', 'completed', 'reopened')
                   OR details ? 'scene_review_id'
                 )
               ORDER BY created_at, id""",
            (task_ids,),
        ).fetchall()
        for row in event_rows:
            details = row[7] if isinstance(row[7], dict) else dict(row[7] or {})
            audit_events.append({
                "user_id": str(row[1]) if row[1] else None,
                "task_id": str(row[2]) if row[2] else None,
                "version_id": str(row[3]) if row[3] else None,
                "event_type": row[4],
                "from_status": row[5],
                "to_status": row[6],
                "details": details,
                "created_at": row[8].isoformat() if row[8] else None,
                "admin_action_id": str(row[9]) if row[9] else None,
                "publication_review_id": details.get("scene_review_id"),
            })
    document = {
        "format": FORMAT_NAME,
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "exported_at": exported_at.isoformat() if hasattr(exported_at, "isoformat") else exported_at,
        "snapshot_isolation": isolation,
        "applied_filters": applied_filter_context(filters),
        "identity_model": IDENTITY_MODEL,
        "excluded_fields": list(EXCLUDED_EXPORT_FIELDS),
        "scenes": scenes,
        "batches": batches,
        "import_runs": import_runs,
        "media_identities": media_identities,
        "users": users,
        "versions": versions,
        "admin_actions": admin_actions,
        "audit_events": audit_events,
        "tasks": tasks,
        "scopes": scopes,
    }
    return json_ready(document)


def export_metadata(conn, output: Path, *, filters: TaskFilter | None = None) -> dict:
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    document = build_metadata_document(conn, filters=filters)
    path = output / METADATA_FILENAME
    write_json_atomic(path, document)
    return {
        "path": str(path),
        "tasks": len(document.get("tasks") or []),
        "scopes": len(document.get("scopes") or []),
        "batches": len(document.get("batches") or []),
        "applied_filters": document.get("applied_filters"),
    }


def verify_metadata_file(path: Path) -> dict:
    document = load_metadata_path(path)
    return {
        "ok": True,
        "tasks": len(document.get("tasks") or []),
        "scopes": len(document.get("scopes") or []),
        "batches": len(document.get("batches") or []),
        "import_runs": len(document.get("import_runs") or []),
        "reviews": sum(len(task.get("reviews") or []) for task in document.get("tasks") or []),
        "predictions": sum(
            len(task.get("predictions") or []) for task in document.get("tasks") or []
        ),
        "format": document.get("format") or FORMAT_NAME,
        "schema_version": document.get("schema_version"),
        "applied_filters": document.get("applied_filters"),
    }


def import_metadata(
    conn, path: Path, *, mapping_path: Path | None = None,
    dry_run: bool = False, replace_scopes: bool = False,
) -> dict:
    document = load_metadata_path(path)
    mapping = load_identity_mapping(mapping_path)
    if not metadata_write_enabled() and not dry_run:
        raise ForbiddenError("Metadata writes are disabled")
    with metadata_transaction(conn, read_only=dry_run):
        with conn.cursor() as cur:
            plan = plan_import(
                cur, document, mapping, replace_scopes=replace_scopes,
            )
            if plan["errors"]:
                raise MetadataImportError(
                    plan["errors"][0],
                    errors=plan["errors"],
                    report={k: v for k, v in plan.items() if k != "ops"},
                )
            if dry_run:
                return {
                    "ok": True, "dry_run": True, "applied": False,
                    "inserted": dict(plan["inserted"]),
                    "unchanged": dict(plan["unchanged"]),
                    "scopes_skipped": list(plan["scopes_skipped"]),
                    "identity_maps": dict(plan["identity_maps"]),
                    "replace_scopes": plan["replace_scopes"],
                }
            apply_import(cur, plan)
            return {
                "ok": True, "dry_run": False, "applied": True,
                "inserted": dict(plan["inserted"]),
                "unchanged": dict(plan["unchanged"]),
                "scopes_skipped": list(plan["scopes_skipped"]),
                "identity_maps": dict(plan["identity_maps"]),
                "replace_scopes": plan["replace_scopes"],
            }


def task_provenance_bundle(cur, task_id) -> dict:
    """Shared source/model/human bundle for Excel and training sidecars."""
    history = list_source_history(cur, task_id)
    current = [item for item in history if item.get("is_current")]
    return {
        "task_id": str(task_id),
        "level": "audio",
        "notice": AUDIO_LEVEL_NOTICE,
        "sources": current,
        "source_history": history,
        "predictions": list_predictions(cur, task_id),
        "prediction": latest_prediction(cur, task_id),
        "reviews": list_reviews_for_task(cur, task_id),
    }


def build_training_scene_sidecar(conn, *, links: list[dict],
                                 applied_filters: dict | None = None) -> dict:
    """Task/segment-linked sidecar. ``data.json`` field set stays unchanged."""
    task_ids = []
    seen: set[str] = set()
    for link in links:
        task_id = str(link["task_id"])
        if task_id not in seen:
            seen.add(task_id)
            task_ids.append(task_id)
    with conn.cursor() as cur:
        tasks = []
        for task_id in task_ids:
            bundle = task_provenance_bundle(cur, task_id)
            rel = cur.execute(
                "SELECT rel_path FROM annotation_tasks WHERE id = %s",
                (uuid.UUID(task_id),),
            ).fetchone()
            bundle["rel_path"] = rel[0] if rel else None
            tasks.append(bundle)
    segments = [
        {
            "audio": link["audio"],
            "task_id": str(link["task_id"]),
            "segment_id": int(link["segment_id"]),
            "level": "audio",
            "notice": AUDIO_LEVEL_NOTICE,
        }
        for link in links
    ]
    document = {
        "format": "training_scene_sidecar",
        "schema_version": SCHEMA_VERSION,
        "level": "audio",
        "notice": AUDIO_LEVEL_NOTICE,
        "applied_filters": applied_filters or applied_filter_context(None),
        "tasks": tasks,
        "segments": segments,
    }
    verify_training_scene_sidecar(document, [link["audio"] for link in links])
    return json_ready(document)


def verify_training_scene_sidecar(document: dict, exported_audio: list[str]) -> None:
    if document.get("format") != "training_scene_sidecar":
        raise MetadataImportError("unknown training sidecar format")
    if document.get("level") != "audio":
        raise MetadataImportError("training sidecar must be audio-level evidence")
    exported = list(exported_audio)
    sidecar_audio = [item["audio"] for item in document.get("segments") or []]
    if sidecar_audio != exported:
        extra = sorted(set(sidecar_audio) - set(exported))
        missing = sorted(set(exported) - set(sidecar_audio))
        raise MetadataImportError(
            f"training sidecar audio mismatch extra={extra} missing={missing}"
        )
    task_ids = {item["task_id"] for item in document.get("tasks") or []}
    linked = {item["task_id"] for item in document.get("segments") or []}
    orphans = sorted(task_ids - linked)
    dangling = sorted(linked - task_ids)
    if orphans or dangling:
        raise MetadataImportError(
            f"training sidecar task references orphans={orphans} dangling={dangling}"
        )
