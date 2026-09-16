"""Compatibility feature flags for the scene-provenance rollout.

The compatibility rollback build ships migration 003 and can read the new
tables. It turns off new claim ordering and write entry points. Scene-scope
enforcement stays on even when the sort policy is fifo.
Checking out the pre-migration git tag is not a valid schema rollback:
``assert_schema_current()`` requires every migration file present in the
running build.
"""

from __future__ import annotations

import os

from annotation_metadata.taxonomy import CLAIM_POLICIES


def _flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def metadata_ui_enabled() -> bool:
    return _flag("ANNOTATION_METADATA_UI", True)


def metadata_write_enabled() -> bool:
    return _flag("ANNOTATION_METADATA_WRITE", True)


def scene_review_write_enabled() -> bool:
    return _flag("ANNOTATION_SCENE_REVIEW_WRITE", True)


def claim_policy_name() -> str:
    raw = (os.environ.get("ANNOTATION_CLAIM_POLICY") or "source_confidence").strip()
    if raw not in CLAIM_POLICIES:
        return "source_confidence"
    return raw


def feature_flags() -> dict:
    return {
        "metadata_ui": metadata_ui_enabled(),
        "metadata_write": metadata_write_enabled(),
        "scene_review_write": scene_review_write_enabled(),
        "claim_policy": claim_policy_name(),
        "scene_scope_enforced": True,
    }
