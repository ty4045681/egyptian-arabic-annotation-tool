"""Concurrent input snapshot and stale preprocess caller acceptance."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import annotation_repository as repo
import classify
import db
import preprocess


def test_prediction_snapshot_revision_and_text_are_atomic(seed_tasks):
    task = seed_tasks(1)[0]
    with db.db_conn() as conn:
        version = conn.execute(
            "SELECT id FROM annotation_versions WHERE task_id=%s AND lifecycle='draft'",
            (task,),
        ).fetchone()[0]
        conn.execute("UPDATE segments SET text='revision-0' WHERE version_id=%s", (version,))
    stop = threading.Event()
    ready = threading.Event()

    def writer():
        with db.db_conn() as conn:
            number = 0
            ready.set()
            while not stop.is_set():
                number += 1
                with conn.transaction():
                    conn.execute("UPDATE segments SET text=%s WHERE version_id=%s",
                                 (f"revision-{number}", version))
                    conn.execute("UPDATE annotation_versions SET revision=%s WHERE id=%s",
                                 (number, version))

    mismatches = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        try:
            assert ready.wait(5)
            for _ in range(100):
                snapshot = classify.freeze_task_snapshot(task, model_name="acceptance-model")
                expected = f"revision-{snapshot['revision']} revision-{snapshot['revision']}"
                if snapshot["input_text"] != expected:
                    mismatches.append((snapshot["revision"], snapshot["input_text"]))
        finally:
            stop.set()
            future.result(timeout=10)
    assert not mismatches, mismatches[:5]


def test_processing_rechecks_human_protection_before_vad(seed_tasks, tmp_path, monkeypatch):
    task = seed_tasks(1)[0]
    user = repo.login("acceptance-stale-preprocess", str(uuid.uuid4()), 1800)
    assigned = repo.claim(user["fence"])
    assert assigned["task_id"] == task
    calls = []

    class VadShouldNotRun(Exception):
        pass

    def vad(*args):
        calls.append(args)
        raise VadShouldNotRun()

    monkeypatch.setattr(preprocess, "run_vad", vad)
    try:
        preprocess.process_audio_with_asr(tmp_path / "audio-000.wav", {"audio_dir": str(tmp_path)})
    except (VadShouldNotRun, repo.ConflictError):
        pass
    assert not calls, "A task claimed after the initial scan must be protected before VAD/ASR starts"
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM assignments WHERE task_id=%s", (task,)).fetchone()[0] == 1
