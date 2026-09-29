# Annotation frontend

The admin console at `/admin` uses Vite, React 19, TypeScript strict, and
Ant Design 6. Flask serves the compiled application and the existing
`/api/admin/*` endpoints. Admin pages live in `src/features/admin`.

The console includes overview, annotators, tasks and corpus, quality,
cross-check review, and activity pages. Theme tokens define the indigo
palette, 8px button corners, dark mode, and responsive navigation.
Workspace and component previews remain behind the preview switch.

Cross-checks follows the supplied HTML reference: four summary cards, state
counts, immediate queue filters, paired annotator names, word-difference
meters, and a sampling dialog. Batch and created-date filters are under
More filters. Counts cover all scenes and dates; pass rate is passed rounds
divided by passed, awaiting-review, and adjudicated rounds.

Review opens a side drawer with paired transcript segments, backend-computed
word differences, waveform seeking, playback speed, and segment playback.
Choose A or B per segment, or edit a segment. Resolve every differing segment
before submitting through the existing adjudication API
with revision checks and confirmation. The reference's reviewer assignment,
rejection workflow, release management, and comparison settings are not added.

Submission status and scene reviews share one comparison card. The A and B
columns show the submitted evidence; Final previews the current decision.
Status controls explain whole-audio skips, and the scene override supports
confirming the source scene, reassignment, and the existing review states.
Pending changes and Reset to defaults apply to this card without discarding
segment choices or the decision reason. Reasons remain required for submission.

Save draft keeps the review in the current browser tab, including across
reloads. Drafts are tied to the round and manuscript revisions, and are cleared
on logout or session expiration. Export draft downloads a copy. Keyboard
shortcuts outside text fields: Space plays or pauses; J/K selects a segment;
N selects the next unresolved segment; 1/2 chooses A/B; E edits; R replays.

## Prereqs

- Node **24.21.0** / npm **11.19.0** (pinned in `.nvmrc`, `package.json`
  `engines`, and CI). `export PATH="$HOME/.local/node24/bin:$PATH"` if your
  shell does not provide it yet.
- Python dev env for the backend: `uv sync --frozen --group dev`.

## Commands (all from the repo root or with `--prefix frontend`)

| Purpose | Command |
| --- | --- |
| Install (reproducible) | `npm --prefix frontend ci` |
| Typecheck | `npm --prefix frontend run typecheck` |
| Lint (zero warnings) | `npm --prefix frontend run lint` |
| Unit tests (pure logic) | `npm --prefix frontend test` |
| Production build → `frontend/dist/` | `npm --prefix frontend run build` |
| P1 verification build → `frontend/dist-p1/` | `npm --prefix frontend run build:p1` |
| Verify both outputs | `npm --prefix frontend run check:dist` |
| Regenerate tokens.css from tokens.ts | `npm --prefix frontend run gen:tokens` |
| Verify tokens.css has no drift | `npm --prefix frontend run check:tokens` |
| Dev server (same-origin API proxy) | `npm --prefix frontend run dev` |

`build` is the deployable production output (no dev matrix);
`build:p1` additionally bundles `src/dev` (component matrix only).
Neither `dist/` nor `node_modules/` is committed.

## Run the admin console

Build the frontend before starting Flask or packaging a deployment:

```sh
npm --prefix frontend ci
npm --prefix frontend run build
```

Open `/admin` on your backend and sign in with its admin key. The admin
console does not require `ANNOTATION_FRONTEND_PREVIEW`. If the selected
build is missing or incomplete, `/admin` returns 503 with a build command.
Include `frontend/dist`, including `.vite/manifest.json`, in the backend
deployment artifact.

For hot reload, keep the backend running and start Vite:

```sh
ANNOTATION_DEV_API_URL=http://127.0.0.1:8088 npm --prefix frontend run dev
```

Open `http://127.0.0.1:5173/admin`. Vite listens on loopback and proxies
`/api` and `/static` to the backend, so login cookies and CSRF requests
remain on the same origin. The default backend is `127.0.0.1:8088`.
Use a backend with non-secure development cookies when accessing it over HTTP.

For the existing test server, run this command on your local machine:

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:18088:127.0.0.1:8088 ubuntu@43.165.0.67
```

Open `http://127.0.0.1:18088/admin`. For Vite hot reload over SSH, forward
local port 15173 to remote `127.0.0.1:5173` and open `/admin` on that port.

## Delivery and preview routes

Backend config (env or app config file; illegal values fail startup):

- `ANNOTATION_FRONTEND_PREVIEW=false` — preview off by default.
- `ANNOTATION_FRONTEND_BUILD=standard|p1` — `p1` requires preview on.

Entries (all `no-store`, admin-equivalent CSP with a per-response style
nonce carried in `<meta name="csp-nonce">`):

| Entry | Requirement |
| --- | --- |
| `/admin`, `/admin/`, `/admin/login` | Built frontend; React checks the admin session |
| `/admin/preview?view=corpus` | Preview on + admin session (else `/admin/login`) |
| `/frontend-preview/workspace` | Preview on + annotator session (legacy reason redirects) |
| `/frontend-preview/components` | Preview on + `p1` build + admin session |
| `/frontend/assets/<hash>` | Current build's hashed assets, immutable cache |

Unknown `/api/*` and unknown resources stay 404/JSON — no SPA fallback.
The shared entry also loads same-origin `/static/offline-drafts.js` (existing
whitelist): the persistence layer reuses the legacy IndexedDB
`annotation-offline-v1` implementation, no second schema.

## Acceptance

- Admin UI and writes:

  ```sh
  uv run --no-sync pytest -q tests/browser/test_admin_console_design.py tests/browser/test_admin_react_migration.py tests/browser/test_regression_scope_*.py tests/browser/test_cross_check_admin.py tests/browser/test_cross_check_diff.py tests/browser/test_cross_check_regressions.py tests/browser/test_cross_check_reference_ui.py tests/browser/test_cross_check_review_drawer.py tests/browser/test_scene_workflow.py
  ```

  These tests use disposable PostgreSQL data and a real Chromium browser.
  They cover scope loading races, revoke and deactivate confirmations,
  review decisions, idempotent retries, conflict recovery, CSP, and mobile UI.
- Backend: `uv run --no-sync pytest -q tests/test_frontend_delivery.py`
- Browser: `uv run --no-sync pytest -q tests/browser/test_frontend_p1_*.py`
  (needs both dists built + disposable PostgreSQL; see
  `scripts/run_pytest_with_postgres.py` for CI usage)
- Capture/perf: `uv run --no-sync python scripts/frontend_p1.py capture`
- Evidence: `docs/plans/frontend-rebuild-p1/` (acceptance table, screenshots,
  raw perf samples, component decision, P2 handoff)
