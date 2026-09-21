#!/usr/bin/env python3
"""PostgreSQL schema, JSON migration, verification and recovery CLI."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import shutil
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from db import apply_migrations, assert_schema_current, db_conn
from annotation_metadata.export_metadata import MetadataImportError
from annotation_repository import ForbiddenError, ValidationError

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "config.json"
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".webm", ".opus", ".wma", ".aac"}
CONTROL_FILES = {"active_users.json", "assignments.json"}
VALID_STATUSES = {"pending", "annotated", "skipped"}
KNOWN_TOP = {
    "audio", "folder", "duration", "status", "skip_reasons", "category",
    "annotated_by", "skipped_by", "last_modified", "last_modified_by",
    "preprocessed_at", "waveform_b64", "waveform", "segments",
    "quality_state", "training_eligible", "quality_round_id",
}
QUALITY_JSON_KEYS = ("quality_state", "training_eligible", "quality_round_id")
KNOWN_SEG = {"id", "start", "end", "duration", "asr_text", "text", "exclude_from_training"}
TASK_NAMESPACE = uuid.UUID("b2234ce1-fc42-41df-af48-11710143274b")


class MigrationError(RuntimeError):
    pass


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def resolve_paths(args) -> tuple[Path, Path]:
    config = load_config()
    annotations = Path(args.annotations or config.get("annotations_dir", "./annotations")).expanduser().resolve()
    audio = Path(args.audio or config.get("audio_dir", "./audio")).expanduser().resolve()
    return annotations, audio


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def strict_json_loads(raw: bytes | str) -> Any:
    return json.loads(raw, parse_constant=_reject_json_constant)


def validate_json_value(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path}: non-finite number")
    if isinstance(value, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError(f"{path}: unpaired Unicode surrogate")
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_json_value(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path}: object key is not a string")
            validate_json_value(item, f"{path}.{key}")


def canonical_hash(value: Any) -> str:
    validate_json_value(value)
    return sha256_bytes(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8"))


def legacy_waveform_payload(data: dict) -> bytes:
    if data.get("waveform_b64"):
        return base64.b64decode(str(data["waveform_b64"]), validate=True)
    if "waveform" not in data or data["waveform"] in (None, []):
        return b""
    waveform = data["waveform"]
    if not isinstance(waveform, list):
        raise ValueError("waveform must be an array")
    import struct
    samples = []
    for index, value in enumerate(waveform):
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"waveform[{index}] must be a finite number")
        number = float(value)
        sample = round(number * 32767) if abs(number) <= 1.0 else round(number)
        samples.append(max(-32767, min(32767, sample)))
    return struct.pack("<" + "h" * len(samples), *samples)


def legacy_key(rel_path: str) -> str:
    return rel_path.rsplit(".", 1)[0].replace("|", "-")


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt
    except (ValueError, TypeError, OSError):
        return None


def iso_legacy(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


def infer_rel_path(data: dict, source_rel: str, audio_paths: set[str]) -> tuple[str | None, str | None]:
    audio_name = str(data.get("audio") or "").strip()
    folder = str(data.get("folder") or "").strip().strip("/")
    candidates: list[str] = []
    if audio_name:
        if folder:
            candidates.append(f"{folder}/{audio_name}")
        source_parent = Path(source_rel).parent.as_posix()
        if source_parent != ".":
            candidates.append(f"{source_parent}/{audio_name}")
        candidates.append(audio_name)
    stem_key = Path(source_rel).with_suffix("").as_posix()
    by_key = sorted(path for path in audio_paths if legacy_key(path) == stem_key)
    candidates.extend(by_key)
    unique = []
    for candidate in candidates:
        normalized = Path(candidate).as_posix().lstrip("./")
        if normalized not in unique:
            unique.append(normalized)
    existing = [candidate for candidate in unique if candidate in audio_paths]
    if len(existing) == 1:
        return existing[0], None
    if len(existing) > 1:
        return None, f"multiple audio matches: {existing}"
    if audio_name:
        fallback = f"{folder}/{audio_name}" if folder else audio_name
        return Path(fallback).as_posix(), "audio file missing"
    return None, "cannot infer audio relative path"


def normalize_legacy(data: dict, rel_path: str) -> dict:
    result = dict(data)
    result["audio"] = str(data.get("audio") or Path(rel_path).name)
    folder = str(data.get("folder") or Path(rel_path).parent.as_posix())
    result["folder"] = "" if folder == "." else folder
    result["duration"] = float(data.get("duration") or 0)
    result["status"] = str(data.get("status") or "pending")
    reasons = data.get("skip_reasons") or []
    result["skip_reasons"] = list(reasons) if isinstance(reasons, list) else [str(reasons)]
    normalized_segments = []
    for raw in data.get("segments") or []:
        seg = dict(raw)
        start = float(raw.get("start") or 0)
        end = float(raw.get("end") or 0)
        seg["id"] = int(raw.get("id"))
        seg["start"] = start
        seg["end"] = end
        seg["duration"] = float(raw.get("duration", end - start))
        seg["asr_text"] = str(raw.get("asr_text") or "")
        seg["text"] = str(raw.get("text") or "")
        seg["exclude_from_training"] = bool(raw.get("exclude_from_training", False))
        normalized_segments.append(seg)
    result["segments"] = normalized_segments
    waveform_from_array = b""
    if "waveform" in data and data.get("waveform") not in (None, []):
        waveform_only = dict(data)
        waveform_only.pop("waveform_b64", None)
        waveform_from_array = legacy_waveform_payload(waveform_only)
    if data.get("waveform_b64"):
        payload = base64.b64decode(str(data["waveform_b64"]), validate=True)
        result["waveform_b64"] = base64.b64encode(payload).decode("ascii")
        if waveform_from_array and waveform_from_array != payload:
            raise ValueError("waveform and waveform_b64 contain different samples")
    elif "waveform_b64" in result:
        result["waveform_b64"] = ""
    validate_json_value(result)
    return result


def iter_annotation_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.json")):
        if path.name in CONTROL_FILES or path.name.startswith("."):
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        yield path


def annotation_inventory(root: Path) -> list[dict]:
    inventory = []
    for path in sorted(root.rglob("*.json")):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        inventory.append({
            "path": path.relative_to(root).as_posix(),
            "size": len(raw),
            "sha256": sha256_bytes(raw),
        })
    return inventory


def audio_inventory(root: Path) -> list[dict]:
    inventory = []
    if not root.exists():
        return inventory
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS and not path.name.startswith("."):
            stat = path.stat()
            inventory.append({
                "path": path.relative_to(root).as_posix(),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            })
    return inventory


def scan_audio(audio_root: Path) -> tuple[set[str], dict[str, list[str]]]:
    inventory = audio_inventory(audio_root)
    paths = {item["path"] for item in inventory}
    keys: dict[str, list[str]] = defaultdict(list)
    for rel in sorted(paths):
        keys[legacy_key(rel)].append(rel)
    return paths, keys


def read_assignments(annotations: Path) -> dict:
    path = annotations / "assignments.json"
    if not path.exists():
        return {}
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def build_manifest(annotations: Path, audio_root: Path,
                   resolutions: list[dict] | None = None) -> dict:
    if not annotations.is_dir():
        raise MigrationError(f"annotations directory does not exist: {annotations}")
    annotations_inventory = annotation_inventory(annotations)
    audios_inventory = audio_inventory(audio_root)
    audio_paths = {item["path"] for item in audios_inventory}
    key_map: dict[str, list[str]] = defaultdict(list)
    for rel in sorted(audio_paths):
        key_map[legacy_key(rel)].append(rel)
    errors: list[dict] = []
    warnings: list[dict] = []
    items: list[dict] = []
    rel_seen: dict[str, str] = {}
    source_seen: set[str] = set()
    status_counts = Counter()
    user_counts: dict[str, Counter] = defaultdict(Counter)
    total_duration = 0.0
    segment_count = 0
    bad_quality_count = 0
    waveform_bytes = 0

    for key, paths in key_map.items():
        if len(paths) > 1:
            errors.append({"type": "legacy_key_collision", "key": key, "paths": paths})

    for order, path in enumerate(iter_annotation_files(annotations), 1):
        source_rel = path.relative_to(annotations).as_posix()
        source_seen.add(source_rel)
        try:
            raw = path.read_bytes()
            data = strict_json_loads(raw)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append({"type": "invalid_json", "source_path": source_rel, "error": str(exc)})
            continue
        if not isinstance(data, dict):
            errors.append({"type": "invalid_shape", "source_path": source_rel, "error": "top-level value is not an object"})
            continue
        if "annotations" in data and "segments" not in data:
            errors.append({"type": "aggregate_format", "source_path": source_rel, "error": "convert legacy aggregate JSON before migration"})
            continue
        if "segments" not in data:
            errors.append({"type": "not_annotation", "source_path": source_rel, "error": "missing segments"})
            continue
        rel_path, rel_warning = infer_rel_path(data, source_rel, audio_paths)
        if not rel_path:
            errors.append({"type": "audio_mapping", "source_path": source_rel, "error": rel_warning})
            continue
        if rel_warning:
            errors.append({"type": "missing_audio", "source_path": source_rel, "rel_path": rel_path})
        if rel_path in rel_seen:
            errors.append({"type": "duplicate_rel_path", "rel_path": rel_path, "sources": [rel_seen[rel_path], source_rel]})
            continue
        rel_seen[rel_path] = source_rel
        try:
            normalized = normalize_legacy(data, rel_path)
        except (ValueError, TypeError, KeyError, base64.binascii.Error) as exc:
            errors.append({"type": "invalid_annotation", "source_path": source_rel, "error": str(exc)})
            continue
        if not math.isfinite(normalized["duration"]) or normalized["duration"] < 0:
            errors.append({"type": "invalid_duration", "source_path": source_rel})
            continue
        status = normalized["status"]
        if status not in VALID_STATUSES:
            errors.append({"type": "invalid_status", "source_path": source_rel, "status": status})
            continue
        ids = [seg["id"] for seg in normalized["segments"]]
        if len(ids) != len(set(ids)):
            errors.append({"type": "duplicate_segment_id", "source_path": source_rel})
            continue
        invalid_times = [
            seg["id"] for seg in normalized["segments"]
            if not math.isfinite(seg["start"])
            or not math.isfinite(seg["end"])
            or not math.isfinite(seg["duration"])
            or seg["start"] < 0
            or seg["end"] <= seg["start"]
        ]
        if invalid_times:
            errors.append({"type": "invalid_segment_time", "source_path": source_rel, "segment_ids": invalid_times[:20]})
            continue
        owner = ""
        if status == "annotated":
            owner = str(normalized.get("annotated_by") or "")
        elif status == "skipped":
            owner = str(normalized.get("skipped_by") or "")
        last_modified_by = str(normalized.get("last_modified_by") or "")
        human_modified = bool(last_modified_by) or any(
            seg["exclude_from_training"]
            or ((seg.get("text") or "").strip()
                and (seg.get("text") or "").strip() != (seg.get("asr_text") or "").strip())
            for seg in normalized["segments"]
        )
        waveform_payload = legacy_waveform_payload(normalized)
        item = {
            "order": order,
            "source_path": source_rel,
            "source_size": len(raw),
            "source_mtime_ns": path.stat().st_mtime_ns,
            "source_sha256": sha256_bytes(raw),
            "semantic_sha256": canonical_hash(normalized),
            "rel_path": rel_path,
            "legacy_key": legacy_key(rel_path),
            "status": status,
            "duration": normalized["duration"],
            "segment_count": len(normalized["segments"]),
            "waveform_bytes": len(waveform_payload),
            "waveform_sha256": sha256_bytes(waveform_payload) if waveform_payload else None,
            "owner": owner,
            "last_modified_by": last_modified_by,
            "human_modified": human_modified,
        }
        items.append(item)
        status_counts[status] += 1
        total_duration += normalized["duration"]
        segment_count += len(normalized["segments"])
        bad_quality_count += sum(1 for seg in normalized["segments"] if seg["exclude_from_training"])
        waveform_bytes += len(waveform_payload)
        if owner:
            user_counts[owner][status] += 1
            if status == "annotated":
                user_counts[owner]["duration_seconds"] += normalized["duration"]

    assignments_raw = read_assignments(annotations)
    assignments: list[dict] = []
    task_by_key = {item["legacy_key"]: item for item in items}
    task_by_rel = {item["rel_path"]: item for item in items}
    assigned_tasks: dict[str, str] = {}
    for username, entry in sorted(assignments_raw.items()):
        if not isinstance(entry, dict):
            errors.append({"type": "invalid_assignment", "username": username})
            continue
        audio_key = str(entry.get("audio") or "")
        rel_path = str(entry.get("rel_path") or "")
        item = task_by_rel.get(rel_path) if rel_path else None
        item = item or task_by_key.get(audio_key)
        if not item:
            errors.append({"type": "orphan_assignment", "username": username, "audio": audio_key, "rel_path": rel_path})
            continue
        if item["status"] != "pending":
            errors.append({"type": "completed_assignment", "username": username, "source_path": item["source_path"], "status": item["status"]})
            continue
        if item["rel_path"] in assigned_tasks:
            errors.append({"type": "duplicate_task_assignment", "rel_path": item["rel_path"], "users": [assigned_tasks[item["rel_path"]], username]})
            continue
        assigned_tasks[item["rel_path"]] = username
        assignments.append({
            "username": username,
            "rel_path": item["rel_path"],
            "source_path": item["source_path"],
            "assigned_at": entry.get("assigned_at"),
            "last_activity": entry.get("last_activity"),
        })

    applied_resolutions: list[dict] = []
    for resolution in resolutions or []:
        action = resolution.get("action")
        if action != "ignore_completed_assignment":
            raise MigrationError(f"unsupported resolution action: {action!r}")
        if not str(resolution.get("reason") or "").strip():
            raise MigrationError("every resolution requires a non-empty reason")
        match_index = next((
            index for index, error in enumerate(errors)
            if error.get("type") == "completed_assignment"
            and error.get("username") == resolution.get("username")
            and error.get("source_path") == resolution.get("source_path")
            and error.get("status") == resolution.get("status")
        ), None)
        if match_index is None:
            raise MigrationError(
                "resolution did not exactly match a completed_assignment error: "
                + json.dumps(resolution, ensure_ascii=False)
            )
        resolved_error = errors.pop(match_index)
        applied_resolutions.append({**resolution, "resolved_error": resolved_error})

    summary = {
        "items": len(items),
        "audio_files": len(audio_paths),
        "statuses": dict(status_counts),
        "duration_seconds": total_duration,
        "segments": segment_count,
        "bad_quality_segments": bad_quality_count,
        "waveform_bytes": waveform_bytes,
        "assignments": len(assignments),
        "errors": len(errors),
        "warnings": len(warnings),
        "users": {name: dict(counts) for name, counts in sorted(user_counts.items())},
    }
    manifest_core = {
        "version": 1,
        "annotations_root": str(annotations),
        "audio_root": str(audio_root),
        "annotations_inventory": annotations_inventory,
        "audio_inventory": audios_inventory,
        "items": items,
        "assignments": assignments,
        "summary": summary,
        "errors": errors,
        "warnings": warnings,
        "resolutions": applied_resolutions,
    }
    manifest_core["manifest_sha256"] = canonical_hash({
        "annotations_root": str(annotations),
        "audio_root": str(audio_root),
        "annotations_inventory": annotations_inventory,
        "audio_inventory": audios_inventory,
        "items": items,
        "assignments": assignments,
        "resolutions": applied_resolutions,
    })
    return manifest_core


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def load_manifest(path: Path) -> dict:
    manifest = strict_json_loads(path.read_text(encoding="utf-8"))
    expected = canonical_hash({
        "annotations_root": manifest["annotations_root"],
        "audio_root": manifest["audio_root"],
        "annotations_inventory": manifest.get("annotations_inventory", []),
        "audio_inventory": manifest.get("audio_inventory", []),
        "items": manifest["items"],
        "assignments": manifest.get("assignments", []),
        "resolutions": manifest.get("resolutions", []),
    })
    if expected != manifest.get("manifest_sha256"):
        raise MigrationError("manifest hash mismatch")
    if manifest.get("errors"):
        raise MigrationError(f"manifest contains {len(manifest['errors'])} blocking error(s)")
    return manifest


def verify_source_inventory(manifest: dict) -> None:
    annotations = Path(manifest["annotations_root"])
    audio = Path(manifest["audio_root"])
    current_annotations = annotation_inventory(annotations)
    current_audio = audio_inventory(audio)
    if current_annotations != manifest.get("annotations_inventory", []):
        raise MigrationError("annotations directory changed after manifest creation")
    if current_audio != manifest.get("audio_inventory", []):
        raise MigrationError("audio directory changed after manifest creation")


def ensure_user(cur, username: str, cache: dict[str, uuid.UUID]) -> uuid.UUID | None:
    username = username.strip()
    if not username:
        return None
    if username in cache:
        return cache[username]
    row = cur.execute("SELECT id FROM annotators WHERE username = %s", (username,)).fetchone()
    if row:
        cache[username] = row[0]
        return row[0]
    user_id = uuid.uuid4()
    cur.execute("INSERT INTO annotators (id, username) VALUES (%s, %s)", (user_id, username))
    cache[username] = user_id
    return user_id


def source_json(annotations: Path, item: dict) -> tuple[bytes, dict, dict]:
    path = annotations / item["source_path"]
    raw = path.read_bytes()
    if sha256_bytes(raw) != item["source_sha256"]:
        raise MigrationError(f"source changed after manifest: {item['source_path']}")
    data = strict_json_loads(raw)
    normalized = normalize_legacy(data, item["rel_path"])
    if canonical_hash(normalized) != item["semantic_sha256"]:
        raise MigrationError(f"semantic hash changed after manifest: {item['source_path']}")
    return raw, data, normalized


def insert_task(cur, item: dict, data: dict, normalized: dict, user_cache: dict[str, uuid.UUID]) -> uuid.UUID:
    existing = cur.execute("SELECT id, source_json_sha256 FROM annotation_tasks WHERE rel_path = %s FOR UPDATE", (item["rel_path"],)).fetchone()
    if existing:
        if existing[1] == item["source_sha256"]:
            return existing[0]
        raise MigrationError(f"database already contains a different task at {item['rel_path']}")
    task_id = uuid.uuid5(TASK_NAMESPACE, item["rel_path"])
    version_id = uuid.uuid5(task_id, "legacy-version-1")
    baseline_id = uuid.uuid5(task_id, "admin-baseline-v1")
    status = normalized["status"]
    owner_name = str(normalized.get("annotated_by") or "") if status == "annotated" else str(normalized.get("skipped_by") or "") if status == "skipped" else ""
    owner_id = ensure_user(cur, owner_name, user_cache)
    last_editor_name = str(normalized.get("last_modified_by") or "")
    last_editor_id = ensure_user(cur, last_editor_name, user_cache)
    # Preserve every legacy top-level field except payload collections.
    # Some pending files legitimately retain historical annotated_by/skipped_by;
    # they are not the current owner but must survive round-trip exactly.
    legacy_top = {key: normalized[key] for key in normalized if key not in {"segments", "waveform_b64"}}
    unknown_top = {key: value for key, value in normalized.items() if key not in KNOWN_TOP}
    version_extra = {
        "legacy_top": legacy_top,
        "legacy_had_waveform_b64": "waveform_b64" in normalized,
        "legacy_waveform_b64_empty": (
            "waveform_b64" in normalized and not normalized.get("waveform_b64")
        ),
    }
    submitted_at = parse_datetime(normalized.get("last_modified")) if status in {"annotated", "skipped"} else None
    preprocessed_at = parse_datetime(normalized.get("preprocessed_at"))
    lifecycle = "published" if status in {"annotated", "skipped"} else "draft"
    cur.execute(
        """INSERT INTO annotation_tasks
               (id, rel_path, legacy_audio_key, filename, folder, duration,
                status, eligible, allocation_order, category, preprocessed_at,
                source_json_sha256, extra)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (task_id, item["rel_path"], item["legacy_key"], normalized["audio"], normalized["folder"], normalized["duration"], status, bool(normalized["segments"]), item["order"], normalized.get("category"), preprocessed_at, item["source_sha256"], Json(unknown_top)),
    )
    cur.execute(
        """INSERT INTO annotation_versions
               (id, task_id, version_no, lifecycle, target_status, revision,
                human_modified, created_by_user_id, modified_by_user_id,
                submitted_by_user_id, skip_reasons,
                created_at, updated_at, submitted_at, extra)
           VALUES (%s, %s, 1, %s, %s, 0, %s, %s, %s, %s, %s,
                   COALESCE(%s, now()), COALESCE(%s, now()), %s, %s)""",
        (version_id, task_id, lifecycle, status, item["human_modified"],
         last_editor_id, last_editor_id, owner_id, normalized["skip_reasons"],
         preprocessed_at, parse_datetime(normalized.get("last_modified")),
         submitted_at, Json(version_extra)),
    )
    for seg in normalized["segments"]:
        extra = {key: value for key, value in seg.items() if key not in KNOWN_SEG}
        cur.execute(
            """INSERT INTO segments
                   (version_id, segment_id, start_s, end_s, duration,
                    asr_text, text, exclude_from_training, extra)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (version_id, seg["id"], seg["start"], seg["end"], seg["duration"], seg["asr_text"], seg["text"], seg["exclude_from_training"], Json(extra)),
        )
    baseline_quality = (
        "exact" if status == "pending" and not item["human_modified"]
        else "reconstructed"
    )
    cur.execute(
        """INSERT INTO annotation_versions
               (id, task_id, version_no, lifecycle, target_status, revision,
                human_modified, extra)
           VALUES (%s, %s, 2, 'baseline', 'pending', 0, false, %s)""",
        (baseline_id, task_id, Json({
            "baseline_source_version_id": str(version_id),
            "baseline_quality": baseline_quality,
            "created_by_legacy_import": True,
        })),
    )
    for seg in normalized["segments"]:
        extra = {key: value for key, value in seg.items() if key not in KNOWN_SEG}
        cur.execute(
            """INSERT INTO segments
                   (version_id, segment_id, start_s, end_s, duration,
                    asr_text, text, exclude_from_training, extra)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                baseline_id, seg["id"], seg["start"], seg["end"],
                seg["duration"], seg["asr_text"],
                seg["text"] if baseline_quality == "exact" else "",
                seg["exclude_from_training"] if baseline_quality == "exact" else False,
                Json(extra),
            ),
        )
    cur.execute(
        "UPDATE annotation_versions SET base_version_id = %s WHERE id = %s",
        (baseline_id, version_id),
    )
    cur.execute(
        """UPDATE annotation_tasks
           SET baseline_version_id = %s, baseline_quality = %s
           WHERE id = %s""",
        (baseline_id, baseline_quality, task_id),
    )
    payload = legacy_waveform_payload(normalized)
    if payload:
        cur.execute(
            "INSERT INTO waveforms (task_id, encoding, point_count, payload, checksum) VALUES (%s, 'int16le', %s, %s, %s)",
            (task_id, len(payload) // 2, payload, sha256_bytes(payload)),
        )
    if lifecycle == "published":
        cur.execute("UPDATE annotation_tasks SET current_published_version_id = %s WHERE id = %s", (version_id, task_id))
        cur.execute(
            """INSERT INTO annotation_events
                   (user_id, task_id, version_id, event_type, from_status, to_status, created_at, details)
               VALUES (%s, %s, %s, 'completed', 'pending', %s, COALESCE(%s, now()), %s)""",
            (owner_id, task_id, version_id, status, submitted_at, Json({"migrated": True})),
        )
    return task_id


def migrate_manifest(manifest: dict) -> uuid.UUID:
    """Import a manifest atomically; any error rolls back every imported row."""
    verify_source_inventory(manifest)
    annotations = Path(manifest["annotations_root"])
    batch_id = uuid.uuid4()
    user_cache: dict[str, uuid.UUID] = {}
    imported = skipped = 0

    with db_conn() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (manifest["manifest_sha256"],),
        )
        existing_batch = cur.execute(
            "SELECT id, status FROM import_batches WHERE manifest_sha256 = %s",
            (manifest["manifest_sha256"],),
        ).fetchone()
        if existing_batch:
            if existing_batch[1] == "completed":
                return existing_batch[0]
            raise MigrationError(
                f"manifest already has non-completed batch {existing_batch[0]}"
            )

        cur.execute(
            """INSERT INTO import_batches
                   (id, source_root, manifest_sha256, status)
               VALUES (%s, %s, %s, 'running')""",
            (batch_id, str(annotations), manifest["manifest_sha256"]),
        )

        for item in manifest["items"]:
            _raw, data, normalized = source_json(annotations, item)
            existing = cur.execute(
                "SELECT id, source_json_sha256 FROM annotation_tasks WHERE rel_path = %s",
                (item["rel_path"],),
            ).fetchone()
            if existing and existing[1] == item["source_sha256"]:
                task_id = existing[0]
                result = "skipped"
                skipped += 1
            else:
                task_id = insert_task(cur, item, data, normalized, user_cache)
                result = "imported"
                imported += 1
            cur.execute(
                """INSERT INTO import_items
                       (batch_id, source_path, source_sha256,
                        semantic_sha256, task_id, result)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (batch_id, item["source_path"], item["source_sha256"],
                 item["semantic_sha256"], task_id, result),
            )

        assignment_by_rel = {
            entry["rel_path"]: entry for entry in manifest.get("assignments", [])
        }
        item_by_rel = {item["rel_path"]: item for item in manifest["items"]}
        for rel_path, entry in assignment_by_rel.items():
            task = cur.execute(
                """SELECT id FROM annotation_tasks
                   WHERE rel_path = %s AND status = 'pending' FOR UPDATE""",
                (rel_path,),
            ).fetchone()
            if not task:
                raise MigrationError(
                    f"assignment target missing or not pending: {rel_path}"
                )
            version = cur.execute(
                """SELECT id, revision FROM annotation_versions
                   WHERE task_id = %s AND lifecycle = 'draft' FOR UPDATE""",
                (task[0],),
            ).fetchone()
            user_id = ensure_user(cur, entry["username"], user_cache)
            conflict = cur.execute(
                """SELECT user_id, task_id, working_version_id
                   FROM assignments
                   WHERE user_id = %s OR task_id = %s FOR UPDATE""",
                (user_id, task[0]),
            ).fetchone()
            if conflict:
                if conflict != (user_id, task[0], version[0]):
                    raise MigrationError(
                        f"assignment conflicts with existing mapping: {rel_path}"
                    )
                continue
            cur.execute(
                """INSERT INTO assignments
                       (user_id, task_id, working_version_id, mode,
                        lease_token, base_revision, assigned_at,
                        last_activity_at, legacy_meta)
                   VALUES (%s, %s, %s, 'annotation', %s, %s,
                           COALESCE(%s, now()), COALESCE(%s, now()), %s)""",
                (user_id, task[0], version[0], uuid.uuid4(), version[1],
                 parse_datetime(entry.get("assigned_at")),
                 parse_datetime(entry.get("last_activity")),
                 Json({
                     "assigned_at": entry.get("assigned_at"),
                     "last_activity": entry.get("last_activity"),
                 })),
            )

        for rel_path, item in item_by_rel.items():
            if rel_path in assignment_by_rel or not item.get("last_modified_by"):
                continue
            user_id = ensure_user(cur, item["last_modified_by"], user_cache)
            cur.execute(
                """UPDATE annotation_tasks
                   SET reserved_for_user_id = %s
                   WHERE rel_path = %s AND status = 'pending'""",
                (user_id, rel_path),
            )

        cur.execute(
            """SELECT setval(
                 'task_allocation_order_seq',
                 GREATEST((SELECT COALESCE(max(allocation_order), 0)
                           FROM annotation_tasks) + 1, 1),
                 false
               )"""
        )
        counts = {
            "imported": imported,
            "skipped": skipped,
            "assignments": len(assignment_by_rel),
        }
        cur.execute(
            """UPDATE import_batches
               SET status = 'completed', completed_at = now(), counts = %s
               WHERE id = %s""",
            (Json(counts), batch_id),
        )
    return batch_id


def db_export_rows(conn) -> Iterable[dict]:
    from annotation_quality.queries import (
        credited_annotator_sql, latest_quality_round_lateral_sql,
        training_export_eligible_sql,
    )
    eligible_sql, eligible_params = training_export_eligible_sql()
    credited = credited_annotator_sql()
    quality_join = latest_quality_round_lateral_sql()
    query = f"""
        SELECT t.id, t.rel_path, t.legacy_audio_key, t.filename, t.folder, t.duration, t.status,
               t.category, t.preprocessed_at, t.extra AS task_extra,
               v.id AS version_id, v.version_no, v.revision, v.target_status,
               v.skip_reasons, v.submitted_at,
               v.updated_at, v.extra AS version_extra,
               u.username AS submitter, editor.username AS modifier,
               w.payload AS waveform_payload,
               COALESCE(
                 jsonb_agg(
                   jsonb_build_object(
                     'id', s.segment_id, 'start', s.start_s, 'end', s.end_s,
                     'duration', s.duration, 'asr_text', s.asr_text,
                     'text', s.text,
                     'exclude_from_training', s.exclude_from_training,
                     'extra', s.extra
                   ) ORDER BY s.segment_id
                 ) FILTER (WHERE s.segment_id IS NOT NULL), '[]'::jsonb
               ) AS segments,
               q.id AS quality_round_id, q.state AS quality_state,
               ({eligible_sql}) AS training_eligible
        FROM annotation_tasks t
        JOIN annotation_versions v ON v.task_id = t.id AND (
             v.id = t.current_published_version_id OR
             (t.current_published_version_id IS NULL AND v.lifecycle = 'draft')
        )
        LEFT JOIN annotators u ON u.id = {credited}
        LEFT JOIN annotators editor ON editor.id = v.modified_by_user_id
        LEFT JOIN waveforms w ON w.task_id = t.id
        LEFT JOIN segments s ON s.version_id = v.id
        {quality_join}
        GROUP BY t.id, v.id, u.username, editor.username, w.payload,
                 q.id, q.state
        ORDER BY t.allocation_order
    """
    with conn.cursor(name="state_export", row_factory=dict_row) as cur:
        cur.itersize = 500
        cur.execute(query, eligible_params)
        yield from cur


def row_to_legacy(row: dict) -> dict:
    version_extra = row.get("version_extra") or {}
    legacy_top = dict(version_extra.get("legacy_top") or {})
    result = {}
    result.update(row.get("task_extra") or {})
    result.update(legacy_top)
    result["audio"] = row["filename"]
    result["folder"] = row["folder"]
    result["duration"] = float(row["duration"] or 0)
    result["status"] = row["status"]
    result["skip_reasons"] = list(row["skip_reasons"] or [])
    if row.get("category") is not None:
        result["category"] = row["category"]
    elif "category" in result and result["category"] is None:
        pass
    submitter = row.get("submitter") or ""
    modifier = row.get("modifier") or submitter
    changed_after_migration = row.get("version_no", 1) > 1 or row.get("revision", 0) > 0
    if row["status"] == "annotated":
        result["annotated_by"] = submitter
        if changed_after_migration:
            result.pop("skipped_by", None)
    elif row["status"] == "skipped":
        result["skipped_by"] = submitter
        if changed_after_migration:
            result.pop("annotated_by", None)
    if changed_after_migration or not legacy_top:
        stamp = iso_legacy(row.get("submitted_at") or row.get("updated_at"))
        if stamp:
            result["last_modified"] = stamp
        if modifier:
            result["last_modified_by"] = modifier
        prep = iso_legacy(row.get("preprocessed_at"))
        if prep:
            result["preprocessed_at"] = prep
    payload = bytes(row["waveform_payload"]) if row.get("waveform_payload") is not None else b""
    if version_extra.get("legacy_waveform_b64_empty"):
        result["waveform_b64"] = ""
    elif payload and (version_extra.get("legacy_had_waveform_b64") or "waveform" not in legacy_top):
        result["waveform_b64"] = base64.b64encode(payload).decode("ascii")
    elif "waveform_b64" in result:
        result["waveform_b64"] = ""
    segments = []
    for raw in row["segments"] or []:
        seg = dict(raw.get("extra") or {})
        seg.update({
            "id": int(raw["id"]),
            "start": float(raw["start"]),
            "end": float(raw["end"]),
            "duration": float(raw["duration"]),
            "asr_text": raw.get("asr_text") or "",
            "text": raw.get("text") or "",
            "exclude_from_training": bool(raw.get("exclude_from_training")),
        })
        segments.append(seg)
    result["segments"] = segments
    result["quality_state"] = row.get("quality_state") or "none"
    result["training_eligible"] = bool(row.get("training_eligible"))
    round_id = row.get("quality_round_id")
    result["quality_round_id"] = str(round_id) if round_id else None
    return normalize_legacy(result, row["rel_path"])


def verify_manifest(manifest: dict) -> dict:
    expected = {item["rel_path"]: item for item in manifest["items"]}
    mismatches: list[dict] = []
    seen: set[str] = set()
    status_counts = Counter()
    with db_conn() as conn:
        for row in db_export_rows(conn):
            rel_path = row["rel_path"]
            seen.add(rel_path)
            item = expected.get(rel_path)
            if not item:
                mismatches.append({"type": "unexpected_database_task", "rel_path": rel_path})
                continue
            exported = row_to_legacy(row)
            for key in QUALITY_JSON_KEYS:
                exported.pop(key, None)
            actual_hash = canonical_hash(exported)
            if actual_hash != item["semantic_sha256"]:
                mismatches.append({"type": "semantic_mismatch", "rel_path": rel_path, "source_path": item["source_path"], "expected": item["semantic_sha256"], "actual": actual_hash})
            status_counts[row["status"]] += 1
        missing = sorted(set(expected) - seen)
        mismatches.extend({"type": "missing_database_task", "rel_path": path} for path in missing)
        assignment_rows = conn.execute(
            """SELECT u.username, t.rel_path, a.assigned_at,
                      a.last_activity_at, a.legacy_meta
               FROM assignments a
               JOIN annotators u ON u.id = a.user_id
               JOIN annotation_tasks t ON t.id = a.task_id
               ORDER BY u.username"""
        ).fetchall()
    expected_assignments = sorted((
        a["username"], a["rel_path"], a.get("assigned_at"), a.get("last_activity")
    ) for a in manifest.get("assignments", []))
    actual_assignments = []
    for username, rel_path, assigned_at, activity, legacy_meta in assignment_rows:
        meta = legacy_meta or {}
        actual_assignments.append((
            username,
            rel_path,
            meta.get("assigned_at", iso_legacy(assigned_at)),
            meta.get("last_activity", activity.timestamp() if activity else None),
        ))
    actual_assignments.sort()
    if expected_assignments != actual_assignments:
        mismatches.append({"type": "assignment_mismatch", "expected": expected_assignments, "actual": actual_assignments})
    return {"ok": not mismatches, "checked": len(seen), "status_counts": dict(status_counts), "mismatches": mismatches}


def export_json(output: Path) -> dict:
    if output.exists():
        raise MigrationError(f"output already exists: {output}")
    temp = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    temp.mkdir(parents=True)
    count = 0
    semantic_items = []
    try:
        with db_conn() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            for row in db_export_rows(conn):
                data = row_to_legacy(row)
                rel_json = Path(
                    (row.get("legacy_audio_key") or Path(row["rel_path"]).with_suffix("").as_posix())
                    + ".json"
                )
                target = temp / rel_json
                write_json_atomic(target, data)
                semantic_items.append({"rel_path": row["rel_path"], "semantic_sha256": canonical_hash(data)})
                count += 1
            assignments = {}
            rows = conn.execute(
                """SELECT u.username, t.legacy_audio_key, t.rel_path,
                          a.assigned_at, a.last_activity_at, a.legacy_meta
                   FROM assignments a
                   JOIN annotators u ON u.id = a.user_id
                   JOIN annotation_tasks t ON t.id = a.task_id
                   ORDER BY u.username"""
            ).fetchall()
            for username, key, rel_path, assigned_at, activity, legacy_meta in rows:
                meta = legacy_meta or {}
                assignments[username] = {
                    "audio": key or legacy_key(rel_path),
                    "rel_path": rel_path,
                    "assigned_at": meta.get("assigned_at", iso_legacy(assigned_at)),
                    "last_activity": meta.get(
                        "last_activity", activity.timestamp() if activity else None
                    ),
                }
            write_json_atomic(temp / "assignments.json", assignments)
            write_json_atomic(temp / "export_manifest.json", {"count": count, "semantic_items": semantic_items, "semantic_manifest_sha256": canonical_hash(semantic_items)})
        os.replace(temp, output)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return {"output": str(output), "count": count, "semantic_manifest_sha256": canonical_hash(semantic_items)}


def command_inspect(args) -> None:
    annotations, audio = resolve_paths(args)
    resolutions = []
    if args.resolutions:
        value = json.loads(Path(args.resolutions).expanduser().resolve().read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise MigrationError("resolutions file must contain a JSON array")
        resolutions = value
    manifest = build_manifest(annotations, audio, resolutions)
    output = Path(args.output).expanduser().resolve()
    write_json_atomic(output, manifest)
    print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))
    print(f"manifest: {output}")
    print(f"manifest_sha256: {manifest['manifest_sha256']}")
    if manifest["errors"]:
        print(f"blocked: {len(manifest['errors'])} error(s)", file=sys.stderr)
        raise SystemExit(2)


def command_apply_migrations(_args) -> None:
    with db_conn() as conn:
        versions = apply_migrations(conn)
    print(f"applied migrations: {versions or 'none'}")


def command_schema(_args) -> None:
    with db_conn() as conn:
        print(json.dumps({"versions": assert_schema_current(conn)}, indent=2))


def command_migrate(args) -> None:
    manifest = load_manifest(Path(args.manifest).expanduser().resolve())
    batch = migrate_manifest(manifest)
    print(f"import batch completed: {batch}")


def command_verify(args) -> None:
    manifest = load_manifest(Path(args.manifest).expanduser().resolve())
    result = verify_manifest(manifest)
    if args.output:
        write_json_atomic(Path(args.output).expanduser().resolve(), result)
    print(json.dumps({"ok": result["ok"], "checked": result["checked"], "status_counts": result["status_counts"], "mismatches": len(result["mismatches"])}, ensure_ascii=False, indent=2))
    if not result["ok"]:
        if not args.output:
            print(json.dumps(result["mismatches"][:20], ensure_ascii=False, indent=2))
        raise SystemExit(3)


def command_export(args) -> None:
    result = export_json(Path(args.output).expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))


def command_inspect_source_manifest(args) -> None:
    from annotation_metadata.ingestion import inspect_source_manifest
    source_root = Path(args.audio_root).expanduser().resolve() if args.audio_root else None
    result = inspect_source_manifest(
        Path(args.manifest).expanduser().resolve(),
        batch_code=args.batch_code,
        source_root=source_root,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def command_import_source_metadata(args) -> None:
    from annotation_metadata.ingestion import import_source_metadata
    with db_conn() as conn:
        result = import_source_metadata(
            conn,
            manifest_path=Path(args.manifest).expanduser().resolve(),
            batch_code=args.batch_code,
            source_root=Path(args.audio_root).expanduser().resolve() if args.audio_root else None,
            audio_root=Path(args.website_audio_root).expanduser().resolve() if args.website_audio_root else None,
            dry_run=args.dry_run,
        )
    print(json.dumps({k: result[k] for k in result if k != "results"},
                     ensure_ascii=False, indent=2, default=str))


def command_verify_source_metadata(args) -> None:
    from annotation_metadata.ingestion import verify_source_metadata
    with db_conn() as conn:
        result = verify_source_metadata(conn, batch_code=args.batch_code)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _task_filter_from_args(args):
    from annotation_metadata.export_metadata import parse_task_filter
    return parse_task_filter({
        "source_scene": getattr(args, "source_scene", None),
        "source_confidence": getattr(args, "source_confidence", None),
        "batch_code": getattr(args, "batch_code", None),
        "review_status": getattr(args, "review_status", None),
        "prediction_scene": getattr(args, "prediction_scene", None),
        "human_scene": getattr(args, "human_scene", None),
    })


def _add_task_filter_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-scene", help="same-evidence source scene filter")
    parser.add_argument("--source-confidence", help="same-evidence source confidence filter")
    parser.add_argument("--batch-code", help="same-evidence source batch filter")
    parser.add_argument("--review-status", help="published human review status filter")
    parser.add_argument("--prediction-scene", help="latest model prediction scene filter")
    parser.add_argument("--human-scene", help="published human scene label filter")


def command_export_metadata(args) -> None:
    from annotation_metadata.export_metadata import export_metadata
    filters = _task_filter_from_args(args)
    # Idle connection: export_metadata starts REPEATABLE READ itself.
    with db_conn() as conn:
        result = export_metadata(
            conn, Path(args.output).expanduser().resolve(), filters=filters,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def command_import_metadata(args) -> None:
    from annotation_metadata.export_metadata import import_metadata
    mapping = Path(args.mapping).expanduser().resolve() if args.mapping else None
    # Idle connection: import_metadata starts REPEATABLE READ itself.
    with db_conn() as conn:
        result = import_metadata(
            conn,
            Path(args.input).expanduser().resolve(),
            mapping_path=mapping,
            dry_run=args.dry_run,
            replace_scopes=args.replace_scopes,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def command_verify_metadata(args) -> None:
    from annotation_metadata.export_metadata import verify_metadata_file
    result = verify_metadata_file(Path(args.input).expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result.get("ok"):
        raise SystemExit(3)


def command_dump_postgres(args) -> None:
    from annotation_metadata.postgres_backup import dump_database
    dsn = os.environ.get("ANNOTATION_DB_DSN", "").strip()
    if not dsn:
        raise MigrationError("ANNOTATION_DB_DSN is not set")
    result = dump_database(
        dsn, Path(args.output).expanduser().resolve(),
        bindir=Path(args.pg_bindir) if args.pg_bindir else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def command_restore_postgres(args) -> None:
    from annotation_metadata.postgres_backup import (
        provenance_counts, provenance_relationships, restore_database,
    )
    target = args.target_dsn.strip()
    if not target:
        raise MigrationError("--target-dsn is required")
    result = restore_database(
        Path(args.dump).expanduser().resolve(), target,
        bindir=Path(args.pg_bindir) if args.pg_bindir else None,
    )
    with psycopg.connect(target) as conn:
        result["counts"] = provenance_counts(conn)
        result["relationships"] = provenance_relationships(conn)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result["relationships"]["ok"]:
        raise SystemExit(3)


def command_assignments(args) -> None:
    with db_conn() as conn:
        rows = conn.execute(
            """SELECT u.username, t.rel_path, a.mode, a.assigned_at,
                      a.last_activity_at
               FROM assignments a
               JOIN annotators u ON u.id = a.user_id
               JOIN annotation_tasks t ON t.id = a.task_id
               ORDER BY a.assigned_at"""
        ).fetchall()
    print(json.dumps([{"username": r[0], "rel_path": r[1], "mode": r[2], "assigned_at": r[3].isoformat(), "last_activity_at": r[4].isoformat()} for r in rows], ensure_ascii=False, indent=2))


def command_release(args) -> None:
    if not args.reason.strip():
        raise MigrationError("--reason is required")
    from annotation_repository import _cancel_in_progress_cross_check
    reason = args.reason.strip()
    with db_conn() as conn, conn.transaction(), conn.cursor() as cur:
        owner = cur.execute(
            "SELECT id FROM annotators WHERE username = %s FOR UPDATE",
            (args.username,),
        ).fetchone()
        if not owner:
            raise MigrationError(f"no assignment for username: {args.username}")
        row = cur.execute(
            """SELECT a.user_id, a.task_id, a.working_version_id, t.status, a.mode,
                      a.cross_check_round_id
               FROM assignments a
               JOIN annotation_tasks t ON t.id = a.task_id
               WHERE a.user_id = %s
               FOR UPDATE OF a""",
            (owner[0],),
        ).fetchone()
        if not row:
            raise MigrationError(f"no assignment for username: {args.username}")
        cur.execute(
            "SELECT id FROM annotation_tasks WHERE id = %s FOR UPDATE",
            (row[1],),
        )
        if row[4] == "revision":
            cur.execute(
                "SELECT id FROM annotation_versions WHERE id = %s FOR UPDATE",
                (row[2],),
            )
            cur.execute(
                """UPDATE annotation_versions
                   SET lifecycle = 'abandoned', updated_at = now()
                   WHERE id = %s AND lifecycle = 'draft'""",
                (row[2],),
            )
        elif row[4] == "cross_check":
            _cancel_in_progress_cross_check(
                cur, round_id=row[5], task_id=row[1], version_id=row[2],
                reason=reason,
            )
            cur.execute(
                """INSERT INTO annotation_events
                       (user_id, task_id, version_id, event_type, from_status,
                        to_status, details)
                   VALUES (%s, %s, %s, 'cross_check_cancelled', %s, %s, %s)""",
                (row[0], row[1], row[2], row[3], row[3], Json({
                    "mode": "cross_check",
                    "round_id": str(row[5]),
                    "termination_reason": reason,
                })),
            )
        cur.execute("DELETE FROM assignments WHERE user_id = %s", (row[0],))
        cur.execute(
            """INSERT INTO annotation_events
                   (user_id, task_id, version_id, event_type, from_status, details)
               VALUES (%s, %s, %s, 'released_admin', %s, %s)""",
            (row[0], row[1], row[2], row[3], Json({"reason": reason})),
        )
    print(f"released assignment for {args.username}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Annotation state management")
    sub = p.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect-json", help="scan legacy JSON and write a blocking manifest")
    inspect.add_argument("--annotations")
    inspect.add_argument("--audio")
    inspect.add_argument("--resolutions", help="audited JSON array resolving exact blocking errors")
    inspect.add_argument("--output", required=True)
    inspect.set_defaults(func=command_inspect)

    apply_cmd = sub.add_parser("apply-migrations")
    apply_cmd.set_defaults(func=command_apply_migrations)

    schema = sub.add_parser("schema")
    schema.set_defaults(func=command_schema)

    migrate = sub.add_parser("migrate-json")
    migrate.add_argument("--manifest", required=True)
    migrate.set_defaults(func=command_migrate)

    verify = sub.add_parser("verify-json")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--output")
    verify.set_defaults(func=command_verify)

    export = sub.add_parser("export-json")
    export.add_argument("--output", required=True)
    export.set_defaults(func=command_export)

    assignments = sub.add_parser("assignments-list")
    assignments.set_defaults(func=command_assignments)

    release = sub.add_parser("assignment-release")
    release.add_argument("--username", required=True)
    release.add_argument("--reason", required=True)
    release.set_defaults(func=command_release)

    inspect_src = sub.add_parser("inspect-source-manifest")
    inspect_src.add_argument("--manifest", required=True)
    inspect_src.add_argument("--batch-code", required=True)
    inspect_src.add_argument("--audio-root")
    inspect_src.set_defaults(func=command_inspect_source_manifest)

    import_src = sub.add_parser("import-source-metadata")
    import_src.add_argument("--manifest", required=True)
    import_src.add_argument("--batch-code", required=True)
    import_src.add_argument("--audio-root")
    import_src.add_argument("--website-audio-root")
    import_src.add_argument("--dry-run", action="store_true")
    import_src.set_defaults(func=command_import_source_metadata)

    verify_src = sub.add_parser("verify-source-metadata")
    verify_src.add_argument("--batch-code", required=True)
    verify_src.set_defaults(func=command_verify_source_metadata)

    from annotation_metadata.export_metadata import IDENTITY_HELP

    export_meta = sub.add_parser(
        "export-metadata",
        help="export versioned scene provenance metadata (not credentials or leases)",
        description=(
            "Write metadata.v1.json as a REPEATABLE READ snapshot. "
            "Legacy export-json is unchanged. Default includes every task."
        ),
        epilog=IDENTITY_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    export_meta.add_argument("--output", required=True, help="output directory")
    _add_task_filter_arguments(export_meta)
    export_meta.set_defaults(func=command_export_metadata)

    import_meta = sub.add_parser(
        "import-metadata",
        help="validate then import metadata.v1.json into matching annotation rows",
        description=(
            "Validate the entire document before applying. Malformed files, "
            "unknown formats, dangling mappings, and identity conflicts roll "
            "back without partial metadata mutation. Repeats are idempotent."
        ),
        epilog=IDENTITY_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    import_meta.add_argument("--input", required=True, help="metadata.v1.json or its directory")
    import_meta.add_argument(
        "--mapping",
        help="explicit UUID mapping JSON (never username or pathname remaps)",
    )
    import_meta.add_argument("--dry-run", action="store_true", help="report without writing")
    import_meta.add_argument(
        "--replace-scopes", action="store_true",
        help="replace annotator scene scopes; omitted scopes are left unchanged",
    )
    import_meta.set_defaults(func=command_import_metadata)

    verify_meta = sub.add_parser(
        "verify-metadata",
        help="validate a metadata.v1.json file without importing",
    )
    verify_meta.add_argument("--input", required=True)
    verify_meta.set_defaults(func=command_verify_metadata)

    dump_pg = sub.add_parser(
        "dump-postgres",
        help="PostgreSQL custom dump of the current ANNOTATION_DB_DSN",
        description=(
            "Complete backup format. Restore only into a newly created database "
            "that has no user schema objects (tables/views/sequences in any "
            "non-system schema). Use same-major pg_dump (pgserver 16 or "
            "/usr/lib/postgresql/18/bin). ANNOTATION_DB_DSN should omit the "
            "password; libpq reads PGPASSWORD, ~/.pgpass, or PGSERVICEFILE."
        ),
    )
    dump_pg.add_argument("--output", required=True, help="output .dump path")
    dump_pg.add_argument("--pg-bindir", help="PostgreSQL binary directory")
    dump_pg.set_defaults(func=command_dump_postgres)

    restore_pg = sub.add_parser(
        "restore-postgres",
        help="pg_restore a custom dump into an empty target DSN and print counts",
        description=(
            "Refuses a target that already has any non-system user schema "
            "object (not merely annotation_tasks rows). Never use a live "
            "staging DSN. Pass --target-dsn without a password; libpq reads "
            "PGPASSWORD, ~/.pgpass, or PGSERVICE/PGSERVICEFILE."
        ),
    )
    restore_pg.add_argument("--dump", required=True)
    restore_pg.add_argument(
        "--target-dsn",
        required=True,
        help=(
            "libpq DSN of an empty database (no user schema objects). Omit "
            "the password; credentials come from PGPASSWORD, ~/.pgpass, or "
            "a service file."
        ),
    )
    restore_pg.add_argument("--pg-bindir", help="PostgreSQL binary directory")
    restore_pg.set_defaults(func=command_restore_postgres)
    return p


def main() -> None:
    args = parser().parse_args()
    try:
        args.func(args)
    except (MigrationError, MetadataImportError, ForbiddenError, ValidationError,
            psycopg.Error, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
