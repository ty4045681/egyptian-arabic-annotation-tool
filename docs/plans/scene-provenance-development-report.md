# Scene provenance development report

Date: 2026-09-14. Branch: `codex/scene-provenance`.

## Product defaults (accepted)

- Annotators may submit scene verification with the task; administrators can audit and correct it.
- Administrators restrict each annotator’s scene scope; annotators select within that scope.
- Existing users default to all scenes. A missing scope row is treated as `all`. An empty restricted list is not all.

## Phase C (this pass) — done

Annotator and administrator UI plus genuine browser acceptance. Phases A/B backend contracts are preserved. Metadata restore/export and scale benchmarks stay for later passes. This pass does **not** complete the overall project.

1. **Review-only save and independent save queue.** `index.html` tracks review dirty state separately from segment dirty. Each in-flight PATCH freezes a cloned `operation_id` and body; later review or transcript edits queue as a new operation. Retry replays the identical body. Review writes are omitted when `scene_review_write` is off, so ordinary transcript saves succeed. Invalid confirmed/mixed choices do not autosave. Conflict still blocks further edits.
2. **Working vs published vs reference review.** Unpublished choices show `未提交` and actual human scene labels. Correction/reopen starts a pending working review with a visible reference to the original publication. Confirmation requires exactly one scene; mixed requires at least two. Human review remains optional for completion; omitting `scene_review` still means no change.
3. **Review taxonomy vs claim scope.** Review controls use `review_taxonomy`. A restricted airport annotator can mark shopping or mixed. `features.metadata_ui` hides metadata chrome; `scene_review_write` disables review writes. Existing assignments resume after scope or selector changes. Idle copy shows claim scope and empty-pool reasons.
4. **Compact source badge and shared `static/metadata.js`.** Badge form is `机场 · 来源置信度高 · 场景待核验`. Details are a collapsed disclosure (basis, video ID, HTTP(S) links, batch, model version/freshness). Source, model, and human labels stay distinct. Historical claim evidence is labeled separately from current sources. Unsafe URLs are not linked; HTML/script in source strings render via `textContent`.
5. **Admin UI.** Source scene/confidence/batch/review/model/human filters update the task list and overview groups. Matched corpus totals use the same list predicate (`matched_count` / `matched_duration_seconds`), including dates, and are not capped at the first page. Scope save and append-only review correction send latest revision/review ID, reason, idempotency, and CSRF. Existing admin actions remain.

## Phase B — done (preserved)

Administrator metadata backend and stable UI API contracts. Phase A identity/snapshot/lease work is preserved. UI interactions, metadata restore/export, and scale benchmarks stay for later passes. This pass does **not** complete the overall project.

1. **`admin_overview` statistics.** Reused the existing contract (`source_scenes`, `confidence_buckets`). Added `source_batches` and mutually exclusive published `review_statuses`, each with distinct task counts and raw audio duration. Scene/batch groups overlap and are labeled as such. Confidence buckets use the highest **matching** evidence in the current filter. Unknown includes legacy no-source tasks plus explicit NULL scene rows, once per task. Repeated sources in one scene/batch do not double-count; two tasks with equal duration both count (`sum(duration)` after unique task rows, never `SUM(DISTINCT duration)`). Draft reviews are not published verification. One overview response uses a single `REPEATABLE READ` snapshot (`as_of` is that transaction's `now()`). `applied_filters` and `definitions` document corpus vs activity date range.
2. **Same-evidence list summaries and headlines.** Airport-medium plus shopping-high: an airport-filtered task row reports medium and the airport batch. Source scene/confidence/batch intersect on matching evidence. Overview source groups under an airport filter do not leak shopping. Detailed task metadata still lists full current provenance. Claim headlines use `claim_source_id` (and the stored claim snapshot if that row was later superseded), not the first current source with the same `scene_code`. Labels such as `N 个来源场景` count distinct scene codes.
3. **Pagination cursors.** Task and annotation cursors bind the complete normalized filter (`q`, status, folder, dates, timezone, metadata). A changed filter rejects a stale cursor. Same-filter continuation still works. `applied_filters` includes search and source fields actually used by the query.
4. **Admin lock order.** `admin_set_scene_scope` now locks `admin_sessions` (via `_begin_admin_action`) before the annotator row, matching revoke/deactivate. Idempotency, expected revision, reason, and audit are unchanged. Existing assignments still resume after a scope change. Concurrency tests cover scope vs revoke/deactivate on the same admin session and scope vs claim, including a forced interleaving through the admin-action lock. No blind deadlock retries.
5. **Stable frontend contracts.** `GET /api/scenes` keeps `scenes` as claim-allowed scenes and adds `review_taxonomy` (all active scenes) plus `claim_scenes` and `features`, so a restricted annotator can still mark shopping/mixed. The metadata blueprint no longer reverse-imports `server`; `json_object` and `admin_query_filters` are injected at registration, and write routes use `request.admin`. Auth/CSRF and existing endpoints are unchanged. Facets stay vocabulary/options.

## Phase A — done (preserved)

Backend-only. Admin UI polish and scene-only save were delivered in Phase C. Export and performance work stay for later passes.

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
| P6 UI | **Phase C done for annotator/admin UI.** Compact badge, review queue, admin filters/stats/scope/correction, and Playwright acceptance. |
| P7 tests | **Phase C browser acceptance added.** Export/restore and load-test p95 remain. |
| Export / admin remaining | Versioned metadata export extras, restore/export round-trip, and load-test p95 are unfinished. |

## Architecture

- Flask / psycopg / Pydantic v2. Package `annotation_metadata/` (adapters, contracts, queries, claiming, ingestion, processing leases, prediction snapshots, reviews, serializers, routes).
- Source evidence, model predictions, and version-linked reviews stay separate tables.
- Claim policy Strategy: `source_confidence` (default) vs `fifo`. Scope enforcement stays on when fifo is selected.
- Feature flags: `ANNOTATION_CLAIM_POLICY`, `ANNOTATION_SCENE_REVIEW_WRITE`, `ANNOTATION_METADATA_UI`, `ANNOTATION_METADATA_WRITE`.
- Admin write lock order: operation advisory lock → `admin_sessions` → annotator (when the command targets a person) → task/version in stable id order.

## Deviations (documented, not silent removals)

- `save`/`complete` still ignore unknown top-level JSON fields (legacy clients); `scene_review` objects forbid extras.
- Training `data.json` field set is unchanged; metadata is a sidecar (`manage_state.py export-metadata`).
- Phase B ran pytest with `--ignore=tests/browser`. Phase C runs the browser suite on real Chromium.
- `preprocess.py --manifest --dry-run` no longer imports torch at module load.
- Facets remain vocabulary/options. Grouped counts live on `admin_overview`, not a second statistics API.
- Overview `from`/`to` still apply to activity cards only; corpus/queue/source/confidence/review groups use the current task snapshot. `applied_filters` reports the request, and `definitions.activity_date_range` states the split.

## Test commands and results (Phase C, 2026-09-14)

Disposable pgserver PostgreSQL 16.2 and native 18.6. Isolated disposable DBs and synthetic WAV only. Chromium 151.0.7922.34 from the normal Playwright cache. No sudo/apt.

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run python -m compileall -q \
  annotation_metadata annotation_repository.py server.py
# COMPILE_OK

# Bundled pgserver PostgreSQL 16.2 — full suite including browser:
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q
# 169 passed in 54.91s

# Native PostgreSQL 18 — browser plus affected API/review/admin tests:
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  /home/ubuntu/annotation-development/scene-provenance/verify-postgres.py \
  /usr/lib/postgresql/18/bin -q --tb=line \
  tests/browser tests/test_scene_reviews.py tests/test_regression_admin_metadata.py \
  tests/test_regression_reopen_source_headline.py tests/test_admin_api.py \
  tests/test_api.py tests/test_metadata_queries.py tests/test_scene_claims.py
# ACCEPTANCE DATABASE BINARY: postgres (PostgreSQL) 18.6 (Ubuntu 18.6-0ubuntu0.26.04.1)
# 73 passed in 40.99s
```

Collected totals: **169** tests ( **13** browser, **156** non-browser ). Browser files: review-only save, save-queue/taxonomy/write-disabled, offline backoff, historical sources, corpus date/matched totals, and `test_scene_workflow.py` (claim → review/text save → complete → reopen/reference → admin filter/stats/scope/correction, plus flags, 409, same-evidence, layout). Reviewer assertions in the regression files were preserved.

Workflow screenshots were written under pytest `tmp_path/artifacts`:

- `desktop-idle-after-complete.png`
- `desktop-reopen-reference.png`
- `mobile-reopen-reference.png`
- `desktop-admin-scope.png`
- `desktop-same-evidence.png`
- `mobile-same-evidence.png`

Repo-local commands (no reviewer control path required after this commit):

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  scripts/run_pytest_with_postgres.py /usr/lib/postgresql/18/bin -q
```

Phase B non-browser results (153 passed on PG16/PG18 with `--ignore=tests/browser`) remain valid for the backend of that pass.

## Limitations / still unfinished

- Export field completeness, metadata restore/export round-trip, and versioned metadata export extras are not part of Phase C.
- 100k-task / 300k-source load test was not re-run; run `uv run python scripts/load_test_100k.py` on an isolated volume before citing p95.
- Staging `/opt/annotation_tool` was not migrated (out of bounds). Schema `[1,2,3]` applies only after `manage_state.py apply-migrations` on a target database.
- Real ASR and production crawler audio were not used; fixtures are synthetic WAV/JSONL.
- Overall scene-provenance delivery is **not** complete until export/restore and scale work finish.
