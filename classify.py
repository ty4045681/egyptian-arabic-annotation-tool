#!/usr/bin/env python3
"""Classify current PostgreSQL annotation tasks with DashScope.

The script fetches small keyset-paginated batches, releases the DB
connection before every external API call, then updates only the task's
category. It never writes the frozen legacy JSON archive.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from db import db_conn, db_tx

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "config.json"

CATEGORIES = [
    "Restaurant", "Hotel", "Taxi", "Airport", "Clinic",
    "Tourism information", "Emergencies", "Spoken languages",
    "Business negotiation", "Shopping",
]

SYSTEM_PROMPT = f"""You are an audio content classifier for Egyptian Arabic conversational audio.

Given the Arabic transcription of an audio conversation, classify it into exactly ONE of these 10 categories:

{chr(10).join(f'- {c}' for c in CATEGORIES)}

If the audio does NOT fit any of the 10 categories, output: Other-<category>
where <category> is a concise English label for the actual topic.

Rules:
- Output ONLY the category name, nothing else.
- Do NOT include any number, bullet, dash, prefix, explanation, or punctuation.
- Match case exactly for the 10 categories.
- For Other, use Title-Case with a hyphen (for example Other-Politics)."""


def load_config() -> dict:
    config = {"asr": {"api_key": ""}}
    if CONFIG_PATH.exists():
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for key, value in loaded.items():
            if isinstance(config.get(key), dict) and isinstance(value, dict):
                config[key].update(value)
            else:
                config[key] = value
    return config


def call_classify(text: str, api_key: str, model: str) -> str:
    import dashscope
    from dashscope import Generation

    dashscope.api_key = api_key
    response = Generation.call(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text[:6000]},
        ],
        temperature=0.1,
        max_tokens=20,
    )
    if response.status_code == 200:
        result = (response.output.get("text", "") or "").strip()
        for line in result.splitlines():
            cleaned = line.strip().strip('"').strip("'").strip(".")
            if cleaned:
                return cleaned
        return result
    code = str(getattr(response, "code", "") or "")
    message = str(getattr(response, "message", "") or "")
    if "DataInspection" in code or "inappropriate" in message.lower():
        return "Rejected"
    raise RuntimeError(f"API error {response.status_code}: {code} - {message}")


def load_batch(after_order: int, *, force: bool, size: int = 200) -> list[dict]:
    category_filter = "" if force else "AND t.category IS NULL"
    with db_conn() as conn:
        rows = conn.execute(
            f"""SELECT t.id, t.allocation_order, t.filename, t.category,
                       btrim(string_agg(
                         COALESCE(NULLIF(btrim(s.text), ''), s.asr_text),
                         ' ' ORDER BY s.segment_id
                       )) AS transcription
                FROM annotation_tasks t
                JOIN annotation_versions v ON v.task_id = t.id AND (
                     v.id = t.current_published_version_id OR
                     (t.current_published_version_id IS NULL AND v.lifecycle = 'draft')
                )
                LEFT JOIN segments s ON s.version_id = v.id
                WHERE t.allocation_order > %s {category_filter}
                GROUP BY t.id
                ORDER BY t.allocation_order
                LIMIT %s""",
            (after_order, size),
        ).fetchall()
    return [
        {"id": row[0], "order": row[1], "filename": row[2],
         "category": row[3], "text": row[4] or ""}
        for row in rows
    ]


def update_category(task_id, category: str) -> None:
    with db_tx() as conn:
        conn.execute(
            "UPDATE annotation_tasks SET category = %s, updated_at = now() WHERE id = %s",
            (category, task_id),
        )


def iter_candidates(force: bool):
    after = -1
    while True:
        batch = load_batch(after, force=force)
        if not batch:
            break
        yield from batch
        after = batch[-1]["order"]


def main() -> None:
    config = load_config()
    parser = argparse.ArgumentParser(description="Classify PostgreSQL annotation tasks")
    parser.add_argument("--model", "-m", default="qwen-turbo")
    parser.add_argument("--force", "-f", action="store_true")
    parser.add_argument("--dry-run", "-n", action="store_true")
    parser.add_argument("--limit", "-l", type=int, default=0)
    args = parser.parse_args()

    api_key = config.get("asr", {}).get("api_key", "")
    if not args.dry_run and not api_key:
        print("DashScope api_key is not configured", file=sys.stderr)
        raise SystemExit(1)

    candidates = []
    no_text = 0
    for task in iter_candidates(args.force):
        if not task["text"]:
            no_text += 1
            continue
        candidates.append(task)
        if args.limit and len(candidates) >= args.limit:
            break

    print(f"Tasks to classify: {len(candidates)} | no text: {no_text}")
    if args.dry_run:
        for task in candidates[:5]:
            print(f"  {task['filename']}: {task['text'][:120]}")
        return

    stats: dict[str, int] = {}
    failed = 0
    started = time.monotonic()
    for index, task in enumerate(candidates, 1):
        try:
            category = call_classify(task["text"], api_key, args.model)
            update_category(task["id"], category)
            stats[category] = stats.get(category, 0) + 1
            print(f"[{index}/{len(candidates)}] {task['filename']} -> {category}")
        except Exception as exc:  # external API errors are isolated per task
            failed += 1
            print(f"[{index}/{len(candidates)}] {task['filename']} failed: {exc}", file=sys.stderr)
            time.sleep(2)

    elapsed = time.monotonic() - started
    print(f"Completed: {len(candidates) - failed}; failed: {failed}; elapsed: {elapsed:.1f}s")
    for category in sorted(stats):
        print(f"  {category}: {stats[category]}")


if __name__ == "__main__":
    main()
