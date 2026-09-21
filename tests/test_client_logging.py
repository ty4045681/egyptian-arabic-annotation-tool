from __future__ import annotations

import json
import logging

import pytest

import server


@pytest.mark.parametrize("destination", ["override", "systemd", "local"])
def test_clientlog_appends_to_configured_destination(tmp_path, monkeypatch, destination):
    code_dir = tmp_path / "release"
    code_dir.mkdir()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setattr(server, "SCRIPT_DIR", code_dir)
    monkeypatch.delenv("ANNOTATION_CLIENT_LOG_PATH", raising=False)
    monkeypatch.delenv("LOGS_DIRECTORY", raising=False)
    if destination == "override":
        path = logs_dir / "custom.log"
        monkeypatch.setenv("ANNOTATION_CLIENT_LOG_PATH", str(path))
        monkeypatch.setenv("LOGS_DIRECTORY", str(tmp_path / "unused"))
    elif destination == "systemd":
        path = logs_dir / "client_errors.log"
        monkeypatch.setenv("LOGS_DIRECTORY", f"{logs_dir}:{tmp_path / 'unused'}")
    else:
        path = code_dir / "client_errors.log"
    path.write_text('{"previous": true}\n', encoding="utf-8")
    # Reproduce an immutable release directory; only the external log is writable.
    if destination != "local":
        code_dir.chmod(0o555)
    try:
        response = server.app.test_client().post("/api/clientlog", json={
            "type": "audio_error", "message": "تعذر تشغيل الصوت\nretry",
            "url": "/index.html",
        })
        assert response.status_code == 200
        assert response.json == {"success": True}
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"previous": True}
        entry = json.loads(lines[1])
        assert entry["type"] == "audio_error"
        assert entry["message"] == "تعذر تشغيل الصوت\nretry"
        assert entry["url"] == "/index.html"
        if destination != "local":
            assert not (code_dir / "client_errors.log").exists()
    finally:
        code_dir.chmod(0o755)


def test_clientlog_preserves_event_in_service_log_if_file_write_fails(
    tmp_path, monkeypatch, caplog,
):
    # A directory as the file target reliably raises OSError, even when run as root.
    monkeypatch.setenv("ANNOTATION_CLIENT_LOG_PATH", str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="annotator"):
        response = server.app.test_client().post("/api/clientlog", json={
            "type": "save_error", "message": "تعذر الحفظ\nretry",
            "url": "/index.html",
        })
    assert response.status_code == 200
    assert response.json == {"success": True}
    warning = next(r.getMessage() for r in caplog.records
                   if r.getMessage().startswith("clientlog write failed:"))
    entry = json.loads(warning.split("; entry=", 1)[1])
    assert entry["type"] == "save_error"
    assert entry["message"] == "تعذر الحفظ\nretry"
    assert entry["url"] == "/index.html"
