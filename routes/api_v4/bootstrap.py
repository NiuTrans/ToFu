"""Canonical API v4 bootstrap routes generated from ``contracts/api_v4.yaml``.

These are the version-negotiation endpoints (``/api/v4/meta`` +
``/api/v4/openapi.json``) that existed before the product migration. They are
always registered (not gated by ``API_V4_ENABLED``) because client preflight
depends on them. Product routes live alongside them in this package.
"""

from __future__ import annotations

import json
import time

from quart import Blueprint, Response

from lib.generated_api_v4_constants import (
    API_MAJOR,
    MIN_ANDROID_BUILD,
    MIN_DESKTOP_BUILD,
)
from lib.log import req_id
# Schema identity is taken from the client-facing generated catalog, never
# from the sidecar process internals (architecture import-direction gate).
from scripts.generated_storage_operations import (
    STORAGE_SCHEMA_VERSION as SCHEMA_VERSION,
)
from lib.version import __version__


api_v4_bp = Blueprint('api_v4', __name__)


@api_v4_bp.route("/api/v4/boot")
def boot_status():
    """Full boot projection: per-phase status for the cold-start splash."""
    from quart import current_app

    from lib.api_response import api_ok

    runner = current_app.extensions.get("tofu_boot_runner")
    if runner is None:
        return api_ok({"status": "ready", "phases": []})
    return api_ok(runner.snapshot())


@api_v4_bp.route("/api/v4/boot/retry", methods=["POST"])
def boot_retry():
    """Re-run the background boot from its failed phase.

    The runner latches 'failed' by design (the process stays up and
    answerable); retry is the operator/browser recovery path.  Phases are
    revision-idempotent, so re-running from the failure point never
    double-applies completed work.
    """
    from quart import current_app

    from lib.api_response import api_error, api_ok
    from lib.error_envelope import make_envelope

    runner = current_app.extensions.get("tofu_boot_runner")
    if runner is None:
        return api_ok({"result": "ready", "status": "ready"})
    outcome = runner.request_retry()
    if outcome == "started":
        return api_ok({"result": "started", "status": runner.status})
    if outcome == "ready":
        return api_ok({"result": "ready", "status": "ready"})
    return api_error(
        make_envelope(
            'bad_request',
            message="Boot is not in a failed state that accepts a retry",
            retryable=False),
        status=409,
        boot={"status": runner.status},
    )


def _json_response(payload: object, *, cache_control: str) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
        status=200,
        headers={'Cache-Control': cache_control},
        content_type='application/json',
    )


@api_v4_bp.get('/api/v4/meta')
def api_meta() -> Response:
    from lib.generated_api_v4 import validate_api_meta_response

    payload = validate_api_meta_response({
        'data': {
            'apiMajor': API_MAJOR,
            'schemaVersion': SCHEMA_VERSION,
            'serverBuild': __version__,
            'minDesktopBuild': MIN_DESKTOP_BUILD,
            'minAndroidBuild': MIN_ANDROID_BUILD,
        },
        'meta': {
            'requestId': req_id() or 'unassigned',
            'serverTimeMs': int(time.time() * 1000),
        },
    })
    return _json_response(payload, cache_control='no-store')


@api_v4_bp.get('/api/v4/openapi.json')
def api_v4_openapi() -> Response:
    # OpenAPI tooling requires a raw document, so this one self-describing
    # endpoint is the declared exception to the v4 data/meta success envelope.
    from lib.generated_api_v4 import OPENAPI_DOCUMENT
    return _json_response(OPENAPI_DOCUMENT, cache_control='no-cache')


__all__ = ['api_v4_bp']
