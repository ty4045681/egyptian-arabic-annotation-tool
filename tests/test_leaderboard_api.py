from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

import annotation_repository as repo
import server


ROOT = Path(__file__).resolve().parents[1]
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
FORBIDDEN_KEYS = {
    "id", "user_id", "task_id", "version_id", "session_id",
    "annotator_id", "operation_id", "event_id", "lease_token",
}
VENDOR_FILES = {
    "chart.umd-4.5.1.min.js":
        "84d0e233daba702b8f77d669d8c137cad36d441a10f200b6f2d3ab553bdfcf6b",
    "chartjs-plugin-annotation-3.1.0.min.js":
        "ff03786a60f6c93ccacfe0fbb6c2e3c671d7930856a3689f2f393113d8d655ec",
}


def _segments(assignment, prefix="text"):
    return [
        {
            "id": segment["id"],
            "start": segment["start"],
            "end": segment["end"],
            "duration": segment["duration"],
            "text": f"{prefix} {segment['id']}",
            "exclude_from_training": False,
        }
        for segment in assignment["segments"]
    ]


def test_leaderboard_is_public_and_keeps_legacy_field(client, database):
    response = client.get("/api/leaderboard")
    assert response.status_code == 200
    assert "leaderboard" in response.json
    assert isinstance(response.json["leaderboard"], list)
    assert response.json["leaderboard"] == []


def test_leaderboard_includes_annotation_speed_shape(
        client, database, seed_tasks, monkeypatch):
    monkeypatch.setattr(
        repo, "utcnow",
        lambda: datetime(2026, 9, 17, 9, 30, tzinfo=timezone.utc),
    )
    seed_tasks(1, duration=3600)
    user = repo.login("Mariam", str(uuid.uuid4()), 1800)
    assignment = repo.claim(user["fence"])
    repo.complete(
        user["fence"], assignment["lease_token"], assignment["revision"],
        "annotated", [], _segments(assignment, "mariam"),
        str(uuid.uuid4()), "complete-speed",
    )
    import db
    with db.db_conn() as conn:
        conn.execute(
            """UPDATE annotation_versions AS version
                  SET submitted_at = %s
                 FROM annotation_tasks AS task
                WHERE task.id = %s
                  AND version.id = task.current_published_version_id""",
            (datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc), assignment["task_id"]),
        )
        conn.commit()
    response = client.get("/api/leaderboard")
    assert response.status_code == 200
    body = response.json
    assert body["leaderboard"][0]["user"] == "Mariam"
    assert body["leaderboard"][0]["annotated"] == 1
    assert "duration_seconds" in body["leaderboard"][0]
    assert "hours" in body["leaderboard"][0]
    speed = body["annotation_speed"]
    assert speed["timezone"] == "Asia/Shanghai"
    assert speed["window_days"] == 28
    assert speed["from"] == "2026-08-21"
    assert speed["through"] == "2026-09-17"
    assert speed["generated_at"].endswith("+08:00")
    assert len(speed["days"]) == 28
    assert len(speed["weeks"]) == 4
    assert all(isinstance(day["duration_seconds"], (int, float)) for day in speed["days"])
    assert all(isinstance(day["date"], str) and len(day["date"]) == 10 for day in speed["days"])
    assert speed["days"][-1]["is_partial"] is True
    assert isinstance(speed["weeks"][0]["average_daily_duration_seconds"], (int, float))


def test_leaderboard_cache_headers_and_no_private_ids(client, database):
    response = client.get("/api/leaderboard")
    assert response.status_code == 200
    cache = response.headers.get("Cache-Control", "")
    assert "no-cache" in cache
    assert "must-revalidate" in cache
    assert "max-age=60" not in cache
    assert "stale-while-revalidate" not in cache
    payload = response.get_json()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in FORBIDDEN_KEYS, key
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            assert not UUID_RE.fullmatch(node), node

    walk(payload)
    dumped = json.dumps(payload)
    assert "task_id" not in dumped
    assert "user_id" not in dumped
    assert "submitted_at" not in dumped


def test_leaderboard_server_ttl_cache_skips_repeat_queries(client, monkeypatch):
    calls = {"n": 0}
    real = repo.public_annotation_speed

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(repo, "public_annotation_speed", wrapped)
    monkeypatch.setitem(server.app.config, "TESTING", False)
    server.clear_leaderboard_cache()
    try:
        assert client.get("/api/leaderboard").status_code == 200
        assert client.get("/api/leaderboard").status_code == 200
        assert calls["n"] == 1
    finally:
        server.clear_leaderboard_cache()
        monkeypatch.setitem(server.app.config, "TESTING", True)


def test_invalid_timezone_fails_at_startup(monkeypatch):
    real_load = server.load_config

    def bad_config():
        config = dict(real_load())
        config["public_dashboard_timezone"] = "Not/AZone"
        config.setdefault("secret_key", "test-secret")
        return config

    monkeypatch.setattr(server, "load_config", bad_config)
    try:
        with pytest.raises(RuntimeError, match="public_dashboard_timezone"):
            server._init_app_config()
    finally:
        monkeypatch.setattr(server, "load_config", real_load)
        server._init_app_config()
    assert server.resolve_public_dashboard_timezone({}) == "Asia/Shanghai"
    assert server.resolve_public_dashboard_timezone(
        {"public_dashboard_timezone": "UTC"}
    ) == "UTC"
    with pytest.raises(RuntimeError, match="public_dashboard_timezone"):
        server.resolve_public_dashboard_timezone(
            {"public_dashboard_timezone": "Not/AZone"}
        )


def test_static_vendor_whitelist_and_checksums(client, database):
    readme = (ROOT / "static/vendor/README.md").read_text(encoding="utf-8")
    for name, digest in VENDOR_FILES.items():
        path = ROOT / "static/vendor" / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == digest
        assert digest in readme
        response = client.get(f"/static/vendor/{name}")
        assert response.status_code == 200, name
        assert "javascript" in response.content_type.lower()
        assert hashlib.sha256(response.data).hexdigest() == digest
    dashboard = client.get("/static/login-dashboard.js")
    assert dashboard.status_code == 200
    assert "javascript" in dashboard.content_type.lower()
    assert client.get("/static/vendor/README.md").status_code == 404
    assert client.get("/static/vendor/chart.umd.min.js").status_code == 404
    assert client.get("/static/secret.js").status_code == 404


def test_login_page_has_no_cdn_and_loads_vendor_scripts(client, database):
    response = client.get("/login.html")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    for needle in (
        "cdn.jsdelivr", "cdnjs.cloudflare", "unpkg.com", "jsdelivr.net",
        "cdnjs.com", "googleapis.com",
    ):
        assert needle not in html
    assert "/static/vendor/chart.umd-4.5.1.min.js" in html
    assert "/static/vendor/chartjs-plugin-annotation-3.1.0.min.js" in html
    assert "/static/login-dashboard.js" in html
    dashboard_js = (ROOT / "static/login-dashboard.js").read_text(encoding="utf-8")
    assert 'cache: "no-store"' in dashboard_js
    assert "loadLeaderboard" not in html
    assert "bootstrap" not in html.lower()
    assert "tailwind" not in html.lower()
