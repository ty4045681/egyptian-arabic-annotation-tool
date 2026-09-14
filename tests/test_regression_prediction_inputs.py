"""Model provenance must describe the actual input/model used before inference."""
import sys
import uuid
import db
import classify


def test_cli_prediction_records_requested_model_and_pre_call_revision(database,seed_tasks,monkeypatch):
    task=seed_tasks(1)[0]
    monkeypatch.setattr(classify,'load_config',lambda:{'asr':{'api_key':'synthetic-test-only'}})
    def inference(text,key,model):
        assert model=='acceptance-model-version'
        with db.db_conn() as conn:
            version=conn.execute("SELECT id FROM annotation_versions WHERE task_id=%s AND lifecycle='draft'",(task,)).fetchone()[0]
            conn.execute('UPDATE segments SET text=%s WHERE version_id=%s',('human changed during inference',version))
            conn.execute('UPDATE annotation_versions SET revision=revision+1 WHERE id=%s',(version,))
        return 'Airport'
    monkeypatch.setattr(classify,'call_classify',inference)
    monkeypatch.setattr(sys,'argv',['classify.py','--model','acceptance-model-version','--force'])
    classify.main()
    with db.db_conn() as conn:
        result=conn.execute('SELECT model_name,input_revision FROM task_scene_predictions WHERE task_id=%s',(task,)).fetchone()
    assert result==('acceptance-model-version',0)


def test_changed_input_produces_changed_input_digest(database,seed_tasks,monkeypatch):
    task=seed_tasks(1)[0]
    monkeypatch.setattr(classify,'load_config',lambda:{'asr':{'api_key':'synthetic-test-only'}})
    monkeypatch.setattr(classify,'call_classify',lambda *args:'Airport')
    monkeypatch.setattr(sys,'argv',['classify.py','--model','acceptance-model-version','--force'])
    classify.main()
    with db.db_conn() as conn:
        first=conn.execute('SELECT input_digest FROM task_scene_predictions WHERE task_id=%s',(task,)).fetchone()[0]
        version=conn.execute("SELECT id FROM annotation_versions WHERE task_id=%s AND lifecycle='draft'",(task,)).fetchone()[0]
        conn.execute('UPDATE segments SET text=%s WHERE version_id=%s',('completely different input text',version))
        conn.execute('UPDATE annotation_versions SET revision=revision+1 WHERE id=%s',(version,))
    classify.main()
    with db.db_conn() as conn:
        rows=conn.execute('SELECT input_digest FROM task_scene_predictions WHERE task_id=%s ORDER BY created_at,id',(task,)).fetchall()
    assert len(rows)==2
    assert rows[0][0]!=rows[1][0]
