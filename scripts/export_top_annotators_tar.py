#!/usr/bin/env python3
"""Export qualifying annotators' published segments as a self-contained tar dataset.

Every exported item corresponds to one non-Bad-quality segment.  Audio is
written as 16 kHz, mono, PCM-16 WAV.  ``data.json`` contains the segment
transcript plus annotator and wall-clock annotation timing.  A separate
``source_path_mapping.json`` maps each segment back to its database-relative
and absolute source audio paths; ``export_metadata.json`` records the export
selection and exclusion rules.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg
import soundfile as sf


TARGET_SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class Annotator:
    rank: int
    user_id: str
    username: str
    annotated_count: int
    annotated_seconds: float


@dataclass(frozen=True)
class Segment:
    rank: int
    username: str
    task_id: str
    rel_path: str
    segment_id: int
    start_s: float
    end_s: float
    text: str
    asr_text: str
    annotation_started_at: str | None = None
    annotation_completed_at: str | None = None
    annotation_elapsed_seconds: float | None = None


@dataclass(frozen=True)
class Snapshot:
    annotators: list[Annotator]
    segments: list[Segment]
    excluded_bad_quality_count: int
    excluded_unusually_fast_tasks: int
    excluded_unusually_fast_segments: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export one 16 kHz mono WAV per usable segment for the top "
            "annotators in the current PostgreSQL database"
        )
    )
    parser.add_argument("--output", type=Path, required=True, help="Output .tar path")
    parser.add_argument("--audio-dir", type=Path, required=True, help="Source audio root")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--top-n", type=int, default=8,
        help="Export the top N qualifying annotators (default: 8)",
    )
    selection.add_argument(
        "--all-qualifying", action="store_true",
        help="Export every annotator whose current annotated duration exceeds --min-hours",
    )
    parser.add_argument("--min-hours", type=float, default=2.0)
    parser.add_argument(
        "--completed-from", type=parse_datetime_arg,
        help="Exclusive lower bound for annotation completion time (ISO-8601)",
    )
    parser.add_argument(
        "--completed-to", type=parse_datetime_arg,
        help="Inclusive upper bound for annotation completion time (ISO-8601)",
    )
    parser.add_argument(
        "--exclude-unusually-fast", action="store_true",
        help=(
            "Exclude records flagged by the admin quality page as unusually fast: "
            "wall clock < max(30 seconds, 25%% of audio duration)"
        ),
    )
    parser.add_argument(
        "--dsn-env",
        default="ANNOTATION_BACKUP_DSN",
        help="Environment variable containing a read-only PostgreSQL DSN",
    )
    parser.add_argument("--source-scene", help="same-evidence source scene filter")
    parser.add_argument("--source-confidence", help="same-evidence source confidence filter")
    parser.add_argument("--batch-code", help="same-evidence source batch filter")
    parser.add_argument("--review-status", help="published human review status filter")
    parser.add_argument("--prediction-scene", help="latest model prediction scene filter")
    parser.add_argument("--human-scene", help="published human scene label filter")
    return parser.parse_args()


def parse_datetime_arg(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "datetime must be a valid ISO-8601 value with a timezone"
        ) from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def load_snapshot(
    dsn: str,
    top_n: int | None,
    min_seconds: float,
    exclude_unusually_fast: bool = False,
    completed_from: datetime | None = None,
    completed_to: datetime | None = None,
) -> Snapshot:
    if completed_from and completed_to and completed_from >= completed_to:
        raise ValueError("completed_from must be earlier than completed_to")

    ranking_sql = """
        SELECT u.id::text, u.username, count(*)::int,
               COALESCE(sum(t.duration), 0)::double precision
        FROM annotation_tasks t
        JOIN annotation_versions v ON v.id = t.current_published_version_id
        JOIN annotators u ON u.id = v.submitted_by_user_id
        WHERE t.status = 'annotated'
        GROUP BY u.id, u.username
        HAVING COALESCE(sum(t.duration), 0) > %s
        ORDER BY count(*) DESC, COALESCE(sum(t.duration), 0) DESC, u.username
    """
    ranking_params: list[object] = [min_seconds]
    if top_n is not None:
        ranking_sql += "\n        LIMIT %s"
        ranking_params.append(top_n)

    # This is intentionally the same rule used by admin_quality().  Records
    # without a claim/reopen event are not displayed as unusually fast there,
    # so they remain eligible for export.
    fast_filter_sql = ""
    fast_condition_sql = """
        completed.id IS NOT NULL
        AND started.at IS NOT NULL
        AND extract(epoch FROM (completed.created_at - started.at))
            < greatest(30.0, t.duration * 0.25)
    """
    if exclude_unusually_fast:
        fast_filter_sql = f"""
          AND NOT ({fast_condition_sql})
        """

    completion_range_sql = ""
    completion_range_params: list[datetime] = []
    if completed_from is not None:
        completion_range_sql += """
          AND COALESCE(completed.created_at, v.submitted_at) > %s
        """
        completion_range_params.append(completed_from)
    if completed_to is not None:
        completion_range_sql += """
          AND COALESCE(completed.created_at, v.submitted_at) <= %s
        """
        completion_range_params.append(completed_to)

    current_annotation_joins = """
        JOIN annotation_versions v ON v.submitted_by_user_id = c.user_id
        JOIN annotation_tasks t ON t.current_published_version_id = v.id
        LEFT JOIN LATERAL (
            SELECT e.id, e.created_at, e.user_id
            FROM annotation_events e
            WHERE e.task_id = t.id
              AND e.version_id = v.id
              AND e.event_type = 'completed'
              AND e.to_status = 'annotated'
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT 1
        ) completed ON TRUE
        LEFT JOIN LATERAL (
            SELECT max(begin_event.created_at) AS at
            FROM annotation_events begin_event
            WHERE begin_event.task_id = t.id
              AND begin_event.user_id = COALESCE(
                  completed.user_id, v.submitted_by_user_id
              )
              AND begin_event.event_type IN ('claimed', 'reopened')
              AND begin_event.created_at <= COALESCE(
                  completed.created_at, v.submitted_at
              )
        ) started ON TRUE
    """
    segments_sql = """
        SELECT c.rank, c.username, t.id::text, t.rel_path, s.segment_id,
               s.start_s, s.end_s, s.text, s.asr_text,
               started.at AS annotation_started_at,
               COALESCE(completed.created_at, v.submitted_at)
                   AS annotation_completed_at,
               CASE
                   WHEN completed.created_at IS NOT NULL
                    AND started.at IS NOT NULL
                   THEN extract(epoch FROM (completed.created_at - started.at))
                   ELSE NULL
               END AS annotation_elapsed_seconds
        FROM unnest(%s::uuid[], %s::int[], %s::text[])
             AS c(user_id, rank, username)
        {current_annotation_joins}
        JOIN segments s ON s.version_id = v.id
        WHERE t.status = 'annotated'
          AND s.exclude_from_training = false
          {completion_range_sql}
          {fast_filter_sql}
        ORDER BY c.rank, t.rel_path, s.segment_id
    """.format(
        current_annotation_joins=current_annotation_joins,
        completion_range_sql=completion_range_sql,
        fast_filter_sql=fast_filter_sql,
    )
    excluded_sql = """
        SELECT count(*)::int
        FROM unnest(%s::uuid[]) AS c(user_id)
        {current_annotation_joins}
        JOIN segments s ON s.version_id = v.id
        WHERE t.status = 'annotated'
          AND s.exclude_from_training = true
          {completion_range_sql}
          {fast_filter_sql}
    """.format(
        current_annotation_joins=current_annotation_joins,
        completion_range_sql=completion_range_sql,
        fast_filter_sql=fast_filter_sql,
    )
    fast_excluded_sql = """
        SELECT count(DISTINCT t.id)::int,
               count(s.segment_id)::int
        FROM unnest(%s::uuid[]) AS c(user_id)
        {current_annotation_joins}
        JOIN segments s ON s.version_id = v.id
        WHERE t.status = 'annotated'
          {completion_range_sql}
          AND ({fast_condition_sql})
    """.format(
        current_annotation_joins=current_annotation_joins,
        completion_range_sql=completion_range_sql,
        fast_condition_sql=fast_condition_sql,
    )

    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            rows = conn.execute(ranking_sql, ranking_params).fetchall()
            annotators = [
                Annotator(
                    rank=index,
                    user_id=row[0],
                    username=row[1],
                    annotated_count=row[2],
                    annotated_seconds=float(row[3]),
                )
                for index, row in enumerate(rows, start=1)
            ]
            if not annotators:
                raise RuntimeError(
                    f"No annotators exceed {min_seconds / 3600:g} hours"
                )
            if top_n is not None and len(annotators) != top_n:
                raise RuntimeError(
                    f"Only {len(annotators)} annotators exceed "
                    f"{min_seconds / 3600:g} hours; requested top {top_n}"
                )

            user_ids = [a.user_id for a in annotators]
            ranks = [a.rank for a in annotators]
            usernames = [a.username for a in annotators]
            segment_rows = conn.execute(
                segments_sql,
                (user_ids, ranks, usernames, *completion_range_params),
            ).fetchall()
            excluded_count = conn.execute(
                excluded_sql, (user_ids, *completion_range_params)
            ).fetchone()[0]
            fast_tasks, fast_segments = conn.execute(
                fast_excluded_sql, (user_ids, *completion_range_params)
            ).fetchone()

    segments = [
        Segment(
            rank=row[0],
            username=row[1],
            task_id=row[2],
            rel_path=row[3],
            segment_id=row[4],
            start_s=float(row[5]),
            end_s=float(row[6]),
            text=row[7] or "",
            asr_text=row[8] or "",
            annotation_started_at=row[9].isoformat() if row[9] else None,
            annotation_completed_at=row[10].isoformat() if row[10] else None,
            annotation_elapsed_seconds=(
                float(row[11]) if row[11] is not None else None
            ),
        )
        for row in segment_rows
    ]
    return Snapshot(
        annotators=annotators,
        segments=segments,
        excluded_bad_quality_count=int(excluded_count or 0),
        excluded_unusually_fast_tasks=(
            int(fast_tasks or 0) if exclude_unusually_fast else 0
        ),
        excluded_unusually_fast_segments=(
            int(fast_segments or 0) if exclude_unusually_fast else 0
        ),
    )


def safe_component(value: str, fallback: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return component[:80] or fallback


def resolve_source(audio_dir: Path, rel_path: str) -> Path:
    relative = PurePosixPath(rel_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"Unsafe database audio path: {rel_path!r}")
    source = (audio_dir / Path(*relative.parts)).resolve()
    try:
        source.relative_to(audio_dir)
    except ValueError as exc:
        raise RuntimeError(f"Audio path escapes source root: {rel_path!r}") from exc
    if not source.is_file():
        raise FileNotFoundError(f"Missing source audio: {source}")
    return source


def normalized_source(source: Path, temp_dir: Path) -> tuple[Path, bool]:
    try:
        info = sf.info(source)
    except RuntimeError:
        info = None
    if info and info.samplerate == TARGET_SAMPLE_RATE:
        return source, False

    converted = temp_dir / "normalized.wav"
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ar",
        str(TARGET_SAMPLE_RATE),
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(converted),
    ]
    subprocess.run(command, check=True)
    return converted, True


def write_segment(source_audio: Path, segment: Segment, destination: Path) -> None:
    with sf.SoundFile(source_audio) as audio:
        if audio.samplerate != TARGET_SAMPLE_RATE:
            raise RuntimeError(
                f"Normalization failed for {source_audio}: {audio.samplerate} Hz"
            )
        start_frame = max(0, round(segment.start_s * TARGET_SAMPLE_RATE))
        end_frame = min(audio.frames, round(segment.end_s * TARGET_SAMPLE_RATE))
        if end_frame <= start_frame:
            raise RuntimeError(
                f"Invalid segment bounds for {segment.rel_path} "
                f"segment {segment.segment_id}: {segment.start_s}-{segment.end_s}"
            )
        expected_end = round(segment.end_s * TARGET_SAMPLE_RATE)
        if expected_end - audio.frames > TARGET_SAMPLE_RATE // 20:
            raise RuntimeError(
                f"Segment exceeds source audio by more than 50 ms: "
                f"{segment.rel_path} segment {segment.segment_id}"
            )
        audio.seek(start_frame)
        frames = audio.read(end_frame - start_frame, dtype="float32", always_2d=True)

    mono = frames.mean(axis=1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sf.write(
        destination,
        mono,
        TARGET_SAMPLE_RATE,
        format="WAV",
        subtype="PCM_16",
    )


def create_dataset(
    staging: Path, audio_dir: Path, segments: list[Segment]
) -> tuple[list[dict[str, object]], list[dict[str, str]], int]:
    by_task: dict[tuple[int, str, str, str], list[Segment]] = defaultdict(list)
    for segment in segments:
        key = (segment.rank, segment.username, segment.task_id, segment.rel_path)
        by_task[key].append(segment)

    items: list[dict[str, object]] = []
    path_mappings: list[dict[str, str]] = []
    converted_sources = 0
    total_tasks = len(by_task)
    for task_index, ((rank, username, task_id, rel_path), task_segments) in enumerate(
        by_task.items(), start=1
    ):
        source = resolve_source(audio_dir, rel_path)
        user_dir = f"{rank:02d}_{safe_component(username, 'annotator')}"
        source_stem = safe_component(source.stem, "audio")
        task_token = task_id.replace("-", "")[:12]

        with tempfile.TemporaryDirectory(prefix="normalize-", dir=staging) as temp_name:
            usable_source, converted = normalized_source(source, Path(temp_name))
            converted_sources += int(converted)
            for segment in task_segments:
                filename = (
                    f"{source_stem}__{task_token}__seg{segment.segment_id:04d}.wav"
                )
                relative_audio = PurePosixPath("audio", user_dir, filename)
                destination = staging / Path(*relative_audio.parts)
                write_segment(usable_source, segment, destination)
                audio_path = relative_audio.as_posix()
                items.append(
                    {
                        "audio": audio_path,
                        "text": segment.text,
                        "asr_text": segment.asr_text,
                        **(
                            {
                                "annotator": segment.username,
                                "annotation_started_at": segment.annotation_started_at,
                                "annotation_completed_at": segment.annotation_completed_at,
                                "annotation_elapsed_seconds": segment.annotation_elapsed_seconds,
                            }
                            if segment.annotation_completed_at is not None
                            else {}
                        ),
                    }
                )
                path_mappings.append(
                    {
                        "audio": audio_path,
                        "source_rel_path": rel_path,
                        "source_absolute_path": str(source),
                    }
                )

        if task_index == 1 or task_index % 10 == 0 or task_index == total_tasks:
            print(
                f"Processed {task_index}/{total_tasks} source audios; "
                f"wrote {len(items)} segments",
                flush=True,
            )
    return items, path_mappings, converted_sources


def pair_items_with_segments(
    items: list[dict[str, object]], segments: list[Segment],
) -> list[dict[str, object]]:
    """Reconstruct the create_dataset grouping so sidecars cannot drift."""
    by_task: dict[tuple[int, str, str, str], list[Segment]] = defaultdict(list)
    for segment in segments:
        key = (segment.rank, segment.username, segment.task_id, segment.rel_path)
        by_task[key].append(segment)
    links: list[dict[str, object]] = []
    index = 0
    for task_segments in by_task.values():
        for segment in task_segments:
            if index >= len(items):
                raise RuntimeError("Exported items are shorter than source segments")
            links.append({
                "audio": items[index]["audio"],
                "task_id": segment.task_id,
                "segment_id": segment.segment_id,
            })
            index += 1
    if index != len(items):
        raise RuntimeError("Exported items are longer than source segments")
    return links


def write_json(target: Path, items: object) -> None:
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(items, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def verify_staging(
    staging: Path,
    items: list[dict[str, object]],
    path_mappings: list[dict[str, str]],
) -> int:
    base_expected_keys = {"audio", "text", "asr_text"}
    timing_keys = {
        "annotator",
        "annotation_started_at",
        "annotation_completed_at",
        "annotation_elapsed_seconds",
    }
    expected_mapping_keys = {
        "audio",
        "source_rel_path",
        "source_absolute_path",
    }
    if len(path_mappings) != len(items):
        raise RuntimeError(
            f"Mapping count {len(path_mappings)} does not match item count {len(items)}"
        )

    mapping_by_audio: dict[str, dict[str, str]] = {}
    for index, mapping in enumerate(path_mappings):
        if set(mapping) != expected_mapping_keys:
            raise RuntimeError(
                f"Mapping {index} has unexpected keys: {sorted(mapping)}"
            )
        audio_rel = mapping["audio"]
        if audio_rel in mapping_by_audio:
            raise RuntimeError(f"Duplicate mapped audio path: {audio_rel}")
        source_rel = PurePosixPath(mapping["source_rel_path"])
        if source_rel.is_absolute() or ".." in source_rel.parts:
            raise RuntimeError(f"Unsafe mapped source path: {source_rel}")
        source_absolute = Path(mapping["source_absolute_path"])
        if not source_absolute.is_absolute() or not source_absolute.is_file():
            raise RuntimeError(f"Invalid mapped source audio: {source_absolute}")
        mapping_by_audio[audio_rel] = mapping

    total_frames = 0
    seen: set[str] = set()
    for index, item in enumerate(items):
        if set(item) not in (base_expected_keys, base_expected_keys | timing_keys):
            raise RuntimeError(f"Item {index} has unexpected keys: {sorted(item)}")
        audio_rel = item["audio"]
        if audio_rel in seen:
            raise RuntimeError(f"Duplicate output audio path: {audio_rel}")
        if audio_rel not in mapping_by_audio:
            raise RuntimeError(f"Missing source path mapping for: {audio_rel}")
        seen.add(audio_rel)
        audio_path = staging / Path(*PurePosixPath(audio_rel).parts)
        info = sf.info(audio_path)
        if info.samplerate != TARGET_SAMPLE_RATE or info.channels != 1:
            raise RuntimeError(
                f"Invalid output format for {audio_rel}: "
                f"{info.samplerate} Hz, {info.channels} channels"
            )
        if info.subtype != "PCM_16":
            raise RuntimeError(f"Invalid output subtype for {audio_rel}: {info.subtype}")
        total_frames += info.frames
    if seen != set(mapping_by_audio):
        raise RuntimeError("Source path mapping contains audio paths absent from data.json")
    return total_frames


def create_tar(staging: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {output}")
    temporary = tempfile.NamedTemporaryFile(
        prefix=f".{output.name}.", suffix=".partial", dir=output.parent, delete=False
    )
    temp_tar = Path(temporary.name)
    temporary.close()
    try:
        with tarfile.open(temp_tar, mode="w", format=tarfile.PAX_FORMAT) as archive:
            archive.add(staging / "data.json", arcname="data.json")
            archive.add(
                staging / "source_path_mapping.json",
                arcname="source_path_mapping.json",
            )
            metadata = staging / "export_metadata.json"
            if metadata.exists():
                archive.add(metadata, arcname="export_metadata.json")
            scene_metadata = staging / "scene_metadata.json"
            if scene_metadata.exists():
                archive.add(scene_metadata, arcname="scene_metadata.json")
            archive.add(staging / "audio", arcname="audio")
        temp_tar.replace(output)
    except BaseException:
        temp_tar.unlink(missing_ok=True)
        raise


def main() -> int:
    args = parse_args()
    if not args.all_qualifying and args.top_n < 1:
        raise SystemExit("--top-n must be at least 1")
    if args.min_hours < 0:
        raise SystemExit("--min-hours cannot be negative")
    if args.output.suffix.lower() != ".tar":
        raise SystemExit("--output must end in .tar")
    if (
        args.completed_from is not None
        and args.completed_to is not None
        and args.completed_from >= args.completed_to
    ):
        raise SystemExit("--completed-from must be earlier than --completed-to")

    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        raise SystemExit(f"Environment variable {args.dsn_env} is not set")
    audio_dir = args.audio_dir.expanduser().resolve()
    output = args.output.expanduser().resolve()

    from annotation_metadata.export_metadata import (
        AUDIO_LEVEL_NOTICE, applied_filter_context, build_training_scene_sidecar,
        parse_task_filter,
    )
    from annotation_metadata.queries import metadata_filter_sql

    task_filter = parse_task_filter({
        "source_scene": args.source_scene,
        "source_confidence": args.source_confidence,
        "batch_code": args.batch_code,
        "review_status": args.review_status,
        "prediction_scene": args.prediction_scene,
        "human_scene": args.human_scene,
    })
    snapshot = load_snapshot(
        dsn,
        None if args.all_qualifying else args.top_n,
        args.min_hours * 3600,
        exclude_unusually_fast=args.exclude_unusually_fast,
        completed_from=args.completed_from,
        completed_to=args.completed_to,
    )
    annotators = snapshot.annotators
    segments = snapshot.segments
    if task_filter is not None:
        with psycopg.connect(dsn) as conn:
            sql, params = metadata_filter_sql(task_filter)
            matching = {
                str(row[0])
                for row in conn.execute(
                    f"SELECT id FROM annotation_tasks t WHERE {sql}", params,
                ).fetchall()
            }
        segments = [item for item in segments if item.task_id in matching]
        if not segments:
            raise RuntimeError("Task filter excluded every selected segment")
    print("Selected annotators:", flush=True)
    for annotator in annotators:
        print(
            f"  {annotator.rank}. {annotator.username}: "
            f"{annotator.annotated_count} audios, "
            f"{annotator.annotated_seconds / 3600:.3f} hours",
            flush=True,
        )
    print(
        f"Exporting {len(segments)} usable segments; "
        f"excluding {snapshot.excluded_bad_quality_count} Bad-quality segments",
        flush=True,
    )
    if args.exclude_unusually_fast:
        print(
            "Excluded unusually-fast records: "
            f"{snapshot.excluded_unusually_fast_tasks} source audios / "
            f"{snapshot.excluded_unusually_fast_segments} segments",
            flush=True,
        )
    if args.completed_from is not None or args.completed_to is not None:
        print(
            "Completion range: "
            f"({args.completed_from.isoformat() if args.completed_from else 'beginning'}, "
            f"{args.completed_to.isoformat() if args.completed_to else 'now'}]",
            flush=True,
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="top-annotators-export-", dir=output.parent
    ) as temp_name:
        staging = Path(temp_name)
        items, path_mappings, converted_sources = create_dataset(
            staging, audio_dir, segments
        )
        write_json(staging / "data.json", items)
        write_json(staging / "source_path_mapping.json", path_mappings)
        total_frames = verify_staging(staging, items, path_mappings)
        links = pair_items_with_segments(items, segments)
        with psycopg.connect(dsn) as conn:
            scene_sidecar = build_training_scene_sidecar(
                conn, links=links,
                applied_filters=applied_filter_context(task_filter),
            )
        write_json(staging / "scene_metadata.json", scene_sidecar)
        write_json(
            staging / "export_metadata.json",
            {
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "scene_metadata": "scene_metadata.json",
                "scene_level": "audio",
                "scene_notice": AUDIO_LEVEL_NOTICE,
                "applied_task_filters": applied_filter_context(task_filter),
                "selection": (
                    "all_qualifying"
                    if args.all_qualifying
                    else {"top_n": args.top_n}
                ),
                "min_annotator_hours": args.min_hours,
                "completed_at_range": {
                    "exclusive_from": (
                        args.completed_from.isoformat()
                        if args.completed_from is not None
                        else None
                    ),
                    "inclusive_to": (
                        args.completed_to.isoformat()
                        if args.completed_to is not None
                        else None
                    ),
                },
                "exclude_unusually_fast": args.exclude_unusually_fast,
                "unusually_fast_rule": (
                    "wall clock < max(30 seconds, 25% of audio duration)"
                ),
                "selected_annotators": [
                    {
                        "rank": annotator.rank,
                        "username": annotator.username,
                        "annotated_count": annotator.annotated_count,
                        "annotated_seconds": annotator.annotated_seconds,
                    }
                    for annotator in annotators
                ],
                "excluded_bad_quality_segments": snapshot.excluded_bad_quality_count,
                "excluded_unusually_fast_tasks": snapshot.excluded_unusually_fast_tasks,
                "excluded_unusually_fast_segments": snapshot.excluded_unusually_fast_segments,
                "exported_segments": len(items),
                "exported_audio_seconds": total_frames / TARGET_SAMPLE_RATE,
                "audio_format": "16 kHz mono PCM-16 WAV",
                "data_json_fields": ["audio", "text", "asr_text"],
            },
        )
        print(
            f"Verified {len(items)} WAV files: 16 kHz, mono, PCM-16 "
            f"({total_frames / TARGET_SAMPLE_RATE / 3600:.3f} hours); "
            f"verified {len(path_mappings)} source path mappings",
            flush=True,
        )
        print(f"Source files normalized with ffmpeg: {converted_sources}", flush=True)
        print("Creating tar archive...", flush=True)
        create_tar(staging, output)

    print(f"Created {output} ({output.stat().st_size} bytes)", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
