"""lib/memory/tools.py — Tool definitions for LLM function calling."""

__all__ = ['ALL_MEMORY_TOOLS', 'MEMORY_TOOL_NAMES',
           'CREATE_MEMORY_TOOL', 'UPDATE_MEMORY_TOOL',
           'DELETE_MEMORY_TOOL', 'MERGE_MEMORY_TOOL',
           'SEARCH_MEMORIES_TOOL']


CREATE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "create_memory",
        "description": (
            "Save verified, reusable project experience for future sessions: "
            "a confirmed convention, reproduced failure/root-cause/fix, or "
            "documented tool/API quirk. Never save user identity/preferences, "
            "one-off requests, chat summaries, speculative reasoning, or a "
            "solution transcript; user-specific facts belong to My Context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Generated FIRST. ONE dense ~120-char sentence that front-loads the concrete search triggers (symptom, the symbol/file name, the fix or rule). This is the primary signal for both search_memories and per-turn prefetch ranking — vague summaries like 'fixes a bug' are useless. Don't pad to a fixed length; pack signal."
                },
                "name": {
                    "type": "string",
                    "description": "Short descriptive name for the memory"
                },
                "body": {
                    "type": "string",
                    "description": "Concise reusable evidence as skimmable Markdown, never a conversation or reasoning recap. Include the verified context, concrete rule/fix, and evidence/test that established it."
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for categorization (e.g. ['python', 'testing', 'convention'])"
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": "Where to store the memory: 'global' (all projects) or 'project' (current project only). Default: 'project'"
                }
            },
            "required": ["description", "name", "body"]
        }
    }
}

UPDATE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "update_memory",
        "description": (
            "Update an existing memory's content, description, or tags. "
            "Use this when you discover new information that extends or corrects "
            "an existing memory, or when a memory's description needs improvement."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Generated FIRST. New ONE dense ~120-char sentence front-loading search triggers (symptom, symbol/file, fix/rule) — the primary ranking signal for search and prefetch."
                },
                "memory_id": {
                    "type": "string",
                    "description": "The ID of the memory to update (the memory's filename without .md, as shown in a search_memories result or the injected <relevant_memories> block)"
                },
                "name": {
                    "type": "string",
                    "description": "New name for the memory (optional)"
                },
                "body": {
                    "type": "string",
                    "description": "New full memory content in Markdown (optional)"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New tags for categorization (optional)"
                }
            },
            "required": ["memory_id"]
        }
    }
}

DELETE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "delete_memory",
        "description": (
            "Remove an outdated, incorrect, or duplicate memory. "
            "Use this when a memory is completely obsolete, contains harmful "
            "misinformation, or is a duplicate of another better memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "The ID of the memory to delete"
                }
            },
            "required": ["memory_id"]
        }
    }
}

MERGE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "merge_memories",
        "description": (
            "Combine multiple overlapping or related memories into one consolidated memory. "
            "The original memories are deleted after merging. Use this when two or more "
            "memories cover similar topics and would be better as a single comprehensive memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "memory_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of memory IDs to merge (at least 2)"
                },
                "description": {
                    "type": "string",
                    "description": "Generated FIRST. ONE dense ~120-char sentence front-loading the merged memory's search triggers (symptom, symbol/file, fix/rule) — the primary ranking signal for search and prefetch."
                },
                "name": {
                    "type": "string",
                    "description": "Name for the merged memory"
                },
                "body": {
                    "type": "string",
                    "description": "The consolidated memory content in Markdown — combine the best parts of all source memories"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for the merged memory (optional — if omitted, tags from all source memories are combined)"
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": "Where to store the merged memory: 'global' or 'project'. Default: 'project'"
                }
            },
            "required": ["memory_ids", "description", "name", "body"]
        }
    }
}

SEARCH_MEMORIES_TOOL = {
    "type": "function",
    "function": {
        "name": "search_memories",
        "description": (
            "Search your accumulated memories (past experiences, bug patterns, "
            "project conventions, workflow recipes) by keyword. "
            "Use this NARROWLY: when you suspect THIS project has an established "
            "convention you've forgotten, or a logged lesson applies to the current "
            "problem. Do NOT use this as a generic discovery step — if the user "
            "mentions a local file path, use read_files/find_files or a plain ls "
            "through run_command; if they ask about "
            "an external project / library / product, use web_search or read its "
            "local copy. A `<relevant_memories>` block, when present, was already "
            "prefetched for this turn — don't re-search the same topic. "
            "Installed skill packages are NOT memories and are not in this corpus — "
            "see the <available_skills> block and use load_skill for those."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords — use specific terms related to what you're looking for (e.g. 'flask blueprint circular import', 'NCCL socket retry', 'cache invalidation pattern')"
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 30)"
                }
            },
            "required": ["query"]
        }
    }
}

ALL_MEMORY_TOOLS = [CREATE_MEMORY_TOOL, UPDATE_MEMORY_TOOL, DELETE_MEMORY_TOOL, MERGE_MEMORY_TOOL, SEARCH_MEMORIES_TOOL]
MEMORY_TOOL_NAMES = {'create_memory', 'update_memory', 'delete_memory', 'merge_memories', 'search_memories'}
