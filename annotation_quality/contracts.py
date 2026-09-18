"""Strict request/response models for cross-check settings, claims, and admin APIs.

Unknown fields are rejected. Integer fields reject booleans. Word-difference
threshold is a constant, not a writable settings field.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from annotation_metadata.contracts import parse_strict as parse_strict


WORD_DIFFERENCE_THRESHOLD_BPS = 1000
COMPARISON_VERSION = "worddiff_v1"

OPEN_ROUND_STATES = frozenset({"in_progress", "awaiting_review"})
COVERING_ROUND_STATES = frozenset({
    "in_progress", "awaiting_review", "passed", "adjudicated",
})
EXCEPTION_REASON_CODES = frozenset({
    "word_difference_exceeded",
    "submission_status_conflict",
    "empty_original_text",
    "empty_secondary_text",
    "bad_quality_conflict",
    "comparison_unavailable",
})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CrossCheckState(StrEnum):
    IN_PROGRESS = "in_progress"
    AWAITING_REVIEW = "awaiting_review"
    PASSED = "passed"
    ADJUDICATED = "adjudicated"
    CANCELLED = "cancelled"
    INVALIDATED = "invalidated"


class CrossCheckReasonCode(StrEnum):
    WORD_DIFFERENCE_EXCEEDED = "word_difference_exceeded"
    SUBMISSION_STATUS_CONFLICT = "submission_status_conflict"
    EMPTY_ORIGINAL_TEXT = "empty_original_text"
    EMPTY_SECONDARY_TEXT = "empty_secondary_text"
    BAD_QUALITY_CONFLICT = "bad_quality_conflict"
    COMPARISON_UNAVAILABLE = "comparison_unavailable"


class CrossCheckDecision(StrEnum):
    ORIGINAL = "original"
    SECONDARY = "secondary"
    EDITED = "edited"


class VersionPurpose(StrEnum):
    ANNOTATION = "annotation"
    CROSS_CHECK = "cross_check"
    ADJUDICATION = "adjudication"


class AssignmentMode(StrEnum):
    ANNOTATION = "annotation"
    REVISION = "revision"
    CROSS_CHECK = "cross_check"


class CrossCheckEditBase(StrEnum):
    ORIGINAL = "original"
    SECONDARY = "secondary"


def _require_uuid(value: str, field: str) -> str:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc
    return str(value)


def _require_reason(value: str) -> str:
    reason = (value or "").strip()
    if not reason:
        raise ValueError("reason is required")
    if len(reason) > 4000:
        raise ValueError("reason is too long")
    return reason


class CrossCheckSettingsView(StrictModel):
    enabled: StrictBool
    sampling_rate_bps: StrictInt
    revision: StrictInt
    word_difference_threshold_bps: StrictInt = WORD_DIFFERENCE_THRESHOLD_BPS
    comparison_version: str = COMPARISON_VERSION
    updated_at: str | None = None
    updated_by_admin_action_id: str | None = None
    action_id: str | None = None


class CrossCheckSettingsUpdateCommand(StrictModel):
    operation_id: str
    expected_revision: StrictInt
    enabled: StrictBool
    sampling_rate_bps: StrictInt
    reason: str

    @field_validator("operation_id", mode="before")
    @classmethod
    def _operation_id(cls, value: Any) -> str:
        return _require_uuid(value, "operation_id")

    @field_validator("expected_revision")
    @classmethod
    def _revision(cls, value: int) -> int:
        if value < 0:
            raise ValueError("expected_revision must be >= 0")
        return value

    @field_validator("sampling_rate_bps")
    @classmethod
    def _rate(cls, value: int) -> int:
        if value < 0 or value > 10000:
            raise ValueError("sampling_rate_bps must be between 0 and 10000")
        return value

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return _require_reason(value)


class CrossCheckAssignmentInfo(StrictModel):
    round_id: str
    state: CrossCheckState

    @field_validator("round_id", mode="before")
    @classmethod
    def _round_id(cls, value: Any) -> str:
        return _require_uuid(value, "round_id")


class CrossCheckCompleteInfo(StrictModel):
    round_id: str
    state: CrossCheckState
    training_export_blocked: StrictBool

    @field_validator("round_id", mode="before")
    @classmethod
    def _round_id(cls, value: Any) -> str:
        return _require_uuid(value, "round_id")


class CrossCheckClaimFilters(StrictModel):
    source_scene: str | None = None
    batch_code: str | None = None
    source_confidence: str | None = None


class CrossCheckListQuery(StrictModel):
    state: CrossCheckState = CrossCheckState.AWAITING_REVIEW
    source_scene: str | None = None
    batch_code: str | None = None
    original_annotator_id: str | None = None
    secondary_annotator_id: str | None = None
    reason_code: str | None = None
    q: str | None = None
    created_from: str | None = None
    created_to: str | None = None
    limit: StrictInt = 50
    cursor: str | None = None

    @field_validator("original_annotator_id", "secondary_annotator_id", mode="before")
    @classmethod
    def _optional_uuid(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return _require_uuid(value, "annotator_id")

    @field_validator("limit")
    @classmethod
    def _limit(cls, value: int) -> int:
        if value < 1 or value > 100:
            raise ValueError("limit must be between 1 and 100")
        return value

    @field_validator("q", "batch_code", "source_scene", "reason_code", "cursor")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        return str(value).strip() or None


class CrossCheckListItem(StrictModel):
    round_id: str
    task_id: str
    state: CrossCheckState
    original_annotator_id: str | None = None
    secondary_annotator_id: str
    duration_seconds: float | None = None
    original_word_count: StrictInt | None = None
    secondary_word_count: StrictInt | None = None
    word_difference_rate: float | None = None
    reason_codes: list[str] = Field(default_factory=list)
    created_at: str
    submitted_at: str | None = None
    resolved_at: str | None = None
    training_export_blocked: StrictBool
    source_scene: str | None = None
    batch_code: str | None = None


class CrossCheckListResponse(StrictModel):
    items: list[CrossCheckListItem] = Field(default_factory=list)
    next_cursor: str | None = None
    applied_filters: dict[str, Any] = Field(default_factory=dict)


class CrossCheckDetailView(StrictModel):
    round_id: str
    task_id: str
    revision: StrictInt
    state: CrossCheckState
    original_version_id: str
    original_annotator_id: str | None = None
    original_review_id: str | None = None
    secondary_version_id: str
    secondary_annotator_id: str
    baseline_version_id: str
    baseline_quality: str
    current_published_version_id: str | None = None
    settings_revision: StrictInt
    sampling_rate_bps: StrictInt
    claim_policy: str
    claim_filters: dict[str, Any] = Field(default_factory=dict)
    comparison_version: str = COMPARISON_VERSION
    threshold_bps: StrictInt = WORD_DIFFERENCE_THRESHOLD_BPS
    original_word_count: StrictInt | None = None
    secondary_word_count: StrictInt | None = None
    edit_distance: StrictInt | None = None
    substitutions: StrictInt | None = None
    insertions: StrictInt | None = None
    deletions: StrictInt | None = None
    word_difference_rate: float | None = None
    original_normalized_summary: str | None = None
    secondary_normalized_summary: str | None = None
    original_input_revision: StrictInt | None = None
    secondary_input_revision: StrictInt | None = None
    diff_ops: Any = None
    segment_map: Any = None
    reason_codes: list[str] = Field(default_factory=list)
    comparison_unavailable: StrictBool = False
    original_segments: list[dict[str, Any]] = Field(default_factory=list)
    secondary_segments: list[dict[str, Any]] = Field(default_factory=list)
    decision: CrossCheckDecision | None = None
    final_version_id: str | None = None
    decided_by_admin_action_id: str | None = None
    decision_reason: str | None = None
    created_at: str
    submitted_at: str | None = None
    compared_at: str | None = None
    resolved_at: str | None = None
    training_export_blocked: StrictBool
    termination_reason: str | None = None


class CrossCheckDecisionCommand(StrictModel):
    operation_id: str
    expected_revision: StrictInt
    expected_original_version_id: str
    expected_secondary_version_id: str
    decision: CrossCheckDecision
    reason: str
    base: CrossCheckEditBase | None = None
    segments: list[dict[str, Any]] | None = None
    target_status: Literal["annotated", "skipped"] | None = None
    skip_reasons: list[str] | None = None
    scene_review: dict[str, Any] | None = None

    @field_validator("operation_id", "expected_original_version_id",
                     "expected_secondary_version_id", mode="before")
    @classmethod
    def _uuids(cls, value: Any, info) -> str:
        return _require_uuid(value, info.field_name)

    @field_validator("expected_revision")
    @classmethod
    def _revision(cls, value: int) -> int:
        if value < 0:
            raise ValueError("expected_revision must be >= 0")
        return value

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return _require_reason(value)

    @model_validator(mode="after")
    def _decision_fields(self) -> "CrossCheckDecisionCommand":
        edit_fields = {
            "base": self.base,
            "segments": self.segments,
            "target_status": self.target_status,
            "skip_reasons": self.skip_reasons,
            "scene_review": self.scene_review,
        }
        if self.decision == CrossCheckDecision.EDITED:
            if self.base is None:
                raise ValueError("base is required when decision is edited")
            if not self.segments:
                raise ValueError("segments are required when decision is edited")
            if self.target_status is None:
                raise ValueError("target_status is required when decision is edited")
            if self.target_status == "skipped" and not self.skip_reasons:
                raise ValueError("skip_reasons are required when target_status is skipped")
            return self
        extra = [name for name, value in edit_fields.items() if value is not None]
        if extra:
            raise ValueError(
                "original/secondary decisions cannot include " + ", ".join(extra)
            )
        return self


class CrossCheckDecisionResult(StrictModel):
    success: StrictBool
    action_id: str
    round_id: str
    state: CrossCheckState
    final_version_id: str | None = None
    final_status: str | None = None
    training_export_blocked: StrictBool


class CrossCheckCancelCommand(StrictModel):
    operation_id: str
    expected_revision: StrictInt
    reason: str
    confirm: StrictBool

    @field_validator("operation_id", mode="before")
    @classmethod
    def _operation_id(cls, value: Any) -> str:
        return _require_uuid(value, "operation_id")

    @field_validator("expected_revision")
    @classmethod
    def _revision(cls, value: int) -> int:
        if value < 0:
            raise ValueError("expected_revision must be >= 0")
        return value

    @field_validator("confirm")
    @classmethod
    def _confirm(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("confirm must be true")
        return value

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return _require_reason(value)


class CrossCheckMineQuery(StrictModel):
    limit: StrictInt = 50
    cursor: str | None = None

    @field_validator("limit")
    @classmethod
    def _limit(cls, value: int) -> int:
        if value < 1 or value > 100:
            raise ValueError("limit must be between 1 and 100")
        return value


class CrossCheckMineItem(StrictModel):
    round_id: str
    task_id: str
    state: CrossCheckState
    submitted_at: str | None = None
    version_id: str


class CrossCheckMineResponse(StrictModel):
    items: list[CrossCheckMineItem] = Field(default_factory=list)
    next_cursor: str | None = None


class CrossCheckSubmissionView(StrictModel):
    round_id: str
    task_id: str
    state: CrossCheckState
    version_id: str
    submitted_at: str | None = None
    target_status: str | None = None
    segments: list[dict[str, Any]] = Field(default_factory=list)
    skip_reasons: list[str] = Field(default_factory=list)
