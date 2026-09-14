# Scene provenance development report

Date: 2026-09-14. Branch: `codex/scene-provenance`.

## Product defaults (accepted)

- Annotators may submit scene verification with the task; administrators can audit and correct it.
- Administrators restrict each annotator’s scene scope; annotators select within that scope.
- Existing users default to all scenes. A missing scope row is treated as `all`. An empty restricted list is not all.

## Phase B (this pass) — done

Administrator metadata backend and stable UI API contracts. Phase A identity/snapshot/lease work is preserved. UI interactions, metadata restore/export, and scale benchmarks stay for later passes. This pass does **not** complete the overall project.

1. **`admin_overview` statistics.** Reused the existing contract (`source_scenes`, `confidence_buckets`). Added `source_batches` and mutually exclusive published `review_statuses`, each with distinct task counts and raw audio duration. Scene/batch groups overlap and are labeled as such. Confidence buckets use the highest **matching** evidence in the current filter. Unknown includes legacy no-source tasks plus explicit NULL scene rows, once per task. Repeated sources in one scene/batch do not double-count; two tasks with equal duration both count (`sum(duration)` after unique task rows, never `SUM(DISTINCT duration)`). Draft reviews are not published verification. One overview response uses a single `REPEATABLE READ` snapshot (`as_of` is that transaction's `now()`). `applied_filters` and `definitions` document corpus vs activity date range.
2. **Same-evidence list summaries and headlines.** Airport-medium plus shopping-high: an airport-filtered task row reports medium and the airport batch. Source scene/confidence/batch intersect on matching evidence. Overview source groups under an airport filter do not leak shopping. Detailed task metadata still lists full current provenance. Claim headlines use `claim_source_id` (and the stored claim snapshot if that row was later superseded), not the first current source with the same `scene_code`. Labels such as `N 个来源场景` count distinct scene codes.
3. **Pagination cursors.** Task and annotation cursors bind the complete normalized filter (`q`, status, folder, dates, timezone, metadata). A changed filter rejects a stale cursor. Same-filter continuation still works. `applied_filters` includes search and source fields actually used by the query.
4. **Admin lock order.** `admin_set_scene_scope` now locks `admin_sessions` (via `_begin_admin_action`) before the annotator row, matching revoke/deactivate. Idempotency, expected revision, reason, and audit are unchanged. Existing assignments still resume after a scope change. Concurrency tests cover scope vs revoke/deactivate on the same admin session and scope vs claim, including a forced interleaving through the admin-action lock. No blind deadlock retries.
5. **Stable frontend contracts.** `GET /api/scenes` keeps `scenes` as claim-allowed scenes and adds `review_taxonomy` (all active scenes) plus `claim_scenes` and `features`, so a restricted annotator can still mark shopping/mixed. The metadata blueprint no longer reverse-imports `server`; `json_object` and `admin_query_filters` are injected at registration, and write routes use `request.admin`. Auth/CSRF and existing endpoints are unchanged. Facets stay vocabulary/options.

## Phase A — done (preserved)

Backend-only. Admin UI polish, scene-only save, export, and performance work stay for later passes.

1. **Preprocess identity.** One video in airport and shopping manifests yields one task, two current source rows, one VAD/ASR invocation, one eligible task.
2. **Classification provenance.** Input snapshot frozen before inference; `score` stays null when the model returns only a label.
3. **Processing leases.** Canonical placeholder + lease before VAD/ASR; expired holder cannot store after takeover.
4. **`--delete-rejected` followup.** Shared fenced rejection path; park/restore on rollback.

## Earlier stages (still true; not re-closed here)

| Stage | Status |
|---|---|
| P0 contracts / crawler adapters | Done in earlier commits. |
| P1 schema | `migrations/003_scene_provenance.sql`. |
| P2 ingestion | Manifest/sidecar import and identity matching existed; Phase A adds alias paths, unique preprocess work list, and real leases. |
| P3 queries / stats | Shared `TaskFilter` + same-evidence `EXISTS`; Phase B finishes overview groups, matched summaries, and snapshot isolation. |
| P4 reviews | Draft save / complete in the same transaction; legacy omit = no change. Draft is not published verification in overview groups. |
| P5 claiming | Forced resume, server-side scope, confidence-then-allocation order; Phase B fixes headline evidence and scope lock order. |
| P6 UI | **Not complete.** Scene-only review save and related browser regressions stay for the UI pass. `GET /api/scenes` now exposes review taxonomy; pages are not re-verified here. |
| P7 tests | **Not complete.** Phase B added backend/API regressions. Browser coverage is intentionally out of this pass. |
| Export / admin remaining | Versioned metadata export extras, admin UX, restore/export round-trip, and load-test p95 are unfinished. |

## Architecture

- Flask / psycopg / Pydantic v2. Package `annotation_metadata/` (adapters, contracts, queries, claiming, ingestion, processing leases, prediction snapshots, reviews, serializers, routes).
- Source evidence, model predictions, and version-linked reviews stay separate tables.
- Claim policy Strategy: `source_confidence` (default) vs `fifo`. Scope enforcement stays on when fifo is selected.
- Feature flags: `ANNOTATION_CLAIM_POLICY`, `ANNOTATION_SCENE_REVIEW_WRITE`, `ANNOTATION_METADATA_UI`, `ANNOTATION_METADATA_WRITE`.
- Admin write lock order: operation advisory lock → `admin_sessions` → annotator (when the command targets a person) → task/version in stable id order.

## Deviations (documented, not silent removals)

- `save`/`complete` still ignore unknown top-level JSON fields (legacy clients); `scene_review` objects forbid extras.
- Training `data.json` field set is unchanged; metadata is a sidecar (`manage_state.py export-metadata`).
- This pass ran pytest with `--ignore=tests/browser`. Chromium is installed; browser regressions are left for the UI pass and were not skipped or weakened.
- `preprocess.py --manifest --dry-run` no longer imports torch at module load.
- Facets remain vocabulary/options. Grouped counts live on `admin_overview`, not a second statistics API.
- Overview `from`/`to` still apply to activity cards only; corpus/queue/source/confidence/review groups use the current task snapshot. `applied_filters` reports the request, and `definitions.activity_date_range` states the split.

## Test commands and results (Phase B, 2026-09-14)

Scope: non-browser only (`--ignore=tests/browser`). Disposable pgserver PostgreSQL 16.2 and native 18.6. Isolated disposable DBs and synthetic WAV only.

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run python -m compileall -q \
  annotation_metadata annotation_repository.py server.py
# COMPILE_OK

# Independent five (previously failing) plus in-repo Phase B tests:
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  /home/ubuntu/annotation-development/scene-provenance/annotation-run-independent-tests.py \
  /home/ubuntu/annotation-development/scene-provenance/annotation-independent-admin-metadata.py -q
# 5 passed in 1.72s

UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q --ignore=tests/browser
# 153 passed in 24.10s  (pgserver PostgreSQL 16.2)

# Native PostgreSQL 18 (repo-local wrapper; same args as the reviewer helper):
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  scripts/run_pytest_with_postgres.py /usr/lib/postgresql/18/bin \
  -q --ignore=tests/browser
# ACCEPTANCE DATABASE BINARY: postgres (PostgreSQL) 18.6 (Ubuntu 18.6-0ubuntu0.26.04.1)
# 153 passed in 29.17s
```

The 153 include the five independent assertions in `tests/test_regression_admin_metadata.py` (filtered confidence leakage, overview scene leakage, explicit+legacy unknown, cursor crossing `q`, claim headline same-scene evidence) plus batch/review/taxonomy/scope/concurrency/HTTP coverage. Browser tests were not run.

Repo-local commands (no reviewer control path required after this commit):

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q --ignore=tests/browser
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  scripts/run_pytest_with_postgres.py /usr/lib/postgresql/18/bin \
  -q --ignore=tests/browser
```

## Limitations / still unfinished

- Browser UI: scene-only review save and related Playwright regressions (`tests/browser/test_regression_review_only_save.py`, `tests/browser/test_regression_review_save_queue.py`) are reviewer QA for the next UI pass. They were not committed, skipped, or marked xfail.
- Admin dashboard charts/filters/UX wiring against the new overview fields is not verified in a browser.
- Export field completeness, metadata restore/export round-trip, and admin remaining gaps are not part of Phase B.
- 100k-task / 300k-source load test was not re-run; run `uv run python scripts/load_test_100k.py` on an isolated volume before citing p95.
- Staging `/opt/annotation_tool` was not migrated (out of bounds). Schema `[1,2,3]` applies only after `manage_state.py apply-migrations` on a target database.
- Real ASR and production crawler audio were not used; fixtures are synthetic WAV/JSONL.
