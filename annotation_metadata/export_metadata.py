"""Versioned metadata sidecar export/import. Legacy JSON export is unchanged."""

from __future__ import annotations

import json
from pathlib import Path

from annotation_metadata import SCHEMA_VERSION
from annotation_metadata.repository import (
    latest_prediction, latest_review, list_current_sources, list_source_history,
    list_active_scenes,
)
from annotation_metadata.queries import load_scope
from annotation_metadata.repository import scope_payload


def export_metadata(conn, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    with conn.cursor() as cur:
        scenes = list_active_scenes(cur)
        batches = cur.execute(
            "SELECT id, batch_code, name, source_note, created_at FROM source_batches"
        ).fetchall()
        tasks = cur.execute("SELECT id FROM annotation_tasks ORDER BY allocation_order").fetchall()
        users = cur.execute("SELECT id, username FROM annotators ORDER BY username").fetchall()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scenes": scenes,
            "batches": [
                {"id": str(row[0]), "batch_code": row[1], "name": row[2],
                 "source_note": row[3],
                 "created_at": row[4].isoformat() if row[4] else None}
                for row in batches
            ],
            "tasks": [],
            "scopes": [],
        }
        for (task_id,) in tasks:
            payload["tasks"].append({
                "task_id": str(task_id),
                "sources": list_current_sources(cur, task_id),
                "source_history": list_source_history(cur, task_id),
                "prediction": latest_prediction(cur, task_id),
                "published_review": latest_review(
                    cur,
                    cur.execute(
                        "SELECT current_published_version_id FROM annotation_tasks WHERE id = %s",
                        (task_id,),
                    ).fetchone()[0],
                ),
            })
        for user_id, username in users:
            payload["scopes"].append({
                "user_id": str(user_id),
                "username": username,
                **scope_payload(load_scope(cur, user_id)),
            })
    path = output / "metadata.v1.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(path), "tasks": len(payload["tasks"]), "scopes": len(payload["scopes"])}


def verify_metadata_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if int(data.get("schema_version") or 0) != SCHEMA_VERSION:
        raise ValueError("unsupported metadata schema_version")
    return {
        "ok": True,
        "tasks": len(data.get("tasks") or []),
        "scopes": len(data.get("scopes") or []),
        "batches": len(data.get("batches") or []),
    }
