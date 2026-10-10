"""routes/api_docs.py — OpenAPI spec + Swagger/ReDoc viewers."""

from __future__ import annotations

import json
import threading
import weakref

from quart import Blueprint, Response, current_app

from lib.api_v4 import raw_success_response
from lib.log import get_logger
from lib.openapi import api_meta, build_spec, media_responses, redoc_html, swagger_html

logger = get_logger(__name__)

api_docs_bp = Blueprint('api_docs', __name__)


# Per-app spec cache. A numeric ``id(app)`` key both retains specs after a test
# app dies and can be reused by a later object, exposing the wrong route set.
# Weak object identity preserves isolation without retaining disposable apps.
_cached_specs: weakref.WeakKeyDictionary[object, dict] = weakref.WeakKeyDictionary()
_cached_specs_lock = threading.Lock()


def _spec(force: bool = False) -> dict:
    app = current_app._get_current_object()
    with _cached_specs_lock:
        if force or app not in _cached_specs:
            _cached_specs[app] = build_spec(app)
        return _cached_specs[app]


@api_docs_bp.route('/api/openapi.json', methods=['GET'])
def openapi_json():
    spec = _spec(force=False)
    return Response(json.dumps(spec, ensure_ascii=False, indent=2),
                    mimetype='application/json')


@api_docs_bp.route('/api/openapi.yaml', methods=['GET'])
@api_meta(
    responses=media_responses('application/yaml'),
)
@raw_success_response
def openapi_yaml():
    spec = _spec(force=False)
    try:
        import yaml  # type: ignore
        text = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)
    except ImportError as e:
        logger.debug('[ApiDocs] pyyaml unavailable, using fallback: %s', e)
        text = ('# pip install pyyaml to get YAML output\n'
                + json.dumps(spec, ensure_ascii=False, indent=2))
    return Response(text, mimetype='application/yaml')


@api_docs_bp.route('/api/openapi.refresh', methods=['POST'])
@api_meta(
    responses=media_responses('text/plain'),
)
@raw_success_response
def openapi_refresh():
    """Bust the cache. Useful after dynamic blueprint changes."""
    # Clear all entries so a re-cache happens for every app, not just
    # the one currently bound.
    with _cached_specs_lock:
        _cached_specs.clear()
    _spec(force=True)
    return Response('refreshed\n', mimetype='text/plain')


@api_docs_bp.route('/api/docs', methods=['GET'])
@api_meta(
    responses=media_responses('text/html'),
)
@raw_success_response
def swagger_ui():
    return Response(swagger_html('/api/openapi.json'),
                    mimetype='text/html; charset=utf-8')


@api_docs_bp.route('/api/redoc', methods=['GET'])
@api_meta(
    responses=media_responses('text/html'),
)
@raw_success_response
def redoc_ui():
    return Response(redoc_html('/api/openapi.json'),
                    mimetype='text/html; charset=utf-8')


__all__ = ['api_docs_bp']
