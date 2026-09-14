from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

import db
from annotation_metadata import ingestion
from annotation_metadata.adapters import (
    AdapterError,
    adapt_crawler_record,
    load_manifest_documents,
    resolve_source_file,
    snapshot_jsonl_bytes,
)


FIXTURE_MANIFEST = Path(__file__).parent / "fixtures" / "crawler" / "manifest.jsonl"


def make_record(root, scene="01_机场", confidence="high", video="testvideo01", sample=0):
    relative = f"{scene}/{confidence}/{video}.wav"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(sample).to_bytes(2, "little", signed=True) * 16000
    import wave
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(frames)
    record = {
        "scene": scene, "confidence": confidence,
        "confidence_basis": "explicit_source_not_content_verified",
        "video_id": video, "url": f"https://www.youtube.com/watch?v={video}",
        "relative_path": relative,
        "audio_filepath": f"/foreign/crawler/0914/{relative}",
        "pcm_sha256": hashlib.sha256(frames).hexdigest(),
        "duration": 1.0, "sample_rate": 16000, "channels": 1,
        "sample_width_bytes": 2, "status": "ready",
        "source_type": "explicit_video",
        "language_verified": False, "dialect_verified": False,
        "scene_verified": False, "channel": "Fixture channel",
    }
    path.with_suffix(".json").write_text(json.dumps(record), encoding="utf-8")
    return record, path


def run_import(root, audio, raw, batch="acceptance-import-2026-09-14"):
    with db.db_conn() as conn:
        return ingestion.import_one_record(
            conn, raw=raw, batch_code=batch, source_root=root,
            audio_root=audio, sidecar_root=root,
        )


def test_jsonl_two_complete_records_and_unterminated_first_line():
    docs, *_ = load_manifest_documents(FIXTURE_MANIFEST)
    assert len(docs) == 2
    assert docs[0]["relative_path"] == "01_机场/high/fixture0000.wav"
    lines, digest, cutoff = snapshot_jsonl_bytes(b'{"incomplete":true')
    assert lines == []
    assert cutoff == 0
    assert digest == hashlib.sha256(b"").hexdigest()


def test_crawler_relative_path_keeps_three_level_depth(tmp_path):
    records = [json.loads(line) for line in FIXTURE_MANIFEST.read_text().splitlines() if line]
    source = adapt_crawler_record(records[0])
    assert source.rel_path == "01_机场/high/fixture0000.wav"
    assert source.channel_title == "Fixture channel"
    root = tmp_path / "src"
    path = root / source.rel_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    resolved = resolve_source_file(root, source, records[0])
    assert resolved.relative_to(root).as_posix() == source.rel_path


def test_malformed_checksum_is_rejected():
    records = [json.loads(line) for line in FIXTURE_MANIFEST.read_text().splitlines() if line]
    records[0]["pcm_sha256"] = "not-a-digest"
    with pytest.raises(AdapterError, match="64-character"):
        adapt_crawler_record(records[0])


def test_missing_status_is_not_ready():
    from annotation_metadata.adapters import is_ready_status
    assert is_ready_status({"scene": "01_机场"}) is False
    assert is_ready_status({"status": "ready"}) is True


def test_actual_shape_import_is_idempotent(database, tmp_path):
    root = tmp_path / "source"
    audio = tmp_path / "audio"
    raw, _path = make_record(root)
    first = run_import(root, audio, raw)
    second = run_import(root, audio, raw)
    assert first.task_id == second.task_id
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM annotation_tasks").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM task_sources").fetchone()[0] == 1
        assert conn.execute("SELECT eligible FROM annotation_tasks").fetchone()[0] is False
    assert (audio / raw["relative_path"]).is_file()


def test_malformed_sidecar_is_reported(database, tmp_path):
    root = tmp_path / "source"
    audio = tmp_path / "audio"
    raw, path = make_record(root)
    path.with_suffix(".json").write_text("{broken", encoding="utf-8")
    result = run_import(root, audio, raw)
    assert result.action in {"invalid", "conflict"}
    assert any("sidecar" in issue.code for issue in result.issues)
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM task_sources").fetchone()[0] == 0


def test_manifest_pcm_must_match_actual_audio(database, tmp_path):
    root = tmp_path / "source"
    audio = tmp_path / "audio"
    raw, path = make_record(root)
    raw["pcm_sha256"] = "f" * 64
    path.with_suffix(".json").write_text(json.dumps(raw), encoding="utf-8")
    result = run_import(root, audio, raw)
    assert result.action in {"invalid", "conflict"}
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM task_sources").fetchone()[0] == 0


def test_same_size_different_destination_is_not_silently_accepted(database, tmp_path):
    root = tmp_path / "source"
    audio = tmp_path / "audio"
    raw, path = make_record(root, sample=0)
    _other, destination = make_record(audio, sample=1)
    before = destination.read_bytes()
    assert path.stat().st_size == destination.stat().st_size
    result = run_import(root, audio, raw)
    assert result.action in {"invalid", "conflict"}
    assert destination.read_bytes() == before


@pytest.mark.parametrize("attempt", range(4))
def test_cross_batch_same_video_concurrent_import_has_one_task(database, tmp_path, attempt):
    root = tmp_path / "source"
    audio = tmp_path / "audio"
    first, _ = make_record(root, scene="01_机场")
    second, _ = make_record(root, scene="03_购物")
    barrier = Barrier(2)

    def coordinated(raw, batch):
        barrier.wait(timeout=10)
        return run_import(root, audio, raw, batch)

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [
            executor.submit(coordinated, raw, f"acceptance-parallel-batch-{i}")
            for i, raw in enumerate([first, second])
        ]
        results = [job.result(timeout=20) for job in jobs]
    assert results[0].task_id == results[1].task_id
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM annotation_tasks").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM task_media_identities").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM task_sources WHERE is_current").fetchone()[0] == 2


def test_source_record_cannot_move_between_tasks(database, seed_tasks):
    first, second = seed_tasks(2)
    source_task = first
    with db.db_conn() as conn, conn.cursor() as cur:
        from annotation_metadata.contracts import NormalizedSource
        from annotation_metadata.repository import ensure_batch, sync_source_record
        batch_id = ensure_batch(cur, "move-batch-2026-09-14")
        source = NormalizedSource(
            record_key="stable-key", scene_code="airport", confidence="high",
            content_digest="a" * 64,
        )
        sync_source_record(cur, task_id=source_task, batch_id=batch_id, source=source)
        with pytest.raises(Exception, match="another task"):
            sync_source_record(
                cur, task_id=second, batch_id=batch_id,
                source=source.model_copy(update={"content_digest": "b" * 64}),
            )
