"""A reserved draft outside the current filters must not starve matching work."""
import pytest

import annotation_repository as repo
import db
from tests.test_scene_claims import source, user


@pytest.mark.parametrize('restriction', ['scene', 'confidence', 'scope'])
def test_nonmatching_reserved_task_does_not_block_matching_unreserved_task(seed_tasks, restriction):
    reserved, matching = seed_tasks(2)
    source(reserved, 'shopping', 'low')
    source(matching, 'airport', 'high')
    uid = user('reserved-filter-boundary')
    with db.db_conn() as conn:
        conn.execute('UPDATE annotation_tasks SET reserved_for_user_id=%s WHERE id=%s', (uid, reserved))
        if restriction == 'scope':
            conn.execute("UPDATE annotator_scene_scopes SET mode='restricted',allow_unknown=false WHERE user_id=%s", (uid,))
            conn.execute("INSERT INTO annotator_scene_access(user_id,scene_code) VALUES(%s,'airport')", (uid,))
    filters = {'source_scene': 'airport'} if restriction == 'scene' else {'source_confidence': 'high'} if restriction == 'confidence' else {}
    assignment = repo.claim(uid, **filters)
    assert assignment['task_id'] == matching
    with db.db_conn() as conn:
        assert str(conn.execute('SELECT reserved_for_user_id FROM annotation_tasks WHERE id=%s', (reserved,)).fetchone()[0]) == str(uid)
