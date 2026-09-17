"""Independent restore acceptance: reject silent history conflicts and bad links."""
import json
import uuid

import pytest

import db
import annotation_repository as repo
from annotation_metadata.export_metadata import (
    MetadataImportError, build_metadata_document, import_metadata,
)
from tests.test_repository import full_segments, make_user
from tests.test_regression_admin_metadata import source


@pytest.fixture
def restored_document(seed_tasks):
    tasks = seed_tasks(2)
    user, _ = make_user('restore-integrity-reviewer')
    assignment = repo.claim(user['fence'])
    repo.complete(user['fence'], assignment['lease_token'], 0, 'annotated', [],
                  full_segments(assignment), str(uuid.uuid4()), 'restore-integrity',
                  scene_review={'status': 'confirmed', 'scene_codes': ['airport']})
    with db.db_conn() as conn:
        version = conn.execute('SELECT current_published_version_id FROM annotation_tasks WHERE id=%s', (tasks[0],)).fetchone()[0]
        conn.execute('''INSERT INTO task_scene_predictions(task_id,input_version_id,input_revision,input_digest,model_name,predicted_label,predicted_scene_code,score,score_meaning)
                        VALUES(%s,%s,0,%s,'fixture-model','Airport','airport',0.2,'fixture probability')''', (tasks[0], version, 'a' * 64))
    with db.db_conn() as conn:
        document = build_metadata_document(conn)
    return document, tasks


def apply_document(document, tmp_path, *, dry_run=False, mapping=None):
    path = tmp_path / 'metadata.json'
    path.write_text(json.dumps(document), encoding='utf-8')
    mapping_path = None
    if mapping is not None:
        mapping_path = tmp_path / 'mapping.json'
        mapping_path.write_text(json.dumps(mapping), encoding='utf-8')
    with db.db_conn() as conn:
        return import_metadata(conn, path, dry_run=dry_run, mapping_path=mapping_path)


def test_unchanged_export_can_be_imported_repeatedly(restored_document, tmp_path):
    document, tasks = restored_document
    source(tasks[0], 'airport', 'high', 'restore-integrity-source')
    with db.db_conn() as conn:
        document = build_metadata_document(conn)
    for _ in range(2):
        report = apply_document(document, tmp_path)
        assert report['ok']
        assert not any(report['inserted'].values()), report


def test_same_review_id_with_different_labels_is_a_conflict(restored_document, tmp_path):
    document, tasks = restored_document
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    task['reviews'][0]['scene_codes'] = ['shopping']
    with pytest.raises(MetadataImportError):
        apply_document(document, tmp_path)


def test_same_prediction_id_with_different_score_is_a_conflict(restored_document, tmp_path):
    document, tasks = restored_document
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    task['predictions'][0]['score'] = 0.9
    with pytest.raises(MetadataImportError):
        apply_document(document, tmp_path)


def test_prediction_input_version_cannot_cross_task(restored_document, tmp_path):
    document, tasks = restored_document
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    other_version = next(item['id'] for item in document['versions'] if item['task_id'] == tasks[1])
    prediction = task['predictions'][0]
    prediction['id'] = str(uuid.uuid4())
    prediction['input_version_id'] = other_version
    with pytest.raises(MetadataImportError):
        apply_document(document, tmp_path)


@pytest.mark.parametrize('field,value', [('score', 1.2), ('input_revision', 'invalid-integer'), ('predicted_scene_code', 'unrecognized_scene')])
def test_dry_run_validates_new_prediction_fields(restored_document, tmp_path, field, value):
    document, tasks = restored_document
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    prediction = task['predictions'][0]
    prediction['id'] = str(uuid.uuid4())
    prediction[field] = value
    with pytest.raises(MetadataImportError):
        apply_document(document, tmp_path, dry_run=True)


def test_unknown_contract_version_is_rejected(restored_document, tmp_path):
    document, _ = restored_document
    document['contract_version'] = 999
    with pytest.raises(MetadataImportError):
        apply_document(document, tmp_path, dry_run=True)


def test_source_basis_change_is_not_hidden_by_reused_digest(restored_document, tmp_path):
    document, tasks = restored_document
    source(tasks[0], 'airport', 'high', 'source-content-conflict')
    with db.db_conn() as conn:
        document = build_metadata_document(conn)
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    task['source_history'][0]['confidence_basis'] = 'changed basis with an unchanged supplied digest'
    with pytest.raises(MetadataImportError):
        apply_document(document, tmp_path)


def test_explicit_review_mapping_replays_without_duplicate_publication_events(restored_document, tmp_path):
    document, tasks = restored_document
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    old_review = task['reviews'][0]['id']
    new_review = str(uuid.uuid4())
    mapping = {'reviews': {old_review: new_review}}
    # A matching annotation dataset with metadata/audit history restored separately.
    with db.db_conn() as conn:
        conn.execute('DELETE FROM annotation_events WHERE task_id=%s', (tasks[0],))
        conn.execute('DELETE FROM scene_reviews WHERE id=%s', (old_review,))
    first = apply_document(document, tmp_path, mapping=mapping)
    assert first['ok']
    with db.db_conn() as conn:
        before = conn.execute('SELECT count(*) FROM annotation_events WHERE task_id=%s', (tasks[0],)).fetchone()[0]
        assert before >= 1
        assert conn.execute("SELECT details->>'scene_review_id' FROM annotation_events WHERE task_id=%s AND event_type='completed'", (tasks[0],)).fetchone()[0] == new_review
    replay = apply_document(document, tmp_path, mapping=mapping)
    assert replay['ok']
    with db.db_conn() as conn:
        after = conn.execute('SELECT count(*) FROM annotation_events WHERE task_id=%s', (tasks[0],)).fetchone()[0]
    assert after == before, 'A mapped repeat import must not append duplicate publication history'
