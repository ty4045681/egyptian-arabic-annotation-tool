"""Exercise the staging deployment through Nginx, including a service restart.

Run on the cloud host as root after seed_demo.py. This creates a clearly named
test annotator and completes one synthetic task; it never connects to production.
"""
from __future__ import annotations

import http.cookiejar
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8080"
ROOT = Path(__file__).resolve().parents[2]


def client():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )


def request(opener, path, body=None, *, method=None, headers=None, status=200):
    req_headers = dict(headers or {})
    if body is not None:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=req_headers, method=method,
    )
    try:
        response = opener.open(req, timeout=15)
    except urllib.error.HTTPError as exc:
        response = exc
    raw = response.read()
    assert response.status == status, (path, response.status, raw[:300])
    data = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
    return data, response.headers


def main():
    env_text = Path("/etc/annotation-staging.env").read_text()
    dsn_line = next(line for line in env_text.splitlines() if line.startswith("ANNOTATION_DB_DSN="))
    assert dsn_line.endswith("/annotation_tool_staging"), "Staging database required"
    assert json.loads((ROOT / "config.json").read_text())["audio_dir"] == "/data/annotation/staging-audio"

    anon, user = client(), client()
    health, _ = request(anon, "/api/health")
    assert health["ok"] and health["schema_versions"] == [1, 2]
    for page in ("/login.html", "/admin", "/admin.css", "/admin.js"):
        request(anon, page)
    request(anon, "/api/assignment", status=401)
    username = "cloud-smoke-" + uuid.uuid4().hex[:10]
    request(user, "/api/login", {"username": username})
    assignment, _ = request(user, "/api/assignment/claim", {})
    task = assignment["task_id"]
    assert assignment["rel_path"].startswith("synthetic-demo/")
    assert assignment["waveform_b64"]
    full, _ = request(user, f"/api/audio/{task}")
    assert full.startswith(b"RIFF") and full[8:12] == b"WAVE"
    partial, headers = request(user, f"/api/audio/{task}", headers={"Range": "bytes=10-99"}, status=206)
    assert partial == full[10:100]
    assert headers["Content-Range"].startswith("bytes 10-99/")
    request(anon, f"/api/audio/{task}", status=401)
    request(user, "/_protected_audio/" + assignment["rel_path"], status=404)
    request(user, f"/api/waveform/{task}")
    print("PASS: health, pages, login, audio bytes/Range, waveform, access control", flush=True)

    segments = [
        {"id": s["id"], "start": s["start"], "end": s["end"],
         "duration": s["duration"], "text": "Cloud deployment smoke test",
         "exclude_from_training": True}
        for s in assignment["segments"]
    ]
    save = {"lease_token": assignment["lease_token"],
            "expected_revision": assignment["revision"],
            "operation_id": str(uuid.uuid4()), "segments": segments}
    saved, _ = request(user, "/api/assignment/current", save, method="PATCH")
    replay, _ = request(user, "/api/assignment/current", save, method="PATCH")
    assert saved == replay
    stale = dict(save, operation_id=str(uuid.uuid4()))
    request(user, "/api/assignment/current", stale, method="PATCH", status=409)

    subprocess.run(["systemctl", "restart", "annotation-staging"], check=True)
    for attempt in range(30):
        try:
            request(anon, "/api/health")
            break
        except (AssertionError, urllib.error.URLError):
            time.sleep(1)
    else:
        raise AssertionError("Service did not become healthy after restart")
    resumed, _ = request(user, "/api/assignment")
    assert resumed["task_id"] == task
    assert resumed["lease_token"] == assignment["lease_token"]
    assert resumed["revision"] == saved["revision"]
    assert resumed["segments"][0]["text"] == segments[0]["text"]
    request(user, "/api/logout", {})
    request(user, "/api/login", {"username": username})
    resumed, _ = request(user, "/api/assignment")
    assert resumed["task_id"] == task
    print("PASS: save, idempotency, stale-save rejection, restart/logout recovery", flush=True)

    complete = {"lease_token": assignment["lease_token"],
                "expected_revision": saved["revision"],
                "operation_id": str(uuid.uuid4()), "segments": segments,
                "target_status": "annotated", "skip_reasons": []}
    result, _ = request(user, "/api/assignment/current/complete", complete)
    assert result["status"] == "annotated"
    records, _ = request(user, "/api/completed")
    assert records["items"][0]["task_id"] == task
    request(user, "/api/logout", {})

    admin = client()
    key = Path("/root/annotation-staging-admin-key.txt").read_text().strip()
    auth, _ = request(admin, "/api/admin/login", {"key": key})
    assert auth["authenticated"]
    for endpoint in ("session", "overview", "annotators", "tasks", "quality", "audit"):
        request(admin, "/api/admin/" + endpoint)
    request(admin, "/api/admin/logout", {}, headers={"X-CSRF-Token": auth["csrf_token"]})
    print("PASS: completion/history, admin login/dashboard/CSRF logout", flush=True)
    print(json.dumps({"ok": True, "username": username, "completed_synthetic_task": task}))


if __name__ == "__main__":
    main()
