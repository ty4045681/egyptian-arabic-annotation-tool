# Scene provenance development report

Date: 2026-09-14. Branch: `codex/scene-provenance`.

## Product defaults (accepted)

- Annotators may submit scene verification with the task; administrators can audit and correct it.
- Administrators restrict each annotator’s scene scope; annotators select within that scope.
- Existing users default to all scenes. A missing scope row is treated as `all`. An empty restricted list is not all.

## Phase A (this pass) — done

Backend-only. Admin UI polish, scene-only save, export, and performance work stay for later passes.

1. **Preprocess identity.** One video in airport and shopping manifests yields one task, two current source rows, one VAD/ASR invocation, one eligible task. Alias copies and hardlinks collapse to a unique work list keyed by canonical task identity and inode. Subsequent scans of the same aliases do not recreate tasks. Manifest metadata import still runs before skip guards. Human-modified / assigned / published tasks skip VAD and are not overwritten.
2. **Classification provenance.** `classify --model` is persisted as `model_name`. Version, revision, prepared inference text, digest, and prompt version are frozen **before** the external call and written as-is. Digest is the inference input, not the output label. Serializer freshness uses the same snapshot contract: matching draft or published input is `stale=false`; a same-version edit is `stale=true`. `score` stays null when the model returns only a label. Source rows and human reviews are not written by classify.
3. **Processing leases.** Canonical placeholder + lease are taken before VAD/ASR. A heartbeat thread renews the lease during long waits. Checkpoint and final writes go through `require_processing_token`. A missing token cannot bypass an active lease. An expired holder cannot store after takeover. Exceptions release a still-held lease so a successor can acquire. Database transactions are not held across VAD/ASR.

## Earlier stages (still true; not re-closed here)

| Stage | Status |
|---|---|
| P0 contracts / crawler adapters | Done in earlier commits. |
| P1 schema | `migrations/003_scene_provenance.sql`. |
| P2 ingestion | Manifest/sidecar import and identity matching existed; Phase A adds alias paths, unique preprocess work list, and real leases. |
| P3 queries / stats | Shared `TaskFilter` + same-evidence `EXISTS`. |
| P4 reviews | Draft save / complete in the same transaction; legacy omit = no change. |
| P5 claiming | Forced resume, server-side scope, confidence-then-allocation order. |
| P6 UI | **Not complete.** Earlier commits wired banners and controls; scene-only review save does not persist (browser regression `tests/browser/test_regression_review_only_save.py`). Next pass. |
| P7 tests | **Not complete.** Phase A added backend regressions. Browser coverage is intentionally out of this pass. |
| Export / admin remaining | Versioned metadata export extras, admin UX gaps, and load-test p95 are unfinished. |

## Architecture

- Flask / psycopg / Pydantic v2. Package `annotation_metadata/` (adapters, contracts, queries, claiming, ingestion, processing leases, prediction snapshots, reviews, serializers, routes).
- Source evidence, model predictions, and version-linked reviews stay separate tables.
- Claim policy Strategy: `source_confidence` (default) vs `fifo`. Scope enforcement stays on when fifo is selected.
- Feature flags: `ANNOTATION_CLAIM_POLICY`, `ANNOTATION_SCENE_REVIEW_WRITE`, `ANNOTATION_METADATA_UI`, `ANNOTATION_METADATA_WRITE`.

## Deviations (documented, not silent removals)

- `save`/`complete` still ignore unknown top-level JSON fields (legacy clients); `scene_review` objects forbid extras.
- Training `data.json` field set is unchanged; metadata is a sidecar (`manage_state.py export-metadata`).
- This pass ran pytest with `--ignore=tests/browser`. Chromium is installed; the review-only browser test is left failing for the UI pass and was not skipped or weakened.
- `preprocess.py --manifest --dry-run` no longer imports torch at module load.

## Test commands and results (Phase A, 2026-09-14)

Scope: non-browser only (`--ignore=tests/browser`). Disposable pgserver 16.2 and native 18.6.

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run python -m compileall -q \
  annotation_metadata classify.py preprocess.py preprocess_store.py
# COMPILE_OK

UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q \
  tests/test_regression_preprocess_identity.py \
  tests/test_regression_prediction_inputs.py \
  tests/test_regression_processing_races.py \
  tests/test_prediction_snapshot.py \
  tests/test_processing_lease.py \
  tests/test_preprocess_worklist.py
# 18 passed (pgserver PostgreSQL 16.2)

UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q --ignore=tests/browser
# 117 passed in 18.72s  (pgserver PostgreSQL 16.2)

UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  /home/ubuntu/annotation-development/scene-provenance/verify-postgres.py \
  /usr/lib/postgresql/18/bin -q --ignore=tests/browser
# ACCEPTANCE DATABASE BINARY: postgres (PostgreSQL) 18.6
# 117 passed in 21.14s
```

Prior independent claim/import/review checks (19) were already green before this pass and were not re-run as a separate external harness here; the in-repo `tests/test_source_import.py`, `tests/test_scene_claims.py`, and `tests/test_scene_reviews.py` are included in the 117.

## Limitations / still unfinished

- Browser UI: scene-only review save does not persist; do not treat P6 as done.
- Export field completeness and admin remaining gaps are not part of Phase A.
- 100k-task / 300k-source load test was not re-run; run `uv run python scripts/load_test_100k.py` on an isolated volume before citing p95.
- Staging `/opt/annotation_tool` was not migrated (out of bounds). Schema `[1,2,3]` applies only after `manage_state.py apply-migrations` on a target database.
- Real ASR and production crawler audio were not used; fixtures are synthetic WAV/JSONL.
