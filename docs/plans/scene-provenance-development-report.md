# Scene provenance development report

Date: 2026-09-14. Branch: `codex/scene-provenance`.

## Product defaults (accepted)

- Annotators may submit scene verification with the task; administrators can audit and correct it.
- Administrators restrict each annotator’s scene scope; annotators select within that scope.
- Existing users default to all scenes. A missing scope row is treated as `all`. An empty restricted list is not all.

## Completed work

| Stage | Status |
|---|---|
| P0 contracts / crawler adapters | Done. JSONL vs JSON by extension and Extra data; `relative_path` keeps scene/confidence/file depth; `channel` maps to `channel_title`; missing crawler `status` is not ready; malformed checksums are rejected. |
| P1 schema | `migrations/003_scene_provenance.sql`. Compatibility rollback is feature flags on the new schema, not `git checkout` of the old tag. |
| P2 ingestion | Manifest/sidecar import, PCM/WAV validation, exclusive audio publish, identity advisory lock + `ON CONFLICT`, metadata-only protected updates, CLI on `manage_state.py` / `preprocess.py --manifest`. |
| P3 queries / stats | Shared `TaskFilter` + same-evidence `EXISTS`. Unique task counts. Overlapping scene groups labelled. |
| P4 reviews | Draft save / complete in the same transaction; legacy omit = no change; admin correction uses `admin_action_id` (not `operations` FK); request hash includes target id. |
| P5 claiming | Forced resume first; scope on the server; high→medium→low/unknown then `allocation_order`; unknown confidence does not match high evidence; assigned matching pool is `temporarily_all_assigned`. |
| P6 UI | Annotator idle picker + banner + review controls; completed detail metadata; admin filters, scope form, review correction. |
| P7 tests | Baseline 69 plus new claim/import/review/lease/query tests. Independent PG18 runner remains the matrix entry for native 18.6. |

## Architecture

- Flask / psycopg / Pydantic v2. New package `annotation_metadata/` (adapters, contracts, queries, claiming strategy, ingestion, reviews, serializers, routes).
- Source evidence, model predictions, and version-linked reviews are separate tables.
- Claim policy Strategy: `source_confidence` (default) vs `fifo`. Scope enforcement stays on when fifo is selected.
- Feature flags: `ANNOTATION_CLAIM_POLICY`, `ANNOTATION_SCENE_REVIEW_WRITE`, `ANNOTATION_METADATA_UI`, `ANNOTATION_METADATA_WRITE`.

## Deviations (documented, not silent removals)

- `save`/`complete` still ignore unknown top-level JSON fields (legacy clients); `scene_review` objects forbid extras.
- Training `data.json` field set is unchanged; metadata is a sidecar (`manage_state.py export-metadata`).
- Playwright browser coverage is skipped if Chromium is not installed.
- `preprocess.py --manifest --dry-run` no longer imports torch at module load.

## Test commands and results (2026-09-14)

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run python -m compileall annotation_metadata annotation_repository.py preprocess_store.py server.py
# COMPILE_OK

UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q
# 101 passed, 1 skipped in 15.38s  (Playwright skipped: no Chromium in this environment)

# Native PostgreSQL 18.6 independent runner (disposable DBs):
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  /home/ubuntu/annotation-development/scene-provenance/annotation-run-independent-tests.py \
  /home/ubuntu/annotation-development/scene-provenance/annotation-independent-claims.py \
  /home/ubuntu/annotation-development/scene-provenance/annotation-independent-imports.py -q
# 15 passed in 3.14s  (7 claim + 8 import acceptance checks)
```

## Limitations

- 100k-task / 300k-source load test was not re-run in this pass after fixture work; run `uv run python scripts/load_test_100k.py` on an isolated volume before citing p95.
- Staging `/opt/annotation_tool` was not migrated (out of bounds). Schema `[1,2,3]` applies only after `manage_state.py apply-migrations` on a target database.
- Real ASR and production crawler audio were not used; fixtures are synthetic WAV/JSONL.
