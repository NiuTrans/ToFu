"""Canonical API v4 bootstrap routes generated from ``contracts/api_v4.yaml``."""

from __future__ import annotations

import json
import time

from quart import Blueprint, Response

from lib.api_v4_constants_generated import (
    API_MAJOR,
    MIN_ANDROID_BUILD,
    MIN_DESKTOP_BUILD,
)
from lib.log import req_id
from lib.storage_sidecar.schema import SCHEMA_VERSION
from lib.version import __version__


api_v4_bp = Blueprint('api_v4', __name__)


def _json_response(payload: object, *, cache_control: str) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
        status=200,
        headers={'Cache-Control': cache_control},
        content_type='application/json',
    )


@api_v4_bp.get('/api/v4/meta')
def api_meta() -> Response:
    from lib.api_v4_generated import validate_api_meta_response

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
    from lib.api_v4_generated import OPENAPI_DOCUMENT
    return _json_response(OPENAPI_DOCUMENT, cache_control='no-cache')


__all__ = ['api_v4_bp']
