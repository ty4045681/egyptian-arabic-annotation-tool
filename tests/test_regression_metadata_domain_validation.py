"""Dry-run validates source and review contracts as thoroughly as real writes."""
import pytest

import db
from annotation_metadata.export_metadata import MetadataImportError, build_metadata_document
from tests.test_regression_admin_metadata import source
from tests.test_regression_metadata_restore_integrity import apply_document, restored_document


def test_dry_run_rejects_invalid_new_source_confidence(restored_document, tmp_path):
    _document, tasks = restored_document
    source(tasks[0], 'airport', 'high', 'domain-validation-source')
    with db.db_conn() as conn:
        document = build_metadata_document(conn)
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    task['source_history'][0]['confidence'] = 'highest'
    with db.db_conn() as conn:
        conn.execute('DELETE FROM task_sources WHERE task_id=%s', (tasks[0],))
    with pytest.raises(MetadataImportError):
        apply_document(document, tmp_path, dry_run=True)


@pytest.mark.parametrize('status,labels', [('invalid_status', []), ('confirmed', []), ('mixed', ['airport'])])
def test_dry_run_rejects_invalid_new_review_contract(restored_document, tmp_path, status, labels):
    document, tasks = restored_document
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    review = task['reviews'][0]
    review['status'], review['scene_codes'] = status, labels
    with db.db_conn() as conn:
        conn.execute('DELETE FROM scene_reviews WHERE id=%s', (review['id'],))
    with pytest.raises(MetadataImportError):
        apply_document(document, tmp_path, dry_run=True)
