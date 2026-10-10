"""Read-only local-deployment surface for Settings → 本地部署.

Three GETs give the redesigned page everything it renders: the live
hardware snapshot, the curated model catalog ranked against that hardware
(with the cheap-cloud fallback), and the managed-deployment ledger with
per-row live status + failure diagnosis. Every write (deploy / stop /
remove) stays with the approval-gated agent tool family — this blueprint
never mutates anything.
"""

from __future__ import annotations

from quart import Blueprint, request

from lib.api_response import api_forbidden, api_internal_error, api_not_found, api_ok
from lib.local_serve import api as serve_api
from lib.local_serve._probe import probe_hardware
from lib.openapi import api_meta

from .auth import current_auth, require_scope


api_v1_local_serve_bp = Blueprint("api_v1_local_serve", __name__)


def _owner_user_id() -> int | None:
    context = current_auth()
    if context is None or context.owner_user_id is None:
        return None
    return context.owner_user_id


@api_v1_local_serve_bp.route("/api/v1/local-serve/hardware", methods=["GET"])
@require_scope("providers")
@api_meta(
    summary="Probe local hardware for model deployment",
    description=(
        "Live GPU/VRAM/RAM/disk snapshot of the machine hosting Tofu. "
        "Individual facets degrade to null independently."
    ),
    tags=["models"],
    scope="providers",
)
def get_local_serve_hardware():
    try:
        return api_ok(hardware=probe_hardware())
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.local_serve.hardware")


@api_v1_local_serve_bp.route("/api/v1/local-serve/recommendations", methods=["GET"])
@require_scope("providers")
@api_meta(
    summary="Rank the open-model catalog against local hardware",
    description=(
        "Returns catalog candidates (best variant/engine/fit tier/download "
        "command) plus a cheap cloud API alternative when local hardware "
        "cannot comfortably run a capable model. Optional ?purpose= filter: "
        "chat|coding|reasoning|vision|multilingual."
    ),
    tags=["models"],
    scope="providers",
)
def get_local_serve_recommendations():
    purpose = (request.args.get("purpose") or "").strip() or None
    try:
        result = serve_api.recommend(purpose)
        result.pop("ok", None)
        return api_ok(result)
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.local_serve.recommendations")


@api_v1_local_serve_bp.route("/api/v1/local-serve/deployments", methods=["GET"])
@require_scope("providers")
@api_meta(
    summary="List managed local deployments with live status",
    description=(
        "Owner-scoped ledger rows reconciled against reality (dead pids, "
        "orphaned installing/starting rows); failed rows carry a structured "
        "diagnosis. Personal mode only."
    ),
    tags=["models"],
    scope="providers",
)
def get_local_serve_deployments():
    owner = _owner_user_id()
    if owner is None:
        return api_not_found("local serve deployments")
    try:
        listed = serve_api.list_deployments(owner_user_id=owner)
        if not listed.get("ok"):
            return api_forbidden(listed.get("error"))
        rows = []
        for row in listed.get("instances") or []:
            instance_id = str(row.get("id") or "")
            live = serve_api.deployment_status(instance_id, owner_user_id=owner)
            rows.append(live if live.get("ok") else row)
        return api_ok(instances=rows, engines=listed.get("engines") or {})
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.local_serve.deployments")
