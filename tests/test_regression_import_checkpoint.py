"""A metadata round trip must retain an import run's saved checkpoint."""
import json
from pathlib import Path

import db
from annotation_metadata.export_metadata import build_metadata_document, import_metadata


def test_metadata_restore_keeps_import_run_checkpoint(database, tmp_path):
    checkpoint = {'processed_bytes': 12345, 'last_complete_line': 17, 'last_record_key': 'fixture-record-17'}
    with db.db_conn() as conn:
        batch = conn.execute("INSERT INTO source_batches(batch_code,name) VALUES('checkpoint-fixture','Checkpoint fixture') RETURNING id").fetchone()[0]
        run = conn.execute("INSERT INTO source_import_runs(batch_id,snapshot_sha256,status,processed_count,checkpoint) VALUES(%s,%s,'partial',17,%s::jsonb) RETURNING id", (batch, 'c' * 64, json.dumps(checkpoint))).fetchone()[0]
    with db.db_conn() as conn:
        document = build_metadata_document(conn)
    path = tmp_path / 'metadata.json'
    path.write_text(json.dumps(document), encoding='utf-8')
    with db.db_conn() as conn:
        conn.execute('DELETE FROM source_import_runs WHERE id=%s', (run,))
    with db.db_conn() as conn:
        assert import_metadata(conn, path)['ok']
    with db.db_conn() as conn:
        restored = conn.execute('SELECT checkpoint FROM source_import_runs WHERE id=%s', (run,)).fetchone()[0]
    assert restored == checkpoint
