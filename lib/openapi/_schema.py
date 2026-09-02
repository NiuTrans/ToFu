"""lib/openapi/_schema.py — OpenAPI ``components`` + response templates.

Holds the static schema library used by :mod:`lib.openapi._spec`:

  * ``_default_responses`` — the shared status-code → response map.
  * ``_tofu_config_schema`` — lazily derived from
    ``lib.agent_options.TofuOptions`` (kept behind a function so we
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
