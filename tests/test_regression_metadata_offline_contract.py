"""Offline contract validation must reject malformed history before DB preflight."""
import copy

import pytest

from annotation_metadata.export_contract import MetadataImportError, parse_metadata_document
from tests.test_regression_metadata_restore_integrity import restored_document


@pytest.mark.parametrize('record_kind', ['prediction', 'review', 'version', 'user', 'event', 'document'])
def test_offline_validation_rejects_malformed_history_timestamps(restored_document, record_kind):
    document, tasks = restored_document
    document = copy.deepcopy(document)
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    targets = {
        'prediction': task['predictions'][0],
        'review': task['reviews'][0],
        'version': document['versions'][0],
        'user': document['users'][0],
        'event': document['audit_events'][0],
        'document': document,
    }
    targets[record_kind]['exported_at' if record_kind == 'document' else 'created_at'] = 'definitely-not-a-timestamp'
    with pytest.raises(MetadataImportError):
        parse_metadata_document(document)


def test_offline_validation_rejects_nan_prediction_score(restored_document):
    document, tasks = restored_document
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    task['predictions'][0]['score'] = float('nan')
    with pytest.raises(MetadataImportError):
        parse_metadata_document(document)


def test_offline_validation_rejects_declared_cross_task_prediction_input(restored_document):
    document, tasks = restored_document
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    other = next(version for version in document['versions'] if version['task_id'] == tasks[1])
    task['predictions'][0]['input_version_id'] = other['id']
    with pytest.raises(MetadataImportError):
        parse_metadata_document(document)


def test_offline_validation_rejects_declared_cross_task_review_version(restored_document):
    document, tasks = restored_document
    task = next(item for item in document['tasks'] if item['task_id'] == tasks[0])
    other = next(version for version in document['versions'] if version['task_id'] == tasks[1])
    task['reviews'][0]['version_id'] = other['id']
    with pytest.raises(MetadataImportError):
        parse_metadata_document(document)
