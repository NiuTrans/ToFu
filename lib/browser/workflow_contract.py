"""Model-facing bounded site workflow schemas; consumed by tools and execution.

Adapter operation schemas remain in adapters.py. References select earlier
results, never execute expressions. Session references are routing hints, not
credentials or authorization grants.
"""

MAX_OUTPUT_CHARS = 80000
MAX_DURATION_S = 120

SESSION_REF_SCHEMA = {'type': 'string', 'minLength': 1, 'maxLength': 1024,
                      'description': 'Routing reference returned by action=status; never a credential.'}

SESSION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['owner', 'device', 'profile', 'site', 'expires_at'],
    'properties': {
        **{key: {'type': 'string', 'maxLength': 256}
           for key in ('owner', 'device', 'profile', 'site')},
        'expires_at': {'type': 'number'},
    },
}

WORKFLOW_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['steps'],
    'properties': {
        'timeout_s': {'type': 'integer', 'minimum': 1, 'maximum': MAX_DURATION_S},
        'max_chars': {'type': 'integer', 'minimum': 4096, 'maximum': MAX_OUTPUT_CHARS},
        'steps': {
            'type': 'array', 'minItems': 1,
            'items': {
                'type': 'object', 'additionalProperties': False,
                'required': ['id', 'operation'],
                'properties': {
                    'id': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,31}$'},
                    'operation': {'type': 'string', 'maxLength': 64},
                    'params': {'type': 'object', 'maxProperties': 16},
                    'foreach': {
                        'type': 'object', 'additionalProperties': False,
                        'required': ['step', 'field', 'parameter'],
                        'properties': {
                            'step': {'type': 'string', 'maxLength': 32},
                            'field': {'type': 'string', 'pattern': '^[a-zA-Z_][a-zA-Z0-9_]{0,63}$'},
                            'parameter': {'type': 'string', 'pattern': '^[a-zA-Z_][a-zA-Z0-9_]{0,63}$'},
                            'limit': {'type': 'integer', 'minimum': 1},
                            'parallelism': {'type': 'integer', 'minimum': 1,
                                'description': 'Optional caller-selected concurrent reads; omitted reads start on demand.'},
                        },
                    },
                },
            },
        },
    },
}

# The base browser catalogue has a strict context budget. Like adapter params,
# detailed workflow schemas are discovered once through site inspection; the
# execution boundary always validates against the full authority above.
WORKFLOW_TOOL_SCHEMA = {
    'type': WORKFLOW_SCHEMA['type'], 'required': WORKFLOW_SCHEMA['required'],
    'additionalProperties': False,
    'properties': {
        **{name: WORKFLOW_SCHEMA['properties'][name] for name in ('timeout_s', 'max_chars')},
        'steps': {**{key: value for key, value in WORKFLOW_SCHEMA['properties']['steps'].items()
                     if key != 'items'}, 'items': {'type': 'object'}},
    },
    'description': 'Bounded read plan. Inspect a site for workflow_schema and examples.',
}
