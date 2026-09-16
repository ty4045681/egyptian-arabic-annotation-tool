"""A DB rollback after local-file work must preserve the recoverable audio."""
from contextlib import contextmanager
import wave

import pytest

import db
from annotation_metadata.processing import acquire_processing_lease
from preprocess_store import finalize_rejected_task


def test_rejection_database_rollback_restores_audio(seed_tasks, tmp_path):
    task = seed_tasks(1)[0]
    path = tmp_path / "audio-000.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000)
    original = path.read_bytes()

    class CommitFailureConnection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        @contextmanager
        def transaction(self):
            with self.conn.transaction():
                yield
                raise RuntimeError("injected transaction commit failure")

    with db.db_conn() as conn, conn.cursor() as cur:
        lease = acquire_processing_lease(cur, task)
        conn.commit()
        with pytest.raises(RuntimeError, match="injected transaction commit failure"):
            finalize_rejected_task(
                CommitFailureConnection(conn), rel_path=path.name,
                filename=path.name, folder="", duration=1.0, segments=[],
                processing_token=lease["token"], reason="no_speech",
                delete_path=path,
            )
        assert path.exists(), "Rollback must not leave an eligible task with irreversibly deleted audio"
        assert path.read_bytes() == original
        assert conn.execute("SELECT eligible FROM annotation_tasks WHERE id=%s", (task,)).fetchone() == (True,)
