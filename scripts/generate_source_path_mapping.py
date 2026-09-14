#!/usr/bin/env python3
"""Map exported segment WAV paths back to their source database audio paths."""

from __future__ import annotations

import argparse
import json
import os
import re
import tarfile
import tempfile
from collections import defaultdict
from pathlib import Path, PurePosixPath

import psycopg


TASK_TOKEN_RE = re.compile(r"^[0-9a-f]{12}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a segment-to-source-path mapping for an export tar"
    )
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dsn-env", default="ANNOTATION_BACKUP_DSN")
    return parser.parse_args()


def read_export_paths(archive_path: Path) -> list[tuple[str, str]]:
    with tarfile.open(archive_path, "r") as archive:
        data_file = archive.extractfile("data.json")
        if data_file is None:
            raise RuntimeError("Archive does not contain data.json")
        data = json.load(data_file)

    if not isinstance(data, list):
        raise RuntimeError("data.json must contain a JSON array")

    parsed: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("audio"), str):
            raise RuntimeError(f"Item {index} has no valid audio path")
        audio_path = item["audio"]
        relative = PurePosixPath(audio_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"Unsafe exported audio path: {audio_path!r}")
        if audio_path in seen:
            raise RuntimeError(f"Duplicate exported audio path: {audio_path}")
        seen.add(audio_path)

        name_parts = relative.name.rsplit("__", 2)
        if len(name_parts) != 3 or not TASK_TOKEN_RE.fullmatch(name_parts[1]):
            raise RuntimeError(
                f"Cannot parse task UUID token from exported path: {audio_path}"
            )
        parsed.append((audio_path, name_parts[1]))
    return parsed


def query_task_paths(dsn: str, tokens: list[str]) -> dict[str, tuple[str, str]]:
    sql = """
        SELECT id::text, replace(id::text, '-', '') AS compact_id, rel_path
        FROM annotation_tasks
        WHERE left(replace(id::text, '-', ''), 12) = ANY(%s)
        ORDER BY id
    """
    matches: dict[str, list[tuple[str, str]]] = defaultdict(list)
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            for task_id, compact_id, rel_path in conn.execute(sql, (tokens,)):
                matches[compact_id[:12]].append((task_id, rel_path))

    missing = [token for token in tokens if token not in matches]
    ambiguous = {token: rows for token, rows in matches.items() if len(rows) != 1}
    if missing:
        raise RuntimeError(
            f"No annotation_tasks row found for {len(missing)} task tokens; "
            f"first missing token: {missing[0]}"
        )
    if ambiguous:
        first_token = next(iter(ambiguous))
        raise RuntimeError(
            f"Task token {first_token} matches {len(ambiguous[first_token])} rows"
        )
    return {token: rows[0] for token, rows in matches.items()}


def resolve_source(audio_dir: Path, rel_path: str) -> Path:
    relative = PurePosixPath(rel_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"Unsafe database rel_path: {rel_path!r}")
    source = (audio_dir / Path(*relative.parts)).resolve()
    try:
        source.relative_to(audio_dir)
    except ValueError as exc:
        raise RuntimeError(f"Database path escapes audio root: {rel_path!r}") from exc
    if not source.is_file():
        raise FileNotFoundError(f"Source audio is missing: {source}")
    return source


def write_mapping(output: Path, rows: list[dict[str, str]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{output.name}.",
        suffix=".partial",
        dir=output.parent,
        delete=False,
    )
    temp_path = Path(temporary.name)
    try:
        with temporary:
            json.dump(rows, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        temp_path.replace(output)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def main() -> None:
    args = parse_args()
    archive_path = args.archive.expanduser().resolve()
    audio_dir = args.audio_dir.expanduser().resolve()
    output = args.output.expanduser().resolve()
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        raise SystemExit(f"Environment variable {args.dsn_env} is not set")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {output}")

    exported = read_export_paths(archive_path)
    tokens = sorted({token for _, token in exported})
    task_paths = query_task_paths(dsn, tokens)

    rows: list[dict[str, str]] = []
    for audio_path, token in exported:
        _task_id, rel_path = task_paths[token]
        source = resolve_source(audio_dir, rel_path)
        rows.append(
            {
                "audio": audio_path,
                "source_rel_path": rel_path,
                "source_absolute_path": str(source),
            }
        )

    write_mapping(output, rows)
    print(
        f"Created {output} with {len(rows)} segment mappings "
        f"across {len(tokens)} source tasks"
    )


if __name__ == "__main__":
    main()
