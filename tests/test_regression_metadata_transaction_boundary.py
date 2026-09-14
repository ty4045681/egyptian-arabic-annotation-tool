"""Export, dry-run, and failed import must never commit a caller's pending work."""
import json
import uuid

import psycopg
import pytest

import db
from annotation_metadata.export_metadata import MetadataImportError, build_metadata_document, import_metadata


@pytest.mark.parametrize('operation', ['export', 'dry_run', 'invalid_import'])
def test_metadata_helper_does_not_commit_enclosing_work(database, seed_tasks, tmp_path, operation):
    task = seed_tasks(1)[0]
    with db.db_conn() as conn:
        before = conn.execute('SELECT category FROM annotation_tasks WHERE id=%s', (task,)).fetchone()[0]
    with db.db_conn() as conn:
        document = build_metadata_document(conn)
    if operation == 'invalid_import':
        document['tasks'][0]['task_id'] = str(uuid.uuid4())
    path = tmp_path / 'metadata.json'
    path.write_text(json.dumps(document), encoding='utf-8')
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE annotation_tasks SET category='uncommitted-caller-work' WHERE id=%s", (task,))
        try:
            if operation == 'export':
                build_metadata_document(conn)
            else:
                import_metadata(conn, path, dry_run=(operation == 'dry_run'))
        except (MetadataImportError, RuntimeError):
            # Requiring an idle connection with a clear error is acceptable;
            # silently committing the caller's transaction is not.
            pass
        conn.rollback()
    with db.db_conn() as conn:
        after = conn.execute('SELECT category FROM annotation_tasks WHERE id=%s', (task,)).fetchone()[0]
    assert after == before, f'{operation} committed unrelated work that the caller rolled back'
