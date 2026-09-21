#!/usr/bin/env python3
"""Read-only aggregate segment profile; never emits transcripts or task identities.

Uses ANNOTATION_DB_DSN supplied through the existing environment. It does not
discover credentials, apply migrations, initialize pools, or modify the database.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SQL = """
WITH active AS (
  SELECT v.id, v.lifecycle
  FROM annotation_versions v
  JOIN annotation_tasks t ON t.id = v.task_id
  WHERE v.lifecycle = 'draft' OR v.id = t.current_published_version_id
), per_version AS (
  SELECT a.id, a.lifecycle, count(s.segment_id) AS segments,
         coalesce(max(length(s.text)), 0) AS longest_transcript_chars
  FROM active a LEFT JOIN segments s ON s.version_id = a.id
  GROUP BY a.id, a.lifecycle
)
SELECT lifecycle, count(*) AS versions, min(segments) AS min_segments,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY segments) AS p50_segments,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY segments) AS p95_segments,
       percentile_disc(0.99) WITHIN GROUP (ORDER BY segments) AS p99_segments,
       max(segments) AS max_segments,
       count(*) FILTER (WHERE segments > 100) AS versions_over_100,
       count(*) FILTER (WHERE segments > 500) AS versions_over_500,
       count(*) FILTER (WHERE segments > 1000) AS versions_over_1000,
       max(longest_transcript_chars) AS max_transcript_chars
FROM per_version GROUP BY lifecycle ORDER BY lifecycle
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dsn = os.environ.get("ANNOTATION_DB_DSN", "").strip()
    result = {"recorded_at": datetime.now(timezone.utc).isoformat(),
              "scope": "draft versions and current published versions, reported separately; no historical superseded versions",
              "sql": SQL.strip()}
    if not dsn:
        result.update(status="unavailable", reason="ANNOTATION_DB_DSN is not configured; no production distribution inferred")
    else:
        with psycopg.connect(dsn, connect_timeout=5, row_factory=dict_row) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '15s'")
            conn.execute("SET LOCAL lock_timeout = '2s'")
            result.update(status="measured", data=conn.execute(SQL).fetchall())
            conn.rollback()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"Segment profile: {result['status']}; output: {args.output}")
    return 0 if dsn else 2


if __name__ == "__main__":
    raise SystemExit(main())
