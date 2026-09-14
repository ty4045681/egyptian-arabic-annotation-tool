"""Canonical inference input snapshot used by classify.py and serializers."""

from __future__ import annotations

import hashlib

CLASSIFY_PROMPT_VERSION = "classify-v1"


def digest_inference_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def join_inference_text(pairs: list[tuple[str, str]]) -> str:
    """Build the exact classifier input from (text, asr_text) rows."""
    parts: list[str] = []
    for text, asr_text in pairs:
        chosen = (text or "").strip() or (asr_text or "").strip()
        if chosen:
            parts.append(chosen)
    return " ".join(parts)


def snapshot_from_pairs(version_id, revision: int | None,
                        pairs: list[tuple[str, str]]) -> dict:
    input_text = join_inference_text(pairs)
    return {
        "version_id": str(version_id) if version_id is not None else None,
        "revision": None if revision is None else int(revision),
        "input_text": input_text,
        "input_digest": digest_inference_text(input_text),
    }


def version_inference_snapshot(cur, version_id) -> dict | None:
    if version_id is None:
        return None
    rows = cur.execute(
        """SELECT v.revision, s.segment_id,
                  COALESCE(s.text, ''), COALESCE(s.asr_text, '')
           FROM annotation_versions v
           LEFT JOIN segments s ON s.version_id = v.id
           WHERE v.id = %s
           ORDER BY s.segment_id NULLS LAST""",
        (version_id,),
    ).fetchall()
    if not rows:
        return None
    pairs = [
        (text, asr_text) for _revision, segment_id, text, asr_text in rows
        if segment_id is not None
    ]
    return snapshot_from_pairs(version_id, rows[0][0], pairs)


def task_inference_snapshot(cur, task_id) -> dict | None:
    """Snapshot the version classify.py would send to the model.

    Published text wins when a published version exists; otherwise the
    current draft. Call this immediately before the external inference
    request so version/revision/digest describe the actual input. The
    join is one statement so revision and segment text are atomic.
    """
    if task_id is None:
        return None
    rows = cur.execute(
        """SELECT v.id, v.revision, s.segment_id,
                  COALESCE(s.text, ''), COALESCE(s.asr_text, '')
           FROM annotation_tasks t
           JOIN annotation_versions v ON v.id = COALESCE(
                t.current_published_version_id,
                (
                  SELECT d.id FROM annotation_versions d
                  WHERE d.task_id = t.id AND d.lifecycle = 'draft'
                  ORDER BY d.version_no DESC LIMIT 1
                )
           )
           LEFT JOIN segments s ON s.version_id = v.id
           WHERE t.id = %s
           ORDER BY s.segment_id NULLS LAST""",
        (task_id,),
    ).fetchall()
    if not rows:
        return None
    version_id, revision = rows[0][0], rows[0][1]
    pairs = [
        (text, asr_text) for _vid, _rev, segment_id, text, asr_text in rows
        if segment_id is not None
    ]
    return snapshot_from_pairs(version_id, revision, pairs)


def freeze_classification_snapshot(snapshot: dict, *, model_name: str,
                                   prompt_version: str = CLASSIFY_PROMPT_VERSION) -> dict:
    frozen = dict(snapshot)
    frozen["model_name"] = model_name
    frozen["prompt_version"] = prompt_version or CLASSIFY_PROMPT_VERSION
    return frozen


def prediction_is_stale(prediction: dict | None, snapshot: dict | None) -> bool:
    if not prediction or not snapshot:
        return False
    if str(prediction.get("input_version_id") or "") != str(snapshot.get("version_id") or ""):
        return True
    pred_digest = prediction.get("input_digest") or ""
    snap_digest = snapshot.get("input_digest") or ""
    if pred_digest and snap_digest and pred_digest != snap_digest:
        return True
    if (prediction.get("input_revision") is not None
            and snapshot.get("revision") is not None
            and int(prediction["input_revision"]) != int(snapshot["revision"])):
        return True
    return False
