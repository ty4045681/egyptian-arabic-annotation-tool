"""Canonical preprocess work list, placeholder lease, and exception recovery."""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

import db
import preprocess
from annotation_metadata.processing import acquire_processing_lease
from annotation_repository import ConflictError


def _wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 16000)


def test_collect_pending_audio_collapses_alias_and_inode(tmp_path):
    audio = tmp_path / "audio"
    airport = audio / "01_机场" / "high" / "samevideo01.wav"
    shopping = audio / "03_购物" / "high" / "samevideo01.wav"
    _wav(airport)
    _wav(shopping)
    known = {
        "01_机场/high/samevideo01.wav": {
            "task_id": "task-1",
            "canonical_rel_path": "01_机场/high/samevideo01.wav",
            "protected": False, "published": False, "assigned": False,
            "human_modified": False, "eligible": False, "rejection": None,
        },
        "03_购物/high/samevideo01.wav": {
            "task_id": "task-1",
            "canonical_rel_path": "01_机场/high/samevideo01.wav",
            "protected": False, "published": False, "assigned": False,
            "human_modified": False, "eligible": False, "rejection": None,
        },
    }
    pending = preprocess.collect_pending_audio(
        [airport, shopping], audio, known,
    )
    assert pending == [airport]

    other = audio / "copy" / "samevideo01.wav"
    _wav(other)
    other.unlink()
    other.hardlink_to(airport)
    pending = preprocess.collect_pending_audio(
        [airport, other], audio,
        {"01_机场/high/samevideo01.wav": known["01_机场/high/samevideo01.wav"]},
    )
    assert pending == [airport]


def test_process_audio_exception_releases_lease(database, tmp_path, monkeypatch):
    audio = tmp_path / "website-audio"
    path = audio / "clip.wav"
    _wav(path)
    monkeypatch.setattr(
        preprocess, "run_vad",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("vad failed")),
    )
    with pytest.raises(RuntimeError, match="vad failed"):
        preprocess.process_audio_with_asr(path, {
            "audio_dir": str(audio),
            "vad": {},
            "asr": {"api_key": "", "workers": 1},
        })
    with db.db_conn() as conn, conn.cursor() as cur:
        row = cur.execute(
            "SELECT id, processing_token, eligible FROM annotation_tasks WHERE rel_path = %s",
            ("clip.wav",),
        ).fetchone()
        assert row is not None
        assert row[1] is None
        assert row[2] is False
        acquire_processing_lease(cur, row[0], ttl_seconds=60)


def test_begin_processing_blocks_second_worker(database, tmp_path):
    from annotation_metadata.ingestion import begin_audio_processing
    audio = tmp_path / "audio"
    path = audio / "only.wav"
    _wav(path)
    with db.db_conn() as conn:
        first = begin_audio_processing(
            conn, rel_path="only.wav", filename="only.wav", folder="",
        )
    with db.db_conn() as conn:
        with pytest.raises(ConflictError):
            begin_audio_processing(
                conn, rel_path="only.wav", filename="only.wav", folder="",
            )
    with db.db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM annotation_tasks WHERE rel_path = %s",
            ("only.wav",),
        )
        assert cur.fetchone()[0] == 1
        from annotation_metadata.processing import complete_processing
        complete_processing(cur, first["task_id"], first["token"])
