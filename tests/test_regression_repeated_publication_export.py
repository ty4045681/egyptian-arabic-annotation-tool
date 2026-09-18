"""Multiple valid reopen/publication cycles must remain replayable as history."""
import uuid

import pytest

import annotation_repository as repo
import db
from annotation_metadata.export_metadata import build_metadata_document
from tests.test_repository import full_segments, make_user
from tests.test_regression_metadata_restore_integrity import apply_document


@pytest.mark.parametrize('with_scene_review', [False, True])
def test_multiple_reopen_cycles_export_replays_without_false_audit_conflicts(seed_tasks, tmp_path, with_scene_review):
    task = seed_tasks(1)[0]
    user, _ = make_user('multiple-publication-history')
    assignment = repo.claim(user['fence'])
    for cycle in range(3):
        review_args = {'scene_review': {'status': 'confirmed', 'scene_codes': ['airport']}} if with_scene_review else {}
        repo.complete(user['fence'], assignment['lease_token'], assignment['revision'],
                      'annotated', [], full_segments(assignment, f'cycle-{cycle}'),
                      str(uuid.uuid4()), f'cycle-{cycle}', **review_args)
        if cycle < 2:
            repo.reopen_completed(user['fence'], task, str(uuid.uuid4()))
            assignment = repo.get_assignment(user['id'])
    with db.db_conn() as conn:
        document = build_metadata_document(conn)
    assert sum(event['event_type'] == 'reopened' for event in document['audit_events']) == 2
    assert sum(event['event_type'] == 'completed' for event in document['audit_events']) == 3
    with db.db_conn() as conn:
        before = conn.execute('SELECT count(*) FROM annotation_events WHERE task_id=%s', (task,)).fetchone()[0]
    for _ in range(2):
        report = apply_document(document, tmp_path)
        assert report['ok']
        assert not any(report['inserted'].values()), 'Unchanged metadata with repeated lifecycle events must replay without inserts'
    with db.db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM annotation_events WHERE task_id=%s', (task,)).fetchone()[0] == before
