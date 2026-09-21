"""Metadata blueprint. Auth and request helpers are injected at registration."""

from __future__ import annotations

from flask import Blueprint, jsonify, request, send_from_directory
from pathlib import Path

import annotation_repository as repo
from annotation_metadata import SCHEMA_VERSION
from annotation_metadata.features import feature_flags, metadata_ui_enabled
from annotation_metadata.queries import load_scope
from annotation_metadata.repository import list_active_scenes, scope_payload
from db import db_tx

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
ALLOWED_STATIC = {
    "metadata.js", "metadata.css",
    "annotator-session.js", "offline-drafts.js",
    "login-dashboard.js",
    "cross-check.js", "admin-cross-check.js",
    "vendor/chart.umd-4.5.1.min.js",
    "vendor/chartjs-plugin-annotation-3.1.0.min.js",
}


def register_metadata_routes(app, *, login_required, admin_required,
                             admin_write_required, json_object,
                             admin_query_filters):
    bp = Blueprint("metadata", __name__)

    @bp.route("/static/<path:filename>")
    def serve_metadata_static(filename: str):
        if filename not in ALLOWED_STATIC:
            return jsonify({"error": "Not found"}), 404
        return send_from_directory(STATIC_DIR, filename)

    @bp.route("/api/scenes")
    @login_required
    def api_scenes():
        user = request.annotator
        with db_tx() as conn, conn.cursor() as cur:
            scope = load_scope(cur, user["id"])
            taxonomy = list_active_scenes(cur, active_only=True)
        claim_scenes = taxonomy
        if not scope.all_scenes:
            allowed = set(scope.effective_scene_codes())
            claim_scenes = [item for item in taxonomy if item["code"] in allowed]
        return jsonify({
            "schema_version": SCHEMA_VERSION,
            "scope": scope_payload(scope),
            "scenes": claim_scenes,
            "claim_scenes": claim_scenes,
            "review_taxonomy": taxonomy,
            "features": feature_flags(),
        })

    @bp.route("/api/admin/metadata/facets")
    @admin_required
    def api_admin_facets():
        return jsonify(repo.admin_metadata_facets(admin_query_filters()))

    @bp.route("/api/admin/annotations/<task_id>/scene-review", methods=["POST"])
    @admin_write_required
    def api_admin_scene_review(task_id: str):
        admin = request.admin
        payload = json_object()
        if not metadata_ui_enabled() and not feature_flags()["scene_review_write"]:
            raise repo.ForbiddenError("Scene review editing is disabled")
        result = repo.admin_correct_scene_review(
            admin["id"], task_id, payload,
        )
        return jsonify(result)

    @bp.route("/api/admin/annotators/<annotator_id>/scene-scope", methods=["PUT"])
    @admin_write_required
    def api_admin_scene_scope(annotator_id: str):
        admin = request.admin
        return jsonify(repo.admin_set_scene_scope(
            admin["id"], annotator_id, json_object(),
        ))

    app.register_blueprint(bp)
    return bp
