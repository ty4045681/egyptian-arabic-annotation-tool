"""Deletion must obey the same human-protection/fencing as metadata writes."""
import uuid
import wave
from pathlib import Path

import numpy as np
import pytest

import annotation_repository as repo
from annotation_metadata.processing import acquire_processing_lease
import db
import preprocess


def wav(tmp_path):
    path = tmp_path / "audio-000.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000)
    return path


def test_explicit_rejection_delete_makes_existing_task_ineligible(seed_tasks, tmp_path, monkeypatch):
    task = seed_tasks(1)[0]
    path = wav(tmp_path)
    monkeypatch.setattr(preprocess, "run_vad", lambda *args: ([], np.zeros(16000), 16000, 16000))
    preprocess.process_audio_with_asr(path, {"audio_dir": str(tmp_path)}, delete_rejected=True)
    assert not path.exists()
    with db.db_conn() as conn:
        row = conn.execute("SELECT eligible FROM annotation_tasks WHERE id=%s", (task,)).fetchone()
    assert row == (False,), "Deleted audio must not remain eligible for annotation"


@pytest.mark.parametrize("new_owner", ["processing_successor", "human_assignment"])
def test_rejected_audio_delete_respects_changed_ownership(seed_tasks, tmp_path, monkeypatch, new_owner):
    task = seed_tasks(1)[0]
    path = wav(tmp_path)
    successor = {}

    def vad(*args):
        if new_owner == "processing_successor":
            with db.db_conn() as conn, conn.cursor() as cur:
                cur.execute("UPDATE annotation_tasks SET processing_lease_until=now()-interval '1 second' WHERE id=%s", (task,))
                successor.update(acquire_processing_lease(cur, task))
        else:
            user = repo.login("acceptance-during-vad", str(uuid.uuid4()), 1800)
            assert repo.claim(user["id"])["task_id"] == task
        return [], np.zeros(16000), 16000, 16000

    monkeypatch.setattr(preprocess, "run_vad", vad)
    try:
        preprocess.process_audio_with_asr(path, {"audio_dir": str(tmp_path)}, delete_rejected=True)
    except repo.ConflictError:
        pass
    assert path.exists(), "A worker must not delete audio after losing its lease or human protection changes"
    if successor:
        with db.db_conn() as conn:
            assert str(conn.execute("SELECT processing_token FROM annotation_tasks WHERE id=%s", (task,)).fetchone()[0]) == successor["token"]


def _content_config(tmp_path):
    return {
        "audio_dir": str(tmp_path),
        "vad": {},
        "asr": {"api_key": "synthetic-test-only", "workers": 1},
    }


def _one_segment():
    return [{
        "id": 1, "start": 0.0, "end": 1.0, "duration": 1.0,
        "asr_text": "", "text": "", "exclude_from_training": False,
    }]


def test_content_rejection_delete_makes_existing_task_ineligible(seed_tasks, tmp_path, monkeypatch):
    task = seed_tasks(1)[0]
    path = wav(tmp_path)
    monkeypatch.setattr(
        preprocess, "run_vad",
        lambda *args: (_one_segment(), np.zeros(16000), 16000, 16000),
    )
    monkeypatch.setattr(preprocess, "init_asr", lambda key: None)
    monkeypatch.setattr(preprocess, "call_asr_one", lambda i, *args: (i, "REJECT"))
    preprocess.process_audio_with_asr(path, _content_config(tmp_path), delete_rejected=True)
    assert not path.exists()
    with db.db_conn() as conn:
        eligible, extra = conn.execute(
            "SELECT eligible, extra FROM annotation_tasks WHERE id=%s", (task,),
        ).fetchone()
        sources = conn.execute(
            "SELECT count(*) FROM task_sources WHERE task_id=%s", (task,),
        ).fetchone()[0]
    assert eligible is False
    assert extra.get("preprocess_rejection") == "content_inspection"
    assert sources == 0


@pytest.mark.parametrize("new_owner", ["processing_successor", "human_assignment"])
def test_content_rejection_delete_respects_changed_ownership(
    seed_tasks, tmp_path, monkeypatch, new_owner,
):
    task = seed_tasks(1)[0]
    path = wav(tmp_path)
    successor = {}

    def vad(*args):
        if new_owner == "processing_successor":
            with db.db_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE annotation_tasks SET processing_lease_until=now()-interval '1 second' "
                    "WHERE id=%s",
                    (task,),
                )
                successor.update(acquire_processing_lease(cur, task))
        else:
            user = repo.login("acceptance-content-during-vad", str(uuid.uuid4()), 1800)
            assert repo.claim(user["id"])["task_id"] == task
        return _one_segment(), np.zeros(16000), 16000, 16000

    monkeypatch.setattr(preprocess, "run_vad", vad)
    monkeypatch.setattr(preprocess, "init_asr", lambda key: None)
    monkeypatch.setattr(preprocess, "call_asr_one", lambda i, *args: (i, "REJECT"))
    try:
        preprocess.process_audio_with_asr(
            path, _content_config(tmp_path), delete_rejected=True,
        )
    except repo.ConflictError:
        pass
    assert path.exists(), "A worker must not delete audio after losing its lease or human protection changes"
    if successor:
        with db.db_conn() as conn:
            assert str(conn.execute(
                "SELECT processing_token FROM annotation_tasks WHERE id=%s", (task,),
            ).fetchone()[0]) == successor["token"]
    else:
        with db.db_conn() as conn:
            assert conn.execute(
                "SELECT count(*) FROM assignments WHERE task_id=%s", (task,),
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT eligible FROM annotation_tasks WHERE id=%s", (task,),
            ).fetchone()[0] is True


def test_delete_rejected_default_keeps_file_and_records_rejection(seed_tasks, tmp_path, monkeypatch):
    task = seed_tasks(1)[0]
    path = wav(tmp_path)
    monkeypatch.setattr(preprocess, "run_vad", lambda *args: ([], np.zeros(16000), 16000, 16000))
    preprocess.process_audio_with_asr(path, {"audio_dir": str(tmp_path)})
    assert path.exists()
    with db.db_conn() as conn:
        eligible, extra = conn.execute(
            "SELECT eligible, extra FROM annotation_tasks WHERE id=%s", (task,),
        ).fetchone()
    assert eligible is False
    assert extra.get("preprocess_rejection") == "no_speech"


def test_rejection_delete_preserves_sources(seed_tasks, tmp_path, monkeypatch):
    task = seed_tasks(1)[0]
    path = wav(tmp_path)
    with db.db_conn() as conn:
        batch = conn.execute(
            "INSERT INTO source_batches(batch_code, name) VALUES(%s, %s) RETURNING id",
            ("rejection-delete-2026-09-14", "rejection-delete"),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO task_sources(
                   task_id, batch_id, record_key, scene_code, confidence,
                   confidence_basis, content_digest)
               VALUES (%s, %s, 'keep-source', 'airport', 'high', 'fixture', %s)""",
            (task, batch, "a" * 64),
        )
        conn.commit()
    monkeypatch.setattr(preprocess, "run_vad", lambda *args: ([], np.zeros(16000), 16000, 16000))
    preprocess.process_audio_with_asr(path, {"audio_dir": str(tmp_path)}, delete_rejected=True)
    assert not path.exists()
    with db.db_conn() as conn:
        sources = conn.execute(
            "SELECT count(*) FROM task_sources WHERE task_id=%s AND is_current",
            (task,),
        ).fetchone()[0]
        eligible, extra = conn.execute(
            "SELECT eligible, extra FROM annotation_tasks WHERE id=%s", (task,),
        ).fetchone()
    assert sources == 1
    assert eligible is False
    assert extra.get("preprocess_rejection") == "no_speech"


def test_rejected_delete_fs_failure_rolls_back_eligibility(seed_tasks, tmp_path, monkeypatch):
    task = seed_tasks(1)[0]
    path = wav(tmp_path)
    monkeypatch.setattr(preprocess, "run_vad", lambda *args: ([], np.zeros(16000), 16000, 16000))
    real_unlink = Path.unlink

    def boom(self, *args, **kwargs):
        if self.resolve() == path.resolve():
            raise OSError("disk full")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", boom)
    with pytest.raises(OSError, match="disk full"):
        preprocess.process_audio_with_asr(
            path, {"audio_dir": str(tmp_path)}, delete_rejected=True,
        )
    assert path.exists()
    with db.db_conn() as conn:
        eligible, extra, token = conn.execute(
            "SELECT eligible, extra, processing_token FROM annotation_tasks WHERE id=%s",
            (task,),
        ).fetchone()
    assert eligible is True
    assert (extra or {}).get("preprocess_rejection") in (None, "")
    assert token is None
