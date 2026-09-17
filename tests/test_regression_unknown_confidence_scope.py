"""Unknown confidence is a filter, never permission to claim an unknown scene."""
import pytest

import annotation_repository as repo
import db
from tests.test_scene_claims import source, user


@pytest.mark.parametrize('operation', ['claim', 'pool'])
@pytest.mark.parametrize('reserve_legacy', [False, True])
def test_unknown_confidence_does_not_bypass_restricted_scene_scope(seed_tasks, operation, reserve_legacy):
    legacy, airport = seed_tasks(2)
    source(airport, 'airport', 'unknown')
    uid = user('airport-without-legacy-unknown')
    with db.db_conn() as conn:
        conn.execute("UPDATE annotator_scene_scopes SET mode='restricted', allow_unknown=false WHERE user_id=%s", (uid,))
        conn.execute("INSERT INTO annotator_scene_access(user_id,scene_code) VALUES(%s,'airport')", (uid,))
        if reserve_legacy:
            conn.execute('UPDATE annotation_tasks SET reserved_for_user_id=%s WHERE id=%s', (uid, legacy))
    if operation == 'claim':
        assignment = repo.claim(repo.load_session_fence(uid), source_confidence='unknown')
        assert assignment['task_id'] == airport, 'Unknown confidence must not authorize a legacy task outside the scene scope'
        assert assignment['task_id'] != legacy
    else:
        pool = repo.pool_state(uid, source_confidence='unknown')
        assert pool['available'] == 1, 'Only the allowed airport task should count; legacy unknown is forbidden for this user'
        assert pool['unknown_available'] == 0
