"""Cross-check admin and annotator-self blueprints. Auth is injected."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, jsonify, request

import annotation_repository as repo
from annotation_quality.contracts import (
    CrossCheckCancelCommand,
    CrossCheckDecisionCommand,
    CrossCheckListQuery,
    CrossCheckMineQuery,
    CrossCheckSettingsUpdateCommand,
    parse_strict,
)
from annotation_quality.service import (
    cancel_cross_check,
    decide_cross_check,
    get_cross_check_detail,
    get_cross_check_settings,
    get_my_submission,
    list_cross_checks,
    list_my_cross_checks,
    update_cross_check_settings,
)


def register_cross_check_routes(
    app, *, login_required, admin_required, admin_write_required, json_object,
    audit_admin_write_failures,
):
    bp = Blueprint("cross_check", __name__)

    @bp.route("/api/admin/cross-check-settings")
    @admin_required
    def api_admin_cross_check_settings_get():
        return jsonify(get_cross_check_settings())

    @bp.route("/api/admin/cross-check-settings", methods=["PUT"])
    @admin_write_required
    @audit_admin_write_failures("update_cross_check_settings")
    def api_admin_cross_check_settings_put():
        command = parse_strict(CrossCheckSettingsUpdateCommand, json_object())
        return jsonify(update_cross_check_settings(
            str(request.admin["id"]), command,
        ))

    @bp.route("/api/admin/cross-checks")
    @admin_required
    def api_admin_cross_checks_list():
        query = parse_strict(CrossCheckListQuery, _list_query_payload())
        timezone_name = (request.args.get("timezone") or "UTC").strip() or "UTC"
        return jsonify(list_cross_checks(query, timezone_name=timezone_name))

    @bp.route("/api/admin/cross-checks/<round_id>")
    @admin_required
    def api_admin_cross_check_detail(round_id: str):
        return jsonify(get_cross_check_detail(round_id))

    @bp.route("/api/admin/cross-checks/<round_id>/decision", methods=["POST"])
    @admin_write_required
    @audit_admin_write_failures("cross_check_decision")
    def api_admin_cross_check_decision(round_id: str):
        command = parse_strict(CrossCheckDecisionCommand, json_object())
        return jsonify(decide_cross_check(
            str(request.admin["id"]), round_id, command,
        ))

    @bp.route("/api/admin/cross-checks/<round_id>/cancel", methods=["POST"])
    @admin_write_required
    @audit_admin_write_failures("cancel_cross_check")
    def api_admin_cross_check_cancel(round_id: str):
        command = parse_strict(CrossCheckCancelCommand, json_object())
        return jsonify(cancel_cross_check(
            str(request.admin["id"]), round_id, command,
        ))

    @bp.route("/api/cross-checks/mine")
    @login_required
    def api_cross_checks_mine():
        query = parse_strict(CrossCheckMineQuery, _mine_query_payload())
        return jsonify(list_my_cross_checks(request.annotator["id"], query))

    @bp.route("/api/cross-checks/<round_id>/submission")
    @login_required
    def api_cross_check_submission(round_id: str):
        return jsonify(get_my_submission(request.annotator["id"], round_id))

    app.register_blueprint(bp)
    return bp


def _query_int(name: str, raw: str | None) -> int | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, bool):
        raise repo.ValidationError(f"{name} must be an integer")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise repo.ValidationError(f"{name} must be an integer") from exc


def _list_query_payload() -> dict:
    args = request.args
    timezone_name = (args.get("timezone") or "UTC").strip() or "UTC"
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise repo.ValidationError("Invalid timezone") from exc
    created_from = repo._parse_admin_datetime(
        args.get("from"), "from", assumed_timezone=zone,
    )
    created_to = repo._parse_admin_datetime(
        args.get("to"), "to", assumed_timezone=zone,
    )
    if created_from and created_to and created_from >= created_to:
        raise repo.ValidationError("from must be earlier than to")
    payload: dict = {}
    if args.get("state"):
        payload["state"] = args.get("state")
    for key in (
        "source_scene", "batch_code", "original_annotator_id",
        "secondary_annotator_id", "reason_code", "q", "cursor",
    ):
        if args.get(key) not in (None, ""):
            payload[key] = args.get(key)
    if created_from:
        payload["created_from"] = created_from.isoformat()
    if created_to:
        payload["created_to"] = created_to.isoformat()
    limit = _query_int("limit", args.get("limit"))
    if limit is not None:
        payload["limit"] = limit
    return payload


def _mine_query_payload() -> dict:
    payload: dict = {}
    if request.args.get("cursor"):
        payload["cursor"] = request.args.get("cursor")
    limit = _query_int("limit", request.args.get("limit"))
    if limit is not None:
        payload["limit"] = limit
    return payload
