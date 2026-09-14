"""End-to-end manifest->VAD/ASR->store acceptance, with fixed local ASR responses."""
import hashlib
import json
import sys
import wave
from pathlib import Path

import numpy as np
import db
import preprocess


def test_cross_scene_same_video_is_preprocessed_once(database,tmp_path,monkeypatch):
    source=tmp_path/'source';audio=tmp_path/'website-audio'
    source.mkdir();audio.mkdir()
    frames=b'\0\0'*16000
    rows=[]
    for scene in ['01_机场','03_购物']:
        relative=f'{scene}/high/samevideo01.wav'
        path=source/relative;path.parent.mkdir(parents=True)
        with wave.open(str(path),'wb') as handle:
            handle.setnchannels(1);handle.setsampwidth(2);handle.setframerate(16000);handle.writeframes(frames)
        raw={'scene':scene,'confidence':'high','confidence_basis':'explicit source',
            'video_id':'samevideo01','url':'https://www.youtube.com/watch?v=samevideo01',
            'source_type':'explicit_video','relative_path':relative,
            'audio_filepath':f'/foreign/source/{relative}','status':'ready',
            'pcm_sha256':hashlib.sha256(frames).hexdigest(),'duration':1.0,
            'sample_rate':16000,'channels':1,'sample_width_bytes':2}
        path.with_suffix('.json').write_text(json.dumps(raw),encoding='utf-8')
        rows.append(raw)
    manifest=source/'manifest.jsonl'
    manifest.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
    monkeypatch.setattr(preprocess,'load_config',lambda:{'audio_dir':str(audio),
        'vad':{'min_speech_duration_ms':100},'asr':{'api_key':'synthetic-test-only','workers':1}})
    monkeypatch.setattr(preprocess,'get_vad',lambda:None)
    calls=[]
    def fake_vad(path,config):
        calls.append(str(path))
        return ([{'id':1,'start':0.0,'end':1.0,'duration':1.0,'asr_text':'','text':'',
                  'exclude_from_training':False}],np.zeros(16000,dtype=np.float32),16000,16000)
    monkeypatch.setattr(preprocess,'run_vad',fake_vad)
    monkeypatch.setattr(preprocess,'init_asr',lambda key:None)
    monkeypatch.setattr(preprocess,'call_asr_one',lambda i,*args:(i,'نص تجريبي'))
    monkeypatch.setattr(sys,'argv',['preprocess.py','--audio-dir',str(audio),
        '--manifest',str(manifest),'--source-audio-root',str(source),
        '--batch-code','acceptance-preprocess-2026-09-14','--workers','1'])
    preprocess.main()
    with db.db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM annotation_tasks').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM task_sources WHERE is_current').fetchone()[0]==2
        assert conn.execute('SELECT count(*) FROM annotation_tasks WHERE eligible').fetchone()[0]==1
    assert len(calls)==1


def test_second_scan_of_source_aliases_does_not_recreate(database,tmp_path,monkeypatch):
    source=tmp_path/'source';audio=tmp_path/'website-audio'
    source.mkdir();audio.mkdir()
    frames=b'\0\0'*16000
    rows=[]
    for scene in ['01_机场','03_购物']:
        relative=f'{scene}/high/samevideo01.wav'
        path=source/relative;path.parent.mkdir(parents=True)
        with wave.open(str(path),'wb') as handle:
            handle.setnchannels(1);handle.setsampwidth(2);handle.setframerate(16000);handle.writeframes(frames)
        raw={'scene':scene,'confidence':'high','confidence_basis':'explicit source',
            'video_id':'samevideo01','url':'https://www.youtube.com/watch?v=samevideo01',
            'source_type':'explicit_video','relative_path':relative,
            'audio_filepath':f'/foreign/source/{relative}','status':'ready',
            'pcm_sha256':hashlib.sha256(frames).hexdigest(),'duration':1.0,
            'sample_rate':16000,'channels':1,'sample_width_bytes':2}
        path.with_suffix('.json').write_text(json.dumps(raw),encoding='utf-8')
        rows.append(raw)
    manifest=source/'manifest.jsonl'
    manifest.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
    monkeypatch.setattr(preprocess,'load_config',lambda:{'audio_dir':str(audio),
        'vad':{'min_speech_duration_ms':100},'asr':{'api_key':'synthetic-test-only','workers':1}})
    monkeypatch.setattr(preprocess,'get_vad',lambda:None)
    calls=[]
    def fake_vad(path,config):
        calls.append(str(path))
        return ([{'id':1,'start':0.0,'end':1.0,'duration':1.0,'asr_text':'','text':'',
                  'exclude_from_training':False}],np.zeros(16000,dtype=np.float32),16000,16000)
    monkeypatch.setattr(preprocess,'run_vad',fake_vad)
    monkeypatch.setattr(preprocess,'init_asr',lambda key:None)
    monkeypatch.setattr(preprocess,'call_asr_one',lambda i,*args:(i,'نص تجريبي'))
    monkeypatch.setattr(sys,'argv',['preprocess.py','--audio-dir',str(audio),
        '--manifest',str(manifest),'--source-audio-root',str(source),
        '--batch-code','acceptance-preprocess-rescan-2026-09-14','--workers','1'])
    preprocess.main()
    first=len(calls)
    preprocess.main()
    assert len(calls)==first
    with db.db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM annotation_tasks').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM task_sources WHERE is_current').fetchone()[0]==2
        assert conn.execute('SELECT count(*) FROM annotation_tasks WHERE eligible').fetchone()[0]==1
