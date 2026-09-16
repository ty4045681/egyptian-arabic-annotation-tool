"""Same-ID history conflicts include every meaningful persisted evidence field."""
import pytest

import db
from annotation_metadata.export_metadata import MetadataImportError, build_metadata_document
from tests.test_regression_admin_metadata import source
from tests.test_regression_metadata_restore_integrity import apply_document, restored_document


@pytest.mark.parametrize('field,value', [('source_url','https://example.com/changed-source'), ('video_id','changed-video'), ('raw_record',{'changed_evidence':True}), ('is_current',False)])
def test_same_source_id_cannot_hide_changed_evidence(restored_document, tmp_path, field, value):
    _document, tasks = restored_document
    source(tasks[0], 'airport', 'high', 'full-evidence-conflict')
    with db.db_conn() as conn:
        document = build_metadata_document(conn)
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    task['source_history'][0][field] = value
    with pytest.raises(MetadataImportError):
        apply_document(document, tmp_path)


@pytest.mark.parametrize('field,value', [('input_revision',1), ('prompt_version','changed-prompt-v2'), ('score_meaning','a different score definition')])
def test_same_prediction_id_cannot_hide_changed_input_or_semantics(restored_document, tmp_path, field, value):
    document, tasks = restored_document
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    task['predictions'][0][field] = value
    with pytest.raises(MetadataImportError):
        apply_document(document, tmp_path)


def test_same_review_id_cannot_hide_changed_history_timestamp(restored_document, tmp_path):
    document, tasks = restored_document
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    task['reviews'][0]['created_at'] = '2000-01-01T00:00:00+00:00'
    with pytest.raises(MetadataImportError):
        apply_document(document, tmp_path)
