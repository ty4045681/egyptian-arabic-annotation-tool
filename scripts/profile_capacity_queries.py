"""Independently profile actual repository queries on a retained synthetic load DB.

Diagnostic timings include instrumentation overhead; HTTP acceptance is run
separately without instrumentation. Never accepts a production database.

Run scripts/load_test_100k.py --keep first, then:
    uv run python scripts/profile_capacity_queries.py --results RESULTS_JSON --output PLANS_JSON
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

import psycopg
from psycopg_pool import ConnectionPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    benchmark = json.loads(args.results.read_text())
    env = benchmark['environment']
    database = env['database']
    pgdata = Path(env['pgdata']).resolve()
    assert re.fullmatch(r'load_e_[0-9a-f]{32}', database), 'Only a generated load database is permitted'
    assert pgdata.parent.name.startswith('annotation-load-100k-') and pgdata.name == 'pgdata'
    assert benchmark.get('retained'), 'Run the independent benchmark with --keep first'
    assert (pgdata / 'PG_VERSION').is_file(), 'The retained synthetic cluster must still exist'
    from tests.pg_runtime import apply_pgserver_bindir
    apply_pgserver_bindir(env['bindir'])
    import pgserver
    cluster = pgserver.get_server(pgdata, cleanup_mode=None)
    dsn = cluster.get_uri(database=database)
    with psycopg.connect(dsn) as conn:
        assert conn.execute('SELECT current_database()').fetchone()[0] == database
        assert conn.execute('SELECT count(*) FROM annotation_tasks').fetchone()[0] == 100000
        uid = conn.execute("SELECT id FROM annotators WHERE username='load-claim-249'").fetchone()[0]
    os.environ['ANNOTATION_DB_DSN'] = dsn
    records = []
    operation = None

    class ProfileCursor(psycopg.Cursor):
        def execute(self, query, params=None, *positional, **kwargs):
            sql = query.as_string(self.connection) if hasattr(query, 'as_string') else query
            if isinstance(sql, bytes):
                sql = sql.decode('utf-8')
            started = time.perf_counter()
            result = super().execute(query, params, *positional, **kwargs)
            elapsed = (time.perf_counter() - started) * 1000
            item = {'operation': operation, 'sql': sql, 'params': params,
                    'execute_ms': elapsed, 'rows': self.rowcount}
            statement = sql.strip()
            select = None
            if re.match(r'^(SELECT|WITH)\b', statement, re.I):
                select = statement
            elif re.match(r'^CREATE\s+TEMP(?:ORARY)?\s+TABLE\b', statement, re.I):
                match = re.search(r'\bAS\s+(SELECT\b.*)', statement, re.I | re.S)
                if match:
                    select = match.group(1)
                    item['plan_note'] = 'Actual SELECT used by CREATE TEMP TABLE AS; creation latency is recorded separately above'
            if select:
                # A base Cursor prevents recursive instrumentation and preserves
                # this cursor's result for the caller's subsequent fetch.
                with psycopg.Cursor(self.connection) as diagnostic:
                    diagnostic.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + select, params)
                    item['plan'] = diagnostic.fetchone()[0]
            records.append(item)
            return result

    import db
    import annotation_repository as repo
    db.close_pool()
    db._pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=1,
                              kwargs={'cursor_factory': ProfileCursor})
    outputs = {}
    try:
        for name, call in (
            ('admin_overview', lambda: repo.admin_overview()),
            ('admin_tasks_50', lambda: repo.admin_tasks(limit=50)),
            ('pool_empty_emergencies', lambda: repo.pool_state(str(uid), source_scene='emergencies')),
        ):
            operation = name
            started = time.perf_counter()
            payload = call()
            outputs[name] = {'instrumented_wall_ms': (time.perf_counter() - started) * 1000,
                             'payload': payload}
    finally:
        db.close_pool()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'note': __doc__, 'environment': env,
                                      'outputs': outputs, 'statements': records},
                                     indent=2, default=str) + '\n')
    for name in outputs:
        selected = [r for r in records if r['operation'] == name]
        largest = sorted(selected, key=lambda r: r['execute_ms'], reverse=True)[:4]
        print(json.dumps({'operation': name, 'statements': len(selected),
                          'top_queries': [{'ms': round(r['execute_ms'], 2),
                                           'sql_start': r['sql'].strip()[:130]} for r in largest]}))
    print(json.dumps({'artifact': str(args.output), 'actual_repository_queries': True}))


if __name__ == '__main__':
    main()
