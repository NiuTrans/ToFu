"""lib/openapi/_spec.py — ``build_spec`` document assembly.

Walks ``app.url_map`` and reads back each handler's ``_api_meta``
(stashed by :func:`lib.openapi._meta.api_meta`) to assemble a full
OpenAPI 3.1 document, pulling helpers from :mod:`lib.openapi._paths`
and :mod:`lib.openapi._schema`.
"""

from __future__ import annotations

from lib.log import get_logger

from lib.openapi._paths import (
    _auto_tags,
    _default_description,
    _flask_to_openapi_path,
    _path_parameters,
    _skip_rule,
)
from lib.openapi._schema import _components, _default_responses

logger = get_logger(__name__)


def build_spec(app, *, title: str = 'Tofu API',
               version: str = '1.0.0',
               description: str = '') -> dict:
    """Walk ``app.url_map`` and build an OpenAPI 3.1 document."""
    paths: dict[str, dict] = {}

    try:
        rules = list(app.url_map.iter_rules())
    except Exception as e:
        logger.warning('[OpenAPI] iter_rules failed: %s', e)
        rules = []

    for rule in rules:
        path = _flask_to_openapi_path(str(rule.rule))
        if _skip_rule(path):
            continue
        view = app.view_functions.get(rule.endpoint)
        if view is None:
            continue
        meta = getattr(view, '_api_meta', None)
        path_params = _path_parameters(str(rule.rule))
        # OPTIONS / HEAD don't need to be documented.
        methods = sorted(m for m in (rule.methods or set())
                          if m not in ('HEAD', 'OPTIONS'))
        if not methods:
            continue

        path_item = paths.setdefault(path, {})
        for method in methods:
            op_id = f'{rule.endpoint.replace(".", "_")}_{method.lower()}'
            op = {
                'operationId': op_id,
                'tags': (meta.get('tags') if meta else []) or _auto_tags(path),
                'summary': (meta.get('summary') if meta else '') or rule.endpoint,
                'description': meta.get('description', '') if meta else '',
                'parameters': path_params + (meta.get('parameters', []) if meta else []),
                'responses': (meta.get('responses') if meta else None) or _default_responses(),
            }
            if meta and meta.get('deprecated'):
                op['deprecated'] = True
            rb = (meta.get('request_body') if meta else None)
            if rb:
                op['requestBody'] = rb
            elif method in ('POST', 'PUT', 'PATCH'):
                op['requestBody'] = {
                    'required': False,
                    'content': {'application/json': {
                        'schema': {'type': 'object',
                                   'additionalProperties': True}}},
                }
            if not (meta and meta.get('public')):
                scope = meta.get('scope') if meta else ''
                if scope:
                    op['security'] = [{'bearerAuth': [scope]},
                                       {'tunnelTokenHeader': []}]
                else:
                    op['security'] = [{'bearerAuth': []},
                                       {'tunnelTokenHeader': []}]
            else:
                op['security'] = []
            path_item[method.lower()] = op

    spec = {
        'openapi': '3.1.0',
        'info': {
            'title': title,
            'version': version,
            'description': description or _default_description(),
        },
        'servers': [{'url': '/', 'description': 'Same-origin'}],
        'security': [{'bearerAuth': []}, {'tunnelTokenHeader': []}],
        'paths': paths,
        'components': _components(),
        'tags': [
            {'name': 'chat', 'description': 'Chat completions (native + OpenAI/Anthropic compat)'},
            {'name': 'tasks', 'description': 'Long-running task lifecycle'},
            {'name': 'conversations', 'description': 'Conversation CRUD + branches'},
            {'name': 'capabilities', 'description': 'Self-describing model/tool registry'},
            {'name': 'agents', 'description': 'Higher-level agents (paper/translate/swarm/etc.)'},
            {'name': 'keys', 'description': 'API key administration'},
            {'name': 'webhooks', 'description': 'Outbound event delivery'},
            {'name': 'compat:openai', 'description': 'OpenAI-compatible adapter'},
            {'name': 'compat:anthropic', 'description': 'Anthropic-compatible adapter'},
        ],
    }
    return spec
