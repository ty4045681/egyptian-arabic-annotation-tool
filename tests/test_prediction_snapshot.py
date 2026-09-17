"""Shared classify/serializer snapshot contract: freshness, drafts, published."""
from __future__ import annotations

import sys
import uuid

import db
import classify
from annotation_metadata.predictions import (
    digest_inference_text,
    join_inference_text,
    prediction_is_stale,
    version_inference_snapshot,
)
from annotation_metadata.serializers import serialize_task_metadata
from tests.test_repository import full_segments, make_user


def _classify(monkeypatch, model="acceptance-model-version"):
    monkeypatch.setattr(classify, "load_config", lambda: {"asr": {"api_key": "synthetic-test-only"}})
    monkeypatch.setattr(classify, "call_classify", lambda *args: "Airport")
    monkeypatch.setattr(sys, "argv", ["classify.py", "--model", model, "--force"])
    classify.main()


def _draft_id(task):
    with db.db_conn() as conn:
        return conn.execute(
            "SELECT id FROM annotation_versions WHERE task_id = %s AND lifecycle = 'draft'",
            (task,),
        ).fetchone()[0]


def test_draft_prediction_fresh_then_same_version_edit_stale(database, seed_tasks, monkeypatch):
    task = seed_tasks(1)[0]
    _classify(monkeypatch)
    draft = _draft_id(task)
    with db.db_conn() as conn, conn.cursor() as cur:
        meta = serialize_task_metadata(cur, task, version_id=draft)
        pred = cur.execute(
            """SELECT model_name, score, input_digest, input_revision, input_version_id
               FROM task_scene_predictions WHERE task_id = %s""",
            (task,),
        ).fetchone()
        snap = version_inference_snapshot(cur, draft)
    assert pred[0] == "acceptance-model-version"
    assert pred[1] is None
    assert pred[2] == snap["input_digest"]
    assert pred[2] == digest_inference_text(snap["input_text"])
    assert pred[3] == 0
    assert str(pred[4]) == str(draft)
    assert meta["prediction"]["stale"] is False
    assert meta["prediction"]["score"] is None
    assert meta["prediction"]["model_name"] == "acceptance-model-version"

    with db.db_conn() as conn:
        conn.execute("UPDATE segments SET text = %s WHERE version_id = %s",
                     ("same version edit", draft))
        conn.execute("UPDATE annotation_versions SET revision = revision + 1 WHERE id = %s",
                     (draft,))
        conn.commit()
    with db.db_conn() as conn, conn.cursor() as cur:
        meta = serialize_task_metadata(cur, task, version_id=draft)
        snap = version_inference_snapshot(cur, draft)
        pred = cur.execute(
            "SELECT input_digest, input_revision FROM task_scene_predictions WHERE task_id = %s",
            (task,),
        ).fetchone()
    assert prediction_is_stale(
        {"input_version_id": str(draft), "input_digest": pred[0], "input_revision": pred[1]},
        snap,
    )
    assert meta["prediction"]["stale"] is True


def test_published_prediction_fresh_then_edit_stale(database, seed_tasks, monkeypatch):
    import annotation_repository as repo
    task = seed_tasks(1)[0]
    user, _ = make_user("snapshot-pub")
    assignment = repo.claim(user["fence"])
    assert assignment["task_id"] == task
    repo.complete(
        user["fence"], assignment["lease_token"], 0, "annotated", [],
        full_segments(assignment), str(uuid.uuid4()), "h",
    )
    _classify(monkeypatch, model="published-model")
    with db.db_conn() as conn, conn.cursor() as cur:
        published = cur.execute(
            "SELECT current_published_version_id FROM annotation_tasks WHERE id = %s",
            (task,),
        ).fetchone()[0]
        pred = cur.execute(
            """SELECT model_name, score, input_version_id, input_digest
               FROM task_scene_predictions WHERE task_id = %s""",
            (task,),
        ).fetchone()
        meta = serialize_task_metadata(
            cur, task, version_id=published, published_version_id=published,
        )
        reviews = cur.execute("SELECT count(*) FROM scene_reviews").fetchone()[0]
        sources = cur.execute("SELECT count(*) FROM task_sources").fetchone()[0]
    assert pred[0] == "published-model"
    assert pred[1] is None
    assert str(pred[2]) == str(published)
    assert meta["prediction"]["stale"] is False
    assert reviews == 0
    assert sources == 0

    with db.db_conn() as conn:
        conn.execute("UPDATE segments SET text = %s WHERE version_id = %s",
                     ("published version edited", published))
        conn.execute("UPDATE annotation_versions SET revision = revision + 1 WHERE id = %s",
                     (published,))
        conn.commit()
    with db.db_conn() as conn, conn.cursor() as cur:
        meta = serialize_task_metadata(
            cur, task, version_id=published, published_version_id=published,
        )
    assert meta["prediction"]["stale"] is True


def test_join_inference_text_matches_digest_contract():
    text = join_inference_text([("", "asr one"), ("human", "asr two"), ("", "")])
    assert text == "asr one human"
    assert digest_inference_text(text) != digest_inference_text("Airport")
