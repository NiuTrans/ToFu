"""Conversation-reference and retained Integration tool definitions.

Project Brain itself is runtime-signal driven and contributes zero model tool
schemas.  Only the two execution controls required by an already-isolated
workspace remain model-callable.
"""

CONV_REF_LIST_TOOL = {
    'type': 'function',
    'function': {
        'name': 'list_conversations',
        'description': (
            'Search other conversations only when the user explicitly asks '
            'for past-conversation information. Project mode scopes to sibling '
            'conversations unless scope=all.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'keyword': {'type': 'string'},
                'limit': {'type': 'integer'},
                'scope': {
                    'type': 'string', 'enum': ['auto', 'project', 'all'],
                },
            },
            'required': [],
        },
    },
}

CONV_REF_GET_TOOL = {
    'type': 'function',
    'function': {
        'name': 'get_conversation',
        'description': (
            'Retrieve by ID only when the user explicitly requests '
            'past-conversation information; never call proactively. Find its ID '
            'with list_conversations.\n\n'
            '`raw` defaults to true: a structured, unsummarized DB record with '
            'row metadata/settings and every field of each delivered message, '
            'including full tool rounds. Set `raw=false` only for a readable '
            'transcript; it drops per-message metadata and condenses tool rounds.\n\n'
            'Long histories return a parseable head+tail window of whole messages. '
            'Check `DELIVERED N of M`; use its positive `before=N` hint for older '
            'pages. `limit` widens the window but may clamp fields, which the '
            'header reports.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'conversation_id': {
                    'type': 'string',
                    'description': 'Conversation ID, usually from list_conversations.',
                },
                'include_tool_details': {
                    'type': 'boolean',
                    'description': (
                        'For raw=false only, include full tool arguments/results '
                        '(default true); false shortens them. Raw mode always '
                        'includes them.'
                    ),
                },
                'raw': {
                    'type': 'boolean',
                    'description': (
                        'Default true: structured raw record with all fields of '
                        'each delivered message. False returns readable prose that '
                        'drops message metadata and condenses tool rounds.'
                    ),
                },
                'limit': {
                    'type': 'integer',
                    'minimum': 1,
                    'description': (
                        'Recent-message count (min 1). Omit for a budget-fit '
                        'whole-message window; too large may clamp fields, reported '
                        'in the header.'
                    ),
                },
                'before': {
                    'type': 'integer',
                    'minimum': 1,
                    'description': (
                        'Exclusive 1-based end cursor (min 1) for an older page. '
                        "Omit for latest; use the header's positive before=N hint. "
                        'Never pass 0.'
                    ),
                },
            },
            'required': ['conversation_id'],
        },
    },
}

CONV_REF_TOOLS = [CONV_REF_LIST_TOOL, CONV_REF_GET_TOOL]
CONV_REF_TOOL_NAMES = {'list_conversations', 'get_conversation'}


INTEGRATION_CHECKPOINT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'integration_checkpoint',
        'description': (
            'Capture the current isolated writer workspace as a checkpoint. '
            "The runtime binds this call to this execution's automatic work ID."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'note': {
                    'type': 'string',
                    'description': 'Optional one-line milestone note.',
                },
            },
            'required': [],
        },
    },
}

INTEGRATION_SUBMIT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'integration_submit',
        'description': (
            'Save a checkpoint of this execution. Automatic task changes are '
            'merged into the current canonical workspace at successful '
            'completion, independently of commits and release gates. '
            'Explicit candidate submissions retain their review/checker policy.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'summary': {
                    'type': 'string',
                    'description': 'What changed and how it was verified.',
                },
            },
            'required': ['summary'],
        },
    },
}

INTEGRATION_TOOLS = [INTEGRATION_CHECKPOINT_TOOL, INTEGRATION_SUBMIT_TOOL]
INTEGRATION_TOOL_NAMES = {'integration_checkpoint', 'integration_submit'}

__all__ = [
    'CONV_REF_LIST_TOOL', 'CONV_REF_GET_TOOL', 'CONV_REF_TOOLS',
    'CONV_REF_TOOL_NAMES', 'INTEGRATION_CHECKPOINT_TOOL',
    'INTEGRATION_SUBMIT_TOOL', 'INTEGRATION_TOOLS',
    'INTEGRATION_TOOL_NAMES',
]
