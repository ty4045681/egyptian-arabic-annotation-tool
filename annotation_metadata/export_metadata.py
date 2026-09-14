"""Versioned metadata sidecar export/import.

PostgreSQL custom dump remains the complete backup. This JSON file is a
supported metadata round trip alongside legacy ``export-json``. Credentials,
session cookies, and active lease tokens are never exported.

Identity matching is exact UUID restore against already-present annotation
rows, or an explicit mapping file. Usernames and unrelated pathnames are
never used as silent remaps.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from psycopg import pq
from psycopg.types.json import Json

from annotation_metadata import CONTRACT_VERSION, SCHEMA_VERSION
from annotation_metadata.contracts import StrictModel, TaskFilter
from annotation_metadata.features import metadata_write_enabled
from annotation_metadata.ingestion import register_path_alias
from annotation_metadata.queries import metadata_filter_sql
from annotation_metadata.repository import (
    latest_prediction,
    latest_review,
    list_active_scenes,
    list_import_runs,
    list_media_identities,
    list_predictions,
    list_reviews_for_task,
    list_source_history,
    path_aliases_for_task,
    scope_payload,
)
from annotation_metadata.queries import load_scope
from annotation_metadata.taxonomy import (
    CONFIDENCE_LEVELS, REVIEW_STATUSES, SCENE_CODES, validate_review_labels,
)
from annotation_repository import ForbiddenError, ValidationError

FORMAT_NAME = "annotation_metadata"
METADATA_FILENAME = "metadata.v1.json"
AUDIO_LEVEL_NOTICE = (
    "Audio-level source, model, and human scene evidence for the parent "
    "recording. Not an inferred per-segment scene label and not a measure "
    "of precise scene-trainable hours."
)
EXCEL_HEADERS = [
    "User", "Folder", "Audio", "Duration (s)", "Status",
    "Source scenes", "Source confidence", "Source scene confidences", "Batches",
    "Scene review", "Human scenes", "Model prediction",
]
IDENTITY_MODEL = {
    "task": {
        "match": "exact annotation_tasks.id",
        "mapping": "mapping.tasks",
        "never": ["username", "rel_path", "filename"],
        "notes": (
            "The target task row must already exist. rel_path and path_aliases "
            "are identity evidence and necessary aliases, not remap keys."
        ),
    },
    "version": {
        "match": "exact annotation_versions.id belonging to the mapped task",
        "mapping": "mapping.versions",
        "never": ["version_no without task"],
    },
    "user": {
        "match": "exact annotators.id",
        "mapping": "mapping.users",
        "never": ["username"],
        "notes": "username is a display field only.",
    },
    "admin_action": {
        "match": "exact admin_actions.id",
        "mapping": "mapping.admin_actions",
        "never": ["admin session cookie", "token_digest", "csrf_digest"],
        "notes": "Stubs omit session tokens. Request cookies are not exported.",
    },
    "import_run": {
        "match": "exact source_import_runs.id, else (batch_code, snapshot_sha256)",
        "mapping": "mapping.import_runs",
    },
    "source": {
        "match": "exact task_sources.id on the mapped task",
        "mapping": "mapping.sources",
        "notes": "Same id with different digest/history is a conflict.",
    },
    "prediction": {
        "match": "exact task_scene_predictions.id",
        "mapping": "mapping.predictions",
    },
    "review": {
        "match": "exact scene_reviews.id linked to the mapped version",
        "mapping": "mapping.reviews",
        "notes": "previous_review_id and publication-event scene_review_id must resolve.",
    },
    "batch": {
        "match": "preserve id when batch_code is new; otherwise batch_code is the stable key",
        "mapping": "mapping.batches",
        "notes": "Matching an existing batch_code with a different id records a reversible id map.",
    },
    "media_identity": {
        "match": "unique (provider, external_id, variant) on the mapped task",
        "mapping": "mapping.media_identities",
    },
}
IDENTITY_HELP = """
Identity rules (no silent remaps):
  * Tasks, versions, users, reviews, predictions, sources, admin actions,
    and import runs restore by exact UUID unless --mapping is supplied.
  * mapping JSON may contain only: mapping.tasks, mapping.versions,
    mapping.users, mapping.admin_actions, mapping.import_runs,
    mapping.sources, mapping.reviews, mapping.predictions, mapping.batches,
    mapping.media_identities.
  * Usernames, rel_path, and unrelated pathnames are not mapping keys.
  * Annotation text, task status, assignments, and scopes are not overwritten
    unless --replace-scopes is set for scopes only.
  * Credentials, session cookies, and active lease tokens are not exported.
  * PostgreSQL custom dump is the complete backup; this JSON is metadata only.
""".strip()

EXCLUDED_EXPORT_FIELDS = (
    "lease_token", "session_id", "token_digest", "csrf_digest",
    "processing_token", "password", "cookie", "active_sessions",
)


class MetadataImportError(ValueError):
    """Import rejected; the transaction must not mutate metadata."""

    def __init__(self, message: str, *, errors: list[str] | None = None,
                 report: dict | None = None):
        super().__init__(message)
        self.errors = list(errors or [message])
        self.report = report or {}


class IdentityMapping(StrictModel):
    tasks: dict[str, str] = {}
    versions: dict[str, str] = {}
    users: dict[str, str] = {}
    admin_actions: dict[str, str] = {}
    import_runs: dict[str, str] = {}
    sources: dict[str, str] = {}
    reviews: dict[str, str] = {}
    predictions: dict[str, str] = {}
    batches: dict[str, str] = {}
    media_identities: dict[str, str] = {}


def json_ready(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, bytes):
        raise TypeError("binary payloads are not metadata")
    return str(value)


def dumps_canonical(value: Any) -> str:
    return json.dumps(
        json_ready(value), ensure_ascii=False, sort_keys=True,
        indent=2, default=str,
    ) + "\n"


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(dumps_canonical(value), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


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


def _as_uuid(value, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise MetadataImportError(f"invalid UUID for {field}: {value!r}") from exc


def _parse_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MetadataImportError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _remap(table: str, export_id, mapping: IdentityMapping) -> uuid.UUID:
    raw = str(export_id)
    table_map = getattr(mapping, table)
    return _as_uuid(table_map.get(raw, raw), f"mapping.{table}")


def _resolve_metadata_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_dir():
        candidate = path / METADATA_FILENAME
        if candidate.is_file():
            return candidate
        raise MetadataImportError(f"missing {METADATA_FILENAME} in {path}")
    if not path.is_file():
        raise MetadataImportError(f"metadata file not found: {path}")
    return path


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


def _started_idle(conn) -> bool:
    return conn.info.transaction_status == pq.TransactionStatus.IDLE


def build_metadata_document(conn, *, filters: TaskFilter | None = None) -> dict:
    """Consistent REPEATABLE READ snapshot with canonical ordering.

    If the caller already has an open transaction, read from that snapshot
    and do not commit or roll it back.
    """
    idle = _started_idle(conn)
    with conn.transaction():
        if idle:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        exported_at = conn.execute("SELECT now()").fetchone()[0]
        with conn.cursor() as cur:
            return _build_document(cur, filters=filters, exported_at=exported_at)


def _build_document(cur, *, filters: TaskFilter | None, exported_at) -> dict:
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
    if filters is not None and not filters.is_empty() and task_ids:
        wanted = {str(row[0]) for row in task_rows}
        media_identities = [
            item for item in media_identities if item["task_id"] in wanted
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
        "snapshot_isolation": "repeatable_read",
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


def comparable_document(document: dict) -> dict:
    data = json.loads(json.dumps(json_ready(document), ensure_ascii=False))
    data.pop("exported_at", None)
    return data


def verify_metadata_file(path: Path) -> dict:
    document = _load_document(path)
    errors = _validate_document_shape(document)
    if errors:
        raise MetadataImportError(
            errors[0], errors=errors,
            report={"ok": False, "errors": errors},
        )
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


def _load_document(path: Path) -> dict:
    target = _resolve_metadata_path(Path(path))
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MetadataImportError(f"malformed metadata JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MetadataImportError("metadata document must be a JSON object")
    return data


def _validate_document_shape(data: dict) -> list[str]:
    errors: list[str] = []
    fmt = data.get("format")
    if fmt not in (None, FORMAT_NAME):
        errors.append(f"unknown metadata format: {fmt!r}")
    try:
        version = int(data.get("schema_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version != SCHEMA_VERSION:
        errors.append("unsupported metadata schema_version")
    try:
        contract = int(data.get("contract_version") or CONTRACT_VERSION)
    except (TypeError, ValueError):
        contract = -1
    if contract != CONTRACT_VERSION:
        errors.append("unsupported metadata contract_version")
    if not isinstance(data.get("tasks"), list):
        errors.append("tasks must be a list")
        return errors
    seen_tasks: set[str] = set()
    for index, task in enumerate(data.get("tasks") or []):
        if not isinstance(task, dict) or not task.get("task_id"):
            errors.append(f"tasks[{index}] is missing task_id")
            continue
        task_id = str(task["task_id"])
        if task_id in seen_tasks:
            errors.append(f"duplicate task_id in export: {task_id}")
        seen_tasks.add(task_id)
        history = task.get("source_history") or task.get("sources") or []
        if not isinstance(history, list):
            errors.append(f"tasks[{index}].source_history must be a list")
    if not isinstance(data.get("scopes") or [], list):
        errors.append("scopes must be a list")
    if not isinstance(data.get("batches") or [], list):
        errors.append("batches must be a list")
    return errors


def load_identity_mapping(path: Path | None) -> IdentityMapping:
    if path is None:
        return IdentityMapping()
    target = Path(path).expanduser().resolve()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MetadataImportError(f"malformed mapping JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise MetadataImportError("mapping file must be a JSON object")
    forbidden = {
        "usernames", "username", "by_username", "rel_path", "rel_paths",
        "by_rel_path", "pathname", "pathnames", "by_path",
    }
    bad = sorted(set(raw) & forbidden)
    if bad:
        raise MetadataImportError(
            "mapping must not remap by username or pathname: " + ", ".join(bad)
        )
    try:
        mapping = IdentityMapping.model_validate(raw)
    except PydanticValidationError as exc:
        raise MetadataImportError(f"unknown or invalid mapping fields: {exc}") from exc
    for table, pairs in mapping.model_dump().items():
        for source, dest in pairs.items():
            _as_uuid(source, f"mapping.{table}.key")
            _as_uuid(dest, f"mapping.{table}.value")
    return mapping


def import_metadata(
    conn, path: Path, *, mapping_path: Path | None = None,
    dry_run: bool = False, replace_scopes: bool = False,
) -> dict:
    document = _load_document(path)
    errors = _validate_document_shape(document)
    if errors:
        raise MetadataImportError(errors[0], errors=errors)
    mapping = load_identity_mapping(mapping_path)
    if not metadata_write_enabled() and not dry_run:
        raise ForbiddenError("Metadata writes are disabled")
    idle = _started_idle(conn)
    with conn.transaction():
        if idle:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        with conn.cursor() as cur:
            plan = _plan_import(
                cur, document, mapping, replace_scopes=replace_scopes,
            )
            if plan["errors"]:
                raise MetadataImportError(
                    plan["errors"][0],
                    errors=plan["errors"],
                    report={k: v for k, v in plan.items() if k != "ops"},
                )
            if dry_run:
                return _report_from_plan(plan, dry_run=True, applied=False)
            _apply_import(cur, plan)
            return _report_from_plan(plan, dry_run=False, applied=True)


def _report_from_plan(plan: dict, *, dry_run: bool, applied: bool) -> dict:
    return {
        "ok": True,
        "dry_run": dry_run,
        "applied": applied,
        "inserted": dict(plan["inserted"]),
        "unchanged": dict(plan["unchanged"]),
        "scopes_skipped": list(plan["scopes_skipped"]),
        "identity_maps": dict(plan["identity_maps"]),
        "replace_scopes": plan["replace_scopes"],
    }


def _lookup(cur, sql: str, params) -> Any:
    row = cur.execute(sql, params).fetchone()
    return row[0] if row else None


def _existing_source(cur, source_id):
    return cur.execute(
        """SELECT id, task_id, record_key, revision, is_current, scene_code,
                  confidence, content_digest, batch_id, confidence_basis
           FROM task_sources WHERE id = %s""",
        (source_id,),
    ).fetchone()


def _score_equal(left, right) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return abs(float(left) - float(right)) < 1e-12


def _validate_source(source: dict) -> None:
    confidence = source.get("confidence") or "unknown"
    if confidence not in CONFIDENCE_LEVELS:
        raise MetadataImportError(f"invalid source confidence: {confidence}")
    code = source.get("scene_code")
    if code and code not in SCENE_CODES:
        raise MetadataImportError(f"unknown source scene_code: {code}")


def _validate_review(review: dict) -> None:
    status = review.get("status") or "pending"
    if status not in REVIEW_STATUSES:
        raise MetadataImportError(f"invalid review status: {status}")
    try:
        validate_review_labels(status, list(review.get("scene_codes") or []))
    except ValueError as exc:
        raise MetadataImportError(str(exc)) from exc


def _validate_prediction(pred: dict) -> None:
    score = pred.get("score")
    if score is not None:
        try:
            number = float(score)
        except (TypeError, ValueError) as exc:
            raise MetadataImportError("prediction score must be a number or null") from exc
        if number < 0 or number > 1:
            raise MetadataImportError("prediction score must be between 0 and 1")
    revision = pred.get("input_revision")
    if revision is not None:
        if isinstance(revision, bool) or not isinstance(revision, int):
            try:
                if str(int(revision)) != str(revision).strip():
                    raise ValueError
                int(revision)
            except (TypeError, ValueError) as exc:
                raise MetadataImportError("prediction input_revision must be an integer") from exc
    code = pred.get("predicted_scene_code")
    if code and code not in SCENE_CODES:
        raise MetadataImportError(f"unknown predicted_scene_code: {code}")


def _plan_import(cur, document: dict, mapping: IdentityMapping,
                 *, replace_scopes: bool) -> dict:
    errors: list[str] = []
    inserted = {
        "batches": 0, "import_runs": 0, "media_identities": 0,
        "path_aliases": 0, "sources": 0, "predictions": 0, "reviews": 0,
        "review_labels": 0, "admin_actions": 0, "audit_events": 0, "scopes": 0,
    }
    unchanged = dict(inserted)
    scopes_skipped: list[str] = []
    identity_maps: dict[str, dict[str, str]] = {
        "tasks": dict(mapping.tasks),
        "versions": dict(mapping.versions),
        "users": dict(mapping.users),
        "batches": dict(mapping.batches),
        "admin_actions": dict(mapping.admin_actions),
        "import_runs": dict(mapping.import_runs),
        "sources": dict(mapping.sources),
        "reviews": dict(mapping.reviews),
        "predictions": dict(mapping.predictions),
        "media_identities": dict(mapping.media_identities),
    }
    ops: list[tuple] = []

    def map_id(table: str, export_id) -> uuid.UUID:
        return _remap(table, export_id, mapping)

    export_task_ids = {str(task["task_id"]) for task in document.get("tasks") or []}
    export_version_ids = {str(item["id"]) for item in document.get("versions") or []}
    export_user_ids = {str(item["id"]) for item in document.get("users") or []}
    for table, pairs in mapping.model_dump().items():
        known = {
            "tasks": export_task_ids,
            "versions": export_version_ids,
            "users": export_user_ids,
            "admin_actions": {str(item["id"]) for item in document.get("admin_actions") or []},
            "import_runs": {str(item["id"]) for item in document.get("import_runs") or []},
            "batches": {str(item["id"]) for item in document.get("batches") or []},
            "sources": {
                str(src["id"])
                for task in document.get("tasks") or []
                for src in (task.get("source_history") or task.get("sources") or [])
                if src.get("id")
            },
            "reviews": {
                str(rev["id"])
                for task in document.get("tasks") or []
                for rev in (task.get("reviews") or [])
                if rev.get("id")
            },
            "predictions": {
                str(pred["id"])
                for task in document.get("tasks") or []
                for pred in (task.get("predictions") or (
                    [task["prediction"]] if task.get("prediction") else []
                ))
                if pred and pred.get("id")
            },
            "media_identities": {
                str(item["id"]) for item in document.get("media_identities") or []
            },
        }.get(table, set())
        for source in pairs:
            if known and source not in known:
                errors.append(f"mapping.{table} refers to id absent from export: {source}")

    dest_task_used: dict[str, str] = {}
    for task in document.get("tasks") or []:
        export_id = str(task["task_id"])
        dest_id = map_id("tasks", export_id)
        owner = dest_task_used.get(str(dest_id))
        if owner and owner != export_id:
            errors.append(
                f"mapping.tasks maps both {owner} and {export_id} onto {dest_id}"
            )
        dest_task_used[str(dest_id)] = export_id
        row = cur.execute(
            "SELECT id, rel_path, status FROM annotation_tasks WHERE id = %s",
            (dest_id,),
        ).fetchone()
        if not row:
            errors.append(
                f"dangling task mapping: {export_id} -> {dest_id} "
                "(target annotation_tasks row is missing)"
            )
            continue
        identity_maps["tasks"][export_id] = str(dest_id)

    for item in document.get("versions") or []:
        export_id = str(item["id"])
        dest_id = map_id("versions", export_id)
        dest_task = identity_maps["tasks"].get(str(item["task_id"]))
        row = cur.execute(
            "SELECT id, task_id FROM annotation_versions WHERE id = %s",
            (dest_id,),
        ).fetchone()
        if not row:
            errors.append(
                f"dangling version mapping: {export_id} -> {dest_id}"
            )
            continue
        if dest_task and str(row[1]) != dest_task:
            errors.append(
                f"version {export_id} belongs to task {row[1]}, mapped task is {dest_task}"
            )
        identity_maps["versions"][export_id] = str(dest_id)

    for item in document.get("users") or []:
        export_id = str(item["id"])
        dest_id = map_id("users", export_id)
        row = cur.execute("SELECT id FROM annotators WHERE id = %s", (dest_id,)).fetchone()
        if not row:
            errors.append(f"dangling user mapping: {export_id} -> {dest_id}")
            continue
        identity_maps["users"][export_id] = str(dest_id)

    for scene in document.get("scenes") or []:
        code = scene.get("code")
        if code and code not in SCENE_CODES:
            errors.append(f"unknown scene code in export: {code}")

    for batch in document.get("batches") or []:
        export_id = str(batch["id"])
        dest_id = map_id("batches", export_id)
        code = batch.get("batch_code")
        if not code:
            errors.append(f"batch {export_id} is missing batch_code")
            continue
        by_code = cur.execute(
            "SELECT id FROM source_batches WHERE batch_code = %s", (code,),
        ).fetchone()
        by_id = cur.execute(
            "SELECT id, batch_code FROM source_batches WHERE id = %s", (dest_id,),
        ).fetchone()
        if by_code and by_id and by_code[0] != by_id[0]:
            errors.append(
                f"conflicting batch identity: code {code} is {by_code[0]}, id maps to {by_id[0]}"
            )
            continue
        if by_id and by_id[1] != code:
            errors.append(
                f"batch id {dest_id} already has batch_code {by_id[1]}, export has {code}"
            )
            continue
        if by_code:
            identity_maps["batches"][export_id] = str(by_code[0])
            unchanged["batches"] += 1
        else:
            identity_maps["batches"][export_id] = str(dest_id)
            inserted["batches"] += 1
            ops.append(("batch", batch, dest_id))

    for run in document.get("import_runs") or []:
        export_id = str(run["id"])
        dest_id = map_id("import_runs", export_id)
        batch_export = str(run.get("batch_id") or "")
        dest_batch = identity_maps["batches"].get(batch_export)
        if not dest_batch:
            # batch_code fallback
            code = run.get("batch_code")
            row = cur.execute(
                "SELECT id FROM source_batches WHERE batch_code = %s", (code,),
            ).fetchone() if code else None
            dest_batch = str(row[0]) if row else None
        if not dest_batch:
            errors.append(f"import run {export_id} has dangling batch")
            continue
        dest_batch_uuid = uuid.UUID(dest_batch)
        existing = cur.execute(
            "SELECT id, snapshot_sha256 FROM source_import_runs WHERE id = %s",
            (dest_id,),
        ).fetchone()
        same_key = cur.execute(
            """SELECT id FROM source_import_runs
               WHERE batch_id = %s AND snapshot_sha256 = %s""",
            (dest_batch_uuid, run.get("snapshot_sha256")),
        ).fetchone()
        if existing and existing[1] != run.get("snapshot_sha256"):
            errors.append(f"import run {export_id} conflicts with an existing run")
            continue
        if existing:
            identity_maps["import_runs"][export_id] = str(existing[0])
            unchanged["import_runs"] += 1
        elif same_key:
            identity_maps["import_runs"][export_id] = str(same_key[0])
            unchanged["import_runs"] += 1
        else:
            identity_maps["import_runs"][export_id] = str(dest_id)
            inserted["import_runs"] += 1
            ops.append(("import_run", run, dest_id, dest_batch_uuid))

    for action in document.get("admin_actions") or []:
        export_id = str(action["id"])
        dest_id = map_id("admin_actions", export_id)
        existing = cur.execute(
            "SELECT id, action_type FROM admin_actions WHERE id = %s",
            (dest_id,),
        ).fetchone()
        if existing:
            if existing[1] != action.get("action_type"):
                errors.append(f"conflicting admin_action identity {export_id}")
                continue
            identity_maps["admin_actions"][export_id] = str(existing[0])
            unchanged["admin_actions"] += 1
        else:
            op_id = action.get("operation_id") or str(uuid.uuid4())
            taken = cur.execute(
                "SELECT id FROM admin_actions WHERE operation_id = %s",
                (_as_uuid(op_id, "admin_action.operation_id"),),
            ).fetchone()
            if taken:
                errors.append(
                    f"admin_action operation_id {op_id} already used by {taken[0]}"
                )
                continue
            identity_maps["admin_actions"][export_id] = str(dest_id)
            inserted["admin_actions"] += 1
            ops.append(("admin_action", action, dest_id, op_id))

    for ident in document.get("media_identities") or []:
        export_id = str(ident["id"])
        dest_id = map_id("media_identities", export_id)
        dest_task = identity_maps["tasks"].get(str(ident["task_id"]))
        if not dest_task:
            errors.append(f"media identity {export_id} has dangling task")
            continue
        dest_task_uuid = uuid.UUID(dest_task)
        existing = cur.execute(
            """SELECT id, task_id FROM task_media_identities
               WHERE provider = %s AND external_id = %s AND variant = %s""",
            (ident.get("provider"), ident.get("external_id"), ident.get("variant")),
        ).fetchone()
        by_id = cur.execute(
            "SELECT id, task_id FROM task_media_identities WHERE id = %s",
            (dest_id,),
        ).fetchone()
        if existing and str(existing[1]) != dest_task:
            errors.append(
                f"media identity {export_id} is bound to a different task"
            )
            continue
        if by_id and str(by_id[1]) != dest_task:
            errors.append(f"media identity id {dest_id} belongs to another task")
            continue
        if existing:
            identity_maps["media_identities"][export_id] = str(existing[0])
            unchanged["media_identities"] += 1
        else:
            identity_maps["media_identities"][export_id] = str(dest_id)
            inserted["media_identities"] += 1
            ops.append(("media_identity", ident, dest_id, dest_task_uuid))

    for task in document.get("tasks") or []:
        export_task = str(task["task_id"])
        dest_task = identity_maps["tasks"].get(export_task)
        if not dest_task:
            continue
        dest_task_uuid = uuid.UUID(dest_task)
        for alias in task.get("path_aliases") or []:
            if not alias or alias == task.get("rel_path"):
                continue
            present = cur.execute(
                """SELECT 1 FROM annotation_tasks
                   WHERE id = %s AND COALESCE(extra->'path_aliases', '[]'::jsonb) ? %s""",
                (dest_task_uuid, alias),
            ).fetchone()
            if present:
                unchanged["path_aliases"] += 1
            else:
                inserted["path_aliases"] += 1
                ops.append(("path_alias", dest_task_uuid, alias))
        history = list(task.get("source_history") or [])
        if not history:
            history = list(task.get("sources") or [])
        history.sort(key=lambda item: (
            item.get("record_key") or "", int(item.get("revision") or 1),
            item.get("id") or "",
        ))
        for source in history:
            if not source.get("id"):
                errors.append(f"source on task {export_task} is missing id")
                continue
            try:
                _validate_source(source)
            except MetadataImportError as exc:
                errors.append(str(exc))
                continue
            export_id = str(source["id"])
            dest_id = map_id("sources", export_id)
            dest_batch = identity_maps["batches"].get(str(source.get("batch_id") or ""))
            if not dest_batch and source.get("batch_code"):
                dest_batch = next(
                    (
                        identity_maps["batches"].get(str(batch["id"]))
                        for batch in document.get("batches") or []
                        if batch.get("batch_code") == source.get("batch_code")
                    ),
                    None,
                )
                if not dest_batch:
                    row = cur.execute(
                        "SELECT id FROM source_batches WHERE batch_code = %s",
                        (source.get("batch_code"),),
                    ).fetchone()
                    dest_batch = str(row[0]) if row else None
            if not dest_batch:
                errors.append(f"source {export_id} has dangling batch")
                continue
            dest_batch_uuid = uuid.UUID(dest_batch)
            existing = _existing_source(cur, dest_id)
            if existing:
                same = (
                    str(existing[1]) == dest_task
                    and existing[2] == source.get("record_key")
                    and int(existing[3]) == int(source.get("revision") or 1)
                    and existing[7] == source.get("content_digest")
                    and (existing[9] or "") == (source.get("confidence_basis") or "")
                    and existing[5] == source.get("scene_code")
                    and (existing[6] or "unknown") == (source.get("confidence") or "unknown")
                )
                if not same:
                    errors.append(
                        f"conflicting source identity {export_id} -> {dest_id}"
                    )
                    continue
                identity_maps["sources"][export_id] = str(existing[0])
                unchanged["sources"] += 1
                continue
            occupied = None
            if source.get("is_current", True):
                occupied = cur.execute(
                    """SELECT id, task_id FROM task_sources
                       WHERE batch_id = %s AND record_key = %s AND is_current""",
                    (dest_batch_uuid, source.get("record_key")),
                ).fetchone()
            if occupied and str(occupied[0]) != str(dest_id):
                errors.append(
                    f"current source ({source.get('batch_code')}, "
                    f"{source.get('record_key')}) already exists as {occupied[0]}"
                )
                continue
            identity_maps["sources"][export_id] = str(dest_id)
            inserted["sources"] += 1
            ops.append(("source", source, dest_id, dest_task_uuid, dest_batch_uuid))

        predictions = list(task.get("predictions") or [])
        if not predictions and task.get("prediction"):
            predictions = [task["prediction"]]
        for pred in predictions:
            if not pred or not pred.get("id"):
                continue
            try:
                _validate_prediction(pred)
            except MetadataImportError as exc:
                errors.append(str(exc))
                continue
            export_id = str(pred["id"])
            dest_id = map_id("predictions", export_id)
            input_version = pred.get("input_version_id")
            dest_version = None
            if input_version:
                dest_version = identity_maps["versions"].get(str(input_version))
                if not dest_version:
                    dest_version = str(map_id("versions", input_version))
                row = cur.execute(
                    "SELECT id, task_id FROM annotation_versions WHERE id = %s",
                    (uuid.UUID(dest_version),),
                ).fetchone()
                if not row:
                    errors.append(
                        f"prediction {export_id} has dangling input_version_id"
                    )
                    continue
                if str(row[1]) != dest_task:
                    errors.append(
                        f"prediction {export_id} input_version_id belongs to another task"
                    )
                    continue
            existing = cur.execute(
                """SELECT id, task_id, input_digest, model_name, predicted_label, score
                   FROM task_scene_predictions WHERE id = %s""",
                (dest_id,),
            ).fetchone()
            if existing:
                same = (
                    str(existing[1]) == dest_task
                    and existing[2] == pred.get("input_digest")
                    and existing[3] == pred.get("model_name")
                    and existing[4] == pred.get("predicted_label")
                    and _score_equal(existing[5], pred.get("score"))
                )
                if not same:
                    errors.append(f"conflicting prediction identity {export_id}")
                    continue
                identity_maps["predictions"][export_id] = str(existing[0])
                unchanged["predictions"] += 1
                continue
            identity_maps["predictions"][export_id] = str(dest_id)
            inserted["predictions"] += 1
            ops.append(("prediction", pred, dest_id, dest_task_uuid, dest_version))

        reviews = list(task.get("reviews") or [])
        if not reviews and task.get("published_review"):
            published = dict(task["published_review"])
            if published.get("id"):
                reviews = [published]
        reviews.sort(key=lambda item: (
            int(item.get("review_no") or 0), item.get("id") or "",
        ))
        for review in reviews:
            if not review.get("id"):
                continue
            try:
                _validate_review(review)
            except MetadataImportError as exc:
                errors.append(str(exc))
                continue
            export_id = str(review["id"])
            dest_id = map_id("reviews", export_id)
            version_export = review.get("version_id")
            if not version_export:
                errors.append(f"review {export_id} is missing version_id")
                continue
            dest_version = identity_maps["versions"].get(str(version_export))
            if not dest_version:
                dest_version = str(map_id("versions", version_export))
            row = cur.execute(
                "SELECT id, task_id FROM annotation_versions WHERE id = %s",
                (uuid.UUID(dest_version),),
            ).fetchone()
            if not row:
                errors.append(f"review {export_id} has dangling version_id")
                continue
            if str(row[1]) != dest_task:
                errors.append(
                    f"review {export_id} version is not on mapped task {dest_task}"
                )
                continue
            previous = review.get("previous_review_id")
            if previous and str(previous) not in identity_maps["reviews"] and previous not in {
                str(item.get("id")) for item in reviews
            }:
                # previous may already exist in dest
                prev_dest = map_id("reviews", previous)
                if not cur.execute(
                    "SELECT 1 FROM scene_reviews WHERE id = %s", (prev_dest,),
                ).fetchone() and str(previous) not in {
                    str(item.get("id")) for item in reviews
                }:
                    errors.append(f"review {export_id} has dangling previous_review_id")
                    continue
            actor_user = review.get("actor_user_id")
            dest_user = None
            if actor_user:
                dest_user = identity_maps["users"].get(str(actor_user))
                if not dest_user:
                    dest_user = str(map_id("users", actor_user))
                if not cur.execute(
                    "SELECT 1 FROM annotators WHERE id = %s",
                    (uuid.UUID(dest_user),),
                ).fetchone():
                    errors.append(f"review {export_id} has dangling actor_user_id")
                    continue
            admin_export = review.get("actor_admin_action_id")
            dest_admin = None
            if admin_export:
                dest_admin = identity_maps["admin_actions"].get(str(admin_export))
                if not dest_admin:
                    dest_admin = str(map_id("admin_actions", admin_export))
                planned = str(admin_export) in identity_maps["admin_actions"]
                exists = cur.execute(
                    "SELECT 1 FROM admin_actions WHERE id = %s",
                    (uuid.UUID(dest_admin),),
                ).fetchone()
                if not planned and not exists:
                    errors.append(
                        f"review {export_id} has dangling actor_admin_action_id"
                    )
                    continue
            existing = cur.execute(
                """SELECT id, version_id, review_no, status, note, superseded
                   FROM scene_reviews WHERE id = %s""",
                (dest_id,),
            ).fetchone()
            if existing:
                existing_labels = [
                    row[0] for row in cur.execute(
                        """SELECT scene_code FROM scene_review_labels
                           WHERE review_id = %s ORDER BY scene_code""",
                        (existing[0],),
                    ).fetchall()
                ]
                wanted_labels = list(review.get("scene_codes") or [])
                same = (
                    str(existing[1]) == dest_version
                    and int(existing[2]) == int(review.get("review_no") or 0)
                    and existing[3] == review.get("status")
                    and (existing[4] or "") == (review.get("note") or "")
                    and existing_labels == wanted_labels
                )
                if not same:
                    errors.append(f"conflicting review identity {export_id}")
                    continue
                identity_maps["reviews"][export_id] = str(existing[0])
                unchanged["reviews"] += 1
                continue
            occupied = cur.execute(
                """SELECT id FROM scene_reviews
                   WHERE version_id = %s AND review_no = %s""",
                (uuid.UUID(dest_version), int(review.get("review_no") or 0)),
            ).fetchone()
            if occupied:
                errors.append(
                    f"review_no {review.get('review_no')} already exists on version "
                    f"{dest_version}"
                )
                continue
            identity_maps["reviews"][export_id] = str(dest_id)
            inserted["reviews"] += 1
            labels = list(review.get("scene_codes") or [])
            inserted["review_labels"] += len(labels)
            ops.append((
                "review", review, dest_id, uuid.UUID(dest_version),
                dest_user, dest_admin, labels,
            ))

    for event in document.get("audit_events") or []:
        dest_task = identity_maps["tasks"].get(str(event.get("task_id") or ""))
        if event.get("task_id") and not dest_task:
            errors.append(f"audit event has dangling task {event.get('task_id')}")
            continue
        dest_version = None
        if event.get("version_id"):
            dest_version = identity_maps["versions"].get(str(event["version_id"]))
            if not dest_version:
                dest_version = str(map_id("versions", event["version_id"]))
            if not cur.execute(
                "SELECT 1 FROM annotation_versions WHERE id = %s",
                (uuid.UUID(dest_version),),
            ).fetchone():
                errors.append(
                    f"audit event has dangling version {event.get('version_id')}"
                )
                continue
        publication = (event.get("details") or {}).get("scene_review_id") or event.get(
            "publication_review_id"
        )
        if publication and str(publication) not in identity_maps["reviews"]:
            mapped_pub = mapping.reviews.get(str(publication), str(publication))
            if not cur.execute(
                "SELECT 1 FROM scene_reviews WHERE id = %s",
                (_as_uuid(mapped_pub, "publication_review_id"),),
            ).fetchone() and str(publication) not in {
                str(rev.get("id"))
                for task in document.get("tasks") or []
                for rev in (task.get("reviews") or [])
            }:
                errors.append(
                    f"audit event has dangling publication review {publication}"
                )
                continue
        match = None
        mapped_publication = ""
        if publication:
            mapped_publication = identity_maps["reviews"].get(
                str(publication), mapping.reviews.get(str(publication), str(publication)),
            )
        if dest_task:
            match = cur.execute(
                """SELECT 1 FROM annotation_events
                   WHERE task_id = %s AND event_type = %s
                     AND COALESCE(details->>'scene_review_id', '') = ANY(%s)""",
                (
                    uuid.UUID(dest_task), event.get("event_type"),
                    [str(publication or ""), str(mapped_publication or "")],
                ),
            ).fetchone()
        if match:
            unchanged["audit_events"] += 1
        else:
            inserted["audit_events"] += 1
            ops.append(("audit_event", event, dest_task, dest_version))

    for scope in document.get("scopes") or []:
        user_export = str(scope.get("user_id") or "")
        dest_user = identity_maps["users"].get(user_export)
        if not dest_user:
            continue
        dest_user_uuid = uuid.UUID(dest_user)
        existing = cur.execute(
            """SELECT mode, allow_unknown, revision FROM annotator_scene_scopes
               WHERE user_id = %s""",
            (dest_user_uuid,),
        ).fetchone()
        wanted_mode = scope.get("mode") or "all"
        wanted_unknown = bool(scope.get("allow_unknown"))
        wanted_codes = list(scope.get("scene_codes") or [])
        if existing:
            current_codes = [
                row[0] for row in cur.execute(
                    """SELECT scene_code FROM annotator_scene_access
                       WHERE user_id = %s ORDER BY scene_code""",
                    (dest_user_uuid,),
                ).fetchall()
            ]
            same = (
                existing[0] == wanted_mode
                and bool(existing[1]) == wanted_unknown
                and current_codes == wanted_codes
            )
            if same:
                unchanged["scopes"] += 1
                continue
            if not replace_scopes:
                scopes_skipped.append(str(dest_user_uuid))
                continue
        inserted["scopes"] += 1
        ops.append(("scope", scope, dest_user_uuid, replace_scopes or existing is None))

    apply_order = {
        "batch": 0, "import_run": 1, "media_identity": 2, "path_alias": 3,
        "source": 4, "prediction": 5, "admin_action": 6, "review": 7,
        "audit_event": 8, "scope": 9,
    }
    ops.sort(key=lambda item: apply_order[item[0]])

    return {
        "errors": errors,
        "inserted": inserted,
        "unchanged": unchanged,
        "scopes_skipped": scopes_skipped,
        "identity_maps": identity_maps,
        "ops": ops,
        "replace_scopes": replace_scopes,
        "document": document,
    }


def _apply_import(cur, plan: dict) -> None:
    identity_maps = plan["identity_maps"]
    for op in plan["ops"]:
        kind = op[0]
        if kind == "batch":
            batch, dest_id = op[1], op[2]
            cur.execute(
                """INSERT INTO source_batches
                       (id, batch_code, name, source_note, created_at)
                   VALUES (%s, %s, %s, %s, COALESCE(%s, now()))""",
                (
                    dest_id, batch["batch_code"], batch.get("name") or batch["batch_code"],
                    batch.get("source_note") or "", _parse_time(batch.get("created_at")),
                ),
            )
        elif kind == "import_run":
            run, dest_id, dest_batch = op[1], op[2], op[3]
            cur.execute(
                """INSERT INTO source_import_runs
                       (id, batch_id, snapshot_sha256, snapshot_bytes, contract_version,
                        status, processed_count, counts, error_report, checkpoint,
                        started_at, completed_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()), %s)""",
                (
                    dest_id, dest_batch, run.get("snapshot_sha256"),
                    run.get("snapshot_bytes"), int(run.get("contract_version") or 1),
                    run.get("status") or "completed", int(run.get("processed_count") or 0),
                    Json(run.get("counts") or {}), Json(run.get("error_report") or []),
                    Json(run.get("checkpoint") or {}),
                    _parse_time(run.get("started_at")), _parse_time(run.get("completed_at")),
                ),
            )
        elif kind == "media_identity":
            ident, dest_id, dest_task = op[1], op[2], op[3]
            cur.execute(
                """INSERT INTO task_media_identities
                       (id, task_id, provider, external_id, variant, pcm_sha256, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, now()))""",
                (
                    dest_id, dest_task, ident.get("provider"), ident.get("external_id"),
                    ident.get("variant") or "pcm16k_mono", ident.get("pcm_sha256"),
                    _parse_time(ident.get("created_at")),
                ),
            )
        elif kind == "path_alias":
            register_path_alias(cur, op[1], op[2])
        elif kind == "source":
            source, dest_id, dest_task, dest_batch = op[1], op[2], op[3], op[4]
            scene = source.get("scene_code")
            cur.execute(
                """INSERT INTO task_sources
                       (id, task_id, batch_id, record_key, revision, is_current,
                        scene_code, confidence, confidence_basis, source_type,
                        source_url, video_id, channel_id, channel_title, provider,
                        raw_record, content_digest, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, COALESCE(%s, now()))""",
                (
                    dest_id, dest_task, dest_batch, source.get("record_key"),
                    int(source.get("revision") or 1), bool(source.get("is_current", True)),
                    scene, source.get("confidence") or "unknown",
                    source.get("confidence_basis") or "", source.get("source_type") or "",
                    source.get("source_url"), source.get("video_id"),
                    source.get("channel_id"), source.get("channel_title"),
                    source.get("provider"), Json(source.get("raw_record") or {}),
                    source.get("content_digest") or "",
                    _parse_time(source.get("created_at")),
                ),
            )
        elif kind == "prediction":
            pred, dest_id, dest_task, dest_version = op[1], op[2], op[3], op[4]
            cur.execute(
                """INSERT INTO task_scene_predictions
                       (id, task_id, input_version_id, input_revision, input_digest,
                        model_name, prompt_version, predicted_label, predicted_scene_code,
                        score, score_meaning, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           COALESCE(%s, now()))""",
                (
                    dest_id, dest_task,
                    uuid.UUID(dest_version) if dest_version else None,
                    pred.get("input_revision"), pred.get("input_digest") or "",
                    pred.get("model_name") or "", pred.get("prompt_version") or "",
                    pred.get("predicted_label") or "", pred.get("predicted_scene_code"),
                    pred.get("score"), pred.get("score_meaning"),
                    _parse_time(pred.get("created_at")),
                ),
            )
        elif kind == "admin_action":
            action, dest_id, op_id = op[1], op[2], op[3]
            cur.execute(
                """INSERT INTO admin_actions
                       (id, operation_id, action_type, reason, request_hash, status,
                        admin_key_id, summary, created_at, completed_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()), %s)""",
                (
                    dest_id, _as_uuid(op_id, "admin_action.operation_id"),
                    action.get("action_type") or "correct_scene_review",
                    action.get("reason") or "",
                    action.get("request_hash") or ("0" * 64),
                    action.get("status") or "completed",
                    action.get("admin_key_id"),
                    Json(action.get("summary") or {}),
                    _parse_time(action.get("created_at")),
                    _parse_time(action.get("completed_at")),
                ),
            )
        elif kind == "review":
            review, dest_id, dest_version, dest_user, dest_admin, labels = (
                op[1], op[2], op[3], op[4], op[5], op[6]
            )
            previous = review.get("previous_review_id")
            prev_id = None
            if previous:
                mapped = identity_maps["reviews"].get(str(previous), str(previous))
                prev_id = _as_uuid(mapped, "previous_review_id")
            admin_id = None
            if dest_admin:
                mapped_admin = identity_maps["admin_actions"].get(
                    str(review.get("actor_admin_action_id")), dest_admin,
                )
                admin_id = _as_uuid(mapped_admin, "actor_admin_action_id")
            user_id = uuid.UUID(dest_user) if dest_user else None
            cur.execute(
                """INSERT INTO scene_reviews
                       (id, version_id, review_no, status, note, actor_user_id,
                        actor_admin_action_id, actor_kind, operation_id,
                        previous_review_id, superseded, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           COALESCE(%s, now()))""",
                (
                    dest_id, dest_version, int(review.get("review_no") or 1),
                    review.get("status") or "pending", review.get("note") or "",
                    user_id, admin_id, review.get("actor_kind") or "system",
                    _as_uuid(review["operation_id"], "review.operation_id")
                    if review.get("operation_id") else None,
                    prev_id, bool(review.get("superseded")),
                    _parse_time(review.get("created_at")),
                ),
            )
            for code in labels:
                if code not in SCENE_CODES:
                    raise MetadataImportError(f"unknown review scene code: {code}")
                cur.execute(
                    """INSERT INTO scene_review_labels (review_id, scene_code)
                       VALUES (%s, %s)""",
                    (dest_id, code),
                )
        elif kind == "audit_event":
            event, dest_task, dest_version = op[1], op[2], op[3]
            details = dict(event.get("details") or {})
            publication = event.get("publication_review_id") or details.get("scene_review_id")
            if publication:
                mapped = identity_maps["reviews"].get(str(publication), str(publication))
                details["scene_review_id"] = mapped
            user_id = None
            if event.get("user_id"):
                mapped_user = identity_maps["users"].get(
                    str(event["user_id"]), str(event["user_id"]),
                )
                user_id = _as_uuid(mapped_user, "audit.user_id")
            admin_id = None
            if event.get("admin_action_id"):
                mapped_admin = identity_maps["admin_actions"].get(
                    str(event["admin_action_id"]), str(event["admin_action_id"]),
                )
                admin_id = _as_uuid(mapped_admin, "audit.admin_action_id")
            cur.execute(
                """INSERT INTO annotation_events
                       (user_id, task_id, version_id, event_type, from_status,
                        to_status, details, created_at, admin_action_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()), %s)""",
                (
                    user_id,
                    uuid.UUID(dest_task) if dest_task else None,
                    uuid.UUID(dest_version) if dest_version else None,
                    event.get("event_type"), event.get("from_status"),
                    event.get("to_status"), Json(details),
                    _parse_time(event.get("created_at")), admin_id,
                ),
            )
        elif kind == "scope":
            scope, dest_user, do_write = op[1], op[2], op[3]
            if not do_write:
                continue
            cur.execute(
                """INSERT INTO annotator_scene_scopes
                       (user_id, mode, allow_unknown, revision, updated_at)
                   VALUES (%s, %s, %s, %s, now())
                   ON CONFLICT (user_id) DO UPDATE SET
                       mode = EXCLUDED.mode,
                       allow_unknown = EXCLUDED.allow_unknown,
                       revision = annotator_scene_scopes.revision + 1,
                       updated_at = now()""",
                (
                    dest_user, scope.get("mode") or "all",
                    bool(scope.get("allow_unknown")),
                    int(scope.get("revision") or 0),
                ),
            )
            cur.execute(
                "DELETE FROM annotator_scene_access WHERE user_id = %s",
                (dest_user,),
            )
            if (scope.get("mode") or "all") == "restricted":
                for code in scope.get("scene_codes") or []:
                    cur.execute(
                        """INSERT INTO annotator_scene_access (user_id, scene_code)
                           VALUES (%s, %s)""",
                        (dest_user, code),
                    )


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
