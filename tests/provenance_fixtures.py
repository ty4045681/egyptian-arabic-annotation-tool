"""Synthetic old+new provenance used by export/restore/dump tests."""

from __future__ import annotations

import hashlib
import json
import uuid

from psycopg.types.json import Json

import annotation_repository as repo
import db
from annotation_metadata.contracts import MediaIdentity, NormalizedSource
from annotation_metadata.ingestion import (
    begin_import_run, finish_import_run, register_path_alias,
)
from annotation_metadata.repository import (
    attach_identity, ensure_batch, insert_prediction, sync_source_record,
)
from tests.test_repository import full_segments, make_user


def _digest(payload: dict) -> str:
    import json
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def wipe_metadata(conn) -> None:
    conn.execute("UPDATE assignments SET claim_source_id = NULL")
    conn.execute("DELETE FROM assignments")
    conn.execute("DELETE FROM scene_review_labels")
    conn.execute("DELETE FROM scene_reviews")
    conn.execute("DELETE FROM task_scene_predictions")
    conn.execute("DELETE FROM task_sources")
    conn.execute("DELETE FROM task_media_identities")
    conn.execute("DELETE FROM source_import_runs")
    conn.execute("DELETE FROM source_batches")
    conn.execute("DELETE FROM annotator_scene_access")
    conn.execute("DELETE FROM annotator_scene_scopes")
    conn.commit()


def _mapped(value, table: str, id_map: dict | None):
    if value is None or not id_map:
        return value
    return id_map.get(table, {}).get(str(value), value)


def clone_annotation_skeleton(src_dsn: str, dest_dsn: str, *, id_map: dict | None = None) -> None:
    """Copy task/version/segment/user rows, not metadata or leases.

    ``id_map`` may remap tasks/versions/users to new UUIDs. That mapping is
    explicit and must be passed to metadata import; nothing is inferred from
    username or pathname.
    """
    with psycopg_connect(src_dsn) as src, psycopg_connect(dest_dsn) as dest:
        with dest.transaction():
            for row in src.execute(
                """SELECT id, username, created_at, status, deactivated_at,
                          deactivated_reason FROM annotators ORDER BY username"""
            ):
                dest.execute(
                    """INSERT INTO annotators
                           (id, username, created_at, status, deactivated_at,
                            deactivated_reason)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (id) DO NOTHING""",
                    (_mapped(row[0], "users", id_map), *row[1:]),
                )
            for index, row in enumerate(src.execute(
                """SELECT id, rel_path, legacy_audio_key, filename, folder, duration,
                          status, eligible, allocation_order, reserved_for_user_id,
                          category, preprocessed_at, source_json_sha256, extra,
                          created_at, updated_at, pcm_sha256, processing_version
                   FROM annotation_tasks ORDER BY allocation_order"""
            )):
                extra = dict(row[13] or {})
                extra.pop("path_aliases", None)
                dest.execute(
                    """INSERT INTO annotation_tasks
                           (id, rel_path, legacy_audio_key, filename, folder, duration,
                            status, eligible, allocation_order, reserved_for_user_id,
                            category, preprocessed_at, source_json_sha256, extra,
                            created_at, updated_at, pcm_sha256, processing_version,
                            current_published_version_id, baseline_version_id,
                            processing_token, processing_lease_until)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, NULL, NULL, NULL, NULL)""",
                    (
                        _mapped(row[0], "tasks", id_map), *row[1:9],
                        _mapped(row[9], "users", id_map), *row[10:13], Json(extra), *row[14:],
                    ),
                )
            for row in src.execute(
                """SELECT id, task_id, version_no, lifecycle, target_status,
                          revision, human_modified, created_by_user_id,
                          modified_by_user_id, submitted_by_user_id, skip_reasons,
                          created_at, updated_at, submitted_at, extra, revoked_at,
                          revoked_reason
                   FROM annotation_versions ORDER BY task_id, version_no"""
            ):
                dest.execute(
                    """INSERT INTO annotation_versions
                           (id, task_id, version_no, lifecycle, target_status,
                            base_version_id, revision, human_modified,
                            created_by_user_id, modified_by_user_id,
                            submitted_by_user_id, skip_reasons, created_at,
                            updated_at, submitted_at, extra, revoked_at,
                            revoked_reason)
                       VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s)""",
                    (
                        _mapped(row[0], "versions", id_map),
                        _mapped(row[1], "tasks", id_map),
                        *row[2:7],
                        _mapped(row[7], "users", id_map),
                        _mapped(row[8], "users", id_map),
                        _mapped(row[9], "users", id_map),
                        row[10], row[11], row[12], row[13], Json(row[14] or {}),
                        row[15], row[16],
                    ),
                )
            for row in src.execute(
                "SELECT id, base_version_id FROM annotation_versions WHERE base_version_id IS NOT NULL"
            ):
                dest.execute(
                    "UPDATE annotation_versions SET base_version_id = %s WHERE id = %s",
                    (
                        _mapped(row[1], "versions", id_map),
                        _mapped(row[0], "versions", id_map),
                    ),
                )
            for row in src.execute(
                """SELECT version_id, segment_id, start_s, end_s, duration, asr_text,
                          text, exclude_from_training, extra FROM segments
                   ORDER BY version_id, segment_id"""
            ):
                dest.execute(
                    """INSERT INTO segments
                           (version_id, segment_id, start_s, end_s, duration, asr_text,
                            text, exclude_from_training, extra)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (_mapped(row[0], "versions", id_map), *row[1:8], Json(row[8] or {}),)
                )
            for row in src.execute(
                """SELECT id, current_published_version_id, baseline_version_id
                   FROM annotation_tasks"""
            ):
                dest.execute(
                    """UPDATE annotation_tasks
                       SET current_published_version_id = %s, baseline_version_id = %s
                       WHERE id = %s""",
                    (
                        _mapped(row[1], "versions", id_map),
                        _mapped(row[2], "versions", id_map),
                        _mapped(row[0], "tasks", id_map),
                    ),
                )


def psycopg_connect(dsn: str):
    import psycopg
    return psycopg.connect(dsn)


def build_rich_provenance(seed_tasks):
    legacy_id, rich_id, extra_id = [uuid.UUID(str(item)) for item in seed_tasks(3)]
    annotator, _ = make_user("prov-annotator")
    scoped, _ = make_user("prov-scoped")
    assignment = repo.claim(annotator["fence"])
    assert assignment["task_id"] == str(legacy_id)
    repo.complete(
        annotator["fence"], assignment["lease_token"], assignment["revision"],
        "annotated", [], full_segments(assignment, "legacy"),
        str(uuid.uuid4()), "legacy-complete",
    )
    with db.db_conn() as conn, conn.cursor() as cur:
        airport_batch = ensure_batch(cur, "acceptance-airport-rev", name="airport rev")
        shopping_batch = ensure_batch(cur, "acceptance-shopping-cross", name="shopping cross")
        first = {
            "video_id": "vid-rich-1", "scene": "airport", "confidence": "low",
            "url": "https://example.com/airport-v1",
        }
        second = {
            "video_id": "vid-rich-1", "scene": "airport", "confidence": "high",
            "url": "https://example.com/airport-v2", "basis": "revised crawl",
        }
        shopping = {
            "video_id": "vid-rich-1", "scene": "shopping", "confidence": "medium",
            "url": "https://example.com/shopping",
        }
        sync_source_record(
            cur, task_id=rich_id, batch_id=airport_batch,
            source=NormalizedSource(
                record_key="youtube:vid-rich-1:airport",
                scene_code="airport", confidence="low",
                confidence_basis="first crawl basis",
                source_type="explicit_video",
                source_url=first["url"], video_id="vid-rich-1",
                channel_id="chan-1", channel_title="Fixture channel",
                provider="youtube", raw_record=first, content_digest=_digest(first),
            ),
        )
        sync_source_record(
            cur, task_id=rich_id, batch_id=airport_batch,
            source=NormalizedSource(
                record_key="youtube:vid-rich-1:airport",
                scene_code="airport", confidence="high",
                confidence_basis="revised crawl basis",
                source_type="explicit_video",
                source_url=second["url"], video_id="vid-rich-1",
                channel_id="chan-1", channel_title="Fixture channel",
                provider="youtube", raw_record=second, content_digest=_digest(second),
            ),
        )
        sync_source_record(
            cur, task_id=rich_id, batch_id=shopping_batch,
            source=NormalizedSource(
                record_key="youtube:vid-rich-1:shopping",
                scene_code="shopping", confidence="medium",
                confidence_basis="cross-scene crawl",
                source_type="explicit_video",
                source_url=shopping["url"], video_id="vid-rich-1",
                channel_title="Fixture channel", provider="youtube",
                raw_record=shopping, content_digest=_digest(shopping),
            ),
        )
        attach_identity(
            cur, rich_id,
            MediaIdentity(
                provider="youtube", external_id="vid-rich-1",
                variant="pcm16k_mono", pcm_sha256="ab" * 32,
            ),
        )
        register_path_alias(cur, rich_id, "alias/folder/rich.wav")
        run_id = begin_import_run(
            cur, batch_id=airport_batch, snapshot_sha256="cd" * 32,
            snapshot_bytes=128, contract_version=1,
        )
        finish_import_run(
            cur, run_id, status="completed",
            counts={"created": 1, "revised": 1}, error_report=[], processed=2,
        )
        cur.execute(
            """UPDATE source_import_runs
               SET checkpoint = %s::jsonb WHERE id = %s""",
            (json.dumps({
                "processed_bytes": 128,
                "last_complete_line": 2,
                "last_record_key": "youtube:vid-rich-1:airport",
            }), run_id),
        )
        draft = cur.execute(
            """SELECT id, revision FROM annotation_versions
               WHERE task_id = %s AND lifecycle = 'draft'""",
            (rich_id,),
        ).fetchone()
        insert_prediction(
            cur, task_id=rich_id, predicted_label="Airport",
            model_name="acceptance-model-v1", prompt_version="classify-v1",
            input_version_id=draft[0], input_revision=int(draft[1]),
            input_digest="d1" * 32, score=None, predicted_scene_code="airport",
        )
    assignment = repo.claim(annotator["fence"])
    assert assignment["task_id"] == str(rich_id)
    saved = repo.save_draft(
        annotator["fence"], assignment["lease_token"], 0,
        full_segments(assignment, "rich"), str(uuid.uuid4()), "rich-save",
        scene_review={"status": "pending", "scene_codes": [], "note": "draft note"},
    )
    completed = repo.complete(
        annotator["fence"], assignment["lease_token"], saved["revision"],
        "annotated", [], full_segments(assignment, "rich"),
        str(uuid.uuid4()), "rich-complete",
        scene_review={"status": "confirmed", "scene_codes": ["airport"], "note": "submitted"},
    )
    published_review_id = completed["scene_review"]["id"]
    with db.db_conn() as conn, conn.cursor() as cur:
        published = cur.execute(
            "SELECT current_published_version_id FROM annotation_tasks WHERE id = %s",
            (rich_id,),
        ).fetchone()[0]
        insert_prediction(
            cur, task_id=rich_id, predicted_label="Shopping",
            model_name="acceptance-model-v2", prompt_version="classify-v1",
            input_version_id=published, input_revision=1,
            input_digest="d2" * 32, score=0.42, predicted_scene_code="shopping",
        )
    detail = repo.admin_annotation_detail(str(rich_id))
    session = repo.create_admin_session(
        "primary", hashlib.sha256(b"token").hexdigest(),
        hashlib.sha256(b"csrf").hexdigest(), 1800, 3600,
    )
    admin = repo.admin_correct_scene_review(
        session["id"], str(rich_id),
        {
            "operation_id": str(uuid.uuid4()),
            "expected_version_id": detail["current_version_id"],
            "expected_review_id": detail["metadata"]["scene_review"]["id"],
            "status": "mixed",
            "scene_codes": ["airport", "shopping"],
            "note": "admin correction after submission",
            "reason": "correct mixed scenes",
        },
    )
    repo.reopen_completed(annotator["fence"], str(rich_id), str(uuid.uuid4()))
    draft_asg = repo.get_assignment(annotator["id"])
    repo.save_draft(
        annotator["fence"], draft_asg["lease_token"], 0,
        full_segments(draft_asg, "rich-edit"), str(uuid.uuid4()), "rich-draft",
        scene_review={"status": "uncertain", "scene_codes": [], "note": "working draft"},
    )
    with db.db_conn() as conn, conn.cursor() as cur:
        from annotation_metadata.repository import replace_scope
        replace_scope(
            cur, scoped["id"], mode="restricted", scene_codes=["shopping"],
            allow_unknown=False, expected_revision=0,
        )
    extra_user, _ = make_user("prov-extra")
    extra_asg = repo.claim(extra_user["fence"])
    assert extra_asg["task_id"] == str(extra_id)
    return {
        "legacy_id": str(legacy_id),
        "rich_id": str(rich_id),
        "extra_id": str(extra_id),
        "annotator_id": str(annotator["id"]),
        "scoped_id": str(scoped["id"]),
        "publication_review_id": published_review_id,
        "admin_review_id": admin["review"]["id"],
        "assignment_user": extra_user["username"],
    }
