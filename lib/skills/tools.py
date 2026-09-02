"""Small model-tool surface for bounded skill progressive disclosure."""

SEARCH_SKILLS_TOOL = {
    'type': 'function',
    'function': {
        'name': 'search_skills',
        'description': (
            'Search installed skills and the offline catalog by task need, '
            'then query ClawHub on demand when online=true. Use this when the '
            'compact <available_skills> index has no match or says entries '
            'were omitted. Send only a short capability phrase—never secrets, '
            'code, or user data. Results contain exact ids; never invent one.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'minLength': 1,
                    'maxLength': 160,
                    'description': 'Short capability or workflow needed.',
                },
                'limit': {
                    'type': 'integer',
                    'minimum': 1,
                    'maximum': 8,
                    'default': 5,
                },
                'online': {
                    'type': 'boolean',
                    'default': True,
                    'description': (
                        'Allow this explicit search call to contact the public '
                        'ClawHub registry. No online catalog is preloaded.'),
                },
            },
            'required': ['query'],
        },
    },
}

LOAD_SKILL_TOOL = {
    'type': 'function',
    'function': {
        'name': 'load_skill',
        'description': (
            'Load the first bounded page of an installed skill guide and its '
            'resource manifest. Call it before using a matching workflow. A '
            'skill is guidance only and grants no permissions.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'skill_id': {
                    'type': 'string',
                    'minLength': 1,
                    'maxLength': 128,
                    'description': 'Exact installed skill id.',
                },
            },
            'required': ['skill_id'],
        },
    },
}

READ_SKILL_RESOURCE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'read_skill_resource',
        'description': (
            'Read one bounded UTF-8 page from an installed skill resource '
            'using its opaque skill:// path. Binary, oversized, symlinked, or '
            'escaping paths are rejected.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'skill_id': {
                    'type': 'string',
                    'minLength': 1,
                    'maxLength': 128,
                },
                'resource': {
                    'type': 'string',
                    'minLength': 1,
                    'maxLength': 512,
                    'description': (
                        'Package-relative path or skill://<id>/<path> from '
                        'load_skill.'),
                },
                'cursor': {
                    'type': 'integer',
                    'minimum': 0,
                    'default': 0,
                },
                'max_chars': {
                    'type': 'integer',
                    'minimum': 1,
                    'maximum': 12000,
                    'default': 6000,
                },
            },
            'required': ['skill_id', 'resource'],
        },
    },
}

REQUEST_SKILL_INSTALL_TOOL = {
    'type': 'function',
    'function': {
        'name': 'request_skill_install',
        'description': (
            'Request installation of one exact verified catalog match from '
            'search_skills. This always pauses for real user confirmation, '
            'even in Auto mode, and is rejected when unattended. It never '
            'runs bundled scripts. Do not call for unavailable entries.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'catalog_id': {
                    'type': 'string',
                    'minLength': 1,
                    'maxLength': 128,
                    'description': 'Exact available catalog_id from search_skills.',
                },
                'source_revision': {
                    'type': 'string',
                    'minLength': 1,
                    'maxLength': 128,
                    'description': (
                        'Exact source_revision from search_skills. Required '
                        'for online matches; omit only for a sealed offline '
                        'catalog entry that did not show one.'),
                },
                'scope': {
                    'type': 'string',
                    'enum': ['global', 'project'],
                    'default': 'global',
                },
                'overwrite': {
                    'type': 'boolean',
                    'default': False,
                    'description': 'Replace the same installed id if present.',
                },
                'reason': {
                    'type': 'string',
                    'maxLength': 500,
                    'description': (
                        'Why this skill materially improves the current task; '
                        'shown in the confirmation dialog.'),
                },
            },
            'required': ['catalog_id', 'reason'],
        },
    },
}

SKILL_READ_TOOLS = [
    SEARCH_SKILLS_TOOL,
    LOAD_SKILL_TOOL,
    READ_SKILL_RESOURCE_TOOL,
]
SKILL_INSTALL_TOOLS = [
    REQUEST_SKILL_INSTALL_TOOL,
]
ALL_SKILL_TOOLS = SKILL_READ_TOOLS + SKILL_INSTALL_TOOLS
SKILL_TOOL_NAMES = {
    'search_skills',
    'load_skill',
    'read_skill_resource',
    'request_skill_install',
}

__all__ = [
    'ALL_SKILL_TOOLS',
    'LOAD_SKILL_TOOL',
    'READ_SKILL_RESOURCE_TOOL',
    'REQUEST_SKILL_INSTALL_TOOL',
    'SEARCH_SKILLS_TOOL',
    'SKILL_INSTALL_TOOLS',
    'SKILL_READ_TOOLS',
    'SKILL_TOOL_NAMES',
]
