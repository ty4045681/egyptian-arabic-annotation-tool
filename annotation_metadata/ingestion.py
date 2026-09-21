"""Source metadata import and idempotent orchestration."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from psycopg.types.json import Json

from annotation_metadata.adapters import (
    AdapterError,
    adapt_crawler_record,
    confined_path,
    inspect_wav_pcm,
    is_ready_status,
    load_sidecar,
    merge_manifest_and_sidecar,
    parse_manifest_snapshot,
    resolve_source_file,
    validate_source_audio,
    validate_website_rel_path,
    website_rel_path,
)
from annotation_metadata.contracts import (
    ImportIssue,
    ImportResult,
    NormalizedSource,
    PreprocessedTaskInput,
)
from annotation_metadata.features import metadata_write_enabled
from annotation_metadata.processing import (
    DEFAULT_LEASE_SECONDS,
    acquire_processing_lease,
    complete_processing,
    require_processing_token,
)
from annotation_metadata.repository import (
    attach_identity,
    ensure_batch,
    find_pcm_duplicates,
    lookup_identity,
    sync_source_record,
)
from annotation_metadata.taxonomy import MEDIA_VARIANT
from annotation_repository import ConflictError, ForbiddenError, ValidationError


def _now():
    return datetime.now(timezone.utc)


def inspect_source_manifest(manifest_path: Path, *, batch_code: str,
                            source_root: Path | None = None) -> dict:
    snapshot = parse_manifest_snapshot(manifest_path, on_error="collect")
    docs, digest, nbytes, fmt = (
        snapshot.documents, snapshot.snapshot_sha256,
        snapshot.snapshot_bytes, snapshot.format,
    )
    ready = 0
    skipped_index = 0
    invalid = 0
    issues: list[dict] = list(snapshot.line_errors)
    keys: set[str] = set()
    duplicates = 0
    for raw in docs:
        if str(raw.get("kind") or "") in {"scene_index", "index", "scene_manifest"}:
            skipped_index += 1
            continue
        try:
            source = adapt_crawler_record(raw)
        except AdapterError as exc:
            invalid += 1
            issues.append({"code": exc.code, "message": str(exc),
                           "details": exc.details})
            continue
        if source.record_key in keys:
            duplicates += 1
        keys.add(source.record_key)
        if is_ready_status(raw):
            ready += 1
            if source_root is not None:
                try:
                    resolve_source_file(source_root, source, raw)
                except AdapterError as exc:
                    issues.append({"code": exc.code, "message": str(exc),
                                   "record_key": source.record_key})
        else:
            issues.append({
                "code": "not_ready",
                "message": f"status={raw.get('status')!r} is not ready",
                "record_key": source.record_key,
            })
    return {
        "batch_code": batch_code,
        "format": fmt,
        "snapshot_sha256": digest,
        "snapshot_bytes": nbytes,
        "documents": len(docs),
        "ready": ready,
        "skipped_index": skipped_index,
        "invalid": invalid,
        "duplicate_keys": duplicates,
        "issues": issues[:200],
        "issue_count": len(issues),
        "line_errors": snapshot.line_errors,
    }


def begin_import_run(cur, *, batch_id, snapshot_sha256: str,
                     snapshot_bytes: int | None, contract_version: int) -> uuid.UUID:
    existing = cur.execute(
        """SELECT id, status FROM source_import_runs
           WHERE batch_id = %s AND snapshot_sha256 = %s""",
        (batch_id, snapshot_sha256),
    ).fetchone()
    if existing:
        return existing[0]
    run_id = uuid.uuid4()
    row = cur.execute(
        """INSERT INTO source_import_runs
               (id, batch_id, snapshot_sha256, snapshot_bytes, contract_version,
                status)
           VALUES (%s, %s, %s, %s, %s, 'running')
           ON CONFLICT (batch_id, snapshot_sha256) DO UPDATE
             SET batch_id = source_import_runs.batch_id
           RETURNING id""",
        (run_id, batch_id, snapshot_sha256, snapshot_bytes, contract_version),
    ).fetchone()
    return row[0]


def finish_import_run(cur, run_id, *, status: str, counts: dict,
                      error_report: list, processed: int) -> None:
    cur.execute(
        """UPDATE source_import_runs
           SET status = %s, counts = %s, error_report = %s,
               processed_count = %s, completed_at = now()
           WHERE id = %s""",
        (status, Json(counts), Json(error_report[:500]), processed, run_id),
    )


def lock_task_by_rel_path(cur, rel_path: str):
    return cur.execute(
        """SELECT id, current_published_version_id, status, pcm_sha256
           FROM annotation_tasks WHERE rel_path = %s FOR UPDATE""",
        (rel_path,),
    ).fetchone()


def task_is_protected(cur, task_id) -> bool:
    published = cur.execute(
        "SELECT current_published_version_id FROM annotation_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if published and published[0]:
        return True
    if cur.execute("SELECT 1 FROM assignments WHERE task_id = %s", (task_id,)).fetchone():
        return True
    draft = cur.execute(
        """SELECT human_modified FROM annotation_versions
           WHERE task_id = %s AND lifecycle = 'draft'""",
        (task_id,),
    ).fetchone()
    if draft and draft[0]:
        return True
    return bool(cur.execute(
        """SELECT 1 FROM annotation_versions
           WHERE task_id = %s
             AND (lifecycle = 'cross_check_submitted'
                  OR purpose IN ('cross_check', 'adjudication'))
           LIMIT 1""",
        (task_id,),
    ).fetchone())


def sync_sources_for_task(cur, task_id, *, batch_id,
                          sources: Iterable[NormalizedSource],
                          identity=None, pcm_sha256: str | None = None) -> str:
    actions = []
    if identity is not None:
        attach_identity(cur, task_id, identity)
    if pcm_sha256:
        cur.execute(
            """UPDATE annotation_tasks
               SET pcm_sha256 = COALESCE(pcm_sha256, %s), updated_at = now()
               WHERE id = %s""",
            (pcm_sha256, task_id),
        )
    for source in sources:
        actions.append(sync_source_record(cur, task_id=task_id, batch_id=batch_id,
                                          source=source))
    if not actions:
        return "unchanged"
    if all(action == "unchanged" for action in actions):
        return "unchanged"
    if "created" in actions or "revised" in actions:
        return "metadata_updated"
    return "unchanged"


def create_placeholder_task(cur, *, rel_path: str, filename: str, folder: str,
                            duration: float = 0.0, pcm_sha256: str | None = None):
    existing = lock_task_by_rel_path(cur, rel_path)
    if existing:
        return existing[0], False
    task_id = uuid.uuid4()
    baseline_id = uuid.uuid4()
    draft_id = uuid.uuid4()
    allocation_order = cur.execute(
        "SELECT nextval('task_allocation_order_seq')"
    ).fetchone()[0]
    cur.execute(
        """INSERT INTO annotation_tasks
               (id, rel_path, filename, folder, duration, status, eligible,
                allocation_order, pcm_sha256, extra)
           VALUES (%s, %s, %s, %s, %s, 'pending', false, %s, %s, %s)""",
        (task_id, rel_path, filename, folder, float(duration or 0),
         allocation_order, pcm_sha256,
         Json({"placeholder": True, "asr_checkpoint_incomplete": True})),
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
    cur.execute(
        """UPDATE annotation_tasks
           SET baseline_version_id = %s, baseline_quality = 'exact'
           WHERE id = %s""",
        (baseline_id, task_id),
    )
    return task_id, True


def register_path_alias(cur, task_id, alias_rel_path: str) -> None:
    """Record a website path that refers to an existing canonical task."""
    text = (alias_rel_path or "").replace("\\", "/").strip()
    if not text:
        return
    row = cur.execute(
        "SELECT rel_path FROM annotation_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if not row or row[0] == text:
        return
    cur.execute(
        """UPDATE annotation_tasks
           SET extra = jsonb_set(
                 COALESCE(extra, '{}'::jsonb),
                 '{path_aliases}',
                 COALESCE(extra->'path_aliases', '[]'::jsonb) || to_jsonb(%s::text),
                 true
               ),
               updated_at = now()
           WHERE id = %s
             AND NOT COALESCE(extra->'path_aliases', '[]'::jsonb) ? %s""",
        (text, task_id, text),
    )


def find_task_row_for_audio_path(cur, rel_path: str):
    """Lock the canonical task for a website path or a recorded alias."""
    found = cur.execute(
        """SELECT id FROM (
               SELECT t.id, 0 AS rank
               FROM annotation_tasks t
               WHERE t.rel_path = %s
               UNION ALL
               SELECT t.id, 1
               FROM annotation_tasks t
               WHERE t.extra->'path_aliases' ? %s
               UNION ALL
               SELECT s.task_id, 2
               FROM task_sources s
               WHERE s.is_current
                 AND (
                   s.raw_record->>'relative_path' = %s
                   OR s.raw_record->>'rel_path' = %s
                   OR s.raw_record->>'audio_path' = %s
                 )
           ) ranked
           ORDER BY rank
           LIMIT 1""",
        (rel_path, rel_path, rel_path, rel_path, rel_path),
    ).fetchone()
    if not found:
        return None
    return cur.execute(
        """SELECT id, current_published_version_id, status,
                  baseline_version_id, rel_path
           FROM annotation_tasks WHERE id = %s FOR UPDATE""",
        (found[0],),
    ).fetchone()


def begin_audio_processing(conn, *, rel_path: str, filename: str, folder: str,
                           identity=None, pcm_sha256: str | None = None,
                           duration: float = 0.0,
                           ttl_seconds: int = DEFAULT_LEASE_SECONDS) -> dict:
    """Create/reuse the canonical placeholder and acquire a processing lease.

    The returned transaction is committed by the caller's connection
    transaction. VAD/ASR must run after this returns, not inside it.
    """
    from annotation_metadata.repository import (
        attach_identity, lock_identity, lookup_identity,
    )

    with conn.transaction(), conn.cursor() as cur:
        task_id = None
        canonical_rel = rel_path
        created = False
        if identity is not None:
            lock_identity(cur, identity)
            existing = lookup_identity(cur, identity)
            if existing:
                task_id = existing["task_id"]
                row = cur.execute(
                    """SELECT rel_path FROM annotation_tasks
                       WHERE id = %s FOR UPDATE""",
                    (task_id,),
                ).fetchone()
                if not row:
                    raise ValidationError("identity task is missing")
                canonical_rel = row[0]
                if rel_path != canonical_rel:
                    register_path_alias(cur, task_id, rel_path)
        if task_id is None:
            found = find_task_row_for_audio_path(cur, rel_path)
            if found:
                task_id = found[0]
                canonical_rel = found[4]
                if rel_path != canonical_rel:
                    register_path_alias(cur, task_id, rel_path)
            else:
                task_id, created = create_placeholder_task(
                    cur, rel_path=rel_path, filename=filename, folder=folder,
                    duration=duration, pcm_sha256=pcm_sha256,
                )
                canonical_rel = rel_path
        if identity is not None:
            try:
                attached = attach_identity(cur, task_id, identity)
            except ConflictError:
                raced = lookup_identity(cur, identity)
                if not raced:
                    raise
                if created and raced["task_id"] != task_id:
                    cur.execute(
                        "DELETE FROM annotation_tasks WHERE id = %s "
                        "AND eligible = false AND current_published_version_id IS NULL "
                        "AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.task_id = %s)",
                        (task_id, task_id),
                    )
                task_id = raced["task_id"]
                created = False
                row = cur.execute(
                    """SELECT rel_path FROM annotation_tasks
                       WHERE id = %s FOR UPDATE""",
                    (task_id,),
                ).fetchone()
                canonical_rel = row[0] if row else rel_path
                if rel_path != canonical_rel:
                    register_path_alias(cur, task_id, rel_path)
            else:
                if attached["task_id"] != task_id:
                    task_id = attached["task_id"]
                    created = False
                    row = cur.execute(
                        "SELECT rel_path FROM annotation_tasks WHERE id = %s FOR UPDATE",
                        (task_id,),
                    ).fetchone()
                    canonical_rel = row[0] if row else rel_path
                    if rel_path != canonical_rel:
                        register_path_alias(cur, task_id, rel_path)
        if task_is_protected(cur, task_id):
            return {
                "task_id": str(task_id),
                "token": None,
                "processing_version": None,
                "rel_path": canonical_rel,
                "created": created,
                "protected": True,
            }
        lease = acquire_processing_lease(cur, task_id, ttl_seconds=ttl_seconds)
        return {
            "task_id": str(task_id),
            "token": lease["token"],
            "processing_version": lease["processing_version"],
            "rel_path": canonical_rel,
            "created": created,
            "protected": False,
        }


def place_audio(source_file: Path, dest: Path, *, source_pcm_sha256: str) -> str:
    """Publish source audio without clobbering a different existing file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        existing = inspect_wav_pcm(dest)
        if existing["pcm_sha256"] != source_pcm_sha256:
            raise AdapterError(
                "website audio already exists with different PCM content",
                code="audio_conflict",
                details={"dest": str(dest)},
            )
        return "exists"
    tmp = dest.with_name(dest.name + f".{uuid.uuid4().hex}.tmp")
    try:
        try:
            os.link(source_file, tmp)
        except OSError:
            shutil.copy2(source_file, tmp)
        try:
            os.link(tmp, dest)
        except FileExistsError:
            existing = inspect_wav_pcm(dest)
            if existing["pcm_sha256"] != source_pcm_sha256:
                raise AdapterError(
                    "website audio already exists with different PCM content",
                    code="audio_conflict",
                    details={"dest": str(dest)},
                )
            return "exists"
        return "placed"
    finally:
        if tmp.exists():
            tmp.unlink()


def _filename_folder(rel_path: str) -> tuple[str, str]:
    path = Path(rel_path)
    filename = path.name
    folder = path.parent.as_posix()
    if folder == ".":
        folder = ""
    return filename, folder


def _invalid(exc: AdapterError, record_key: str | None = None) -> ImportResult:
    return ImportResult(
        action="invalid" if exc.code not in {"audio_conflict", "identity_conflict",
                                             "identity_path_mismatch"} else "conflict",
        record_key=record_key,
        issues=[ImportIssue(code=exc.code, message=str(exc),
                            record_key=record_key, details=exc.details)],
    )


def import_one_record(conn, *, raw: dict[str, Any], batch_code: str,
                      source_root: Path | None, audio_root: Path | None,
                      sidecar_root: Path | None = None,
                      dry_run: bool = False,
                      place_files: bool = True) -> ImportResult:
    from pydantic import ValidationError as PydanticValidationError
    from annotation_metadata.repository import lock_identity

    if not metadata_write_enabled() and not dry_run:
        raise ForbiddenError("Metadata writes are disabled")
    if str(raw.get("kind") or "") in {"scene_index", "index", "scene_manifest"}:
        return ImportResult(action="invalid", issues=[ImportIssue(
            code="skip_index", message="scene index is not an audio record")])
    try:
        source = adapt_crawler_record(raw)
    except AdapterError as exc:
        return _invalid(exc)
    except PydanticValidationError as exc:
        return ImportResult(action="invalid", issues=[ImportIssue(
            code="invalid_record", message=str(exc))])

    if not is_ready_status(raw):
        return ImportResult(
            action="invalid", record_key=source.record_key,
            issues=[ImportIssue(code="not_ready",
                                message="record status is not ready",
                                record_key=source.record_key)],
        )

    source_file = None
    audio_info = None
    try:
        if source_root is not None:
            source_file = resolve_source_file(source_root, source, raw)
            sidecar = load_sidecar(source_file)
            if sidecar:
                source = merge_manifest_and_sidecar(raw, sidecar)
            audio_info = validate_source_audio(
                source_file, raw, expected_pcm=source.pcm_sha256,
            )
            source.pcm_sha256 = audio_info["pcm_sha256"]
            if source.identity is not None:
                source.identity = source.identity.model_copy(
                    update={"pcm_sha256": audio_info["pcm_sha256"]}
                )
    except AdapterError as exc:
        return _invalid(exc, source.record_key)

    rel_path = validate_website_rel_path(
        source.rel_path or (website_rel_path(source, source_file)
                            if source_file else source.source_audio_relpath
                            or source.record_key)
    )
    filename, folder = _filename_folder(rel_path)
    issues: list[ImportIssue] = []

    if audio_root is not None and source_file is not None and place_files and not dry_run:
        try:
            dest = confined_path(audio_root, audio_root / rel_path)
            place_audio(
                source_file, dest,
                source_pcm_sha256=audio_info["pcm_sha256"] if audio_info else source.pcm_sha256,
            )
        except AdapterError as exc:
            return _invalid(exc, source.record_key)

    if dry_run:
        with conn.cursor() as cur:
            identity = source.identity
            existing_identity = lookup_identity(cur, identity) if identity else None
            existing_path = cur.execute(
                "SELECT id FROM annotation_tasks WHERE rel_path = %s", (rel_path,),
            ).fetchone()
            if existing_identity:
                task_id = existing_identity["task_id"]
                action = "protected_text" if task_is_protected(cur, task_id) else "unchanged"
                return ImportResult(action=action, task_id=str(task_id),
                                    record_key=source.record_key, issues=issues)
            if existing_path:
                action = "protected_text" if task_is_protected(cur, existing_path[0]) else "unchanged"
                return ImportResult(action=action, task_id=str(existing_path[0]),
                                    record_key=source.record_key, issues=issues)
        return ImportResult(action="created", record_key=source.record_key, issues=issues)

    try:
        with conn.transaction(), conn.cursor() as cur:
            batch_id = ensure_batch(cur, batch_code)
            identity = source.identity
            created = False
            orphan_id = None
            if identity is not None:
                lock_identity(cur, identity)
            existing_identity = lookup_identity(cur, identity) if identity else None
            task_row = lock_task_by_rel_path(cur, rel_path)
            if existing_identity:
                task_id = existing_identity["task_id"]
                cur.execute(
                    "SELECT id FROM annotation_tasks WHERE id = %s FOR UPDATE",
                    (task_id,),
                )
                if task_row and task_row[0] != task_id:
                    return ImportResult(
                        action="conflict", record_key=source.record_key,
                        issues=[ImportIssue(
                            code="identity_path_mismatch",
                            message="identity and rel_path point at different tasks",
                            record_key=source.record_key,
                        )],
                    )
            elif task_row:
                task_id = task_row[0]
            else:
                task_id, created = create_placeholder_task(
                    cur, rel_path=rel_path, filename=filename, folder=folder,
                    duration=float(source.duration or (audio_info or {}).get("duration") or 0),
                    pcm_sha256=source.pcm_sha256,
                )
                if created:
                    orphan_id = task_id
            if identity is not None:
                try:
                    attached = attach_identity(cur, task_id, identity)
                except ConflictError as exc:
                    raced = lookup_identity(cur, identity)
                    if raced:
                        if orphan_id and orphan_id != raced["task_id"]:
                            cur.execute(
                                "DELETE FROM annotation_tasks WHERE id = %s "
                                "AND eligible = false AND current_published_version_id IS NULL "
                                "AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.task_id = %s)",
                                (orphan_id, orphan_id),
                            )
                        task_id = raced["task_id"]
                        created = False
                        cur.execute(
                            "SELECT id FROM annotation_tasks WHERE id = %s FOR UPDATE",
                            (task_id,),
                        )
                    else:
                        return ImportResult(
                            action="conflict", record_key=source.record_key,
                            task_id=str(task_id),
                            issues=[ImportIssue(code="identity_conflict",
                                                message=str(exc),
                                                record_key=source.record_key)],
                        )
                else:
                    if attached["task_id"] != task_id:
                        task_id = attached["task_id"]
                        created = False
            if source.pcm_sha256:
                dupes = find_pcm_duplicates(cur, source.pcm_sha256,
                                            exclude_task_id=task_id)
                for dupe in dupes:
                    issues.append(ImportIssue(
                        code="potential_duplicate_pcm",
                        message="same PCM digest exists on a different video/task; not merged",
                        record_key=source.record_key,
                        details=dupe,
                    ))
            action = sync_sources_for_task(
                cur, task_id, batch_id=batch_id, sources=[source],
                identity=None, pcm_sha256=source.pcm_sha256,
            )
            if created:
                action = "created"
            canonical = cur.execute(
                "SELECT rel_path FROM annotation_tasks WHERE id = %s",
                (task_id,),
            ).fetchone()
            if canonical and canonical[0] != rel_path:
                register_path_alias(cur, task_id, rel_path)
            protected = task_is_protected(cur, task_id)
            if protected and action != "created":
                action = "protected_text"
            return ImportResult(
                action=action, task_id=str(task_id), record_key=source.record_key,
                issues=issues,
            )
    except AdapterError as exc:
        return _invalid(exc, source.record_key)


def import_source_metadata(conn, *, manifest_path: Path, batch_code: str,
                           source_root: Path | None = None,
                           audio_root: Path | None = None,
                           dry_run: bool = False,
                           contract_version: int = 1) -> dict:
    snapshot = parse_manifest_snapshot(manifest_path, on_error="collect")
    docs, digest, nbytes, fmt = (
        snapshot.documents, snapshot.snapshot_sha256,
        snapshot.snapshot_bytes, snapshot.format,
    )
    counts: Counter[str] = Counter()
    error_report: list[dict] = []
    results: list[ImportResult] = []
    run_id = None
    for line_error in snapshot.line_errors:
        results.append(ImportResult(action="invalid", issues=[ImportIssue(
            code=line_error.get("code") or "invalid_jsonl",
            message=line_error.get("message") or "invalid JSONL line",
            details=line_error,
        )]))
        counts["invalid"] += 1
        error_report.append(line_error)
    if not dry_run:
        with conn.transaction(), conn.cursor() as cur:
            batch_id = ensure_batch(cur, batch_code)
            run_id = begin_import_run(
                cur, batch_id=batch_id, snapshot_sha256=digest,
                snapshot_bytes=nbytes, contract_version=contract_version,
            )
    for raw in docs:
        result = import_one_record(
            conn, raw=raw, batch_code=batch_code,
            source_root=source_root, audio_root=audio_root,
            sidecar_root=source_root, dry_run=dry_run,
        )
        results.append(result)
        counts[result.action] += 1
        for issue in result.issues:
            error_report.append(issue.model_dump())
    status = "completed"
    if counts.get("invalid") or counts.get("conflict"):
        status = "partial" if counts.get("created") or counts.get("metadata_updated") or counts.get("unchanged") or counts.get("protected_text") else "failed"
    if not dry_run and run_id is not None:
        with conn.transaction(), conn.cursor() as cur:
            finish_import_run(
                cur, run_id, status=status, counts=dict(counts),
                error_report=error_report, processed=len(results),
            )
    return {
        "run_id": str(run_id) if run_id else None,
        "batch_code": batch_code,
        "snapshot_sha256": digest,
        "format": fmt,
        "dry_run": dry_run,
        "counts": dict(counts),
        "status": status,
        "results": [item.model_dump() for item in results],
        "issues": error_report,
    }


def verify_source_metadata(conn, *, batch_code: str) -> dict:
    with conn.cursor() as cur:
        batch = cur.execute(
            "SELECT id FROM source_batches WHERE batch_code = %s",
            (batch_code,),
        ).fetchone()
        if not batch:
            raise ValidationError(f"unknown batch_code: {batch_code}")
        current = cur.execute(
            """SELECT count(*) FROM task_sources
               WHERE batch_id = %s AND is_current""",
            (batch[0],),
        ).fetchone()[0]
        tasks = cur.execute(
            """SELECT count(DISTINCT task_id) FROM task_sources
               WHERE batch_id = %s AND is_current""",
            (batch[0],),
        ).fetchone()[0]
        latest_run = cur.execute(
            """SELECT id, status, counts, snapshot_sha256, completed_at
               FROM source_import_runs
               WHERE batch_id = %s
               ORDER BY started_at DESC LIMIT 1""",
            (batch[0],),
        ).fetchone()
    return {
        "batch_code": batch_code,
        "current_source_rows": int(current),
        "distinct_tasks": int(tasks),
        "latest_run": None if not latest_run else {
            "id": str(latest_run[0]),
            "status": latest_run[1],
            "counts": latest_run[2],
            "snapshot_sha256": latest_run[3],
            "completed_at": latest_run[4].isoformat() if latest_run[4] else None,
        },
    }


def apply_store_side_effects(cur, task_id, input_data: PreprocessedTaskInput,
                             batch_id=None) -> str:
    if input_data.processing_token:
        require_processing_token(cur, task_id, input_data.processing_token)
    action = "unchanged"
    if input_data.sources:
        if batch_id is None and input_data.batch_code:
            batch_id = ensure_batch(cur, input_data.batch_code)
        if batch_id is None:
            batch_id = ensure_batch(cur, "legacy-unspecified-batch")
        action = sync_sources_for_task(
            cur, task_id, batch_id=batch_id, sources=input_data.sources,
            identity=input_data.identity, pcm_sha256=input_data.pcm_sha256,
        )
    elif input_data.identity is not None:
        attach_identity(cur, task_id, input_data.identity)
    if input_data.processing_token:
        complete_processing(cur, task_id, input_data.processing_token)
    return action
