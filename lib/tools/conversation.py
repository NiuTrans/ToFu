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
            'Retrieve a conversation by ID only when the user explicitly '
            'requests past-conversation information.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'conversation_id': {'type': 'string'},
                'include_tool_details': {'type': 'boolean'},
                'raw': {'type': 'boolean'},
                'limit': {'type': 'integer', 'minimum': 1},
                'before': {'type': 'integer', 'minimum': 1},
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
            'Run enabled project checkers, checkpoint, and submit this '
            "execution's isolated workspace for human review. Checker "
            'failure rejects submission and records a Project Feed narrative.'
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
