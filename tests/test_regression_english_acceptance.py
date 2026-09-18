"""Independent acceptance for the user's ten-scene source categorization."""
import re

import pytest

import annotation_repository as repo
import db
from tests.test_scene_claims import source, user
from tests.test_admin_scope import _set_scope
from tests.test_admin_repository import _admin_session, _make_user

CATALOG = [
    ('restaurant', 'Restaurant'), ('hotel', 'Hotel'), ('taxi', 'Taxi'),
    ('airport', 'Airport'), ('clinic', 'Clinic'),
    ('tourism_information', 'Tourism information'), ('emergencies', 'Emergencies'),
    ('spoken_languages', 'Spoken languages'),
    ('business_negotiation', 'Business negotiation'), ('shopping', 'Shopping'),
]


def test_public_catalog_is_ten_english_scenes_in_requested_order(client):
    assert client.post('/api/login', json={'username': 'english-catalog'}).status_code == 200
    data = client.get('/api/scenes').json
    for field in ('scenes', 'claim_scenes', 'review_taxonomy'):
        assert [(s['code'], s['label_en']) for s in data[field]] == CATALOG
    assert [s['code'] for s in data['scope']['scenes']] == [c for c, _ in CATALOG]


def seed_mixed_sources(seed_tasks):
    tasks = seed_tasks(7)
    source(tasks[1], None, 'unknown', 'acceptance-null-unknown')
    source(tasks[2], None, 'high', 'acceptance-null-high')
    source(tasks[3], 'spoken_languages', 'low', 'acceptance-explicit-low')
    source(tasks[4], None, 'high', 'acceptance-null-high')
    source(tasks[4], 'spoken_languages', 'low', 'acceptance-explicit-low')
    source(tasks[5], None, 'low', 'acceptance-null-low')
    source(tasks[5], 'airport', 'high', 'acceptance-airport-high')
    source(tasks[6], 'airport', 'high', 'acceptance-airport-high')
    return tasks


@pytest.mark.parametrize('source_filter', ['spoken_languages', 'unknown'])
def test_source_fallback_uses_same_evidence_and_unique_groups(seed_tasks, source_filter):
    tasks = seed_mixed_sources(seed_tasks)
    filters = {'source_scene': source_filter}
    listing = repo.admin_tasks(filters)
    assert {t['task_id'] for t in listing['items']} == set(tasks[:6])
    assert all(t['source_scenes'] == ['spoken_languages'] for t in listing['items'])
    overview = repo.admin_overview(filters)
    assert overview['totals']['total_audio_count'] == 6
    assert overview['totals']['total_audio_duration_seconds'] == 60
    assert [(g['scene_code'], g['task_count']) for g in overview['source_scenes']] == [('spoken_languages', 6)]
    for confidence, expected in [('high', [tasks[2], tasks[4]]),
                                 ('low', [tasks[3], tasks[4], tasks[5]]),
                                 ('unknown', [tasks[0], tasks[1]])]:
        selection = dict(filters, source_confidence=confidence)
        assert {t['task_id'] for t in repo.admin_tasks(selection)['items']} == set(expected)
        assert repo.pool_state(user('pool-' + confidence), **selection)['available'] == len(expected)
    cross_evidence = dict(filters, source_confidence='high', batch_code='acceptance-explicit-low')
    assert repo.admin_tasks(cross_evidence)['matched_count'] == 0
    assert repo.admin_overview(cross_evidence)['totals']['total_audio_count'] == 0
    assert repo.pool_state(user('cross-evidence'), **cross_evidence)['available'] == 0
    claimed = repo.claim(repo.load_session_fence(user('priority-' + source_filter)), source_scene=source_filter)
    assert claimed['task_id'] == tasks[2]
    metadata = claimed['metadata']
    assert metadata['claim_context']['scene_code'] == 'spoken_languages'
    assert metadata['claim_context']['confidence'] == 'high'
    assert all(s['scene_code'] == 'spoken_languages' for s in metadata['sources'])
    assert 'Spoken languages' in metadata['headline']
    assert not re.search(r'[\u3400-\u9fff]', metadata['headline'])
    assert metadata['prediction'] is None
    assert metadata['scene_review']['status'] == 'pending'
    assert metadata['scene_review']['scene_codes'] == []
    # Source fallback must not create model or human labels.
    for field in ('prediction_scene', 'human_scene'):
        assert repo.admin_tasks({field: 'spoken_languages'})['matched_count'] == 0
        assert repo.admin_tasks({field: 'unknown'})['matched_count'] == 7
    with db.db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM task_sources WHERE scene_code IS NULL').fetchone()[0] == 4


@pytest.mark.parametrize('legacy_permission', [False, True])
def test_spoken_scope_includes_fallback_and_can_be_revoked(seed_tasks, legacy_permission):
    seed_mixed_sources(seed_tasks)
    annotator, _ = _make_user('spoken-scope')
    admin = _admin_session()
    result = _set_scope(admin, annotator, mode='restricted',
                        scene_codes=[] if legacy_permission else ['spoken_languages'],
                        allow_unknown=legacy_permission)
    assert 'spoken_languages' in result['scope']['scene_codes']
    assert repo.pool_state(annotator['id'])['available'] == 6
    assert {r['scene_code'] for r in repo.pool_state(annotator['id'])['by_scene']} == {'spoken_languages'}
    _set_scope(admin, annotator, mode='restricted', scene_codes=['airport'],
               allow_unknown=False, revision=1)
    pool = repo.pool_state(annotator['id'])
    assert pool['available'] == 2
    assert {r['scene_code'] for r in pool['by_scene']} == {'airport'}
    with pytest.raises(repo.ForbiddenError):
        repo.pool_state(annotator['id'], source_scene='spoken_languages')
    claimed = repo.claim(annotator['fence'])
    assert claimed['metadata']['claim_context']['scene_code'] == 'airport'


@pytest.mark.parametrize('mode', ['all', 'none'])
def test_legacy_unknown_permission_remains_accepted_for_nonrestricted_modes(database, mode):
    # The former UI allowed the independent legacy checkbox for every mode.
    annotator, _ = _make_user('legacy-mode-' + mode)
    result = _set_scope(_admin_session(), annotator, mode=mode, allow_unknown=True)
    assert result['scope']['mode'] == mode
    assert result['scope']['scene_codes'] == []


def test_existing_explicit_spoken_prediction_is_filterable_without_rewriting_history(seed_tasks):
    task = seed_tasks(1)[0]
    # Before the catalog extension this explicit model label was model-only.
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO task_scene_predictions(task_id,input_digest,model_name,predicted_label,predicted_scene_code) "
            "VALUES(%s,'english-legacy-model','fixture-model','Spoken languages',NULL)", (task,))
    assert repo.admin_tasks({'prediction_scene': 'spoken_languages'})['matched_count'] == 1
    assert repo.admin_tasks({'prediction_scene': 'unknown'})['matched_count'] == 0
    metadata = repo.claim(repo.load_session_fence(user('legacy-spoken-model')))['metadata']
    assert metadata['prediction']['predicted_label'] == 'Spoken languages'
    assert metadata['scene_review']['scene_codes'] == []
    with db.db_conn() as conn:
        assert conn.execute('SELECT predicted_scene_code FROM task_scene_predictions WHERE task_id=%s', (task,)).fetchone()[0] is None


def test_unknown_is_source_compat_only_and_invalid_elsewhere(database):
    from annotation_metadata.contracts import ClaimRequest, SceneReviewInput, TaskFilter
    from annotation_metadata.taxonomy import model_scene_code, resolve_scene_code

    assert resolve_scene_code("unknown") is None
    assert resolve_scene_code("Spoken languages") == "spoken_languages"
    assert model_scene_code("Spoken languages") == "spoken_languages"
    assert model_scene_code("unknown") is None
    assert ClaimRequest(source_scene="unknown").source_scene == "spoken_languages"
    assert TaskFilter(source_scene="unknown").source_scene == "spoken_languages"
    assert TaskFilter(prediction_scene="unknown").prediction_scene == "unknown"
    assert TaskFilter(human_scene="unknown").human_scene == "unknown"
    with pytest.raises(Exception):
        SceneReviewInput(status="confirmed", scene_codes=["unknown"])
    with pytest.raises(Exception):
        TaskFilter(prediction_scene="not-a-scene")
    with pytest.raises(Exception):
        ClaimRequest(source_scene="not-a-scene")
