"""Import planning and persistence for versioned metadata.

Callers must already have parsed the document. Comparisons use named fields
from SELECT aliases, never positional tuple indexes.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from psycopg.types.json import Json

from annotation_metadata.export_contract import (
    IdentityMapping, MetadataImportError, as_uuid, json_ready, parse_time,
)
from annotation_metadata.ingestion import register_path_alias
from annotation_metadata.taxonomy import SCENE_CODES


def _score_equal(left, right) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return abs(float(left) - float(right)) < 1e-12


def _norm(value: Any, *, field: str):
    if field.endswith("_at") or field in {"created_at", "completed_at", "started_at", "submitted_at"}:
        parsed = parse_time(value)
        return None if parsed is None else parsed.timestamp()
    if field in {"raw_record", "counts", "checkpoint", "error_report", "details", "summary"}:
        return json.dumps(json_ready(value or ({} if field != "error_report" else [])),
                          sort_keys=True, default=str)
    if field == "score":
        return None if value is None else float(value)
    if field == "scene_codes":
        return list(value or [])
    if field in {"is_current", "superseded", "allow_unknown"}:
        return bool(value)
    if field.endswith("_id") or field in {"id", "task_id", "version_id", "batch_id"}:
        return str(value) if value not in (None, "") else None
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def records_equal(expected: dict, actual: dict, fields: tuple[str, ...]) -> bool:
    for field in fields:
        left = _norm(expected.get(field), field=field)
        right = _norm(actual.get(field), field=field)
        if field == "score":
            if not _score_equal(left, right):
                return False
            continue
        if left != right:
            return False
    return True


def _fetch(cur, sql: str, params, fields: tuple[str, ...]) -> dict | None:
    row = cur.execute(sql, params).fetchone()
    if not row:
        return None
    return {name: row[index] for index, name in enumerate(fields)}


def _remap(table: str, export_id, mapping: IdentityMapping) -> uuid.UUID:
    raw = str(export_id)
    table_map = getattr(mapping, table)
    return as_uuid(table_map.get(raw, raw), f"mapping.{table}")


SOURCE_FIELDS = (
    "id", "task_id", "record_key", "revision", "is_current", "scene_code",
    "confidence", "confidence_basis", "source_type", "source_url", "video_id",
    "channel_id", "channel_title", "provider", "raw_record", "content_digest",
    "created_at", "batch_id",
)
SOURCE_COMPARE = (
    "task_id", "record_key", "revision", "is_current", "scene_code",
    "confidence", "confidence_basis", "source_type", "source_url", "video_id",
    "channel_id", "channel_title", "provider", "raw_record", "content_digest",
    "created_at", "batch_id",
)
PREDICTION_FIELDS = (
    "id", "task_id", "input_version_id", "input_revision", "input_digest",
    "model_name", "prompt_version", "predicted_label", "predicted_scene_code",
    "score", "score_meaning", "created_at",
)
PREDICTION_COMPARE = (
    "task_id", "input_version_id", "input_revision", "input_digest",
    "model_name", "prompt_version", "predicted_label", "predicted_scene_code",
    "score", "score_meaning", "created_at",
)
REVIEW_FIELDS = (
    "id", "version_id", "review_no", "status", "note", "actor_kind",
    "actor_user_id", "actor_admin_action_id", "operation_id",
    "previous_review_id", "superseded", "created_at",
)
REVIEW_COMPARE = (
    "version_id", "review_no", "status", "note", "actor_kind",
    "actor_user_id", "actor_admin_action_id", "operation_id",
    "previous_review_id", "superseded", "created_at", "scene_codes",
)
BATCH_FIELDS = ("id", "batch_code", "name", "source_note", "created_at")
BATCH_COMPARE = ("batch_code", "name", "source_note", "created_at")
RUN_FIELDS = (
    "id", "batch_id", "snapshot_sha256", "snapshot_bytes", "contract_version",
    "status", "processed_count", "counts", "error_report", "checkpoint",
    "started_at", "completed_at",
)
RUN_COMPARE = (
    "batch_id", "snapshot_sha256", "snapshot_bytes", "contract_version",
    "status", "processed_count", "counts", "error_report", "checkpoint",
    "started_at", "completed_at",
)
IDENTITY_FIELDS = (
    "id", "task_id", "provider", "external_id", "variant", "pcm_sha256", "created_at",
)
IDENTITY_COMPARE = (
    "task_id", "provider", "external_id", "variant", "pcm_sha256", "created_at",
)
ACTION_FIELDS = (
    "id", "operation_id", "action_type", "reason", "request_hash", "status",
    "admin_key_id", "summary", "created_at", "completed_at",
)
ACTION_COMPARE = (
    "operation_id", "action_type", "reason", "request_hash", "status",
    "admin_key_id", "summary", "created_at", "completed_at",
)
EVENT_FIELDS = (
    "id", "user_id", "task_id", "version_id", "event_type", "from_status",
    "to_status", "details", "created_at", "admin_action_id",
)
EVENT_COMPARE = (
    "user_id", "task_id", "version_id", "event_type", "from_status",
    "to_status", "details", "created_at", "admin_action_id",
)


class ImportPlan:
    def __init__(self, document: dict, mapping: IdentityMapping, *, replace_scopes: bool):
        self.document = document
        self.mapping = mapping
        self.replace_scopes = replace_scopes
        self.errors: list[str] = []
        self.inserted = {
            "batches": 0, "import_runs": 0, "media_identities": 0,
            "path_aliases": 0, "sources": 0, "predictions": 0, "reviews": 0,
            "review_labels": 0, "admin_actions": 0, "audit_events": 0, "scopes": 0,
        }
        self.unchanged = dict(self.inserted)
        self.scopes_skipped: list[str] = []
        self.identity_maps = {
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
        self.ops: list[tuple] = []

    def map_id(self, table: str, export_id) -> uuid.UUID:
        return _remap(table, export_id, self.mapping)

    def result(self) -> dict:
        order = {
            "batch": 0, "import_run": 1, "media_identity": 2, "path_alias": 3,
            "source": 4, "prediction": 5, "admin_action": 6, "review": 7,
            "audit_event": 8, "scope": 9,
        }
        self.ops.sort(key=lambda item: order[item[0]])
        return {
            "errors": self.errors,
            "inserted": self.inserted,
            "unchanged": self.unchanged,
            "scopes_skipped": self.scopes_skipped,
            "identity_maps": self.identity_maps,
            "ops": self.ops,
            "replace_scopes": self.replace_scopes,
            "document": self.document,
        }


def plan_import(cur, document: dict, mapping: IdentityMapping,
                *, replace_scopes: bool) -> dict:
    plan = ImportPlan(document, mapping, replace_scopes=replace_scopes)
    _plan_mapping_presence(plan)
    _plan_core_identities(cur, plan)
    _plan_batches(cur, plan)
    _plan_import_runs(cur, plan)
    _plan_admin_actions(cur, plan)
    _plan_media_identities(cur, plan)
    _plan_task_payloads(cur, plan)
    _plan_audit_events(cur, plan)
    _plan_scopes(cur, plan)
    return plan.result()


def _plan_mapping_presence(plan: ImportPlan) -> None:
    document = plan.document
    known = {
        "tasks": {str(task["task_id"]) for task in document.get("tasks") or []},
        "versions": {str(item["id"]) for item in document.get("versions") or []},
        "users": {str(item["id"]) for item in document.get("users") or []},
        "admin_actions": {str(item["id"]) for item in document.get("admin_actions") or []},
        "import_runs": {str(item["id"]) for item in document.get("import_runs") or []},
        "batches": {str(item["id"]) for item in document.get("batches") or []},
        "sources": {
            str(src["id"])
            for task in document.get("tasks") or []
            for src in (task.get("source_history") or [])
        },
        "reviews": {
            str(rev["id"])
            for task in document.get("tasks") or []
            for rev in (task.get("reviews") or [])
        },
        "predictions": {
            str(pred["id"])
            for task in document.get("tasks") or []
            for pred in (task.get("predictions") or [])
        },
        "media_identities": {
            str(item["id"]) for item in document.get("media_identities") or []
        },
    }
    for table, pairs in plan.mapping.model_dump().items():
        present = known.get(table) or set()
        for source in pairs:
            if present and source not in present:
                plan.errors.append(
                    f"mapping.{table} refers to id absent from export: {source}"
                )


def _plan_core_identities(cur, plan: ImportPlan) -> None:
    dest_task_used: dict[str, str] = {}
    for task in plan.document.get("tasks") or []:
        export_id = str(task["task_id"])
        dest_id = plan.map_id("tasks", export_id)
        owner = dest_task_used.get(str(dest_id))
        if owner and owner != export_id:
            plan.errors.append(
                f"mapping.tasks maps both {owner} and {export_id} onto {dest_id}"
            )
        dest_task_used[str(dest_id)] = export_id
        row = cur.execute(
            "SELECT id FROM annotation_tasks WHERE id = %s", (dest_id,),
        ).fetchone()
        if not row:
            plan.errors.append(
                f"dangling task mapping: {export_id} -> {dest_id} "
                "(target annotation_tasks row is missing)"
            )
            continue
        plan.identity_maps["tasks"][export_id] = str(dest_id)
    for item in plan.document.get("versions") or []:
        export_id = str(item["id"])
        dest_id = plan.map_id("versions", export_id)
        dest_task = plan.identity_maps["tasks"].get(str(item["task_id"]))
        row = cur.execute(
            "SELECT id, task_id FROM annotation_versions WHERE id = %s",
            (dest_id,),
        ).fetchone()
        if not row:
            plan.errors.append(f"dangling version mapping: {export_id} -> {dest_id}")
            continue
        if dest_task and str(row[1]) != dest_task:
            plan.errors.append(
                f"version {export_id} belongs to task {row[1]}, mapped task is {dest_task}"
            )
        plan.identity_maps["versions"][export_id] = str(dest_id)
    for item in plan.document.get("users") or []:
        export_id = str(item["id"])
        dest_id = plan.map_id("users", export_id)
        row = cur.execute("SELECT id FROM annotators WHERE id = %s", (dest_id,)).fetchone()
        if not row:
            plan.errors.append(f"dangling user mapping: {export_id} -> {dest_id}")
            continue
        plan.identity_maps["users"][export_id] = str(dest_id)


def _plan_batches(cur, plan: ImportPlan) -> None:
    for batch in plan.document.get("batches") or []:
        export_id = str(batch["id"])
        dest_id = plan.map_id("batches", export_id)
        by_code = _fetch(
            cur, "SELECT id, batch_code, name, source_note, created_at FROM source_batches WHERE batch_code = %s",
            (batch["batch_code"],), BATCH_FIELDS,
        )
        by_id = _fetch(
            cur, "SELECT id, batch_code, name, source_note, created_at FROM source_batches WHERE id = %s",
            (dest_id,), BATCH_FIELDS,
        )
        if by_code and by_id and str(by_code["id"]) != str(by_id["id"]):
            plan.errors.append(
                f"conflicting batch identity: code {batch['batch_code']} is "
                f"{by_code['id']}, id maps to {by_id['id']}"
            )
            continue
        existing = by_code or by_id
        if existing:
            expected = {**batch, "id": str(existing["id"])}
            if not records_equal(expected, existing, BATCH_COMPARE):
                plan.errors.append(f"conflicting batch payload for {export_id}")
                continue
            plan.identity_maps["batches"][export_id] = str(existing["id"])
            plan.unchanged["batches"] += 1
        else:
            plan.identity_maps["batches"][export_id] = str(dest_id)
            plan.inserted["batches"] += 1
            plan.ops.append(("batch", batch, dest_id))


def _plan_import_runs(cur, plan: ImportPlan) -> None:
    for run in plan.document.get("import_runs") or []:
        export_id = str(run["id"])
        dest_id = plan.map_id("import_runs", export_id)
        dest_batch = plan.identity_maps["batches"].get(str(run.get("batch_id") or ""))
        if not dest_batch and run.get("batch_code"):
            row = cur.execute(
                "SELECT id FROM source_batches WHERE batch_code = %s",
                (run.get("batch_code"),),
            ).fetchone()
            dest_batch = str(row[0]) if row else None
        if not dest_batch:
            plan.errors.append(f"import run {export_id} has dangling batch")
            continue
        dest_batch_uuid = uuid.UUID(dest_batch)
        existing = _fetch(
            cur,
            """SELECT id, batch_id, snapshot_sha256, snapshot_bytes, contract_version,
                      status, processed_count, counts, error_report, checkpoint,
                      started_at, completed_at
               FROM source_import_runs WHERE id = %s""",
            (dest_id,), RUN_FIELDS,
        )
        same_key = _fetch(
            cur,
            """SELECT id, batch_id, snapshot_sha256, snapshot_bytes, contract_version,
                      status, processed_count, counts, error_report, checkpoint,
                      started_at, completed_at
               FROM source_import_runs
               WHERE batch_id = %s AND snapshot_sha256 = %s""",
            (dest_batch_uuid, run.get("snapshot_sha256")), RUN_FIELDS,
        )
        candidate = existing or same_key
        expected = {**run, "batch_id": dest_batch}
        if candidate:
            if not records_equal(expected, candidate, RUN_COMPARE):
                plan.errors.append(f"conflicting import run payload for {export_id}")
                continue
            plan.identity_maps["import_runs"][export_id] = str(candidate["id"])
            plan.unchanged["import_runs"] += 1
        else:
            plan.identity_maps["import_runs"][export_id] = str(dest_id)
            plan.inserted["import_runs"] += 1
            plan.ops.append(("import_run", run, dest_id, dest_batch_uuid))


def _plan_admin_actions(cur, plan: ImportPlan) -> None:
    for action in plan.document.get("admin_actions") or []:
        export_id = str(action["id"])
        dest_id = plan.map_id("admin_actions", export_id)
        existing = _fetch(
            cur,
            """SELECT id, operation_id, action_type, reason, request_hash, status,
                      admin_key_id, summary, created_at, completed_at
               FROM admin_actions WHERE id = %s""",
            (dest_id,), ACTION_FIELDS,
        )
        if existing:
            if not records_equal(action, existing, ACTION_COMPARE):
                plan.errors.append(f"conflicting admin_action payload for {export_id}")
                continue
            plan.identity_maps["admin_actions"][export_id] = str(existing["id"])
            plan.unchanged["admin_actions"] += 1
            continue
        op_id = action.get("operation_id") or str(uuid.uuid4())
        taken = cur.execute(
            "SELECT id FROM admin_actions WHERE operation_id = %s",
            (as_uuid(op_id, "admin_action.operation_id"),),
        ).fetchone()
        if taken:
            plan.errors.append(
                f"admin_action operation_id {op_id} already used by {taken[0]}"
            )
            continue
        plan.identity_maps["admin_actions"][export_id] = str(dest_id)
        plan.inserted["admin_actions"] += 1
        plan.ops.append(("admin_action", action, dest_id, op_id))


def _plan_media_identities(cur, plan: ImportPlan) -> None:
    for ident in plan.document.get("media_identities") or []:
        export_id = str(ident["id"])
        dest_id = plan.map_id("media_identities", export_id)
        dest_task = plan.identity_maps["tasks"].get(str(ident["task_id"]))
        if not dest_task:
            plan.errors.append(f"media identity {export_id} has dangling task")
            continue
        dest_task_uuid = uuid.UUID(dest_task)
        existing = _fetch(
            cur,
            """SELECT id, task_id, provider, external_id, variant, pcm_sha256, created_at
               FROM task_media_identities
               WHERE provider = %s AND external_id = %s AND variant = %s""",
            (ident.get("provider"), ident.get("external_id"), ident.get("variant")),
            IDENTITY_FIELDS,
        )
        by_id = _fetch(
            cur,
            """SELECT id, task_id, provider, external_id, variant, pcm_sha256, created_at
               FROM task_media_identities WHERE id = %s""",
            (dest_id,), IDENTITY_FIELDS,
        )
        candidate = existing or by_id
        expected = {**ident, "task_id": dest_task}
        if candidate:
            if not records_equal(expected, candidate, IDENTITY_COMPARE):
                plan.errors.append(f"conflicting media identity payload for {export_id}")
                continue
            plan.identity_maps["media_identities"][export_id] = str(candidate["id"])
            plan.unchanged["media_identities"] += 1
        else:
            plan.identity_maps["media_identities"][export_id] = str(dest_id)
            plan.inserted["media_identities"] += 1
            plan.ops.append(("media_identity", ident, dest_id, dest_task_uuid))


def _plan_task_payloads(cur, plan: ImportPlan) -> None:
    for task in plan.document.get("tasks") or []:
        export_task = str(task["task_id"])
        dest_task = plan.identity_maps["tasks"].get(export_task)
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
                plan.unchanged["path_aliases"] += 1
            else:
                plan.inserted["path_aliases"] += 1
                plan.ops.append(("path_alias", dest_task_uuid, alias))
        _plan_sources(cur, plan, task, dest_task, dest_task_uuid)
        _plan_predictions(cur, plan, task, dest_task, dest_task_uuid)
        _plan_reviews(cur, plan, task, dest_task, dest_task_uuid)


def _resolve_batch(cur, plan: ImportPlan, source: dict) -> str | None:
    dest_batch = plan.identity_maps["batches"].get(str(source.get("batch_id") or ""))
    if dest_batch:
        return dest_batch
    if source.get("batch_code"):
        dest_batch = next(
            (
                plan.identity_maps["batches"].get(str(batch["id"]))
                for batch in plan.document.get("batches") or []
                if batch.get("batch_code") == source.get("batch_code")
            ),
            None,
        )
        if dest_batch:
            return dest_batch
        row = cur.execute(
            "SELECT id FROM source_batches WHERE batch_code = %s",
            (source.get("batch_code"),),
        ).fetchone()
        return str(row[0]) if row else None
    return None


def _plan_sources(cur, plan: ImportPlan, task: dict, dest_task: str, dest_task_uuid) -> None:
    history = list(task.get("source_history") or [])
    history.sort(key=lambda item: (
        item.get("record_key") or "", int(item.get("revision") or 1), item.get("id") or "",
    ))
    for source in history:
        export_id = str(source["id"])
        dest_id = plan.map_id("sources", export_id)
        dest_batch = _resolve_batch(cur, plan, source)
        if not dest_batch:
            plan.errors.append(f"source {export_id} has dangling batch")
            continue
        dest_batch_uuid = uuid.UUID(dest_batch)
        existing = _fetch(
            cur,
            """SELECT id, task_id, record_key, revision, is_current, scene_code,
                      confidence, confidence_basis, source_type, source_url, video_id,
                      channel_id, channel_title, provider, raw_record, content_digest,
                      created_at, batch_id
               FROM task_sources WHERE id = %s""",
            (dest_id,), SOURCE_FIELDS,
        )
        expected = {**source, "task_id": dest_task, "batch_id": dest_batch}
        if existing:
            if not records_equal(expected, existing, SOURCE_COMPARE):
                plan.errors.append(f"conflicting source identity {export_id} -> {dest_id}")
                continue
            plan.identity_maps["sources"][export_id] = str(existing["id"])
            plan.unchanged["sources"] += 1
            continue
        occupied = None
        if source.get("is_current", True):
            occupied = cur.execute(
                """SELECT id FROM task_sources
                   WHERE batch_id = %s AND record_key = %s AND is_current""",
                (dest_batch_uuid, source.get("record_key")),
            ).fetchone()
        if occupied and str(occupied[0]) != str(dest_id):
            plan.errors.append(
                f"current source ({source.get('batch_code')}, "
                f"{source.get('record_key')}) already exists as {occupied[0]}"
            )
            continue
        plan.identity_maps["sources"][export_id] = str(dest_id)
        plan.inserted["sources"] += 1
        plan.ops.append(("source", source, dest_id, dest_task_uuid, dest_batch_uuid))


def _plan_predictions(cur, plan: ImportPlan, task: dict, dest_task: str, dest_task_uuid) -> None:
    for pred in task.get("predictions") or []:
        export_id = str(pred["id"])
        dest_id = plan.map_id("predictions", export_id)
        dest_version = None
        if pred.get("input_version_id"):
            dest_version = plan.identity_maps["versions"].get(str(pred["input_version_id"]))
            if not dest_version:
                dest_version = str(plan.map_id("versions", pred["input_version_id"]))
            row = cur.execute(
                "SELECT id, task_id FROM annotation_versions WHERE id = %s",
                (uuid.UUID(dest_version),),
            ).fetchone()
            if not row:
                plan.errors.append(f"prediction {export_id} has dangling input_version_id")
                continue
            if str(row[1]) != dest_task:
                plan.errors.append(
                    f"prediction {export_id} input_version_id belongs to another task"
                )
                continue
        existing = _fetch(
            cur,
            """SELECT id, task_id, input_version_id, input_revision, input_digest,
                      model_name, prompt_version, predicted_label, predicted_scene_code,
                      score, score_meaning, created_at
               FROM task_scene_predictions WHERE id = %s""",
            (dest_id,), PREDICTION_FIELDS,
        )
        expected = {**pred, "task_id": dest_task, "input_version_id": dest_version}
        if existing:
            if not records_equal(expected, existing, PREDICTION_COMPARE):
                plan.errors.append(f"conflicting prediction identity {export_id}")
                continue
            plan.identity_maps["predictions"][export_id] = str(existing["id"])
            plan.unchanged["predictions"] += 1
            continue
        plan.identity_maps["predictions"][export_id] = str(dest_id)
        plan.inserted["predictions"] += 1
        plan.ops.append(("prediction", pred, dest_id, dest_task_uuid, dest_version))


def _plan_reviews(cur, plan: ImportPlan, task: dict, dest_task: str, dest_task_uuid) -> None:
    reviews = list(task.get("reviews") or [])
    reviews.sort(key=lambda item: (int(item.get("review_no") or 0), item.get("id") or ""))
    for review in reviews:
        export_id = str(review["id"])
        dest_id = plan.map_id("reviews", export_id)
        version_export = review.get("version_id")
        if not version_export:
            plan.errors.append(f"review {export_id} is missing version_id")
            continue
        dest_version = plan.identity_maps["versions"].get(str(version_export))
        if not dest_version:
            dest_version = str(plan.map_id("versions", version_export))
        row = cur.execute(
            "SELECT id, task_id FROM annotation_versions WHERE id = %s",
            (uuid.UUID(dest_version),),
        ).fetchone()
        if not row:
            plan.errors.append(f"review {export_id} has dangling version_id")
            continue
        if str(row[1]) != dest_task:
            plan.errors.append(
                f"review {export_id} version is not on mapped task {dest_task}"
            )
            continue
        previous = review.get("previous_review_id")
        if previous:
            prev_dest = plan.identity_maps["reviews"].get(str(previous)) or str(
                plan.map_id("reviews", previous)
            )
            in_export = str(previous) in {str(item.get("id")) for item in reviews}
            exists = cur.execute(
                "SELECT 1 FROM scene_reviews WHERE id = %s",
                (uuid.UUID(prev_dest),),
            ).fetchone()
            if not in_export and not exists:
                plan.errors.append(f"review {export_id} has dangling previous_review_id")
                continue
        dest_user = None
        if review.get("actor_user_id"):
            dest_user = plan.identity_maps["users"].get(str(review["actor_user_id"]))
            if not dest_user:
                dest_user = str(plan.map_id("users", review["actor_user_id"]))
            if not cur.execute(
                "SELECT 1 FROM annotators WHERE id = %s", (uuid.UUID(dest_user),),
            ).fetchone():
                plan.errors.append(f"review {export_id} has dangling actor_user_id")
                continue
        dest_admin = None
        if review.get("actor_admin_action_id"):
            dest_admin = plan.identity_maps["admin_actions"].get(
                str(review["actor_admin_action_id"])
            )
            if not dest_admin:
                dest_admin = str(plan.map_id("admin_actions", review["actor_admin_action_id"]))
            planned = str(review["actor_admin_action_id"]) in plan.identity_maps["admin_actions"]
            exists = cur.execute(
                "SELECT 1 FROM admin_actions WHERE id = %s", (uuid.UUID(dest_admin),),
            ).fetchone()
            if not planned and not exists:
                plan.errors.append(f"review {export_id} has dangling actor_admin_action_id")
                continue
        existing = _fetch(
            cur,
            """SELECT id, version_id, review_no, status, note, actor_kind,
                      actor_user_id, actor_admin_action_id, operation_id,
                      previous_review_id, superseded, created_at
               FROM scene_reviews WHERE id = %s""",
            (dest_id,), REVIEW_FIELDS,
        )
        if existing:
            labels = [
                row[0] for row in cur.execute(
                    """SELECT scene_code FROM scene_review_labels
                       WHERE review_id = %s ORDER BY scene_code""",
                    (existing["id"],),
                ).fetchall()
            ]
            existing["scene_codes"] = labels
            expected = {
                **review,
                "version_id": dest_version,
                "actor_user_id": dest_user,
                "actor_admin_action_id": dest_admin,
                "previous_review_id": (
                    plan.identity_maps["reviews"].get(str(previous), str(previous))
                    if previous else None
                ),
            }
            if not records_equal(expected, existing, REVIEW_COMPARE):
                plan.errors.append(f"conflicting review identity {export_id}")
                continue
            plan.identity_maps["reviews"][export_id] = str(existing["id"])
            plan.unchanged["reviews"] += 1
            continue
        occupied = cur.execute(
            """SELECT id FROM scene_reviews WHERE version_id = %s AND review_no = %s""",
            (uuid.UUID(dest_version), int(review.get("review_no") or 0)),
        ).fetchone()
        if occupied:
            plan.errors.append(
                f"review_no {review.get('review_no')} already exists on version {dest_version}"
            )
            continue
        plan.identity_maps["reviews"][export_id] = str(dest_id)
        plan.inserted["reviews"] += 1
        labels = list(review.get("scene_codes") or [])
        plan.inserted["review_labels"] += len(labels)
        plan.ops.append((
            "review", review, dest_id, uuid.UUID(dest_version), dest_user, dest_admin, labels,
        ))


def _plan_audit_events(cur, plan: ImportPlan) -> None:
    for event in plan.document.get("audit_events") or []:
        dest_task = plan.identity_maps["tasks"].get(str(event.get("task_id") or ""))
        if event.get("task_id") and not dest_task:
            plan.errors.append(f"audit event has dangling task {event.get('task_id')}")
            continue
        dest_version = None
        if event.get("version_id"):
            dest_version = plan.identity_maps["versions"].get(str(event["version_id"]))
            if not dest_version:
                dest_version = str(plan.map_id("versions", event["version_id"]))
            if not cur.execute(
                "SELECT 1 FROM annotation_versions WHERE id = %s",
                (uuid.UUID(dest_version),),
            ).fetchone():
                plan.errors.append(
                    f"audit event has dangling version {event.get('version_id')}"
                )
                continue
        publication = (event.get("details") or {}).get("scene_review_id") or event.get(
            "publication_review_id"
        )
        mapped_publication = None
        if publication:
            mapped_publication = plan.identity_maps["reviews"].get(
                str(publication), plan.mapping.reviews.get(str(publication), str(publication)),
            )
            exists = cur.execute(
                "SELECT 1 FROM scene_reviews WHERE id = %s",
                (as_uuid(mapped_publication, "publication_review_id"),),
            ).fetchone()
            in_export = str(publication) in {
                str(rev.get("id"))
                for task in plan.document.get("tasks") or []
                for rev in (task.get("reviews") or [])
            }
            if not exists and not in_export:
                plan.errors.append(
                    f"audit event has dangling publication review {publication}"
                )
                continue
        if not dest_task:
            plan.inserted["audit_events"] += 1
            plan.ops.append(("audit_event", event, dest_task, dest_version))
            continue
        matches = cur.execute(
            """SELECT id, user_id, task_id, version_id, event_type, from_status,
                      to_status, details, created_at, admin_action_id
               FROM annotation_events
               WHERE task_id = %s AND event_type = %s
                 AND COALESCE(details->>'scene_review_id', '') = ANY(%s)""",
            (
                uuid.UUID(dest_task), event.get("event_type"),
                [str(publication or ""), str(mapped_publication or "")],
            ),
        ).fetchall()
        expected = {
            **event,
            "task_id": dest_task,
            "version_id": dest_version,
            "details": dict(event.get("details") or {}),
        }
        if mapped_publication:
            expected["details"]["scene_review_id"] = mapped_publication
            expected["publication_review_id"] = mapped_publication
        if event.get("user_id"):
            expected["user_id"] = plan.identity_maps["users"].get(
                str(event["user_id"]), str(event["user_id"]),
            )
        if event.get("admin_action_id"):
            expected["admin_action_id"] = plan.identity_maps["admin_actions"].get(
                str(event["admin_action_id"]), str(event["admin_action_id"]),
            )
        found_equal = False
        found_conflict = False
        for row in matches:
            actual = {name: row[index] for index, name in enumerate(EVENT_FIELDS)}
            if records_equal(expected, actual, EVENT_COMPARE):
                found_equal = True
                break
            found_conflict = True
            plan.errors.append(
                f"conflicting audit event payload for {event.get('event_type')} "
                f"on task {dest_task}"
            )
            break
        if found_conflict:
            continue
        if found_equal:
            plan.unchanged["audit_events"] += 1
        else:
            plan.inserted["audit_events"] += 1
            plan.ops.append(("audit_event", event, dest_task, dest_version))


def _plan_scopes(cur, plan: ImportPlan) -> None:
    for scope in plan.document.get("scopes") or []:
        user_export = str(scope.get("user_id") or "")
        dest_user = plan.identity_maps["users"].get(user_export)
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
                plan.unchanged["scopes"] += 1
                continue
            if not plan.replace_scopes:
                plan.scopes_skipped.append(str(dest_user_uuid))
                continue
        plan.inserted["scopes"] += 1
        plan.ops.append(("scope", scope, dest_user_uuid, plan.replace_scopes or existing is None))


def apply_import(cur, plan: dict) -> None:
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
                    batch.get("source_note") or "", parse_time(batch.get("created_at")),
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
                    parse_time(run.get("started_at")), parse_time(run.get("completed_at")),
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
                    parse_time(ident.get("created_at")),
                ),
            )
        elif kind == "path_alias":
            register_path_alias(cur, op[1], op[2])
        elif kind == "source":
            source, dest_id, dest_task, dest_batch = op[1], op[2], op[3], op[4]
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
                    source.get("scene_code"), source.get("confidence") or "unknown",
                    source.get("confidence_basis") or "", source.get("source_type") or "",
                    source.get("source_url"), source.get("video_id"),
                    source.get("channel_id"), source.get("channel_title"),
                    source.get("provider"), Json(source.get("raw_record") or {}),
                    source.get("content_digest") or "",
                    parse_time(source.get("created_at")),
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
                    parse_time(pred.get("created_at")),
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
                    dest_id, as_uuid(op_id, "admin_action.operation_id"),
                    action.get("action_type") or "correct_scene_review",
                    action.get("reason") or "",
                    action.get("request_hash") or ("0" * 64),
                    action.get("status") or "completed",
                    action.get("admin_key_id"),
                    Json(action.get("summary") or {}),
                    parse_time(action.get("created_at")),
                    parse_time(action.get("completed_at")),
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
                prev_id = as_uuid(mapped, "previous_review_id")
            admin_id = None
            if dest_admin:
                mapped_admin = identity_maps["admin_actions"].get(
                    str(review.get("actor_admin_action_id")), dest_admin,
                )
                admin_id = as_uuid(mapped_admin, "actor_admin_action_id")
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
                    as_uuid(review["operation_id"], "review.operation_id")
                    if review.get("operation_id") else None,
                    prev_id, bool(review.get("superseded")),
                    parse_time(review.get("created_at")),
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
                user_id = as_uuid(mapped_user, "audit.user_id")
            admin_id = None
            if event.get("admin_action_id"):
                mapped_admin = identity_maps["admin_actions"].get(
                    str(event["admin_action_id"]), str(event["admin_action_id"]),
                )
                admin_id = as_uuid(mapped_admin, "audit.admin_action_id")
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
                    parse_time(event.get("created_at")), admin_id,
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
                "DELETE FROM annotator_scene_access WHERE user_id = %s", (dest_user,),
            )
            if (scope.get("mode") or "all") == "restricted":
                for code in scope.get("scene_codes") or []:
                    cur.execute(
                        """INSERT INTO annotator_scene_access (user_id, scene_code)
                           VALUES (%s, %s)""",
                        (dest_user, code),
                    )
