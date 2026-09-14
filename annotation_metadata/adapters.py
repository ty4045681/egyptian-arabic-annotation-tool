"""Adapters from crawler manifests, sidecars, and legacy kwargs into DTOs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from annotation_metadata.contracts import (
    CrawlerRecord,
    MediaIdentity,
    NormalizedSource,
    PreprocessedTaskInput,
)
from annotation_metadata.taxonomy import (
    MEDIA_VARIANT,
    VOLATILE_RAW_KEYS,
    resolve_scene_code,
)

CORE_COMPARE_FIELDS = (
    "scene_code", "confidence", "confidence_basis", "source_type",
    "source_url", "video_id", "provider", "pcm_sha256",
)
JSONL_SUFFIXES = {".jsonl", ".ndjson"}
JSON_SUFFIXES = {".json"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class AdapterError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_record",
                 field: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.field = field
        self.details = details or {}


@dataclass
class ManifestSnapshot:
    documents: list[dict[str, Any]]
    snapshot_sha256: str
    snapshot_bytes: int
    format: str
    line_errors: list[dict[str, Any]] = field(default_factory=list)


def parse_hex_digest(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if not HEX64.fullmatch(text):
        raise AdapterError(
            f"{field} must be a 64-character hex digest",
            code="invalid_checksum",
            field=field,
            details={"value": str(value)[:80]},
        )
    return text


def snapshot_jsonl_bytes(data: bytes) -> tuple[list[str], str, int]:
    """Return complete newline-terminated JSONL records.

    A growing file with no newline yet is an unterminated first line and
    contributes no records. A trailing partial line after the last newline
    is deferred. The snapshot digest covers only the consumed prefix.
    """
    if b"\n" not in data:
        return [], hashlib.sha256(b"").hexdigest(), 0
    cutoff = len(data) if data.endswith(b"\n") else data.rfind(b"\n") + 1
    complete = data[:cutoff]
    digest = hashlib.sha256(complete).hexdigest()
    lines = complete.decode("utf-8").splitlines()
    return lines, digest, cutoff


def snapshot_jsonl(path: Path) -> tuple[list[str], str, int]:
    return snapshot_jsonl_bytes(path.read_bytes())


def _suffix(path: Path) -> str:
    return path.suffix.lower()


def _looks_like_jsonl(raw: bytes, suffix: str) -> bool:
    if suffix in JSONL_SUFFIXES:
        return True
    if suffix in JSON_SUFFIXES:
        return False
    stripped = raw.lstrip()
    if stripped.startswith(b"["):
        return False
    if not stripped.startswith(b"{"):
        return True
    try:
        json.loads(raw.decode("utf-8"))
        return False
    except json.JSONDecodeError as exc:
        if getattr(exc, "pos", None) and "extra data" in str(exc).lower():
            return True
        return b"\n" in raw


def _parse_jsonl_lines(lines: list[str], *, on_error: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    docs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            item = {
                "code": "invalid_jsonl",
                "message": f"invalid JSONL on complete line {index}: {exc}",
                "line": index,
            }
            if on_error == "raise":
                raise AdapterError(
                    item["message"], code="invalid_jsonl",
                    details={"line": index},
                ) from exc
            errors.append(item)
            continue
        if not isinstance(value, dict):
            item = {
                "code": "invalid_jsonl",
                "message": f"JSONL line {index} is not an object",
                "line": index,
            }
            if on_error == "raise":
                raise AdapterError(item["message"], code="invalid_jsonl",
                                   details={"line": index})
            errors.append(item)
            continue
        docs.append(value)
    return docs, errors


def parse_manifest_snapshot(path: Path, *, on_error: str = "raise") -> ManifestSnapshot:
    if path.is_dir():
        raise AdapterError("manifest path is a directory", code="invalid_manifest")
    raw = path.read_bytes()
    suffix = _suffix(path)
    if _looks_like_jsonl(raw, suffix):
        lines, digest, cutoff = snapshot_jsonl_bytes(raw)
        docs, errors = _parse_jsonl_lines(lines, on_error=on_error)
        return ManifestSnapshot(docs, digest, cutoff, "jsonl", errors)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise AdapterError(f"invalid JSON manifest: {exc}", code="invalid_json") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if isinstance(parsed, list):
        docs = [item for item in parsed if isinstance(item, dict)]
        return ManifestSnapshot(docs, digest, len(raw), "json_array")
    if isinstance(parsed, dict):
        items = parsed.get("items") or parsed.get("records") or parsed.get("audios")
        if isinstance(items, list):
            docs = [item for item in items if isinstance(item, dict)]
            return ManifestSnapshot(docs, digest, len(raw), "json_object")
        if parsed.get("kind") == "scene_index":
            return ManifestSnapshot([], digest, len(raw), "scene_index")
        return ManifestSnapshot([parsed], digest, len(raw), "json_object")
    raise AdapterError("manifest JSON must be an object or array", code="invalid_json")


def load_manifest_documents(path: Path) -> tuple[list[dict[str, Any]], str, int, str]:
    snapshot = parse_manifest_snapshot(path, on_error="raise")
    return snapshot.documents, snapshot.snapshot_sha256, snapshot.snapshot_bytes, snapshot.format


def load_sidecar(audio_path: Path) -> dict[str, Any] | None:
    sidecar = audio_path.with_suffix(".json")
    if not sidecar.is_file():
        alt = Path(str(audio_path) + ".json")
        sidecar = alt if alt.is_file() else sidecar
    if not sidecar.is_file():
        return None
    try:
        value = json.loads(sidecar.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AdapterError(
            f"invalid sidecar JSON: {sidecar}",
            code="sidecar_invalid_json",
            details={"path": str(sidecar)},
        ) from exc
    if not isinstance(value, dict):
        raise AdapterError("sidecar JSON must be an object", code="sidecar_invalid_json")
    return value


def normalize_url(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AdapterError(
            "source URL must be http or https",
            code="invalid_url",
            field="source_url",
        )
    return text


def infer_provider(url: str | None, explicit: str | None, video_id: str | None) -> str | None:
    if explicit:
        return explicit.strip().lower()
    if url:
        host = urlparse(url).netloc.lower()
        if "youtube" in host or host.endswith("youtu.be"):
            return "youtube"
        if "bilibili" in host:
            return "bilibili"
    if video_id:
        return "youtube"
    return None


def _core_digest(payload: dict[str, Any]) -> str:
    filtered = {
        key: value for key, value in payload.items()
        if key not in VOLATILE_RAW_KEYS and not str(key).endswith("_at")
    }
    return hashlib.sha256(
        json.dumps(filtered, ensure_ascii=False, sort_keys=True, default=str,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def relative_audio_path(record: CrawlerRecord) -> str | None:
    """Prefer the crawler's explicit relative_path and keep its full depth."""
    for value in (record.relative_path, record.rel_path, record.audio_path):
        text = (value or "").strip().replace("\\", "/")
        if text:
            if text.startswith("/") or ".." in Path(text).parts:
                raise AdapterError(
                    "relative audio path must be relative and must not contain ..",
                    code="invalid_path",
                    field="relative_path",
                )
            return text
    return None


def record_key_for(record: CrawlerRecord, scene_code: str | None,
                   identity: MediaIdentity | None) -> str:
    if record.record_key:
        return record.record_key.strip()
    if record.record_id:
        return f"{record.record_id}:{scene_code or 'unknown'}"
    if identity:
        return f"{identity.provider}:{identity.external_id}:{scene_code or 'unknown'}"
    rel = relative_audio_path(record)
    return f"path:{rel or 'unknown'}:{scene_code or 'unknown'}"


def adapt_crawler_record(raw: dict[str, Any]) -> NormalizedSource:
    record = CrawlerRecord.model_validate(raw)
    if (record.kind or "").strip() in {"scene_index", "index", "scene_manifest"}:
        raise AdapterError("scene index records are not audio sources", code="skip_index")
    scene_code = resolve_scene_code(record.scene_code or record.scene)
    confidence = (record.confidence or "unknown").strip().lower()
    if confidence not in {"high", "medium", "low", "unknown"}:
        raise AdapterError(
            f"invalid confidence {record.confidence!r}",
            code="invalid_confidence",
            field="confidence",
        )
    url = None
    if record.url or record.source_url:
        url = normalize_url(record.source_url or record.url)
    video_id = (record.video_id or "").strip() or None
    provider = infer_provider(url, record.provider, video_id)
    pcm = parse_hex_digest(
        record.pcm_sha256 or record.audio_sha256 or record.checksum,
        field="pcm_sha256",
    )
    identity = None
    if provider and video_id:
        identity = MediaIdentity(
            provider=provider,
            external_id=video_id,
            variant=MEDIA_VARIANT,
            pcm_sha256=pcm,
        )
    rel_path = relative_audio_path(record)
    extras = dict(record.model_extra or {})
    dumped = record.model_dump()
    raw_record = {**{k: v for k, v in dumped.items() if v is not None}, **extras}
    channel_title = (
        (record.channel_title or "").strip()
        or (record.channel or "").strip()
        or None
    )
    core = {
        "scene_code": scene_code,
        "confidence": confidence,
        "confidence_basis": (record.confidence_basis or "").strip(),
        "source_type": (record.source_type or "").strip(),
        "source_url": url,
        "video_id": video_id,
        "provider": provider,
        "pcm_sha256": pcm,
        "channel_id": (record.channel_id or "").strip() or None,
        "channel_title": channel_title,
    }
    key = record_key_for(record, scene_code, identity)
    return NormalizedSource(
        record_key=key,
        scene_code=scene_code,
        confidence=confidence,  # type: ignore[arg-type]
        confidence_basis=core["confidence_basis"] or "",
        source_type=core["source_type"] or "",
        source_url=url,
        video_id=video_id,
        channel_id=core["channel_id"],
        channel_title=channel_title,
        provider=provider,
        raw_record=raw_record,
        content_digest=_core_digest(core),
        rel_path=rel_path,
        source_audio_relpath=rel_path,
        pcm_sha256=pcm,
        duration=record.duration,
        identity=identity,
    )


def merge_manifest_and_sidecar(
    manifest_raw: dict[str, Any] | None,
    sidecar_raw: dict[str, Any] | None,
) -> NormalizedSource:
    if not manifest_raw and not sidecar_raw:
        raise AdapterError("no source document", code="missing_record")
    if manifest_raw and sidecar_raw:
        left = adapt_crawler_record(manifest_raw)
        right = adapt_crawler_record(sidecar_raw)
        for field_name in CORE_COMPARE_FIELDS:
            a = getattr(left, field_name)
            b = getattr(right, field_name)
            if a and b and a != b:
                raise AdapterError(
                    f"manifest/sidecar conflict on {field_name}",
                    code="manifest_sidecar_conflict",
                    field=field_name,
                    details={"manifest": a, "sidecar": b},
                )
        merged_raw = {**sidecar_raw, **manifest_raw}
        return adapt_crawler_record(merged_raw)
    return adapt_crawler_record(manifest_raw or sidecar_raw or {})


def is_ready_status(raw: dict[str, Any]) -> bool:
    """Crawler records are ready only when status is explicitly ready."""
    if "status" not in raw or raw.get("status") in (None, ""):
        return False
    status = str(raw.get("status")).strip().lower()
    return status in {"ready", "ok", "complete", "completed"}


def inspect_wav_pcm(path: Path) -> dict[str, Any]:
    """Decode a WAV file and hash its PCM frames (not the container)."""
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            nframes = handle.getnframes()
            frames = handle.readframes(nframes)
    except (wave.Error, EOFError, OSError) as exc:
        raise AdapterError(
            f"audio is not a readable WAV file: {path}",
            code="invalid_audio",
            details={"path": str(path), "error": str(exc)},
        ) from exc
    duration = (nframes / sample_rate) if sample_rate else 0.0
    return {
        "channels": channels,
        "sample_width": sample_width,
        "sample_rate": sample_rate,
        "nframes": nframes,
        "duration": duration,
        "pcm_sha256": hashlib.sha256(frames).hexdigest(),
        "size": path.stat().st_size,
    }


def validate_source_audio(path: Path, raw: dict[str, Any] | None = None,
                          expected_pcm: str | None = None) -> dict[str, Any]:
    info = inspect_wav_pcm(path)
    raw = raw or {}
    expected = expected_pcm or parse_hex_digest(
        raw.get("pcm_sha256") or raw.get("audio_sha256") or raw.get("checksum"),
        field="pcm_sha256",
    )
    if expected and info["pcm_sha256"] != expected:
        raise AdapterError(
            "manifest pcm_sha256 does not match the audio file",
            code="pcm_mismatch",
            field="pcm_sha256",
            details={"expected": expected, "actual": info["pcm_sha256"]},
        )
    declared_rate = raw.get("sample_rate")
    if declared_rate not in (None, "") and int(declared_rate) != int(info["sample_rate"]):
        raise AdapterError(
            "sample_rate does not match the audio file",
            code="audio_format_mismatch",
            field="sample_rate",
        )
    declared_channels = raw.get("channels")
    if declared_channels not in (None, "") and int(declared_channels) != int(info["channels"]):
        raise AdapterError(
            "channel count does not match the audio file",
            code="audio_format_mismatch",
            field="channels",
        )
    declared_width = raw.get("sample_width_bytes")
    if declared_width not in (None, "") and int(declared_width) != int(info["sample_width"]):
        raise AdapterError(
            "sample width does not match the audio file",
            code="audio_format_mismatch",
            field="sample_width_bytes",
        )
    declared_duration = raw.get("duration")
    if declared_duration not in (None, "") and abs(float(declared_duration) - info["duration"]) > 0.05:
        raise AdapterError(
            "duration does not match the audio file",
            code="audio_format_mismatch",
            field="duration",
        )
    return info


def confined_path(root: Path, candidate: Path) -> Path:
    root_resolved = root.resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise AdapterError(
            "path escapes the allowed root directory",
            code="path_escape",
            details={"root": str(root_resolved), "path": str(resolved)},
        )
    return resolved


def resolve_source_file(source_root: Path, record: NormalizedSource,
                        raw: dict[str, Any] | None = None) -> Path:
    """Map crawler paths onto an explicit source-audio root.

    Foreign-machine absolute ``audio_filepath`` values are never used as
    website paths. The crawler's ``relative_path`` (full scene/confidence/file
    depth) is joined onto ``source_root``.
    """
    candidates: list[Path] = []
    rel = None
    if raw:
        rel = (raw.get("relative_path") or raw.get("rel_path")
               or raw.get("audio_path") or "")
        rel = str(rel).replace("\\", "/").strip() or None
    rel = rel or record.source_audio_relpath or record.rel_path
    if rel:
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise AdapterError("relative_path must stay inside the source root",
                               code="invalid_path")
        candidates.append(source_root / rel)
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = confined_path(source_root, candidate)
        except AdapterError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    raise AdapterError(
        "audio file not found under source root",
        code="missing_audio",
        details={"rel": rel, "root": str(source_root)},
    )


def website_rel_path(record: NormalizedSource, source_file: Path) -> str:
    if record.rel_path:
        return record.rel_path.replace("\\", "/")
    if record.source_audio_relpath:
        return record.source_audio_relpath.replace("\\", "/")
    return source_file.name


def validate_website_rel_path(rel_path: str) -> str:
    text = rel_path.replace("\\", "/").strip().lstrip("/")
    if not text or text.startswith("/") or ".." in Path(text).parts:
        raise AdapterError("invalid website rel_path", code="invalid_path")
    return text


def adapt_legacy_store_kwargs(kwargs: dict[str, Any]) -> PreprocessedTaskInput:
    return PreprocessedTaskInput(
        rel_path=kwargs["rel_path"],
        filename=kwargs["filename"],
        folder=kwargs.get("folder") or "",
        duration=float(kwargs.get("duration") or 0),
        segments=list(kwargs.get("segments") or []),
        waveform_payload=kwargs.get("waveform_payload"),
        preprocessed_at=kwargs.get("preprocessed_at"),
        category=kwargs.get("category"),
        skip_if_human_modified=kwargs.get("skip_if_human_modified", True),
        eligible=kwargs.get("eligible"),
        task_extra=kwargs.get("task_extra"),
        sources=list(kwargs.get("sources") or []),
        identity=kwargs.get("identity"),
        pcm_sha256=kwargs.get("pcm_sha256"),
        processing_token=kwargs.get("processing_token"),
        metadata_only=bool(kwargs.get("metadata_only") or False),
        batch_code=kwargs.get("batch_code"),
    )
