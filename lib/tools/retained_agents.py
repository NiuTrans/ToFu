"""Model-facing contract for durable, reusable Swarm identities."""

MANAGE_AGENTS_TOOL = {
    'type': 'function',
    'function': {
        'name': 'manage_agents',
        'description': (
            'Manage reusable team agents. A retained agent remembers prior '
            'assignments across turns and restarts while using no worker when '
            'idle. Use spawn_agents for one-off independent work. Create once, '
            'then assign later objectives by agent_id; the assignment runs '
            'as an ordinary async Swarm child and returns via <swarm-update>. '

            'Assign requires an idle identity; a busy agent rejects another assignment. '
            'Use send_agent_message with retained_agent_id to message this colleague: '
            'busy adds input to the owned current assignment; idle starts the next turn '
            'with saved dialogue. Cancelled colleagues require intent=resume. '
            'For steering only, send the exact agent_id plus swarm_run_id; it never '
            'starts replacement work. Use get_agent_messages with the original target '
            'and idempotency_key to reconcile uncertainty. Do not treat assign as live steering. '
            'Scope defaults to the current project, or conversation when no '
            'project is attached. Current tool permissions apply on every run. '
            'Use list_agents to inspect a bounded memory view. Reset clears working memory; '
            'archive pauses assignments; delete removes the retained record. '
            'Promote copies a completed temporary Swarm child in this '
            'conversation into a new retained identity.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'action': {
                    'type': 'string',
                    'enum': ['create', 'promote', 'assign',
                             'reset', 'archive', 'restore', 'delete'],
                },
                'scope': {'type': 'string', 'enum': ['project', 'conversation'],
                          'description': 'Omit to use the current default scope.'},
                'agent_id': {'type': 'string', 'description': 'Required except for create and list.'},
                'source_agent_id': {'type': 'string',
                                    'description': 'Completed Swarm child ID for promote.'},
                'name': {'type': 'string', 'description': 'Human-readable name for create.'},
                'role': {'type': 'string', 'description': 'Spawnable Swarm role for create.'},
                'objective': {'type': 'string', 'description': 'New assignment objective.'},
                'context': {'type': 'string', 'description': 'Fresh assignment context.'},
                'tools': {'type': 'array', 'items': {'type': 'string'},
                          'description': 'Optional narrow-only tool allowlist for this run.'},
                'abandon': {'type': 'boolean',
                            'description': 'For reset of a busy agent: fence its late memory result.'},
            },
            'required': ['action'],
        },
    },
}


LIST_AGENTS_TOOL = {
    'type': 'function',
    'function': {
        'name': 'list_agents',
        'description': (
            'List reusable team agents in the current project or conversation. '
            'Pass agent_id to inspect one agent and a bounded view of its '
            'remembered recent dialogue. This is read-only; use manage_agents '
            'to create, assign, reset, archive, restore, or delete.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'agent_id': {'type': 'string',
                             'description': 'Optional retained agent to inspect.'},
                'scope': {'type': 'string', 'enum': ['project', 'conversation'],
                          'description': 'Omit to use the current default scope.'},
            },
        },
    },
}


__all__ = ['MANAGE_AGENTS_TOOL', 'LIST_AGENTS_TOOL']
