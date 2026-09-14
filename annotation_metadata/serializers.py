"""Single serializer for assignment, completed, history, and admin detail."""

from __future__ import annotations

from annotation_metadata import SCHEMA_VERSION
from annotation_metadata.features import feature_flags, metadata_ui_enabled
from annotation_metadata.predictions import (
    prediction_is_stale,
    version_inference_snapshot,
)
from annotation_metadata.repository import (
    assignment_claim_context,
    latest_prediction,
    latest_review,
    list_current_sources,
)
from annotation_metadata.taxonomy import (
    confidence_label,
    review_label,
    scene_label,
)


def _safe_url(url: str | None) -> str | None:
    if not url:
        return None
    lowered = url.strip().lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return url.strip()
    return None


def public_source(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "scene_code": item.get("scene_code"),
        "scene_label": item.get("scene_label") or scene_label(item.get("scene_code")),
        "confidence": item.get("confidence") or "unknown",
        "confidence_basis": item.get("confidence_basis") or "",
        "provider": item.get("provider"),
        "video_id": item.get("video_id"),
        "source_url": _safe_url(item.get("source_url")),
        "batch_code": item.get("batch_code"),
        "source_type": item.get("source_type") or "",
        "channel_title": item.get("channel_title"),
    }


def headline(metadata: dict) -> str:
    sources = metadata.get("sources") or []
    review = metadata.get("scene_review") or {}
    claim = metadata.get("claim_context") or {}
    primary = None
    if claim.get("scene_code"):
        primary = next(
            (item for item in sources if item.get("scene_code") == claim.get("scene_code")),
            None,
        )
        if primary is None:
            primary = {
                "scene_code": claim.get("scene_code"),
                "scene_label": scene_label(claim.get("scene_code")),
                "confidence": claim.get("confidence") or "unknown",
            }
    elif len(sources) == 1:
        primary = sources[0]
    if not sources and primary is None:
        scene_part = "来源场景未知"
        conf_part = "来源置信度未知"
    elif primary is not None:
        scene_part = primary.get("scene_label") or scene_label(primary.get("scene_code"))
        conf_part = confidence_label(primary.get("confidence"))
        if len(sources) > 1:
            scene_part = f"{scene_part}（{len(sources)} 个来源场景）"
    else:
        labels = [
            f"{item.get('scene_label') or scene_label(item.get('scene_code'))}·"
            f"{confidence_label(item.get('confidence'))}"
            for item in sources
        ]
        scene_part = "；".join(labels)
        conf_part = "来源置信度按场景"
    review_part = review_label((review or {}).get("status"))
    draft = metadata.get("draft_review")
    if draft and not (review or {}).get("submitted"):
        review_part = "本人已保存，未提交"
    return f"{scene_part} · {conf_part} · {review_part}"


def serialize_task_metadata(cur, task_id, *, version_id=None,
                            published_version_id=None,
                            assignment_user_id=None,
                            include_draft_review: bool = False) -> dict:
    sources = [public_source(item) for item in list_current_sources(cur, task_id)]
    sources.sort(key=lambda item: (item.get("scene_code") or "", item.get("id") or ""))
    prediction = latest_prediction(cur, task_id)
    published_review = latest_review(cur, published_version_id) if published_version_id else None
    working_review = latest_review(cur, version_id) if version_id else None
    claim_context = None
    if assignment_user_id is not None:
        claim_context = assignment_claim_context(cur, assignment_user_id)
    stale_prediction = False
    if prediction and version_id:
        stale_prediction = prediction_is_stale(
            prediction, version_inference_snapshot(cur, version_id),
        )
    correcting = bool(
        include_draft_review and version_id and published_version_id
        and str(version_id) != str(published_version_id)
    )
    if correcting:
        official = working_review or {
            "status": "pending", "scene_codes": [], "note": "", "id": None,
        }
        submitted = False
    else:
        official = published_review or working_review or {
            "status": "pending", "scene_codes": [], "note": "", "id": None,
        }
        submitted = published_review is not None
    scene_review = {
        "status": (official or {}).get("status") or "pending",
        "scene_codes": list((official or {}).get("scene_codes") or []),
        "note": (official or {}).get("note") or "",
        "id": (official or {}).get("id"),
        "submitted": submitted,
        "actor_kind": (official or {}).get("actor_kind"),
    }
    draft_review = None
    if correcting:
        draft_review = {
            "status": (working_review or {}).get("status") or "pending",
            "scene_codes": list((working_review or {}).get("scene_codes") or []),
            "note": (working_review or {}).get("note") or "",
            "id": (working_review or {}).get("id"),
            "submitted": False,
        }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "sources": sources,
        "prediction": (
            None if not prediction else {
                "predicted_label": prediction["predicted_label"],
                "predicted_scene_code": prediction["predicted_scene_code"],
                "score": prediction["score"],
                "model_name": prediction["model_name"],
                "stale": stale_prediction,
                "created_at": prediction["created_at"],
            }
        ),
        "scene_review": scene_review,
        "draft_review": draft_review,
        "reference_review": published_review if correcting else None,
        "claim_context": (
            None if not claim_context else {
                "scene_code": claim_context.get("scene_code"),
                "confidence": claim_context.get("confidence"),
                "policy": claim_context.get("policy"),
                "source_id": claim_context.get("source_id"),
            }
        ),
        "headline": "",
        "unknown": {
            "source_scene": not sources,
            "source_confidence": not sources,
            "scene_review": scene_review.get("status") == "pending",
        },
        "notice": "来源判断尚未代表人工核验",
        "features": feature_flags(),
    }
    payload["headline"] = headline(payload)
    if not metadata_ui_enabled():
        payload["ui_enabled"] = False
    else:
        payload["ui_enabled"] = True
    return payload


def attach_metadata(cur, payload: dict, task_id, **kwargs) -> dict:
    payload["metadata"] = serialize_task_metadata(cur, task_id, **kwargs)
    return payload
