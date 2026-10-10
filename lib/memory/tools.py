"""Provider-facing memory tool schemas backed by shared payload limits."""

from lib.memory.contracts import (
    MEMORY_BODY_MAX_CHARS,
    MEMORY_DESCRIPTION_MAX_CHARS,
    MEMORY_ID_MAX_CHARS,
    MEMORY_MERGE_MAX_ITEMS,
    MEMORY_NAME_MAX_CHARS,
    MEMORY_SEARCH_QUERY_MAX_CHARS,
    MEMORY_SEARCH_TOP_K_DEFAULT,
    MEMORY_SEARCH_TOP_K_MAX,
    MEMORY_SEARCH_TOP_K_MIN,
    MEMORY_TAG_MAX_CHARS,
    MEMORY_TAG_MAX_ITEMS,
)

__all__ = ['ALL_MEMORY_TOOLS', 'MEMORY_TOOL_NAMES',
           'CREATE_MEMORY_TOOL', 'UPDATE_MEMORY_TOOL',
           'DELETE_MEMORY_TOOL', 'MERGE_MEMORY_TOOL',
           'SEARCH_MEMORIES_TOOL']


CREATE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "create_memory",
        "description": (
            "Save a verified reusable lesson: project convention, "
            "reproduced failure/root cause/fix, or documented tool/API quirk. "
            "Never store identity/preferences (use My Context), one-off "
            "requests, chat summaries, speculation, reasoning, or transcripts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_DESCRIPTION_MAX_CHARS,
                    "description": (
                        "Write first: dense sentence front-loading searchable "
                        "symptom, symbol/file, and verified fix/rule; drives "
                        "search and prefetch."
                    ),
                },
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_NAME_MAX_CHARS,
                    "description": "Short descriptive title."
                },
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_BODY_MAX_CHARS,
                    "description": (
                        "Concise Markdown: context, reusable rule/fix, and "
                        "verification evidence; no chat/reasoning recap."
                    ),
                },
                "tags": {
                    "type": "array",
                    "maxItems": MEMORY_TAG_MAX_ITEMS,
                    "uniqueItems": True,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": MEMORY_TAG_MAX_CHARS,
                    },
                    "description": "Optional category tags."
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": (
                        "global applies across projects; project only to the "
                        "current project. Default: project"
                    ),
                }
            },
            "required": ["description", "name", "body"],
            "additionalProperties": False,
        }
    }
}

UPDATE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "update_memory",
        "description": (
            "Correct or extend an existing memory's search description, title, "
            "full body, or tags."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "maxLength": MEMORY_DESCRIPTION_MAX_CHARS,
                    "description": (
                        "Write first: one dense sentence front-loading symptom, "
                        "symbol/file, and verified fix/rule for search/prefetch."
                    ),
                },
                "memory_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_ID_MAX_CHARS,
                    "description": (
                        "ID from search_memories or <relevant_memories>."
                    ),
                },
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_NAME_MAX_CHARS,
                    "description": "New title."
                },
                "body": {
                    "type": "string",
                    "maxLength": MEMORY_BODY_MAX_CHARS,
                    "description": "New complete Markdown body; replaces the old body."
                },
                "tags": {
                    "type": "array",
                    "maxItems": MEMORY_TAG_MAX_ITEMS,
                    "uniqueItems": True,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": MEMORY_TAG_MAX_CHARS,
                    },
                    "description": "Complete replacement tag list."
                }
            },
            "required": ["memory_id"],
            "additionalProperties": False,
        }
    }
}

DELETE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "delete_memory",
        "description": "Delete an obsolete, incorrect, or duplicate memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_ID_MAX_CHARS,
                    "description": "Memory ID to delete."
                }
            },
            "required": ["memory_id"],
            "additionalProperties": False,
        }
    }
}

MERGE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "merge_memories",
        "description": (
            "Consolidate 2–32 overlapping memories, then delete originals. "
            "Use when one replacement is clearer; deletion failures are reported."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "memory_ids": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": MEMORY_MERGE_MAX_ITEMS,
                    "uniqueItems": True,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": MEMORY_ID_MAX_CHARS,
                    },
                    "description": "Unique source memory IDs."
                },
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_DESCRIPTION_MAX_CHARS,
                    "description": (
                        "Write first: dense sentence front-loading merged search "
                        "triggers and the verified rule."
                    ),
                },
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_NAME_MAX_CHARS,
                    "description": "Merged memory title."
                },
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_BODY_MAX_CHARS,
                    "description": "Complete consolidated Markdown body."
                },
                "tags": {
                    "type": "array",
                    "maxItems": MEMORY_TAG_MAX_ITEMS,
                    "uniqueItems": True,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": MEMORY_TAG_MAX_CHARS,
                    },
                    "description": "Merged tags; omit to union source tags."
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": "Destination scope. Default: project"
                }
            },
            "required": ["memory_ids", "description", "name", "body"],
            "additionalProperties": False,
        }
    }
}

SEARCH_MEMORIES_TOOL = {
    "type": "function",
    "function": {
        "name": "search_memories",
        "description": (
            "Search durable lessons narrowly when this project may have an "
            "established convention or past fix. Not for local files "
            "(read/find/run_command), external facts (web), an already-prefetched "
            "<relevant_memories> topic, or skills (load_skill)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MEMORY_SEARCH_QUERY_MAX_CHARS,
                    "description": "Specific symptom, symbol/file, fix, or rule keywords."
                },
                "top_k": {
                    "type": "integer",
                    "minimum": MEMORY_SEARCH_TOP_K_MIN,
                    "maximum": MEMORY_SEARCH_TOP_K_MAX,
                    "default": MEMORY_SEARCH_TOP_K_DEFAULT,
                    "description": "Maximum results."
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        }
    }
}

ALL_MEMORY_TOOLS = [CREATE_MEMORY_TOOL, UPDATE_MEMORY_TOOL, DELETE_MEMORY_TOOL, MERGE_MEMORY_TOOL, SEARCH_MEMORIES_TOOL]
MEMORY_TOOL_NAMES = {'create_memory', 'update_memory', 'delete_memory', 'merge_memories', 'search_memories'}
