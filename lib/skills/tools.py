"""lib/skills/tools.py — Tool schema for runtime skill loading.

Exactly ONE tool: ``load_skill`` (progressive disclosure). There is no
``list_skills`` tool on purpose — the ``<available_skills>`` index rides the
system prompt on every tool-bearing turn, so a list tool would only
duplicate what the model can already see.
"""

LOAD_SKILL_TOOL = {
    'type': 'function',
    'function': {
        'name': 'load_skill',
        'description': (
            'Load an installed skill package\'s full instructions '
            '(progressive disclosure). The <available_skills> block in the '
            'system prompt lists every enabled, eligible skill by id + one-line '
            'trigger description; when the user\'s task matches a skill\'s '
            'description, call this BEFORE doing the task to load the full '
            'SKILL.md guide and a manifest of its bundled reference/script '
            'files (read those on demand with read_files). Skills are '
            'USER-installed capability packs — a different thing from '
            'memories (use search_memories for those). Loading lasts for the '
            'current task; after compaction, call load_skill again if full '
            'details are needed. Do NOT call this '
            'when no installed skill matches the task.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'skill_id': {
                    'type': 'string',
                    'description': (
                        'The exact skill id, as '
                        'listed in <available_skills>.'
                    ),
                },
            },
            'required': ['skill_id'],
        },
    },
}

ALL_SKILL_TOOLS = [LOAD_SKILL_TOOL]
SKILL_TOOL_NAMES = {'load_skill'}

__all__ = ['LOAD_SKILL_TOOL', 'ALL_SKILL_TOOLS', 'SKILL_TOOL_NAMES']
