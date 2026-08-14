"""routes/api_docs.py — OpenAPI spec + Swagger/ReDoc viewers."""

from __future__ import annotations

import json

from quart import Blueprint, Response, current_app

from lib.log import get_logger
from lib.openapi import build_spec, redoc_html, swagger_html

logger = get_logger(__name__)

api_docs_bp = Blueprint('api_docs', __name__)


# Per-app spec cache. Different apps (e.g. unit test fixtures vs the
# real server) register different blueprint subsets — caching by app
# identity prevents one fixture's cached spec from leaking into a
# differently-configured app within the same process.
_cached_specs: dict[int, dict] = {}


def _spec(force: bool = False) -> dict:
    app = current_app._get_current_object()
    key = id(app)
    if force or key not in _cached_specs:
        try:
            _cached_specs[key] = build_spec(app)
        except Exception as e:
            logger.warning('[OpenAPI] build failed: %s', e, exc_info=True)
            _cached_specs[key] = {'openapi': '3.1.0',
                                    'info': {'title': 'Tofu API',
                                             'version': '1.0.0',
                                             'description': str(e)},
                                    'paths': {}}
    return _cached_specs[key]


@api_docs_bp.route('/api/openapi.json', methods=['GET'])
def openapi_json():
    spec = _spec(force=False)
    return Response(json.dumps(spec, ensure_ascii=False, indent=2),
                    mimetype='application/json')


@api_docs_bp.route('/api/openapi.yaml', methods=['GET'])
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
def openapi_refresh():
    """Bust the cache. Useful after dynamic blueprint changes."""
    # Clear all entries so a re-cache happens for every app, not just
    # the one currently bound.
    _cached_specs.clear()
    _spec(force=True)
    return Response('refreshed\n', mimetype='text/plain')


@api_docs_bp.route('/api/docs', methods=['GET'])
def swagger_ui():
    return Response(swagger_html('/api/openapi.json'),
                    mimetype='text/html; charset=utf-8')


@api_docs_bp.route('/api/redoc', methods=['GET'])
def redoc_ui():
    return Response(redoc_html('/api/openapi.json'),
                    mimetype='text/html; charset=utf-8')


__all__ = ['api_docs_bp']
