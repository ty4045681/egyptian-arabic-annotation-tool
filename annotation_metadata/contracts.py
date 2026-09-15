"""Typed boundary models. API requests reject unknown fields; crawler
records keep unknown extensions in raw_record without giving them meaning.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from annotation_metadata.taxonomy import (
    CLAIM_POLICIES,
    CONFIDENCE_LEVELS,
    FALLBACK_SOURCE_SCENE,
    MEDIA_VARIANT,
    REVIEW_STATUSES,
    SCENE_CODES,
    SCOPE_MODES,
    is_source_fallback_filter,
    normalize_source_scene_filter,
    resolve_scene_code,
    validate_review_labels,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LooseModel(BaseModel):
    """Crawler input: extra keys are preserved, not interpreted."""

    model_config = ConfigDict(extra="allow")


Confidence = Literal["high", "medium", "low", "unknown"]
ReviewStatus = Literal["pending", "confirmed", "mixed", "out_of_scope", "uncertain"]
ScopeMode = Literal["all", "restricted", "none"]
ClaimPolicyName = Literal["source_confidence", "fifo"]


def _digest(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SceneReviewInput(StrictModel):
    status: ReviewStatus
    scene_codes: list[str] = Field(default_factory=list)
    note: str = ""

    @field_validator("note")
    @classmethod
    def _note(cls, value: str) -> str:
        text = value or ""
        if len(text) > 4000:
            raise ValueError("note is too long")
        return text

    @field_validator("scene_codes")
    @classmethod
    def _codes(cls, value: list[str]) -> list[str]:
        return list(value or [])

    @model_validator(mode="after")
    def _status_rules(self) -> "SceneReviewInput":
        try:
            object.__setattr__(
                self, "scene_codes",
                sorted(validate_review_labels(self.status, self.scene_codes)),
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self


class ClaimRequest(StrictModel):
    source_scene: str | None = None
    batch_code: str | None = None
    source_confidence: str | None = None

    @field_validator("source_scene")
    @classmethod
    def _scene(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        try:
            return normalize_source_scene_filter(value)
        except ValueError as exc:
            raise ValueError("source_scene is not a recognised scene") from exc

    @field_validator("batch_code")
    @classmethod
    def _batch(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        text = value.strip()
        if len(text) < 4 or text.isdigit():
            raise ValueError(
                "batch_code must be a globally unique code, not a short date folder"
            )
        return text

    @field_validator("source_confidence")
    @classmethod
    def _confidence(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        text = value.strip().lower()
        if text not in CONFIDENCE_LEVELS:
            raise ValueError("source_confidence must be high|medium|low|unknown")
        return text


class AdminSceneReviewCommand(StrictModel):
    operation_id: str
    expected_version_id: str
    expected_review_id: str | None = None
    status: ReviewStatus
    scene_codes: list[str] = Field(default_factory=list)
    note: str = ""
    reason: str

    @model_validator(mode="after")
    def _status_rules(self) -> "AdminSceneReviewCommand":
        object.__setattr__(
            self, "scene_codes",
            sorted(validate_review_labels(self.status, self.scene_codes)),
        )
        reason = (self.reason or "").strip()
        if not reason:
            raise ValueError("reason is required")
        if len(reason) > 4000:
            raise ValueError("reason is too long")
        object.__setattr__(self, "reason", reason)
        uuid.UUID(str(self.operation_id))
        uuid.UUID(str(self.expected_version_id))
        if self.expected_review_id:
            uuid.UUID(str(self.expected_review_id))
        return self


class SceneScopeCommand(StrictModel):
    operation_id: str
    expected_revision: int
    mode: ScopeMode
    scene_codes: list[str] = Field(default_factory=list)
    allow_unknown: bool = False
    reason: str

    @model_validator(mode="after")
    def _scope_rules(self) -> "SceneScopeCommand":
        uuid.UUID(str(self.operation_id))
        if self.expected_revision < 0:
            raise ValueError("expected_revision must be >= 0")
        reason = (self.reason or "").strip()
        if not reason:
            raise ValueError("reason is required")
        codes: list[str] = []
        seen: set[str] = set()
        for raw in self.scene_codes:
            code = resolve_scene_code(raw)
            if code is None:
                raise ValueError(f"unknown scene code: {raw!r}")
            if code not in seen:
                seen.add(code)
                codes.append(code)
        allow_unknown = bool(self.allow_unknown)
        if self.mode == "restricted":
            if allow_unknown and FALLBACK_SOURCE_SCENE not in codes:
                # Legacy allow_unknown=true is Spoken languages permission.
                codes.append(FALLBACK_SOURCE_SCENE)
            if FALLBACK_SOURCE_SCENE in codes:
                allow_unknown = True
            if not codes:
                # Explicit empty restricted list is unclaimable.
                allow_unknown = False
        else:
            if codes:
                raise ValueError("scene_codes are only valid when mode is restricted")
            # Keep allow_unknown as sent for all/none compatibility; it does
            # not grant scenes when mode is not restricted.
        object.__setattr__(self, "scene_codes", codes)
        object.__setattr__(self, "allow_unknown", allow_unknown)
        object.__setattr__(self, "reason", reason)
        return self


class TaskFilter(StrictModel):
    """Shared filter object for pool, claim, admin lists, and statistics."""

    source_scene: str | None = None
    source_confidence: str | None = None
    batch_code: str | None = None
    review_status: str | None = None
    prediction_scene: str | None = None
    human_scene: str | None = None
    selected_scene: str | None = None

    @field_validator("source_scene", "selected_scene")
    @classmethod
    def _optional_source_scene(cls, value: str | None) -> str | None:
        if value in (None, "", "all"):
            return None
        return normalize_source_scene_filter(value)

    @field_validator("prediction_scene", "human_scene")
    @classmethod
    def _optional_result_scene(cls, value: str | None) -> str | None:
        # prediction_scene=unknown / human_scene=unknown keep absent-result
        # semantics. Do not coerce them to Spoken languages.
        if value in (None, "", "all"):
            return None
        if value == "unknown":
            return "unknown"
        code = resolve_scene_code(value)
        if code is None:
            raise ValueError("unrecognised scene filter")
        return code

    @field_validator("source_confidence")
    @classmethod
    def _confidence(cls, value: str | None) -> str | None:
        if value in (None, "", "all"):
            return None
        if value not in CONFIDENCE_LEVELS:
            raise ValueError("source_confidence must be high|medium|low|unknown")
        return value

    @field_validator("review_status")
    @classmethod
    def _review(cls, value: str | None) -> str | None:
        if value in (None, "", "all"):
            return None
        allowed = set(REVIEW_STATUSES) | {
            "unreviewed_unpublished", "unreviewed_published",
        }
        if value not in allowed:
            raise ValueError("invalid review_status")
        return value

    @field_validator("batch_code")
    @classmethod
    def _batch(cls, value: str | None) -> str | None:
        if value in (None, "", "all"):
            return None
        return value.strip()

    def digest(self) -> str:
        return _digest(self.model_dump())[:16]

    def is_empty(self) -> bool:
        data = self.model_dump()
        data.pop("selected_scene", None)
        return not any(data.values())

    def source_scene_value(self) -> str | None:
        return self.selected_scene or self.source_scene

    def requires_source_row(self) -> bool:
        scene = self.source_scene_value()
        if self.batch_code:
            return True
        if scene and not is_source_fallback_filter(scene):
            return True
        if self.source_confidence and self.source_confidence != "unknown":
            return True
        return False

    def allows_virtual_unknown(self) -> bool:
        """No-source tasks match Spoken languages when confidence/batch allow it."""
        if self.batch_code:
            return False
        scene = self.source_scene_value()
        if scene and not is_source_fallback_filter(scene):
            return False
        if self.source_confidence and self.source_confidence != "unknown":
            return False
        return True


class MediaIdentity(StrictModel):
    provider: str
    external_id: str
    variant: str = MEDIA_VARIANT
    pcm_sha256: str | None = None

    @field_validator("provider", "external_id", "variant")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("identity fields must be non-empty")
        return text

    @field_validator("pcm_sha256")
    @classmethod
    def _sha(cls, value: str | None) -> str | None:
        if not value:
            return None
        text = value.strip().lower()
        if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
            raise ValueError("pcm_sha256 must be a 64-character hex digest")
        return text


class NormalizedSource(StrictModel):
    record_key: str
    scene_code: str | None = None
    confidence: Confidence = "unknown"
    confidence_basis: str = ""
    source_type: str = ""
    source_url: str | None = None
    video_id: str | None = None
    channel_id: str | None = None
    channel_title: str | None = None
    provider: str | None = None
    raw_record: dict[str, Any] = Field(default_factory=dict)
    content_digest: str
    rel_path: str | None = None
    source_audio_relpath: str | None = None
    pcm_sha256: str | None = None
    duration: float | None = None
    identity: MediaIdentity | None = None


class CrawlerRecord(LooseModel):
    status: str | None = None
    scene: str | None = None
    scene_code: str | None = None
    confidence: str | None = None
    confidence_basis: str | None = None
    video_id: str | None = None
    url: str | None = None
    source_url: str | None = None
    source_type: str | None = None
    audio_filepath: str | None = None
    relative_path: str | None = None
    rel_path: str | None = None
    audio_path: str | None = None
    provider: str | None = None
    pcm_sha256: str | None = None
    checksum: str | None = None
    audio_sha256: str | None = None
    channel: str | None = None
    channel_id: str | None = None
    channel_title: str | None = None
    record_id: str | None = None
    record_key: str | None = None
    duration: float | None = None
    kind: str | None = None
    verified: bool | None = None


class PreprocessedTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    rel_path: str
    filename: str
    folder: str = ""
    duration: float = 0.0
    segments: list[dict[str, Any]] = Field(default_factory=list)
    waveform_payload: bytes | None = None
    preprocessed_at: Any = None
    category: str | None = None
    skip_if_human_modified: bool = True
    eligible: bool | None = None
    task_extra: dict[str, Any] | None = None
    sources: list[NormalizedSource] = Field(default_factory=list)
    identity: MediaIdentity | None = None
    pcm_sha256: str | None = None
    processing_token: str | None = None
    metadata_only: bool = False
    batch_code: str | None = None


class ImportIssue(StrictModel):
    code: str
    message: str
    record_key: str | None = None
    path: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ImportResult(StrictModel):
    action: str
    task_id: str | None = None
    record_key: str | None = None
    issues: list[ImportIssue] = Field(default_factory=list)


def parse_strict(model_cls: type[BaseModel], payload: dict[str, Any]):
    """Parse a request body; map Pydantic errors onto repository ValidationError."""
    from annotation_repository import ValidationError

    try:
        return model_cls.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError
        errors = getattr(exc, "errors", lambda: [])()
        if errors:
            first = errors[0]
            loc = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
            message = first.get("msg") or str(exc)
            raise ValidationError(
                message if not loc else f"{loc}: {message}",
                code="invalid_field",
                field=loc or None,
            ) from exc
        raise ValidationError(str(exc), code="invalid_field") from exc
