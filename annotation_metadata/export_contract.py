"""Typed metadata export document, identity mapping, and structural validation.

verify-metadata and import --dry-run share this layer. Database identity
checks live in export_import.py.
"""

from __future__ import annotations

import json
import os
import uuid
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError as PydanticValidationError
from pydantic import field_validator, model_validator

from annotation_metadata import CONTRACT_VERSION, SCHEMA_VERSION
from annotation_metadata.contracts import (
    Confidence, ReviewStatus, SceneReviewInput, ScopeMode, StrictModel,
)
from annotation_metadata.taxonomy import MEDIA_VARIANT, SCENE_CODES

FORMAT_NAME = "annotation_metadata"
METADATA_FILENAME = "metadata.v1.json"
GENERATED_METADATA_KEYS = frozenset({"exported_at"})
AUDIO_LEVEL_NOTICE = (
    "Audio-level source, model, and human scene evidence for the parent "
    "recording. Not an inferred per-segment scene label and not a measure "
    "of precise scene-trainable hours."
)
EXCEL_HEADERS = [
    "User", "Folder", "Audio", "Duration (s)", "Status",
    "Source scenes", "Source confidence", "Source scene confidences", "Batches",
    "Scene review", "Human scenes", "Model prediction",
    "Quality state", "Training eligible",
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
        "notes": "Same id with any differing persisted evidence is a conflict.",
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
        "notes": "Matching an existing batch_code with a different payload is a conflict.",
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
  * export/import require an idle connection (they start REPEATABLE READ) or
    an already-open REPEATABLE READ/SERIALIZABLE transaction. They never
    commit a caller's unrelated READ COMMITTED work.
""".strip()
EXCLUDED_EXPORT_FIELDS = (
    "lease_token", "session_id", "token_digest", "csrf_digest",
    "processing_token", "password", "cookie", "active_sessions",
)
FORBIDDEN_MAPPING_KEYS = {
    "usernames", "username", "by_username", "rel_path", "rel_paths",
    "by_rel_path", "pathname", "pathnames", "by_path",
}
ActorKind = Literal["annotator", "admin", "system"]
ImportRunStatus = Literal["running", "completed", "failed", "partial"]


class MetadataImportError(ValueError):
    """Import rejected; the transaction must not mutate metadata."""

    def __init__(self, message: str, *, errors: list[str] | None = None,
                 report: dict | None = None):
        super().__init__(message)
        self.errors = list(errors or [message])
        self.report = report or {}


class IdentityMapping(StrictModel):
    tasks: dict[str, str] = Field(default_factory=dict)
    versions: dict[str, str] = Field(default_factory=dict)
    users: dict[str, str] = Field(default_factory=dict)
    admin_actions: dict[str, str] = Field(default_factory=dict)
    import_runs: dict[str, str] = Field(default_factory=dict)
    sources: dict[str, str] = Field(default_factory=dict)
    reviews: dict[str, str] = Field(default_factory=dict)
    predictions: dict[str, str] = Field(default_factory=dict)
    batches: dict[str, str] = Field(default_factory=dict)
    media_identities: dict[str, str] = Field(default_factory=dict)


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


def comparable_document(document: dict) -> dict:
    """Full document minus documented generated metadata (exported_at)."""
    data = json.loads(json.dumps(json_ready(document), ensure_ascii=False))
    for key in GENERATED_METADATA_KEYS:
        data.pop(key, None)
    return data


def as_uuid(value, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise MetadataImportError(f"invalid UUID for {field}: {value!r}") from exc


def parse_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise MetadataImportError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _uuid_str(value: Any) -> str:
    return str(uuid.UUID(str(value)))


def _optional_uuid(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return _uuid_str(value)


def _optional_time(value: Any) -> str | None:
    parsed = parse_time(value)
    return parsed.isoformat() if parsed else None


class ExportedScene(StrictModel):
    code: str
    label_zh: str
    label_en: str
    active: bool = True
    sort_order: int = 0

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        if value not in SCENE_CODES:
            raise ValueError(f"unknown scene code: {value}")
        return value


class ExportedBatch(StrictModel):
    id: str
    batch_code: str
    name: str = ""
    source_note: str = ""
    created_at: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _uuid_str(value)

    @field_validator("created_at")
    @classmethod
    def _ts(cls, value: str | None) -> str | None:
        return _optional_time(value)


class ExportedImportRun(StrictModel):
    id: str
    batch_id: str
    batch_code: str | None = None
    snapshot_sha256: str
    snapshot_bytes: int | None = None
    contract_version: int = 1
    status: ImportRunStatus
    processed_count: int = 0
    counts: dict[str, Any] = Field(default_factory=dict)
    error_report: list[Any] = Field(default_factory=list)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None

    @field_validator("id", "batch_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _uuid_str(value)

    @field_validator("started_at", "completed_at")
    @classmethod
    def _ts(cls, value: str | None) -> str | None:
        return _optional_time(value)


class ExportedMediaIdentity(StrictModel):
    id: str
    task_id: str
    provider: str
    external_id: str
    variant: str = MEDIA_VARIANT
    pcm_sha256: str | None = None
    created_at: str | None = None

    @field_validator("id", "task_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _uuid_str(value)

    @field_validator("created_at")
    @classmethod
    def _ts(cls, value: str | None) -> str | None:
        return _optional_time(value)


class ExportedUser(StrictModel):
    id: str
    username: str
    created_at: str | None = None
    status: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _uuid_str(value)

    @field_validator("created_at")
    @classmethod
    def _ts(cls, value: str | None) -> str | None:
        return _optional_time(value)


class ExportedVersion(StrictModel):
    id: str
    task_id: str
    version_no: int
    lifecycle: str
    revision: int = 0
    created_at: str | None = None
    submitted_at: str | None = None

    @field_validator("id", "task_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _uuid_str(value)

    @field_validator("revision", "version_no")
    @classmethod
    def _nonneg(cls, value: int) -> int:
        if int(value) < 0:
            raise ValueError("revision/version_no must be >= 0")
        return int(value)

    @field_validator("created_at", "submitted_at")
    @classmethod
    def _ts(cls, value: str | None) -> str | None:
        return _optional_time(value)


class ExportedAdminAction(StrictModel):
    id: str
    operation_id: str | None = None
    action_type: str
    reason: str = ""
    request_hash: str | None = None
    status: str | None = None
    admin_key_id: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    completed_at: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _uuid_str(value)

    @field_validator("operation_id")
    @classmethod
    def _op(cls, value: str | None) -> str | None:
        return _optional_uuid(value)

    @field_validator("created_at", "completed_at")
    @classmethod
    def _ts(cls, value: str | None) -> str | None:
        return _optional_time(value)


class ExportedAuditEvent(StrictModel):
    user_id: str | None = None
    task_id: str | None = None
    version_id: str | None = None
    event_type: str
    from_status: str | None = None
    to_status: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    admin_action_id: str | None = None
    publication_review_id: str | None = None

    @field_validator("user_id", "task_id", "version_id", "admin_action_id",
                     "publication_review_id")
    @classmethod
    def _ids(cls, value: str | None) -> str | None:
        return _optional_uuid(value)

    @field_validator("created_at")
    @classmethod
    def _ts(cls, value: str | None) -> str | None:
        return _optional_time(value)


class ExportedSource(StrictModel):
    id: str
    record_key: str
    revision: int = 1
    is_current: bool = True
    scene_code: str | None = None
    scene_label: str | None = None
    confidence: Confidence = "unknown"
    confidence_basis: str = ""
    source_type: str = ""
    source_url: str | None = None
    video_id: str | None = None
    channel_id: str | None = None
    channel_title: str | None = None
    provider: str | None = None
    raw_record: dict[str, Any] = Field(default_factory=dict)
    content_digest: str = ""
    created_at: str | None = None
    batch_code: str | None = None
    batch_id: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _uuid_str(value)

    @field_validator("batch_id")
    @classmethod
    def _batch_id(cls, value: str | None) -> str | None:
        return _optional_uuid(value)

    @field_validator("revision")
    @classmethod
    def _revision(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError("source revision must be >= 1")
        return int(value)

    @field_validator("scene_code")
    @classmethod
    def _scene(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if value not in SCENE_CODES:
            raise ValueError(f"unknown source scene_code: {value}")
        return value

    @field_validator("created_at")
    @classmethod
    def _ts(cls, value: str | None) -> str | None:
        return _optional_time(value)


class ExportedPrediction(StrictModel):
    id: str
    predicted_label: str
    predicted_scene_code: str | None = None
    score: float | None = None
    score_meaning: str | None = None
    model_name: str
    prompt_version: str = ""
    input_version_id: str | None = None
    input_revision: int | None = None
    input_digest: str = ""
    created_at: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _uuid_str(value)

    @field_validator("input_version_id")
    @classmethod
    def _input_version(cls, value: str | None) -> str | None:
        return _optional_uuid(value)

    @field_validator("input_revision", mode="before")
    @classmethod
    def _revision(cls, value):
        if value in (None, ""):
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("input_revision must be an integer")
        return value

    @field_validator("score")
    @classmethod
    def _score(cls, value: float | None) -> float | None:
        if value is None:
            return None
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("prediction score must be a finite number")
        if number < 0 or number > 1:
            raise ValueError("prediction score must be between 0 and 1")
        return number

    @field_validator("predicted_scene_code")
    @classmethod
    def _scene(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if value not in SCENE_CODES:
            raise ValueError(f"unknown predicted_scene_code: {value}")
        return value

    @field_validator("created_at")
    @classmethod
    def _ts(cls, value: str | None) -> str | None:
        return _optional_time(value)


class ExportedReview(StrictModel):
    id: str
    version_id: str | None = None
    review_no: int = 0
    status: ReviewStatus
    note: str = ""
    actor_kind: ActorKind | None = None
    actor_user_id: str | None = None
    actor_admin_action_id: str | None = None
    operation_id: str | None = None
    previous_review_id: str | None = None
    superseded: bool = False
    created_at: str | None = None
    version_no: int | None = None
    version_lifecycle: str | None = None
    scene_codes: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _uuid_str(value)

    @field_validator("version_id", "actor_user_id", "actor_admin_action_id",
                     "operation_id", "previous_review_id")
    @classmethod
    def _ids(cls, value: str | None) -> str | None:
        return _optional_uuid(value)

    @field_validator("created_at")
    @classmethod
    def _ts(cls, value: str | None) -> str | None:
        return _optional_time(value)

    @model_validator(mode="after")
    def _review_contract(self) -> "ExportedReview":
        parsed = SceneReviewInput.model_validate({
            "status": self.status,
            "scene_codes": self.scene_codes,
            "note": self.note,
        })
        object.__setattr__(self, "scene_codes", list(parsed.scene_codes))
        object.__setattr__(self, "note", parsed.note)
        return self


class ExportedTask(StrictModel):
    task_id: str
    rel_path: str | None = None
    legacy_audio_key: str | None = None
    path_aliases: list[str] = Field(default_factory=list)
    sources: list[ExportedSource] = Field(default_factory=list)
    source_history: list[ExportedSource] = Field(default_factory=list)
    prediction: ExportedPrediction | None = None
    predictions: list[ExportedPrediction] = Field(default_factory=list)
    published_review: ExportedReview | None = None
    reviews: list[ExportedReview] = Field(default_factory=list)

    @field_validator("task_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _uuid_str(value)

    @model_validator(mode="after")
    def _history(self) -> "ExportedTask":
        if not self.source_history and self.sources:
            object.__setattr__(self, "source_history", list(self.sources))
        if not self.predictions and self.prediction is not None:
            object.__setattr__(self, "predictions", [self.prediction])
        if not self.reviews and self.published_review is not None:
            object.__setattr__(self, "reviews", [self.published_review])
        for review in self.reviews:
            if not review.version_id:
                raise ValueError(f"review {review.id} is missing version_id")
        return self


class ExportedScope(StrictModel):
    user_id: str
    username: str | None = None
    mode: ScopeMode
    allow_unknown: bool = False
    revision: int = 0
    scene_codes: list[str] = Field(default_factory=list)
    scenes: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("user_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _uuid_str(value)


class MetadataDocument(StrictModel):
    format: str | None = FORMAT_NAME
    schema_version: int
    contract_version: int = CONTRACT_VERSION
    exported_at: str | None = None
    snapshot_isolation: str | None = None
    applied_filters: dict[str, Any] | None = None
    identity_model: dict[str, Any] | None = None
    excluded_fields: list[str] = Field(default_factory=list)
    scenes: list[ExportedScene] = Field(default_factory=list)
    batches: list[ExportedBatch] = Field(default_factory=list)
    import_runs: list[ExportedImportRun] = Field(default_factory=list)
    media_identities: list[ExportedMediaIdentity] = Field(default_factory=list)
    users: list[ExportedUser] = Field(default_factory=list)
    versions: list[ExportedVersion] = Field(default_factory=list)
    admin_actions: list[ExportedAdminAction] = Field(default_factory=list)
    audit_events: list[ExportedAuditEvent] = Field(default_factory=list)
    tasks: list[ExportedTask]
    scopes: list[ExportedScope] = Field(default_factory=list)

    @field_validator("format")
    @classmethod
    def _format(cls, value: str | None) -> str:
        if value in (None, "", FORMAT_NAME):
            return FORMAT_NAME
        raise ValueError(f"unknown metadata format: {value!r}")

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: int) -> int:
        if int(value) != SCHEMA_VERSION:
            raise ValueError("unsupported metadata schema_version")
        return int(value)

    @field_validator("contract_version")
    @classmethod
    def _contract(cls, value: int) -> int:
        if int(value) != CONTRACT_VERSION:
            raise ValueError("unsupported metadata contract_version")
        return int(value)

    @field_validator("exported_at")
    @classmethod
    def _exported_at(cls, value: str | None) -> str | None:
        return _optional_time(value)


def _pydantic_messages(exc: PydanticValidationError) -> list[str]:
    messages = []
    for item in exc.errors():
        loc = ".".join(str(part) for part in item.get("loc") or ())
        msg = item.get("msg") or "invalid value"
        messages.append(f"{loc}: {msg}" if loc else msg)
    return messages


def _raise_errors(errors: list[str]) -> None:
    if errors:
        raise MetadataImportError(errors[0], errors=errors, report={"ok": False, "errors": errors})


def parse_metadata_document(data: Any) -> dict:
    """Structural and domain validation shared by verify-metadata and import."""
    if not isinstance(data, dict):
        raise MetadataImportError("metadata document must be a JSON object")
    try:
        document = MetadataDocument.model_validate(data)
    except PydanticValidationError as exc:
        _raise_errors(_pydantic_messages(exc))
    dumped = document.model_dump()
    errors = _graph_errors(dumped)
    _raise_errors(errors)
    return dumped


def _graph_errors(document: dict) -> list[str]:
    errors: list[str] = []

    def add_unique(bag: dict[str, str], key, kind: str) -> None:
        if not key:
            return
        text = str(key)
        if text in bag:
            errors.append(f"duplicate {kind} in export: {text}")
        else:
            bag[text] = kind

    tasks: dict[str, str] = {}
    sources: dict[str, str] = {}
    predictions: dict[str, str] = {}
    reviews: dict[str, str] = {}
    batches: dict[str, str] = {}
    runs: dict[str, str] = {}
    identities: dict[str, str] = {}
    versions: dict[str, str] = {}
    users: dict[str, str] = {}
    actions: dict[str, str] = {}
    natural_sources: set[tuple] = set()
    natural_reviews: set[tuple] = set()
    for item in document.get("batches") or []:
        add_unique(batches, item.get("id"), "batch_id")
    for item in document.get("import_runs") or []:
        add_unique(runs, item.get("id"), "import_run_id")
        if item.get("batch_id") and batches and item["batch_id"] not in batches:
            errors.append(f"import run {item['id']} has dangling batch_id")
    version_owner: dict[str, str] = {}
    for item in document.get("versions") or []:
        add_unique(versions, item.get("id"), "version_id")
        version_owner[str(item["id"])] = str(item["task_id"])
    for item in document.get("users") or []:
        add_unique(users, item.get("id"), "user_id")
    for item in document.get("admin_actions") or []:
        add_unique(actions, item.get("id"), "admin_action_id")
    for item in document.get("media_identities") or []:
        add_unique(identities, item.get("id"), "media_identity_id")
    for task in document.get("tasks") or []:
        add_unique(tasks, task.get("task_id"), "task_id")
        history = task.get("source_history") or task.get("sources") or []
        for source in history:
            add_unique(sources, source.get("id"), "source_id")
            natural = (
                source.get("batch_code") or source.get("batch_id"),
                source.get("record_key"), int(source.get("revision") or 1),
            )
            if natural in natural_sources:
                errors.append(
                    f"duplicate source natural key {natural} on task {task.get('task_id')}"
                )
            natural_sources.add(natural)
        for pred in task.get("predictions") or []:
            add_unique(predictions, pred.get("id"), "prediction_id")
            input_version = pred.get("input_version_id")
            if versions and input_version and input_version not in versions:
                errors.append(
                    f"prediction {pred.get('id')} has dangling input_version_id"
                )
            owner = version_owner.get(str(input_version or ""))
            if owner and owner != str(task.get("task_id")):
                errors.append(
                    f"prediction {pred.get('id')} input_version_id belongs to another task"
                )
        for review in task.get("reviews") or []:
            add_unique(reviews, review.get("id"), "review_id")
            version_id = review.get("version_id")
            if versions and version_id and version_id not in versions:
                errors.append(f"review {review.get('id')} has dangling version_id")
            owner = version_owner.get(str(version_id or ""))
            if owner and owner != str(task.get("task_id")):
                errors.append(
                    f"review {review.get('id')} version_id belongs to another task"
                )
            natural = (version_id, int(review.get("review_no") or 0))
            if natural in natural_reviews:
                errors.append(f"duplicate review natural key {natural}")
            natural_reviews.add(natural)
    for task in document.get("tasks") or []:
        for review in task.get("reviews") or []:
            previous = review.get("previous_review_id")
            if previous and previous not in reviews:
                errors.append(
                    f"review {review.get('id')} has dangling previous_review_id"
                )
        for source in task.get("source_history") or []:
            batch_id = source.get("batch_id")
            if batches and batch_id and batch_id not in batches:
                errors.append(f"source {source.get('id')} has dangling batch_id")
    for event in document.get("audit_events") or []:
        publication = event.get("publication_review_id") or (
            event.get("details") or {}
        ).get("scene_review_id")
        if publication and reviews and str(publication) not in reviews:
            errors.append(f"audit event has dangling publication review {publication}")
        version_id = event.get("version_id")
        if versions and version_id and version_id not in versions:
            errors.append(f"audit event has dangling version {version_id}")
        task_id = event.get("task_id")
        if tasks and task_id and task_id not in tasks:
            errors.append(f"audit event has dangling task {task_id}")
    return errors


def load_metadata_path(path: Path) -> dict:
    target = Path(path).expanduser().resolve()
    if target.is_dir():
        candidate = target / METADATA_FILENAME
        if not candidate.is_file():
            raise MetadataImportError(f"missing {METADATA_FILENAME} in {target}")
        target = candidate
    if not target.is_file():
        raise MetadataImportError(f"metadata file not found: {target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MetadataImportError(f"malformed metadata JSON: {exc}") from exc
    return parse_metadata_document(data)


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
    bad = sorted(set(raw) & FORBIDDEN_MAPPING_KEYS)
    if bad:
        raise MetadataImportError(
            "mapping must not remap by username or pathname: " + ", ".join(bad)
        )
    try:
        mapping = IdentityMapping.model_validate(raw)
    except PydanticValidationError as exc:
        raise MetadataImportError(
            f"unknown or invalid mapping fields: {exc}",
            errors=_pydantic_messages(exc),
        ) from exc
    for table, pairs in mapping.model_dump().items():
        for source, dest in pairs.items():
            as_uuid(source, f"mapping.{table}.key")
            as_uuid(dest, f"mapping.{table}.value")
    return mapping
