"""lib/openapi.py — OpenAPI 3.1 spec generation.

Provides:
  - ``@api_meta(...)`` decorator that attaches OpenAPI metadata to a
    route handler (summary, description, tags, scopes, request/response
    schemas).
  - ``build_spec(app)`` that walks ``app.url_map`` and builds a full
    OpenAPI 3.1 document.
  - ``swagger_html(spec_url)`` / ``redoc_html(spec_url)`` for the
    interactive viewers (single-file CDN bundles, no build step — meets
    CLAUDE.md §3.2 "no frameworks, no build step").

The decorator stores metadata on the function object (``fn._api_meta``).
``build_spec`` reads it back during introspection. Routes without
``@api_meta`` are still listed (with a generic ``operationId`` and no
schemas) so the spec stays drift-free even if a developer forgets.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
from typing import Any, Optional

from lib.log import get_logger

logger = get_logger(__name__)


# ── Decorator ──────────────────────────────────────────────────────

def api_meta(*, summary: str = '', description: str = '',
             tags: Optional[list[str]] = None,
             scope: str = '',
             request_body: Optional[dict] = None,
             responses: Optional[dict] = None,
             parameters: Optional[list[dict]] = None,
             deprecated: bool = False,
             public: bool = False) -> Any:
    """Attach OpenAPI metadata to a route handler.

    Parameters
    ----------
    summary : str
        Short title for the operation (shown in Swagger UI's nav).
    description : str
        Long-form description (Markdown allowed).
    tags : list[str]
        OpenAPI tags — used for grouping in the UI.
    scope : str
        Required API-key scope (e.g. ``'chat'``, ``'admin'``). Becomes
        the security requirement on the operation.
    request_body : dict | None
        OpenAPI ``requestBody`` object. Caller supplies an inline schema
        or ``$ref``. If None, no body is documented.
    responses : dict | None
        Mapping of status code → response object. Defaults to a generic
        200 ``{ok:true}`` response when unspecified.
    parameters : list[dict] | None
        OpenAPI ``parameters`` list (path/query/header).
    deprecated : bool
        Mark the operation deprecated.
    public : bool
        If True, document the operation as not requiring auth (e.g.
        capabilities/health endpoints).
    """
    meta = {
        'summary': summary,
        'description': description,
        'tags': list(tags or []),
        'scope': scope,
        'request_body': request_body,
        'responses': responses,
        'parameters': list(parameters or []),
        'deprecated': bool(deprecated),
        'public': bool(public),
    }

    def decorator(fn):
        fn._api_meta = meta

        # Dual-mode: an async handler MUST stay a coroutine function or
        # Quart will run the sync wrapper in its thread-pool and try to
        # serialize the returned coroutine object as the response body.
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                return await fn(*args, **kwargs)
        else:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                return fn(*args, **kwargs)

        wrapper._api_meta = meta
        return wrapper

    return decorator


# ── Path conversion ─────────────────────────────────────────────────

_FLASK_VAR_RE = re.compile(r'<(?:[^:>]+:)?([^>]+)>')


def _flask_to_openapi_path(rule: str) -> str:
    """Convert ``/api/v1/tasks/<task_id>`` → ``/api/v1/tasks/{task_id}``."""
    return _FLASK_VAR_RE.sub(r'{\1}', rule)


def _path_parameters(rule: str) -> list[dict]:
    """Extract path parameters from a Flask rule into OpenAPI shape."""
    out = []
    for m in _FLASK_VAR_RE.finditer(rule):
        name = m.group(1)
        out.append({
            'name': name, 'in': 'path', 'required': True,
            'schema': {'type': 'string'},
        })
    return out


# ── Spec builder ───────────────────────────────────────────────────

# Routes we explicitly skip — internal/static/legacy.
_SKIP_PATTERNS = (
    re.compile(r'^/static(/|$)'),
    re.compile(r'^/$'),
    re.compile(r'^/(trading\.html|index\.html|favicon\.ico)$'),
    re.compile(r'^/api/openapi\.'),
    re.compile(r'^/api/docs(/|$)'),
    re.compile(r'^/api/redoc(/|$)'),
)


def _skip_rule(path: str) -> bool:
    return any(p.match(path) for p in _SKIP_PATTERNS)


def _default_responses() -> dict:
    return {
        '200': {'description': 'OK',
                 'content': {'application/json': {
                     'schema': {'type': 'object',
                                'properties': {'ok': {'type': 'boolean'}}}}}},
        '400': {'description': 'Bad Request',
                 'content': {'application/json': {
                     'schema': {'$ref': '#/components/schemas/ErrorEnvelope'}}}},
        '401': {'description': 'Unauthorized',
                 'content': {'application/json': {
                     'schema': {'$ref': '#/components/schemas/ErrorEnvelope'}}}},
        '403': {'description': 'Forbidden',
                 'content': {'application/json': {
                     'schema': {'$ref': '#/components/schemas/ErrorEnvelope'}}}},
        '404': {'description': 'Not Found',
                 'content': {'application/json': {
                     'schema': {'$ref': '#/components/schemas/ErrorEnvelope'}}}},
        '429': {'description': 'Too Many Requests',
                 'headers': {
                     'Retry-After': {'schema': {'type': 'integer'}},
                     'X-RateLimit-Remaining-Requests':
                         {'schema': {'type': 'integer'}},
                 },
                 'content': {'application/json': {
                     'schema': {'$ref': '#/components/schemas/ErrorEnvelope'}}}},
        '500': {'description': 'Internal Server Error',
                 'content': {'application/json': {
                     'schema': {'$ref': '#/components/schemas/ErrorEnvelope'}}}},
    }


def _tofu_config_schema() -> dict:
    """Generate the TofuConfig OpenAPI schema from the typed dataclass.

    Lives behind a function so we don't import lib.agent_options at module
    import time (lib.log + dataclasses keep this cheap, but we follow the
    project's lazy-import convention for cross-package edges).
    """
    from lib.agent_options import TofuOptions
    return TofuOptions.openapi_schema()


def _components() -> dict:
    return {
        'securitySchemes': {
            'bearerAuth': {
                'type': 'http', 'scheme': 'bearer',
                'bearerFormat': 'tofu_live_<32hex> | tofu_admin_<32hex>',
                'description': (
                    'API key bearer token. Issued via '
                    '`POST /api/v1/keys` (admin scope) or the Settings UI.'
                ),
            },
            'tunnelTokenHeader': {
                'type': 'apiKey', 'in': 'header',
                'name': 'X-Tunnel-Token',
                'description': (
                    'UI/cookie path. Single shared secret in '
                    '`TUNNEL_TOKEN` env var. Use Bearer for headless.'
                ),
            },
        },
        'schemas': {
            'ErrorEnvelope': {
                'type': 'object',
                'properties': {
                    'ok': {'type': 'boolean', 'enum': [False]},
                    'error': {
                        'oneOf': [
                            {'type': 'string'},
                            {'type': 'object',
                             'properties': {
                                 'kind': {'type': 'string'},
                                 'detail': {'type': 'string'},
                                 'context': {'type': 'string'},
                                 'source': {'type': 'string'},
                             }},
                        ],
                    },
                    'request_id': {'type': 'string'},
                },
                'required': ['ok', 'error'],
            },
            'ChatMessage': {
                'type': 'object',
                'required': ['role', 'content'],
                'properties': {
                    'role': {'type': 'string',
                              'enum': ['system', 'user', 'assistant', 'tool']},
                    'content': {
                        'oneOf': [
                            {'type': 'string'},
                            {'type': 'array',
                             'items': {'type': 'object'}},
                            {'type': 'null'},
                        ],
                    },
                    'name': {'type': 'string'},
                    'tool_calls': {'type': 'array',
                                    'items': {'type': 'object'}},
                    'tool_call_id': {'type': 'string'},
                },
            },
            'ChatCompletionRequest': {
                'type': 'object',
                'required': ['messages'],
                'properties': {
                    'model': {'type': 'string'},
                    'messages': {'type': 'array',
                                  'items': {'$ref': '#/components/schemas/ChatMessage'}},
                    'tools': {'type': 'array',
                               'items': {'type': 'object'}},
                    'tool_choice': {
                        'oneOf': [{'type': 'string'},
                                   {'type': 'object'}],
                    },
                    'temperature': {'type': 'number'},
                    'max_tokens': {'type': 'integer', 'minimum': 1},
                    'top_p': {'type': 'number'},
                    'stream': {'type': 'boolean'},
                    'stop': {
                        'oneOf': [{'type': 'string'},
                                   {'type': 'array',
                                    'items': {'type': 'string'}}],
                    },
                    'seed': {'type': 'integer'},
                    'response_format': {'type': 'object'},
                    'user': {'type': 'string'},
                    # Tofu-specific:
                    'config': {'$ref': '#/components/schemas/TofuConfig'},
                    'conversation_id': {'type': 'string'},
                    'idempotency_key': {'type': 'string'},
                },
            },
            'ChatCompletionResponse': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string'},
                    'object': {'type': 'string', 'enum': ['chat.completion']},
                    'created': {'type': 'integer'},
                    'model': {'type': 'string'},
                    'choices': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'index': {'type': 'integer'},
                                'message': {'$ref': '#/components/schemas/ChatMessage'},
                                'finish_reason': {'type': 'string'},
                            },
                        },
                    },
                    'usage': {
                        'type': 'object',
                        'properties': {
                            'prompt_tokens': {'type': 'integer'},
                            'completion_tokens': {'type': 'integer'},
                            'total_tokens': {'type': 'integer'},
                        },
                    },
                    'task_id': {'type': 'string',
                                 'description': 'Tofu-specific. Useful for '
                                                'follow-up polling/abort.'},
                },
            },
            # TofuConfig is generated from lib.agent_options.TofuOptions —
            # the canonical typed schema for an agent run.  Adding a field
            # there auto-propagates here.
            'TofuConfig': _tofu_config_schema(),
            'TaskState': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string'},
                    'kind': {'type': 'string'},
                    'status': {'type': 'string',
                                'enum': ['pending', 'running',
                                         'done', 'error', 'aborted']},
                    'created_at': {'type': 'number'},
                    'finished_at': {'type': 'number', 'nullable': True},
                    'result': {},
                    'error': {'$ref': '#/components/schemas/ErrorEnvelope'},
                    'meta': {'type': 'object', 'additionalProperties': True},
                },
            },
            'ApiKey': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string'},
                    'name': {'type': 'string'},
                    'prefix': {'type': 'string'},
                    'scopes': {'type': 'array', 'items': {'type': 'string'}},
                    'rate_limit_rpm': {'type': 'integer'},
                    'rate_limit_tpd': {'type': 'integer'},
                    'created_at': {'type': 'number'},
                    'last_used_at': {'type': 'number', 'nullable': True},
                    'expires_at': {'type': 'number', 'nullable': True},
                    'disabled': {'type': 'boolean'},
                },
            },
        },
    }


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


def _auto_tags(path: str) -> list[str]:
    if path.startswith('/api/v1/chat'):
        return ['chat']
    if path.startswith('/api/v1/tasks'):
        return ['tasks']
    if path.startswith('/api/v1/conversations'):
        return ['conversations']
    if path.startswith('/api/v1/capabilities'):
        return ['capabilities']
    if path.startswith('/api/v1/agents'):
        return ['agents']
    if path.startswith('/api/v1/keys'):
        return ['keys']
    if path.startswith('/api/v1/webhooks'):
        return ['webhooks']
    if path == '/v1/chat/completions' or path.startswith('/v1/chat'):
        return ['compat:openai']
    if path.startswith('/v1/messages'):
        return ['compat:anthropic']
    if path.startswith('/v1/'):
        return ['compat:openai']
    if path.startswith('/api/v1'):
        return ['v1']
    return ['legacy']


def _default_description() -> str:
    return (
        'Tofu is a self-hosted AI assistant. This OpenAPI spec covers '
        'three surfaces:\n\n'
        '* `/api/v1/*` — Tofu-native API. Stable, versioned, fully '
        'expressive (tool streams, plan/critic verdicts, swarm, paper, '
        'translate, scheduler, memory, MCP, trading).\n'
        '* `/v1/chat/completions`, `/v1/models`, `/v1/embeddings` — '
        'OpenAI-compatible adapter. Use any OpenAI SDK by pointing '
        '`base_url` at this server.\n'
        '* `/v1/messages` — Anthropic Messages API adapter. Use the '
        'Anthropic SDK by pointing `base_url` at this server.\n\n'
        'Auth: `Authorization: Bearer tofu_live_…` for headless callers, '
        'or `X-Tunnel-Token` / cookie for the browser UI. See '
        '[`docs/HEADLESS_API.md`](/docs/HEADLESS_API.md) for the full guide.'
    )


# ── Swagger UI / ReDoc embed pages ────────────────────────────────

def swagger_html(spec_url: str = '/api/openapi.json') -> str:
    """Return a single-file Swagger UI HTML page (CDN, no build step)."""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Tofu API · Swagger UI</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
  <style>
    body {{ margin: 0; background: #1e1e1e; }}
    .topbar {{ display: none; }}
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.ui = SwaggerUIBundle({{
      url: {json.dumps(spec_url)},
      dom_id: '#swagger-ui',
      deepLinking: true,
      persistAuthorization: true,
      displayRequestDuration: true,
      filter: true,
      tryItOutEnabled: true,
      defaultModelsExpandDepth: 1,
    }});
  </script>
</body>
</html>"""


def redoc_html(spec_url: str = '/api/openapi.json') -> str:
    """Return a single-file ReDoc HTML page."""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Tofu API · ReDoc</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>body {{margin: 0;}}</style>
</head>
<body>
  <redoc spec-url={json.dumps(spec_url)}></redoc>
  <script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"></script>
</body>
</html>"""


__all__ = ['api_meta', 'build_spec', 'swagger_html', 'redoc_html']
