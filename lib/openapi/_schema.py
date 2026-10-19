"""lib/openapi/_schema.py — OpenAPI ``components`` + response templates.

Holds the static schema library used by :mod:`lib.openapi._spec`:

  * ``_default_responses`` — the shared status-code → response map.
  * ``_tofu_config_schema`` — lazily derived from
    ``lib.agent_loop.agent_options.TofuOptions`` (kept behind a function so we
    don't import that module at import time).
  * ``_components`` — the full ``components`` object (security schemes +
    schemas), stitching ``_tofu_config_schema`` in as ``TofuConfig``.
"""

from __future__ import annotations

from lib.error_envelope import typed_error_envelope_schema
from lib.log import get_logger

logger = get_logger(__name__)


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


def media_responses(*media_types: str, conditional: bool = False) -> dict:
    """Declare success media without guessing product/file shape from a MIME.

    JSON permits arbitrary domain values; text is a string and binary media
    are bytes. Routes opt into Range/cache responses only when their serving
    seam implements them. Standard error statuses remain documented.
    """
    if not media_types:
        raise ValueError('At least one success media type is required')
    content = {}
    for media_type in media_types:
        schema = {} if media_type in ('application/json', '*/*') else {'type': 'string'}
        if schema and not media_type.startswith('text/'):
            schema['format'] = 'binary'
        content[media_type] = {'schema': schema}
    responses = _default_responses()
    responses['200'] = {'description': 'OK', 'content': content}
    if conditional:
        from copy import deepcopy
        responses['206'] = {'description': 'Partial content', 'content': deepcopy(content)}
        responses['304'] = {'description': 'Not modified'}
        responses['416'] = {'description': 'Range not satisfiable', 'content': {
            'application/json': {'schema': {'$ref': '#/components/schemas/ErrorEnvelope'}}}}
    return responses


def _tofu_config_schema() -> dict:
    """Generate the TofuConfig OpenAPI schema from the typed dataclass.

    Lives behind a function so we don't import lib.agent_loop.agent_options at module
    import time (lib.log + dataclasses keep this cheap, but we follow the
    project's lazy-import convention for cross-package edges).
    """
    from lib.agent_loop.agent_options import TofuOptions
    return TofuOptions.openapi_schema()


def _components() -> dict:
    from copy import deepcopy
    from lib.generated_api_v4 import OPENAPI_DOCUMENT

    result = {
        'securitySchemes': {
            'bearerAuth': {
                'type': 'http', 'scheme': 'bearer',
                'bearerFormat': 'tofu_live_<32hex> | tofu_admin_<32hex>',
                'description': (
                    'API key bearer token. Issued via '
                    '`POST /api/v4/keys` (admin scope) or the Settings UI.'
                ),
            },
        },
        'schemas': {
            # The inner, reusable error object. Runtime validation and this
            # schema share one definition in lib.error_envelope.
            'TypedErrorEnvelope': typed_error_envelope_schema(),
            # HTTP error-response wrapper. Keep this established component
            # name for SDK compatibility; task.error references the typed
            # inner object instead.
            'ErrorEnvelope': {
                'type': 'object',
                'properties': {
                    'ok': {'type': 'boolean', 'enum': [False]},
                    'error': {
                        'anyOf': [
                            {'type': 'string'},
                            {'$ref': '#/components/schemas/TypedErrorEnvelope'},
                            {
                                'type': 'object',
                                'additionalProperties': True,
                                'description': (
                                    'Legacy structured error. New endpoints '
                                    'should emit TypedErrorEnvelope.'),
                            },
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
            'ChatCompletionBaseRequest': {
                'type': 'object',
                'required': ['messages'],
                'properties': {
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
            'NativeModelSelection': {
                'oneOf': [
                    {
                        'type': 'object',
                        'additionalProperties': False,
                        'required': ['creator_id', 'model_id'],
                        'properties': {
                            'creator_id': {'type': 'string', 'minLength': 1},
                            'model_id': {'type': 'string', 'minLength': 1},
                        },
                    },
                    {
                        'type': 'object',
                        'additionalProperties': False,
                        'required': ['provider_id', 'offering_id'],
                        'properties': {
                            'provider_id': {'type': 'string', 'minLength': 1},
                            'offering_id': {'type': 'string', 'minLength': 1},
                        },
                    },
                ],
            },
            # Compatibility/direct endpoints retain the upstream string wire
            # shape; only the native route uses the structured v2 identity.
            'ChatCompletionRequest': {
                'allOf': [
                    {'$ref': '#/components/schemas/ChatCompletionBaseRequest'},
                    {
                        'type': 'object',
                        'required': ['model'],
                        'properties': {'model': {'type': 'string'}},
                    },
                ],
            },
            'NativeChatCompletionRequest': {
                'allOf': [
                    {'$ref': '#/components/schemas/ChatCompletionBaseRequest'},
                    {
                        'type': 'object',
                        'required': ['model'],
                        'properties': {
                            'model': {
                                '$ref': '#/components/schemas/NativeModelSelection',
                            },
                            'routing': {'type': 'object'},
                        },
                    },
                ],
            },
            'ChatCompletionResponse': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string'},
                    'object': {'type': 'string', 'enum': ['chat.completion']},
                    'created': {'type': 'integer'},
                    'model': {'type': 'string'},
                    'provider_id': {'type': 'string', 'maxLength': 160,
                                    'description': 'Final dispatch provider identity; empty when unknown. Earlier attempts may use other routes.'},
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
            # TofuConfig is generated from lib.agent_loop.agent_options.TofuOptions —
            # the canonical typed schema for an agent run.  Adding a field
            # there auto-propagates here.
            'TofuConfig': _tofu_config_schema(),
            'TaskState': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string'},
                    'kind': {'type': 'string'},
                    'readSource': {'type': 'string', 'enum': ['live', 'durable'],
                                   'description': 'Authority used for this state read; durable is a committed owner-scoped row.'},
                    'streamState': {'type': 'string',
                                    'description': 'Native provider stream termination evidence, retained in committed task results.'},
                    'status': {'type': 'string',
                                'enum': ['pending', 'running',
                                         'done', 'error', 'aborted',
                                         'interrupted']},
                    'created_at': {'type': 'number'},
                    'finished_at': {'type': 'number', 'nullable': True},
                    'event_replay': {
                        'type': 'object',
                        'description': 'Retained event-window summary. Fetch '
                                       'event bodies from the cursor replay '
                                       'endpoint.',
                        'properties': {
                            'retained_count': {'type': 'integer'},
                            'base_cursor': {'type': 'integer'},
                            'next_cursor': {'type': 'integer'},
                        },
                    },
                    'result': {},
                    'result_available': {'type':'boolean', 'description':'Cold research-action records report whether the original result payload was retained.'},
                    'result_digest': {'type':'string', 'nullable':True, 'pattern':'^[0-9a-f]{64}$'},
                    'error': {
                        'oneOf': [
                            {'$ref': '#/components/schemas/TypedErrorEnvelope'},
                            {'type': 'null'},
                        ],
                    },
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
    for kind, values in OPENAPI_DOCUMENT['components'].items():
        result.setdefault(kind, {}).update(deepcopy(values))
    return result
