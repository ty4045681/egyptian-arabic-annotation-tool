"""Shared Playwright fixtures for scene-provenance browser acceptance."""
from __future__ import annotations

import hashlib
import threading
import wave
from pathlib import Path

import pytest

from werkzeug.serving import make_server

import db
import server

ADMIN_KEY = "test-admin-key-0123456789abcdef-0123456789abcdef"
ADMIN_KEY_DIGEST = hashlib.sha256(ADMIN_KEY.encode()).hexdigest()


def write_silence_wav(path: Path, seconds: float = 10.0, rate: int = 16000) -> None:
    frames = int(seconds * rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\0\0" * frames)


def insert_source(task_id, *, scene, confidence, batch, basis="explicit source fixture",
                  url="https://www.youtube.com/watch?v=fixture0001", video_id="fixture0001"):
    with db.db_conn() as conn:
        batch_id = conn.execute(
            "INSERT INTO source_batches(batch_code,name) VALUES(%s,%s) "
            "ON CONFLICT(batch_code) DO UPDATE SET name=EXCLUDED.name RETURNING id",
            (batch, batch),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO task_sources(
                   task_id,batch_id,record_key,scene_code,confidence,confidence_basis,
                   source_url,video_id,content_digest
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (task_id, batch_id, f"{batch}-{scene}-{confidence}", scene, confidence,
             basis, url, video_id, "0" * 64),
        )


def configure_admin(monkeypatch) -> None:
    monkeypatch.setenv("ANNOTATION_ADMIN_KEY_SHA256", ADMIN_KEY_DIGEST)
    monkeypatch.setitem(server.app.config, "ADMIN_KEY_SHA256", ADMIN_KEY_DIGEST)
    monkeypatch.setitem(server.app.config, "ADMIN_KEY_ID", "primary")
    monkeypatch.setitem(server.app.config, "ADMIN_SESSION_COOKIE_NAME", "admin_session")
    monkeypatch.setitem(server.app.config, "ADMIN_SESSION_COOKIE_SECURE", False)
    monkeypatch.setitem(server.app.config, "ADMIN_SESSION_IDLE_SECONDS", 1800)
    monkeypatch.setitem(server.app.config, "ADMIN_SESSION_ABSOLUTE_SECONDS", 28800)


def start_app_server():
    httpd = make_server("127.0.0.1", 0, server.app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, f"http://127.0.0.1:{httpd.server_port}"


def stop_app_server(httpd, thread) -> None:
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.fixture
def live_site(client, seed_tasks, tmp_path, monkeypatch):
    """One airport/high task plus a live threaded webserver. Used by reviewer regressions."""
    configure_admin(monkeypatch)
    task = seed_tasks(1)[0]
    audio = Path(server.app.config["AUDIO_DIR"]) / "audio-000.wav"
    write_silence_wav(audio)
    insert_source(task, scene="airport", confidence="high", batch="acceptance-browser-batch")
    httpd, thread, url = start_app_server()
    yield url, task
    stop_app_server(httpd, thread)


@pytest.fixture
def provenance_site(client, seed_tasks, tmp_path, monkeypatch):
    """Richer fixture for end-to-end annotator/admin browser acceptance."""
    configure_admin(monkeypatch)
    tasks = seed_tasks(2)
    audio_dir = Path(server.app.config["AUDIO_DIR"])
    write_silence_wav(audio_dir / "audio-000.wav")
    write_silence_wav(audio_dir / "audio-001.wav")
    insert_source(
        tasks[0], scene="airport", confidence="high", batch="acceptance-browser-batch",
        basis="<script>alert('xss-basis')</script>",
        url="https://www.youtube.com/watch?v=fixture0001",
        video_id="<img src=x onerror=alert(1)>",
    )
    insert_source(
        tasks[0], scene="shopping", confidence="low", batch="acceptance-browser-xss",
        basis="second source with unsafe url",
        url="javascript:alert('xss-link')",
        video_id="unsafe-video",
    )
    insert_source(
        tasks[1], scene="airport", confidence="medium", batch="acceptance-same-evidence-airport",
    )
    insert_source(
        tasks[1], scene="shopping", confidence="high", batch="acceptance-same-evidence-shopping",
    )
    with db.db_conn() as conn:
        conn.execute(
            """INSERT INTO task_scene_predictions(
                   task_id, input_digest, model_name, predicted_label, predicted_scene_code
               ) VALUES (%s, %s, %s, %s, %s)""",
            (tasks[0], "browser-pred", "fixture-model", "Airport", "airport"),
        )
    httpd, thread, url = start_app_server()
    yield {"url": url, "tasks": tasks, "admin_key": ADMIN_KEY}
    stop_app_server(httpd, thread)


@pytest.fixture
def cross_check_site(client, seed_tasks, monkeypatch):
    """Live server for cross-check UI tests. Callers seed rounds as needed."""
    configure_admin(monkeypatch)
    httpd, thread, url = start_app_server()
    yield {"url": url, "client": client, "seed_tasks": seed_tasks, "admin_key": ADMIN_KEY}
    stop_app_server(httpd, thread)
