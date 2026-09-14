# Scene provenance development report

Date: 2026-09-14. Branch: `codex/scene-provenance`.

## Product defaults (accepted)

- Annotators may submit scene verification with the task; administrators can audit and correct it.
- Administrators restrict each annotator’s scene scope; annotators select within that scope.
- Existing users default to all scenes. A missing scope row is treated as `all`. An empty restricted list is not all.

## Phase E (this pass) — capacity, performance, 16/18 matrix

Final implementation pass on `codex/scene-provenance` (base `897954c`). Phases A/B/C/D behavior is preserved. Real ASR accuracy and production dataset migration remain deferred. Independent reviewer certification / GitHub / isolated preview publish are out of this pass.

1. **100k load script.** `scripts/load_test_100k.py` now seeds a uniquely named disposable database (never `DROP DATABASE` on an arbitrary configured DSN): 100,000 tasks with baseline + draft/published versions and two segments each, 300,589 current source rows (~308k including history), 20 scoped users plus dedicated claim users, high-heavy airport / high-rare shopping / narrow taxi / empty emergencies, and 20 concurrent new claims. COPY via pgserver/psycopg; no production DSN; no real audio. `--keep` retains the cluster and prints paths; default deletes the unique database and pgdata.
2. **HTTP measurement.** After pytest, the script starts Gunicorn with `gunicorn_config.py` **2 workers × 4 threads** (isolated 4-core/~8GB preview). Authentication is outside the timer. New-claim samples require distinct task IDs and `resumed=false` (20 concurrent workers; users are not reused). Release/reset is not used between new-claim samples, so later samples are not turned into reserved/resume paths. Artifacts: `docs/plans/load-test-100k/{results.json,samples.jsonl,dataset.json,explain-*.json}`.
3. **Measured gates (pgserver PostgreSQL 16.2, this host).** New-claim API p95 **172 ms** (target ≤500 ms); 50-row admin list p95 **53 ms** (≤500 ms); 100k overview p95 **1907 ms** (≤2 s). Empty-pool 409 keeps `no_matching_scene`; lock-busy 409 keeps `temporarily_busy`. 20 concurrent claims: 20 unique IDs, 213 claims/s. Claim SQL `EXPLAIN (ANALYZE, BUFFERS)` for the high band is **0.18 ms**.
4. **Fixes that made the gates real.** Batched scene counts; reservation-then-confidence-band `SKIP LOCKED` (no sort of 85k pending rows); named-scene source-first `IN` so empty emergencies is O(1); overview snapshot temp table + `work_mem`/`jit=off`/parallel workers for source groups; additive `migrations/004_claim_capacity.sql` indexes. Scope/unknown-confidence still cannot claim legacy unknown when restricted. Multiple publication cycles replay without false audit conflicts (match events by task/type/version/`created_at`).
5. **PG 16/18 matrix.** `tests/pg_runtime.py` + `scripts/run_pytest_with_postgres.py` select an explicit bindir and/or `ANNOTATION_TEST_PG_ADMIN_DSN`. Dump/restore refuse a client/server major mismatch and still refuse any non-system user schema object before `pg_restore`. GitHub Actions `.github/workflows/test.yml` runs the **full suite including Playwright** on postgres:16 and postgres:18 services (required, not skipped).
6. **CLI copy.** `restore-postgres` help matches the user-schema-object guard. DSN arguments are password-free; libpq uses `PGPASSWORD` / `~/.pgpass` / `PGSERVICEFILE`.

## Phase D — done (preserved)

Metadata export/import, training/Excel sidecars, dump/restore, and compatibility-flag fallback. Phases A/B/C are preserved.

1. **UI copy.** `reference_review` is the latest published review, which may be an administrator correction after the original submission. The badge now reads `上次已发布的核验`, not `提交时的核验`. Source/model/human separation is unchanged.
2. **Versioned metadata contract.** `manage_state.py export-metadata` writes `metadata.v1.json` as a REPEATABLE READ snapshot and replaces the file only after successful generation. The document keeps the old task/sources/source_history/prediction/published_review/scopes fields and adds complete source revisions (`raw_record`, basis, url, video, batch), all predictions (input version/revision/digest/model/prompt/nullable score), all version-linked reviews, scopes, taxonomy, batches, import runs, media identities, path aliases, version/user/admin-action identity rows, and publication-event `scene_review_id` audit references. Credentials, session cookies, and active lease tokens are not exported.
3. **Import.** `import-metadata` validates the whole document before writing. Unknown format, bad schema, dangling mappings, and identity conflicts abort the transaction with no partial metadata mutation. Repeats are idempotent and preserve timestamps/stable UUIDs. Human text, task status, and assignments are never updated. Scopes are not overwritten unless `--replace-scopes`. `--dry-run` reports. Identity match is exact UUID, or an explicit mapping file; usernames and pathnames are rejected as mapping keys.
4. **Excel and training.** Excel adds explicit source scene/confidence pairs, batches, human review status/scenes, and model prediction. Training `data.json` fields are unchanged. `scripts/export_top_annotators_tar.py` writes `scene_metadata.json` linked by task_id/segment_id/audio, marked audio-level (not inferred segment labels or scene-trainable hours), with source/model/human separate. Shared helpers live in `annotation_metadata/export_metadata.py`. Optional filters use the same-evidence `TaskFilter` and record `applied_filters`; default remains all data.
5. **Restore tests.** Synthetic old+new tasks cover cross-scene/cross-batch source revisions, two predictions, draft/published/admin-corrected reviews, scopes, identities, and audit history. Export → wipe/import on matching IDs → re-export compares histories; clone into a fresh migrated DB; explicit UUID mapping; idempotent repeat; bad mapping/conflict rollback. A separate test runs real `pg_dump --format=custom` / `pg_restore` into a disposable database and checks task/version/segment/assignment/source/review/audit counts and relationships. Binaries are the running same-major tools (pgserver 16 or `/usr/lib/postgresql/18/bin`).
6. **Compatibility flags.** Checking out `5567209` after migration 003 is not rollback. The same new-schema build with `ANNOTATION_METADATA_UI=0`, `ANNOTATION_METADATA_WRITE=0`, `ANNOTATION_SCENE_REVIEW_WRITE=0`, `ANNOTATION_CLAIM_POLICY=fifo` can health, read old+new tasks, resume an assignment, and export preserved metadata. Scope is still enforced. Omitting `scene_review` on save/complete keeps review history. Prohibited writes raise clearly. Tables are not dropped. Commands with placeholders are in `DEPLOY.md` §16. Actual production data migration remains deferred.

### Phase D closing review (this follow-up)

Typed validation, full same-ID comparison, explicit transaction ownership, module split, and production backup runtime. Reviewer assertions are preserved.

1. **Typed document contract.** `annotation_metadata/export_contract.py` uses Pydantic `StrictModel` plus `SceneReviewInput` / confidence literals. `verify-metadata` and import share `parse_metadata_document` (containers, UUIDs, timestamps, revisions, enums, finite score range, review cardinality, duplicate IDs/natural keys, declared history links including cross-task version ownership). Database identity checks stay in import preflight. Unknown contract versions and malformed records raise `MetadataImportError`, not SQL/type accidents.
2. **Complete same-ID replay.** Existing UUIDs are unchanged only when the mapped persisted record agrees on source URL/video/channel/provider/raw/current/batch/timestamps as well as scene/confidence/basis/digest; prediction input version/revision/prompt/scene/score meaning/time as well as label/model/digest; review labels/authorship/previous/superseded/operation/time as well as status/note. Batch, import-run (including checkpoint), media-identity, admin-action, and audit payloads conflict when they differ. Scope replacement stays `--replace-scopes`.
3. **Transactions.** Export/import require an idle connection (they start REPEATABLE READ) or an already-open REPEATABLE READ/SERIALIZABLE transaction. Open READ COMMITTED is refused with a clear error; caller work is never committed. CLI uses a fresh idle `db_conn()`. Exact-ID export → restore → re-export compares `comparable_document` (drops only `exported_at`) and requires a nonempty import checkpoint.
4. **Modules.** Public facade `export_metadata.py` stays stable. Typed models/validation live in `export_contract.py`; named-field planning/persistence in `export_import.py`. No plugin/event framework.
5. **Backup runtime.** No mandatory `pgserver` import. Passwords stay in `PGPASSWORD`; `sslmode`/`options` and other non-password libpq keys remain on `--dbname`. Client version comes from `pg_dump --version`. Restore refuses any non-system user schema object before invoking `pg_restore`. Reports omit credentials.

## Phase C — done (preserved)

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
| P7 tests | **Phase E:** full suite including Playwright on actual PostgreSQL 16.2 and 18.6. Load-test p95 recorded. |
| Export / admin remaining | **Phase D metadata export/import, Excel/training sidecars, dump/restore, and flag fallback are done.** Phase E capacity p95 is measured. |

## Architecture

- Flask / psycopg / Pydantic v2. Package `annotation_metadata/` (adapters, contracts, queries, claiming, ingestion, processing leases, prediction snapshots, reviews, serializers, routes).
- Source evidence, model predictions, and version-linked reviews stay separate tables.
- Claim policy Strategy: `source_confidence` (default) vs `fifo`. Scope enforcement stays on when fifo is selected.
- Feature flags: `ANNOTATION_CLAIM_POLICY`, `ANNOTATION_SCENE_REVIEW_WRITE`, `ANNOTATION_METADATA_UI`, `ANNOTATION_METADATA_WRITE`.
- Schema versions `[1,2,3,4]`. Migration 004 adds claim/list/overview indexes only.
- Claim selection: reserved row, then confidence bands with `FOR UPDATE SKIP LOCKED`; named scenes use a source-first `IN` list.
- Admin write lock order: operation advisory lock → `admin_sessions` → annotator (when the command targets a person) → task/version in stable id order.
- 4-core/~8GB Gunicorn measurement and isolated preview: 2 workers × 4 threads (`GUNICORN_WORKERS`/`GUNICORN_THREADS` or CLI). `gunicorn_config.py` still defaults to 4 workers for larger hosts.

## Deviations (documented, not silent removals)

- `save`/`complete` still ignore unknown top-level JSON fields (legacy clients); `scene_review` objects forbid extras.
- Training `data.json` field set is unchanged; scene evidence is `scene_metadata.json` from the training exporter, and the full provenance sidecar is `manage_state.py export-metadata`.
- Phase B ran pytest with `--ignore=tests/browser`. Phase C onward runs the browser suite on real Chromium; CI does not skip it.
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

## Test commands and results (Phase D closing review, 2026-09-14)

Disposable pgserver PostgreSQL 16.2 and native 18.6. Isolated disposable DBs, synthetic WAV only. No sudo/apt. No live `/opt/annotation_tool` mutation.

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run python -m compileall -q \
  annotation_metadata
# COMPILE_OK

# Bundled pgserver PostgreSQL 16.2 — full non-browser:
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q --ignore=tests/browser
# 210 passed in 32.62s

# Bundled PG16 — affected browser (review reference copy + flags/save queue):
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q --tb=line \
  tests/browser/test_scene_workflow.py \
  tests/browser/test_regression_review_only_save.py \
  tests/browser/test_regression_review_save_queue.py
# 8 passed in 21.30s

# Native PostgreSQL 18 — full non-browser:
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  scripts/run_pytest_with_postgres.py /usr/lib/postgresql/18/bin -q --tb=line \
  --ignore=tests/browser
# ACCEPTANCE DATABASE BINARY: postgres (PostgreSQL) 18.6 (Ubuntu 18.6-0ubuntu0.26.04.1)
# 210 passed in 35.99s

# Native PG18 — same affected browser files:
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  scripts/run_pytest_with_postgres.py /usr/lib/postgresql/18/bin -q --tb=line \
  tests/browser/test_scene_workflow.py \
  tests/browser/test_regression_review_only_save.py \
  tests/browser/test_regression_review_save_queue.py
# ACCEPTANCE DATABASE BINARY: postgres (PostgreSQL) 18.6 (Ubuntu 18.6-0ubuntu0.26.04.1)
# 8 passed in 21.49s
```

Non-browser total is **210**. Reviewer regressions for domain validation, history conflicts, offline contract, transaction boundary, backup runtime, restore-empty-target, checkpoint, and restore integrity passed without skips or xfails. Real custom dump/restore and training CLI round-trips remain.

Remaining after Phase D was Phase E (now recorded below).

## Test commands and results (Phase E, 2026-09-14)

Disposable pgserver PostgreSQL **16.2** and native **18.6** (`/usr/lib/postgresql/18/bin`). Isolated disposable DBs, synthetic WAV only. Chromium from the Playwright cache. No sudo/apt. No live `/opt/annotation_tool` mutation. Load test ran **after** the pytest matrix, not concurrently.

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run python -m compileall -q \
  annotation_metadata annotation_repository.py server.py \
  scripts/load_test_100k.py scripts/run_pytest_with_postgres.py tests/pg_runtime.py
# COMPILE_OK

# Bundled pgserver PostgreSQL 16.2 — full suite including browser:
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q --tb=line
# 240 passed in 71.09s   (227 non-browser + 13 browser)

# Native PostgreSQL 18.6 — full suite including browser:
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  scripts/run_pytest_with_postgres.py /usr/lib/postgresql/18/bin -q --tb=line
# ACCEPTANCE DATABASE BINARY: postgres (PostgreSQL) 18.6 (Ubuntu 18.6-0ubuntu0.26.04.1)
# 240 passed in 74.69s
```

Non-browser **227** includes Phase D export/restore/flag/offline/full-history/restore-target regressions plus Phase E claim-count, reserved-filter, unknown-confidence-scope, repeated-publication replay, dump major-mismatch, PostgreSQL runner argv, and load-harness concurrency tests. Browser **13** are the genuine Playwright files (no Chromium skip).

Capacity (Gunicorn 2×4, pgserver 16.2, 100,000 tasks / 300,589 current sources):

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run python \
  scripts/load_test_100k.py --artifact-dir docs/plans/load-test-100k
# LOAD_TEST_100K_OK
```

| Gate | p50 | p95 | p99 | Target p95 |
|---|---:|---:|---:|---:|
| New claim API (80 samples, 20 concurrent workers, distinct task IDs, no resumes) | 56 ms | **172 ms** | 184 ms | ≤500 ms |
| Admin 50-row list | 30 ms | **53 ms** | 53 ms | ≤500 ms |
| 100k-task overview | 1430 ms | **1907 ms** | 2206 ms | ≤2 s |
| 20 concurrent unique claims | 41 ms | 56 ms | 62 ms | unique IDs |

Artifacts: `docs/plans/load-test-100k/results.json`, `samples.jsonl`, `dataset.json`, `explain-claim-band_high.json` (0.184 ms), `explain-list.json` (0.076 ms), `explain-overview-matched.json` (22.5 ms). Database ~476 MiB. Deadlocks 0.

## Limitations / still unfinished

- Empty-pool `409` responses still run `pool_snapshot` (nine-scene counts + matching totals); one emergencies sample took **2.85 s**. That is not the new-claim p95 gate. Taxi-narrow 20-way claims measured p95 **2.26 s** (small candidate set under 20 concurrent workers on 8 gunicorn threads); the accepted 500 ms gate is the normal mixed-pool new-claim API.
- Staging `/opt/annotation_tool` was not migrated (out of bounds). Schema versions are `[1,2,3,4]` after `manage_state.py apply-migrations` on a target database.
- Real ASR provider accuracy and production crawler audio/dataset migration were not part of synthetic acceptance and remain deferred.
- Independent reviewer certification, GitHub publish, and isolated preview cutover are not done in this pass. This report does not mark those operational gates complete.
