"""lib/skills/tools.py — Tool definitions for LLM function calling."""

__all__ = ['ALL_SKILL_TOOLS', 'SKILL_TOOL_NAMES',
           'CREATE_SKILL_TOOL', 'UPDATE_SKILL_TOOL',
           'DELETE_SKILL_TOOL', 'MERGE_SKILLS_TOOL']


CREATE_SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "create_skill",
        "description": (
            "Save a new skill (accumulated experience) for future sessions. "
            "Call this proactively whenever you discover a bug pattern, project "
            "convention, user preference, complex workflow, or tool/API quirk. "
            "Skills are stored as Markdown files and can be loaded in future "
            "sessions when the user enables them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short descriptive name for the skill"
                },
                "description": {
                    "type": "string",
                    "description": "One-line description of what this skill captures"
                },
                "body": {
                    "type": "string",
                    "description": "The full skill content in Markdown — instructions, patterns, conventions, code examples"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for categorization (e.g. ['python', 'testing', 'convention'])"
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": "Where to store the skill: 'global' (all projects) or 'project' (current project only). Default: 'project'"
                }
            },
            "required": ["name", "description", "body"]
        }
    }
}

UPDATE_SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "update_skill",
        "description": (
            "Update an existing skill's content, description, or tags. "
            "Use this when you discover new information that extends or corrects "
            "an existing skill, or when a skill's description needs improvement."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": "The ID of the skill to update (from list_all_skills or the skill's filename without .md)"
                },
                "name": {
                    "type": "string",
                    "description": "New name for the skill (optional)"
                },
                "description": {
                    "type": "string",
                    "description": "New one-line description (optional)"
                },
                "body": {
                    "type": "string",
                    "description": "New full skill content in Markdown (optional)"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New tags for categorization (optional)"
                }
            },
            "required": ["skill_id"]
        }
    }
}

DELETE_SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "delete_skill",
        "description": (
            "Remove an outdated, incorrect, or duplicate skill. "
            "Use this when a skill is completely obsolete, contains harmful "
            "misinformation, or is a duplicate of another better skill."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": "The ID of the skill to delete"
                }
            },
            "required": ["skill_id"]
        }
    }
}

MERGE_SKILLS_TOOL = {
    "type": "function",
    "function": {
        "name": "merge_skills",
        "description": (
            "Combine multiple overlapping or related skills into one consolidated skill. "
            "The original skills are deleted after merging. Use this when two or more "
            "skills cover similar topics and would be better as a single comprehensive skill."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of skill IDs to merge (at least 2)"
                },
                "name": {
                    "type": "string",
                    "description": "Name for the merged skill"
                },
                "description": {
                    "type": "string",
                    "description": "One-line description for the merged skill"
                },
                "body": {
                    "type": "string",
                    "description": "The consolidated skill content in Markdown — combine the best parts of all source skills"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for the merged skill (optional — if omitted, tags from all source skills are combined)"
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": "Where to store the merged skill: 'global' or 'project'. Default: 'project'"
                }
            },
            "required": ["skill_ids", "name", "description", "body"]
        }
    }
}

ALL_SKILL_TOOLS = [CREATE_SKILL_TOOL, UPDATE_SKILL_TOOL, DELETE_SKILL_TOOL, MERGE_SKILLS_TOOL]
SKILL_TOOL_NAMES = {'create_skill', 'update_skill', 'delete_skill', 'merge_skills'}
