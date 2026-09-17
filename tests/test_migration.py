from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import pytest

import manage_state as ms
from export import export_xlsx
from manage_state import (MigrationError, build_manifest, export_json,
                          load_manifest, migrate_manifest, verify_manifest)


def write_legacy(root: Path, rel: str, *, status="pending", user="",
                 text="human", extra=True):
    audio = root / "audio" / rel
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFF-test")
    ann = root / "annotations" / Path(rel).with_suffix(".json")
    ann.parent.mkdir(parents=True, exist_ok=True)
    waveform = struct.pack("<hhhh", 1, -2, 3, -4)
    data = {
        "audio": Path(rel).name,
        "folder": Path(rel).parent.as_posix() if Path(rel).parent.as_posix() != "." else "",
        "duration": 10.5,
        "status": status,
        "skip_reasons": ["noisy"] if status == "skipped" else [],
        "preprocessed_at": "2026-01-01T00:00:00",
        "last_modified": "2026-01-02T00:00:00",
        "last_modified_by": user,
        "waveform_b64": base64.b64encode(waveform).decode(),
        "segments": [{
            "id": 1, "start": 0.0, "end": 5.0, "duration": 5.0,
            "asr_text": "asr", "text": text,
            "exclude_from_training": False,
        }],
    }
    if status == "annotated":
        data["annotated_by"] = user
    if status == "skipped":
        data["skipped_by"] = user
    if extra:
        data["category"] = "News"
        data["custom_top"] = {"keep": True}
        data["segments"][0]["custom_segment"] = 42
    ann.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def test_manifest_migrate_verify_export_and_excel(database, tmp_path):
    (tmp_path / "annotations").mkdir()
    write_legacy(tmp_path, "folder/a.wav", status="annotated", user="alice")
    write_legacy(tmp_path, "folder/b.wav", status="pending", user="bob", text="draft")
    write_legacy(tmp_path, "pipe|folder/c.wav", status="pending", user="", text="")
    legacy_pipe_json = tmp_path / "annotations" / "pipe|folder" / "c.json"
    legacy_dash_json = tmp_path / "annotations" / "pipe-folder" / "c.json"
    legacy_dash_json.parent.mkdir(parents=True)
    legacy_pipe_json.replace(legacy_dash_json)
    legacy_pipe_json.parent.rmdir()
    assignments = {
        "bob": {
            "audio": "folder/b", "rel_path": "folder/b.wav",
            "assigned_at": "2026-01-03T00:00:00", "last_activity": 1767225600,
        }
    }
    (tmp_path / "annotations" / "assignments.json").write_text(
        json.dumps(assignments), encoding="utf-8"
    )

    manifest = build_manifest(tmp_path / "annotations", tmp_path / "audio")
    assert manifest["summary"]["errors"] == 0
    assert manifest["summary"]["items"] == 3
    assert manifest["summary"]["assignments"] == 1
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    loaded = load_manifest(manifest_path)

    first_batch = migrate_manifest(loaded)
    assert migrate_manifest(loaded) == first_batch
    verification = verify_manifest(loaded)
    assert verification["ok"], verification["mismatches"]
    assert verification["status_counts"] == {"annotated": 1, "pending": 2}

    output = tmp_path / "exported"
    exported = export_json(output)
    assert exported["count"] == 3
    restored = json.loads((output / "folder" / "a.json").read_text())
    assert restored["custom_top"] == {"keep": True}
    assert restored["segments"][0]["custom_segment"] == 42
    assert restored["waveform_b64"]
    restored_assignments = json.loads((output / "assignments.json").read_text())
    assert restored_assignments["bob"]["rel_path"] == "folder/b.wav"
    assert (output / "pipe-folder" / "c.json").exists()
    assert not (output / "pipe|folder" / "c.json").exists()

    xlsx = tmp_path / "export.xlsx"
    assert export_xlsx(xlsx) == 1
    assert xlsx.exists() and xlsx.stat().st_size > 0


def test_manifest_blocks_corrupt_json_and_key_collision(tmp_path):
    (tmp_path / "annotations").mkdir()
    write_legacy(tmp_path, "a.wav")
    # Same legacy key as a.wav but different extension.
    (tmp_path / "audio" / "a.mp3").write_bytes(b"mp3")
    (tmp_path / "annotations" / "broken.json").write_text("{bad", encoding="utf-8")
    manifest = build_manifest(tmp_path / "annotations", tmp_path / "audio")
    types = {entry["type"] for entry in manifest["errors"]}
    assert "legacy_key_collision" in types
    assert "invalid_json" in types
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(MigrationError, match="blocking"):
        load_manifest(path)


def test_source_change_after_manifest_aborts(database, tmp_path):
    (tmp_path / "annotations").mkdir()
    write_legacy(tmp_path, "a.wav")
    manifest = build_manifest(tmp_path / "annotations", tmp_path / "audio")
    target = tmp_path / "annotations" / "a.json"
    data = json.loads(target.read_text())
    data["duration"] = 99
    target.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(MigrationError, match="changed after manifest"):
        migrate_manifest(manifest)


def test_new_file_after_manifest_aborts(database, tmp_path):
    (tmp_path / "annotations").mkdir()
    write_legacy(tmp_path, "a.wav")
    manifest = build_manifest(tmp_path / "annotations", tmp_path / "audio")
    write_legacy(tmp_path, "b.wav")
    with pytest.raises(MigrationError, match="annotations directory changed"):
        migrate_manifest(manifest)


def test_import_failure_rolls_back_entire_manifest(database, tmp_path, monkeypatch):
    import db

    (tmp_path / "annotations").mkdir()
    write_legacy(tmp_path, "a.wav")
    write_legacy(tmp_path, "b.wav")
    manifest = build_manifest(tmp_path / "annotations", tmp_path / "audio")
    original = ms.insert_task
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise MigrationError("injected failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(ms, "insert_task", fail_second)
    with pytest.raises(MigrationError, match="injected failure"):
        migrate_manifest(manifest)
    with db.db_conn() as conn:
        assert conn.execute("SELECT count(*) FROM annotation_tasks").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM import_batches").fetchone()[0] == 0


def test_legacy_waveform_array_round_trip(database, tmp_path):
    (tmp_path / "annotations").mkdir()
    data = write_legacy(tmp_path, "array.wav")
    path = tmp_path / "annotations" / "array.json"
    data.pop("waveform_b64")
    data["waveform"] = [0.0, 0.5, -0.5, 1.0]
    path.write_text(json.dumps(data), encoding="utf-8")
    manifest = build_manifest(tmp_path / "annotations", tmp_path / "audio")
    assert manifest["summary"]["errors"] == 0
    migrate_manifest(manifest)
    assert verify_manifest(manifest)["ok"]
    out = tmp_path / "out"
    export_json(out)
    restored = json.loads((out / "array.json").read_text())
    assert restored["waveform"] == data["waveform"]
    assert "waveform_b64" not in restored


def test_non_finite_unknown_json_is_blocked(tmp_path):
    (tmp_path / "annotations").mkdir()
    data = write_legacy(tmp_path, "nan.wav")
    data["custom_top"] = {"bad": float("nan")}
    (tmp_path / "annotations" / "nan.json").write_text(json.dumps(data))
    manifest = build_manifest(tmp_path / "annotations", tmp_path / "audio")
    assert any(error["type"] == "invalid_json" for error in manifest["errors"])


def test_post_migration_completion_exports_current_audit(database, tmp_path):
    import annotation_repository as repo

    (tmp_path / "annotations").mkdir()
    write_legacy(tmp_path, "draft.wav", user="alice", text="draft")
    manifest = build_manifest(tmp_path / "annotations", tmp_path / "audio")
    migrate_manifest(manifest)
    sid = str(ms.uuid.uuid4())
    user = repo.login("alice", sid, 1800)
    assignment = repo.claim(user["fence"])
    segments = [{
        "id": segment["id"], "start": segment["start"], "end": segment["end"],
        "duration": segment["duration"], "text": "current text",
        "exclude_from_training": False,
    } for segment in assignment["segments"]]
    repo.complete(
        user["fence"], assignment["lease_token"], assignment["revision"],
        "annotated", [], segments, str(ms.uuid.uuid4()), "complete-audit",
    )
    out = tmp_path / "current-export"
    export_json(out)
    restored = json.loads((out / "draft.json").read_text())
    assert restored["last_modified_by"] == "alice"
    assert restored["last_modified"] != "2026-01-02T00:00:00"
    assert restored["annotated_by"] == "alice"
