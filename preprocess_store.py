"""Task ingestion into PostgreSQL — shared by preprocess.py, manage_state.py
and tests. Deliberately free of torch/VAD/ASR imports so it stays testable
in the lightweight dev environment.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Json


class TaskProtectedError(Exception):
    """Task has human work and must not be overwritten."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def store_preprocessed_task(
    conn: psycopg.Connection,
    *,
    rel_path: str,
    filename: str,
    folder: str,
    duration: float,
    segments: list[dict],
    waveform_payload: bytes | None = None,
    preprocessed_at: datetime | None = None,
    category: str | None = None,
    skip_if_human_modified: bool = True,
    eligible: bool | None = None,
    task_extra: dict | None = None,
) -> dict:
    """Insert or refresh a pending task with a draft version.

    Returns {"action": "created"|"updated"|"skipped_human", "task_id", ...}.
    Raises TaskProtectedError when the task is published or holds human
    edits and skip_if_human_modified is False.
    """
    task_eligible = bool(segments) if eligible is None else bool(eligible)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """SELECT id, current_published_version_id, status,
                      baseline_version_id
               FROM annotation_tasks WHERE rel_path = %s FOR UPDATE""",
            (rel_path,),
        )
        row = cur.fetchone()

        if row is not None:
            task_id, published_vid, status, baseline_vid = row
            protected = published_vid is not None or cur.execute(
                "SELECT 1 FROM assignments WHERE task_id = %s", (task_id,)
            ).fetchone()
            draft = cur.execute(
                """SELECT id, revision, human_modified
                   FROM annotation_versions
                   WHERE task_id = %s AND lifecycle = 'draft' FOR UPDATE""",
                (task_id,),
            ).fetchone()
            protected = protected or (draft and draft[2])
            if protected:
                if skip_if_human_modified:
                    return {"action": "skipped_human", "task_id": str(task_id)}
                raise TaskProtectedError(
                    f"{rel_path}: task is assigned, published, or human-modified"
                )
            if draft:
                draft_vid = draft[0]
                _replace_draft(cur, task_id, draft_vid, duration, segments)
                if baseline_vid:
                    _replace_baseline(cur, baseline_vid, segments)
                _upsert_waveform(cur, task_id, waveform_payload)
                _touch_task(cur, task_id, filename=filename, folder=folder,
                            duration=duration, eligible=task_eligible,
                            preprocessed_at=preprocessed_at,
                            category=category, extra=task_extra)
                return {"action": "updated", "task_id": str(task_id)}

        # New task
        task_id = uuid.uuid4()
        baseline_id = uuid.uuid4()
        draft_id = uuid.uuid4()
        allocation_order = cur.execute(
            "SELECT nextval('task_allocation_order_seq')"
        ).fetchone()[0]
        cur.execute(
            """INSERT INTO annotation_tasks
                   (id, rel_path, filename, folder, duration, status, eligible,
                    allocation_order, category, preprocessed_at, extra)
               VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s)""",
            (task_id, rel_path, filename, folder, float(duration), task_eligible,
             allocation_order, category, preprocessed_at or datetime.now(timezone.utc),
             Json(task_extra or {})),
        )
        cur.execute(
            """INSERT INTO annotation_versions
                   (id, task_id, version_no, lifecycle, target_status, revision,
                    base_version_id, extra)
               VALUES
                   (%s, %s, 1, 'baseline', 'pending', 0, NULL,
                    '{"baseline_quality":"exact"}'::jsonb),
                   (%s, %s, 2, 'draft', 'pending', 0, %s, '{}'::jsonb)""",
            (baseline_id, task_id, draft_id, task_id, baseline_id),
        )
        _insert_segments(cur, baseline_id, segments)
        _insert_segments(cur, draft_id, segments)
        cur.execute(
            """UPDATE annotation_tasks
               SET baseline_version_id = %s, baseline_quality = 'exact'
               WHERE id = %s""",
            (baseline_id, task_id),
        )
        _upsert_waveform(cur, task_id, waveform_payload)
        return {"action": "created", "task_id": str(task_id)}


def _replace_draft(cur, task_id, draft_vid, duration, segments) -> None:
    cur.execute("DELETE FROM segments WHERE version_id = %s", (draft_vid,))
    _insert_segments(cur, draft_vid, segments)
    cur.execute(
        """UPDATE annotation_versions
           SET revision = revision + 1, updated_at = now()
           WHERE id = %s""",
        (draft_vid,),
    )


def _replace_baseline(cur, baseline_vid, segments) -> None:
    """Refresh preprocessing truth before a task has any human work.

    Baselines are immutable through annotation/admin save paths. The ingestion
    path may refresh one only while the caller has already proven the task has
    no assignment, publication, or human-modified draft.
    """
    cur.execute("DELETE FROM segments WHERE version_id = %s", (baseline_vid,))
    _insert_segments(cur, baseline_vid, segments)
    cur.execute(
        """UPDATE annotation_versions
           SET updated_at = now(), extra = COALESCE(extra, '{}'::jsonb)
               || '{"baseline_quality":"exact"}'::jsonb
           WHERE id = %s AND lifecycle = 'baseline'""",
        (baseline_vid,),
    )


def _insert_segments(cur, version_id, segments: list[dict]) -> None:
    for seg in segments:
        sid = int(seg["id"])
        start = float(seg["start"])
        end = float(seg["end"])
        duration = float(seg.get("duration", end - start))
        asr_text = seg.get("asr_text", "") or ""
        text = seg.get("text", "") or ""
        exclude = bool(seg.get("exclude_from_training", False))
        known = {"id", "start", "end", "duration", "asr_text", "text",
                 "exclude_from_training"}
        extra = {k: v for k, v in seg.items() if k not in known}
        cur.execute(
            """INSERT INTO segments
                   (version_id, segment_id, start_s, end_s, duration,
                    asr_text, text, exclude_from_training, extra)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (version_id, sid, start, end, duration, asr_text, text, exclude,
             Json(extra)),
        )


def _upsert_waveform(cur, task_id, payload: bytes | None) -> None:
    if payload is None:
        return
    checksum = hashlib.sha256(payload).hexdigest()
    cur.execute(
        """INSERT INTO waveforms (task_id, encoding, point_count, payload, checksum)
           VALUES (%s, 'int16le', %s, %s, %s)
           ON CONFLICT (task_id) DO UPDATE SET
               payload = EXCLUDED.payload,
               point_count = EXCLUDED.point_count,
               checksum = EXCLUDED.checksum""",
        (task_id, len(payload) // 2, payload, checksum),
    )


def _touch_task(cur, task_id, *, filename, folder, duration, eligible,
                preprocessed_at, category, extra) -> None:
    cur.execute(
        """UPDATE annotation_tasks
           SET filename = %s, folder = %s, duration = %s, eligible = %s,
               preprocessed_at = COALESCE(%s, preprocessed_at),
               category = COALESCE(%s, category),
               extra = COALESCE(extra, '{}'::jsonb) || %s::jsonb,
               updated_at = now()
           WHERE id = %s""",
        (filename, folder, float(duration), eligible, preprocessed_at, category,
         Json(extra or {}), task_id),
    )


def ingestion_states(conn: psycopg.Connection) -> dict[str, dict]:
    """Return preprocessing state keyed by audio path."""
    rows = conn.execute(
        """SELECT t.rel_path,
                  t.current_published_version_id IS NOT NULL AS published,
                  EXISTS (SELECT 1 FROM assignments a WHERE a.task_id = t.id) AS assigned,
                  EXISTS (
                    SELECT 1 FROM annotation_versions v
                    WHERE v.task_id = t.id AND v.lifecycle = 'draft'
                      AND v.human_modified
                  ) AS human_modified,
                  t.eligible,
                  t.extra->>'preprocess_rejection' AS rejection
           FROM annotation_tasks t"""
    ).fetchall()
    return {
        row[0]: {
            "protected": bool(row[1] or row[2] or row[3]),
            "published": bool(row[1]),
            "assigned": bool(row[2]),
            "human_modified": bool(row[3]),
            "eligible": bool(row[4]),
            "rejection": row[5],
        }
        for row in rows
    }


def load_draft_segments(conn: psycopg.Connection, rel_path: str) -> list[dict]:
    rows = conn.execute(
        """SELECT s.segment_id, s.start_s, s.end_s, s.duration,
                  s.asr_text, s.text, s.exclude_from_training
           FROM annotation_tasks t
           JOIN annotation_versions v ON v.task_id = t.id AND v.lifecycle = 'draft'
           JOIN segments s ON s.version_id = v.id
           WHERE t.rel_path = %s AND NOT v.human_modified
             AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.task_id = t.id)
           ORDER BY s.segment_id""",
        (rel_path,),
    ).fetchall()
    return [
        {"id": row[0], "start": row[1], "end": row[2], "duration": row[3],
         "asr_text": row[4], "text": row[5],
         "exclude_from_training": row[6]}
        for row in rows
    ]


def b64_to_waveform_payload(waveform_b64: str) -> bytes:
    return base64.b64decode(waveform_b64, validate=True)


def waveform_payload_to_b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")
