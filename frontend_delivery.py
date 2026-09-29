"""Delivery for the React admin console and gated frontend previews.

Flask helpers for React builds. The canonical builds are
Vite production outputs:

- ``frontend/dist/``     — standard build (no dev component matrix)
- ``frontend/dist-p1/``  — P1 verification build (includes the matrix)

Contract (see docs/plans/2026-09-21-frontend-rebuild-p1-implementation.md
sections 4.2/4.3):

- Config ``ANNOTATION_FRONTEND_PREVIEW`` (default false) and
  ``ANNOTATION_FRONTEND_BUILD`` (``standard`` | ``p1``, default
  ``standard``) are parsed in one place. Illegal values fail closed at
  startup; ``p1`` requires the preview switch to be on.
- Tests toggle behaviour via ``app.config`` monkeypatch
  (``FRONTEND_PREVIEW_ENABLED`` / ``FRONTEND_BUILD``); route handlers read
  ``app.config`` per request.
- HTML entries are ``no-store`` and carry the same CSP/security headers as
  the admin pages. The shared header helper is idempotent so a per-response
  nonce (added by later work packages) is never overwritten by
  ``after_request``.
- Hash assets are served only from the selected ``dist*/assets`` root with
  ``public, max-age=31536000, immutable`` + ``nosniff`` + correct MIME.
  Traversal, hidden files, manifests, source maps, HTML, and repo sources
  are all 404 JSON (never HTML).
- Unknown ``/api/*`` and unknown resources keep their 404/JSON errors;
  there is no SPA HTML fallback.
- Admin HTML and shared assets are available with previews off. A missing
  admin build returns 503 with the build command.
- Preview off → preview HTML is 404.
- Preview on but the selected build is missing/incomplete → explicit
  failure (500 JSON at request time, RuntimeError at startup preflight),
  never a silent fallback to the legacy pages.
"""

from __future__ import annotations

import mimetypes
import os
import secrets
from pathlib import Path

from flask import Response

SCRIPT_DIR = Path(__file__).parent.resolve()
FRONTEND_ROOT = SCRIPT_DIR / "frontend"
DIST_STANDARD = FRONTEND_ROOT / "dist"
DIST_P1 = FRONTEND_ROOT / "dist-p1"

VALID_BUILDS = ("standard", "p1")

CSP_NONCE_PLACEHOLDER = "%FRONTEND_CSP_NONCE%"
CSP_NONCE_META_NAME = "csp-nonce"

PREVIEW_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'"
)


def new_csp_nonce() -> str:
    """Generate a per-response CSP nonce (unguessable, single use)."""
    return secrets.token_urlsafe(16)


def preview_csp_value(nonce: str | None = None) -> str:
    """Admin-equivalent CSP for preview HTML.

    With a nonce, ``style-src`` additionally allows that exact nonce so the
    component library's runtime-injected ``<style nonce="…">`` elements work
    without ``unsafe-inline``. ``script-src`` stays ``'self'``: preview
    entries load external hash chunks only, never inline scripts.
    """
    if not nonce:
        return PREVIEW_CSP
    return (
        "default-src 'self'; script-src 'self'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    )


def render_preview_html(dist_dir: Path, nonce: str) -> Response:
    """Render the built ``index.html`` with the per-response nonce injected.

    The Vite build leaves the ``%FRONTEND_CSP_NONCE%`` meta placeholder
    untouched; it is replaced here so every preview response carries a fresh
    nonce in both the CSP header and the ``<meta name="csp-nonce">`` tag the
    client style provider reads. The response is ``no-store``.
    """
    html_path = dist_dir / "index.html"
    html = html_path.read_text(encoding="utf-8")
    if CSP_NONCE_PLACEHOLDER not in html:
        raise RuntimeError(
            f"Preview build {dist_dir} index.html has no "
            f"{CSP_NONCE_PLACEHOLDER} meta placeholder"
        )
    html = html.replace(CSP_NONCE_PLACEHOLDER, nonce)
    response = Response(html, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = preview_csp_value(nonce)
    return response


# ---------------------------------------------------------------------------
# Config parsing (single place, fail closed)
# ---------------------------------------------------------------------------

def parse_preview_enabled(raw: object) -> bool:
    """Parse the preview switch. Illegal values raise RuntimeError."""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        # Empty string means "not set" → default off. Explicit "false"
        # variants also map here; anything else below raises.
        if text == "":
            return False
        return False
    raise RuntimeError(
        f"Invalid ANNOTATION_FRONTEND_PREVIEW {raw!r}; "
        "expected a boolean (true/false, 1/0, yes/no, on/off)"
    )


def parse_frontend_build(raw: object) -> str:
    """Parse the build selector. Illegal values raise RuntimeError."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return "standard"
    text = str(raw).strip().lower()
    if text in VALID_BUILDS:
        return text
    raise RuntimeError(
        f"Invalid ANNOTATION_FRONTEND_BUILD {raw!r}; "
        "expected 'standard' or 'p1'"
    )


def resolve_frontend_config(
    config: dict | None = None,
    environ: dict | None = None,
) -> tuple[bool, str]:
    """Resolve (preview_enabled, build) from config file + environment.

    Environment wins over the config file, mirroring the admin-cookie
    pattern in server.py. ``p1`` with the preview switch off is a startup
    error.
    """
    env = environ if environ is not None else os.environ
    cfg = config or {}
    raw_preview = env.get("ANNOTATION_FRONTEND_PREVIEW", cfg.get("frontend_preview", False))
    raw_build = env.get("ANNOTATION_FRONTEND_BUILD", cfg.get("frontend_build", "standard"))
    enabled = parse_preview_enabled(raw_preview)
    build = parse_frontend_build(raw_build)
    if build == "p1" and not enabled:
        raise RuntimeError(
            "Invalid frontend config: ANNOTATION_FRONTEND_BUILD='p1' "
            "requires ANNOTATION_FRONTEND_PREVIEW=true"
        )
    return enabled, build


def init_frontend_config(app, config: dict | None = None) -> tuple[bool, str]:
    """Store preview config on ``app.config`` and run startup preflight.

    Preview off → missing dist never blocks startup (admin reports 503).
    Preview on → the selected build must be complete or startup raises.
    """
    enabled, build = resolve_frontend_config(config)
    app.config["FRONTEND_PREVIEW_ENABLED"] = enabled
    app.config["FRONTEND_BUILD"] = build
    if enabled:
        dist_dir = selected_dist_dir(app)
        problems = verify_build_complete(dist_dir)
        if problems:
            raise RuntimeError(
                "Frontend preview enabled but build incomplete in "
                f"{dist_dir}: {'; '.join(problems)}"
            )
    return enabled, build


# ---------------------------------------------------------------------------
# Request-time accessors (tests monkeypatch app.config directly)
# ---------------------------------------------------------------------------

def is_preview_enabled(app) -> bool:
    return bool(app.config.get("FRONTEND_PREVIEW_ENABLED", False))


def selected_build(app) -> str:
    build = str(app.config.get("FRONTEND_BUILD", "standard")).strip().lower()
    if build not in VALID_BUILDS:
        raise RuntimeError(
            f"Invalid FRONTEND_BUILD {build!r}; expected 'standard' or 'p1'"
        )
    return build


def selected_dist_dir(app) -> Path:
    build = selected_build(app)
    return DIST_P1 if build == "p1" else DIST_STANDARD


def verify_build_complete(dist_dir: Path) -> list[str]:
    """Return a list of problems; empty means the build is complete."""
    problems: list[str] = []
    html = dist_dir / "index.html"
    manifest = dist_dir / ".vite" / "manifest.json"
    assets = dist_dir / "assets"
    if not html.is_file():
        problems.append("missing index.html")
    if not manifest.is_file():
        problems.append("missing .vite/manifest.json")
    else:
        try:
            import json as _json

            data = _json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - report, don't raise
            problems.append(f"manifest is not valid JSON: {exc}")
            data = None
        if data is not None:
            if not isinstance(data, dict) or not data:
                problems.append("manifest is empty")
            else:
                for key, entry in data.items():
                    if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
                        problems.append(f"manifest entry {key!r} has no file")
                        continue
                    for rel in [entry["file"], *(entry.get("css") or []), *(entry.get("assets") or [])]:
                        if not (dist_dir / str(rel)).is_file():
                            problems.append(f"manifest file missing: {rel}")
    if not assets.is_dir():
        problems.append("missing assets/ directory")
    else:
        js = list(assets.rglob("*.js"))
        if not js:
            problems.append("assets/ has no .js chunk")
        maps = list(dist_dir.rglob("*.map"))
        if maps:
            problems.append(f"sourcemaps must be off ({len(maps)} .map file(s) found)")
    return problems


def build_complete(dist_dir: Path) -> bool:
    return not verify_build_complete(dist_dir)


# ---------------------------------------------------------------------------
# Unified security headers (idempotent, nonce-safe)
# ---------------------------------------------------------------------------

def apply_preview_html_headers(response):
    """Apply no-store + admin-equivalent CSP/security headers to preview HTML.

    Idempotent: if the response already carries a CSP containing a
    per-response ``nonce-`` value (added by later work packages), it is
    kept instead of being overwritten.
    """
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    existing = response.headers.get("Content-Security-Policy", "")
    if "nonce-" in existing:
        return response
    response.headers["Content-Security-Policy"] = PREVIEW_CSP
    return response


def apply_preview_asset_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------------------
# Asset safety
# ---------------------------------------------------------------------------

def validate_asset_filename(filename: str) -> str | None:
    """Return an error string, or None when the filename is servable."""
    if not filename or filename.strip() == "":
        return "empty filename"
    if filename.startswith("/") or filename.startswith("\\"):
        return "absolute path"
    parts = Path(filename).parts
    if any(part in ("..", ".") for part in parts):
        return "traversal"
    if any(part.startswith(".") for part in parts):
        return "hidden file"
    lowered = filename.lower()
    if lowered.endswith(".map"):
        return "source maps are not served"
    if lowered.endswith(".html") or lowered.endswith(".htm"):
        return "html is not served as an asset"
    if "manifest" in lowered:
        return "manifest is not served as an asset"
    return None


def asset_mimetype(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    if filename.lower().endswith(".js") or filename.lower().endswith(".mjs"):
        return "text/javascript"
    if filename.lower().endswith(".css"):
        return "text/css"
    return guessed or "application/octet-stream"
