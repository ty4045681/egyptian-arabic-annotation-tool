# Cross-annotation quality backend development report

Date: 2026-09-19. Branch: `0918` (this pass:
`execute-plan/d2000a7a-pr-8-acceptance-matrix-capacity-and-development-report`).
Base: `6419a6b15e3900bbb6b5930fa8879f64e8c928e9`.

Backend-only delivery. Administrators can run the whole quality loop through
HTTP APIs. This round does **not** add a QC page, claim-mode chrome, or any
HTML/CSS/JavaScript change. Missing frontend UI is intentional and is not a
substitute for unfinished backend work.

## What shipped

Schema 008 plus `annotation_quality/` and the existing claim/complete/admin
export paths. Default new-claim sampling is **off**. Word-difference threshold
is fixed at 1000 bps (`> 10%`); it is not a writable setting.

| Area | Delivered |
|---|---|
| Schema 008 | `cross_check_settings` singleton (`enabled=false`, `sampling_rate_bps=1000`), `cross_check_rounds`, `task_annotation_participants` backfill, version `purpose` / `credited_annotator_id` / `published_by_admin_action_id`, assignment `cross_check` mode, unique open-round and original-version indexes. Migrations 001–007 unchanged. |
| Claim | Random split on **new** claims only; in-flight assignments always resume. Historical and new `annotated` publications are the same pool. Scene/scope/batch/confidence filters and “someone else” participation rules are server-side. Blind baseline copy: empty text, no original review, no prediction, no original author. |
| Submit | Same save/complete/session/revision/idempotency path. Merged draft compared with `worddiff_v1`. `passed` keeps original published pointer; `awaiting_review` releases the assignment but blocks training export. |
| Admin APIs | Settings GET/PUT, queue list/detail, decision `original` / `secondary` / `edited`, cancel in-progress only, quality counts, CSRF + admin session, optimistic revision, operation-id replay. |
| Annotator self | `GET /api/cross-checks/mine` and `GET /api/cross-checks/<id>/submission` return the caller’s secondary result only. |
| Lifecycle | Reopen/revoke/restore/deactivate/release/abandon/session takeover cancel or invalidate open rounds instead of leaving orphan drafts. `awaiting_review` is not unlocked by cancel, disable, logout, or session expiry. |
| Stats / export | Unique-task corpus duration (A then B on one 60s audio stays 60s). Cross-check workload is separate. Open rounds (`in_progress`, `awaiting_review`) are excluded from training tar selection. Excel stays one row per audio and adds eligibility columns. JSON/DB backup keep pending-review rows. |
| Comparison | Pure `worddiff_v1`: integer rate, exactly 10% does not queue on words, skip/empty/Bad Quality conflicts queue, over-budget does not invent distance 0. |

Health `schema_versions` is `[1, 2, 3, 4, 5, 6, 7, 8]`. Confirmed in
`README.md` (login-page stats section) and `DEPLOY.md` §§18–20.

`git diff --name-only 6419a6b -- '*.html' '*.css' '*.js' static/` is empty:
`index.html`, `admin.html`, `login.html`, `completed.html`, `admin.js`,
`admin.css`, and `static/` were not modified.

## Schema versions

Applied and expected versions are `[1, 2, 3, 4, 5, 6, 7, 8]`.

| Version | File | Role |
|---|---|---|
| 1 | `001_initial.sql` | Tasks, versions, assignments |
| 2 | `002_admin.sql` | Admin sessions, revoke/deactivate |
| 3 | `003_scene_provenance.sql` | Sources, reviews, scopes |
| 4 | `004_claim_capacity.sql` | Claim/list/overview indexes |
| 5 | `005_ten_scene_catalog.sql` | Spoken languages catalog |
| 6 | `006_session_takeover.sql` | Session generation / takeover |
| 7 | `007_annotation_speed_indexes.sql` | Public 28-day speed indexes |
| 8 | `008_cross_annotation_quality.sql` | Cross-check |

Gunicorn only **checks** schema. It does not migrate. `GET /api/health` on a
fresh test database in this session:

```json
{
  "ok": true,
  "schema_versions": [1, 2, 3, 4, 5, 6, 7, 8],
  "timestamp": "2026-09-19T05:16:19"
}
```

## HTTP API examples

Captured from the Flask test client against an isolated PostgreSQL 16.2
database (same patterns as `tests/test_cross_check_admin.py` and
`tests/test_cross_check_submit.py`). Admin writes need an admin session cookie
plus `X-CSRF-Token`. Annotator writes need an annotator session cookie.
UUIDs below are from that run.

### Settings

`GET /api/admin/cross-check-settings` (admin session):

```json
{
  "enabled": false,
  "sampling_rate_bps": 1000,
  "revision": 0,
  "word_difference_threshold_bps": 1000,
  "comparison_version": "worddiff_v1",
  "updated_at": "2026-09-19T05:16:18.959051+08:00",
  "updated_by_admin_action_id": null,
  "action_id": null
}
```

`PUT /api/admin/cross-check-settings` with `X-CSRF-Token`:

```json
{
  "operation_id": "2d614e8e-d3fa-41ae-bcf4-ac3cdee0f14b",
  "expected_revision": 0,
  "enabled": true,
  "sampling_rate_bps": 10000,
  "reason": "Enable cross-check claims at 100 percent for examples"
}
```

Response `200`:

```json
{
  "enabled": true,
  "sampling_rate_bps": 10000,
  "revision": 1,
  "word_difference_threshold_bps": 1000,
  "comparison_version": "worddiff_v1",
  "updated_at": "2026-09-19T05:16:19.089485+08:00",
  "updated_by_admin_action_id": "ab99703d-702d-453f-815b-e25f568e30af",
  "action_id": "ab99703d-702d-453f-815b-e25f568e30af"
}
```

`word_difference_threshold_bps` is echoed and is **not** accepted on PUT.
Replay of the same `operation_id` returns `idempotent_replay: true`. Stale
`expected_revision` returns `409`.

### Claim (`mode=cross_check`)

`POST /api/assignment/claim`

```json
{"source_scene": "airport"}
```

Response `200` (segments are the baseline copy: empty text, Bad Quality
cleared):

```json
{
  "assigned": true,
  "mode": "cross_check",
  "resumed": false,
  "task_id": "27ede5ef-77af-4235-b05e-1b2dc0c42ba2",
  "version_id": "6fea51ae-c45e-4875-88dc-4b0de341620e",
  "status": "annotated",
  "revision": 0,
  "lease_token": "2a33341f-481a-4a83-8753-25d7b9b53b73",
  "cross_check": {
    "round_id": "6422722f-d147-40bc-aa13-bd6a58990644",
    "state": "in_progress"
  },
  "filename": "audio-000.wav",
  "duration": 10.0,
  "segments": [
    {"id": 1, "start": 0.0, "end": 5.0, "text": "", "exclude_from_training": false},
    {"id": 2, "start": 5.0, "end": 10.0, "text": "", "exclude_from_training": false}
  ]
}
```

The payload does not include original text, original annotator, original
review, `reference_review`, or model prediction. `GET /api/assignment` resumes
the same round. A second concurrent claim from the same user also resumes it
(`tests/test_cross_check_concurrency.py`).

### Complete: auto-pass vs review queue

`POST /api/assignment/current/complete`

```json
{
  "lease_token": "2a33341f-481a-4a83-8753-25d7b9b53b73",
  "expected_revision": 0,
  "target_status": "annotated",
  "skip_reasons": [],
  "operation_id": "<uuid>",
  "segments": [
    {"id": 1, "start": 0.0, "end": 5.0, "duration": 5.0, "text": "<secondary transcript>", "exclude_from_training": false},
    {"id": 2, "start": 5.0, "end": 10.0, "duration": 5.0, "text": "", "exclude_from_training": true}
  ]
}
```

Identical text → `passed` (original published pointer unchanged, secondary
frozen as `cross_check_submitted`, training export not blocked):

```json
{
  "success": true,
  "task_id": "27ede5ef-77af-4235-b05e-1b2dc0c42ba2",
  "status": "annotated",
  "published": false,
  "skip_reasons": [],
  "cross_check": {
    "round_id": "6422722f-d147-40bc-aa13-bd6a58990644",
    "state": "passed",
    "training_export_blocked": false
  }
}
```

Word difference strictly `> 10%` (11/100 in the test) → `awaiting_review`
(`published` stays false; assignment is released; training export blocked):

```json
{
  "success": true,
  "task_id": "ce6f73ac-57b0-4595-9225-cb5bfecac632",
  "status": "annotated",
  "published": false,
  "skip_reasons": [],
  "cross_check": {
    "round_id": "38bf5642-6177-4036-87fe-59a29b35f0cc",
    "state": "awaiting_review",
    "training_export_blocked": true
  }
}
```

Exactly 10% word difference does **not** queue on words
(`tests/test_cross_check_submit.py`). Skip, empty text, and Bad Quality
coverage conflicts still queue.

### Admin queue, decision, cancel

`GET /api/admin/cross-checks` defaults to `state=awaiting_review` and omits
full transcripts. `GET /api/admin/cross-checks/<round_id>` is the admin
detail (both texts, `diff_ops`, `audio_url`).

`POST /api/admin/cross-checks/<round_id>/decision` with `X-CSRF-Token`.

Keep original:

```json
{
  "operation_id": "93a1ddf8-5ce2-4210-8ed2-e5919a019b54",
  "expected_revision": 1,
  "expected_original_version_id": "69bc8ad2-f801-42c3-b1d2-0dc76eb90226",
  "expected_secondary_version_id": "8d91dae9-517b-4b49-812d-b7f2226e8fe6",
  "decision": "original",
  "reason": "keep original"
}
```

```json
{
  "success": true,
  "action_id": "b115dca3-5338-43fc-9164-30cd35e173ae",
  "round_id": "6c1d5f28-a367-4bf4-9d2a-7103f6bb2f3d",
  "state": "adjudicated",
  "final_version_id": "69bc8ad2-f801-42c3-b1d2-0dc76eb90226",
  "final_status": "annotated",
  "training_export_blocked": false
}
```

Adopt secondary (same shape, `"decision": "secondary"`). Response
`final_version_id` is the frozen B version; original becomes `superseded`.

Admin edit (`decision=edited` requires `base`, `segments`, `target_status`):

```json
{
  "operation_id": "6c86d720-d86b-4834-9d1a-453095dcc513",
  "expected_revision": 1,
  "expected_original_version_id": "d2c6d4f7-7611-4ad6-88b8-704dd0214c87",
  "expected_secondary_version_id": "ac4c2869-7d93-4bb5-8c1f-7f9954e10201",
  "decision": "edited",
  "reason": "admin rewrite",
  "base": "original",
  "target_status": "annotated",
  "scene_review": {
    "status": "confirmed",
    "scene_codes": ["airport"],
    "note": "admin-final-note"
  },
  "segments": [{"id": 1, "text": "admin edited ...", "exclude_from_training": false}]
}
```

Response `200`: `state=adjudicated`, `final_version_id` is a **new**
`purpose=adjudication` published version (not A or B). `submitted_by_user_id`
is null; `credited_annotator_id` stays the adopted side’s annotator.
`skip_reasons` is required when `target_status=skipped`.

Cancel is only valid for `in_progress`:

```http
POST /api/admin/cross-checks/dfbc1079-0c6a-4107-911c-46bf7bbcc1f2/cancel
X-CSRF-Token: <csrf>
```

```json
{
  "operation_id": "bdf630d1-1fb2-41d9-959d-3f041bc187e9",
  "expected_revision": 0,
  "reason": "annotator unavailable",
  "confirm": true
}
```

```json
{
  "success": true,
  "action_id": "3020bca7-f3b3-4776-b1a1-36bf293dd7e1",
  "round_id": "dfbc1079-0c6a-4107-911c-46bf7bbcc1f2",
  "state": "cancelled",
  "training_export_blocked": false
}
```

`awaiting_review` cancel returns `409` / `cross_check_state_conflict`.

### Annotator mine

`GET /api/cross-checks/mine` (annotator session):

```json
{
  "items": [
    {
      "round_id": "5aff4ab2-2226-4627-acd2-1cf376391879",
      "task_id": "f36d995f-63ad-49ce-8167-bb97016cd97e",
      "state": "adjudicated",
      "submitted_at": "2026-09-19T05:16:20.283592+08:00",
      "version_id": "ac4c2869-7d93-4bb5-8c1f-7f9954e10201"
    }
  ],
  "next_cursor": null
}
```

`GET /api/cross-checks/<round_id>/submission` returns the caller’s secondary
segments and review. Original transcript is not included. Original author
and a third user get `404`.

## Test commands and results

All commands used `UV_PYTHON_INSTALL_DIR=/opt/annotation-python`. Disposable
databases only. No production DSN.

### Cross-check suite (required)

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q tests/test_cross_check_*.py
```

```
ACCEPTANCE DATABASE BINARY: postgres (PostgreSQL) 16.2 (pgserver)
ACCEPTANCE DATABASE BINDIR: .../site-packages/pgserver/pginstall/bin
108 passed in 78.08s (0:01:18)
```

Files (108 tests):

| File | Tests |
|---|---|
| `tests/test_cross_check_migration.py` | 9 |
| `tests/test_cross_check_comparison.py` | 20 |
| `tests/test_cross_check_claim.py` | 18 |
| `tests/test_cross_check_blind.py` | 5 |
| `tests/test_cross_check_submit.py` | 13 |
| `tests/test_cross_check_admin.py` | 14 |
| `tests/test_cross_check_lifecycle.py` | 12 |
| `tests/test_cross_check_stats.py` | 7 |
| `tests/test_cross_check_export.py` | 7 |
| `tests/test_cross_check_concurrency.py` | 3 |

Concurrency (isolated Postgres + threads, same Barrier style as
`tests/test_repository.py` / `tests/test_load_harness_concurrency.py`):

- Same user, two overlapping claims: one assignment, one `in_progress` round,
  second response `resumed=true`.
- Two users on one published task: exactly one `in_progress` round and one
  assignment; the other call is `NoTaskAvailable` or `TaskPoolBusy`.
- Twenty users / twenty published tasks: twenty distinct `cross_check`
  assignments (nice-to-have; finished in the same 9s file run).

### Full suite including Playwright (PostgreSQL 16.2)

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run pytest -q
```

```
447 passed, 1 skipped in 377.68s (0:06:17)
```

The skip is `test_speed_query_plan_on_100k_current_versions`
(`ANNOTATION_SPEED_EXPLAIN=1` opt-in). Browser tests ran on real Chromium
(Playwright). Default-disabled claim UI is the existing workspace: no QC
chrome was added, and claim → save → complete still works. No QC UI tests
were added.

### PostgreSQL 18.6

Native binaries `/usr/lib/postgresql/18/bin`. Command:

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run --no-sync python \
  scripts/run_pytest_with_postgres.py /usr/lib/postgresql/18/bin -q --tb=line
```

```
ACCEPTANCE DATABASE BINARY: postgres (PostgreSQL) 18.6 (Ubuntu 18.6-0ubuntu0.26.04.1)
ACCEPTANCE DATABASE BINDIR: /usr/lib/postgresql/18/bin
447 passed, 1 skipped in 406.25s (0:06:46)
```

This is a real 16.2 vs 18.6 matrix (pgserver 16.2 and native 18.6), not the
same major version run twice.

## Migration and rollback

1. Backup PostgreSQL. Record the running commit.
2. Deploy code that includes `migrations/008_cross_annotation_quality.sql`.
3. `uv run python manage_state.py apply-migrations` then `schema`.
4. Start Gunicorn. `/api/health` must return `[1, 2, 3, 4, 5, 6, 7, 8]`.
5. New claims stay disabled until an administrator PUTs settings. That is a
   deploy compatibility switch, not an incomplete feature. Exercise
   claim → pass / review → decision → export block/unblock on an isolated
   database before enabling production sampling.
6. Compatible rollback: PUT `enabled=false` (keep `sampling_rate_bps` if
   desired). Existing rounds, decision/cancel APIs, and training-export
   blocks remain. **Do not drop tables. Do not roll back 008. Do not run an
   old binary that does not know schema 8 / `cross_check` assignment mode.**
   `assert_schema_current()` refuses a build whose migration files do not
   match applied versions.

## Remaining limits (not hidden behind “frontend later”)

| Item | Status |
|---|---|
| QC / adjudication HTML, CSS, JS | Not in this round. **Intentional.** Backend loop is closed via API. Enabling sampling without a QC page means annotators can still complete blind re-annotation through the existing workspace (`mode=cross_check` looks like a normal assignment by design). Admins must list/decide/cancel through the admin APIs above, not a new screen. |
| 100k HTTP capacity with cross-check pools | **NOT DONE this session.** `scripts/load_test_100k.py` was not executed (runtime: seed 100k tasks + Gunicorn p50/p95/p99; too heavy to finish alongside the functional matrix). The script also has **no** `cross_check` / `sampling_rate_bps` scenarios, so a stock run would not satisfy plan §13.9 (off/10%/100%, 20-user CC claims, CC `EXPLAIN (ANALYZE, BUFFERS)`, 100/1000/3000-word compare timings). Do not invent p95 numbers. |
| PostgreSQL 16/18 matrix | **Done.** 16.2 pgserver and native 18.6 each: 447 passed, 1 skipped. |
| Review-queue notifications / auto-timeout | Out of scope (plan §11). Backlog is queried and processed by hand. |
| Pre-sampled dual assignment, risk-weighted draw, third annotator, quality ranking | Out of scope (plan §1.2). |

Commands for the remaining 100k job (isolated DB, never a live DSN):

```bash
UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run python \
  scripts/load_test_100k.py --artifact-dir docs/plans/load-test-100k

UV_PYTHON_INSTALL_DIR=/opt/annotation-python uv run python \
  scripts/load_test_100k.py --pg-bindir /usr/lib/postgresql/18/bin
```

Extending that script with cross-check claim/export scenarios and recording
p50/p95/p99 plus query plans is remaining **backend** work, not UI work.

## Plan §14 checklist

- [x] Migration 008, backfill, constraints, indexes; 001–007 untouched
- [x] Settings and all listed APIs callable; request/response examples above
- [x] Random split at claim; historical publications eligible; scene/scope/other-person rules on the server
- [x] Blind baseline re-annotation; read paths do not leak the original
- [x] `worddiff_v1` and strict `> 10%`; exceptions do not auto-pass
- [x] Auto-pass, three admin decisions, cancel/invalidate, lifecycle hooks
- [x] Unique audio count/duration; credited result vs secondary workload
- [x] Training export blocked while open; backup/JSON keep pending review
- [x] Session/revision/idempotency/concurrency tests; PG 16.2 and 18.6 full suites including Playwright
- [x] No QC UI/UX in frontend sources
- [x] This report: shipped work, schema versions, API examples, real pytest output, 100k remaining, migration/rollback, leftover limits
