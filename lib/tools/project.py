"""lib/tools/project.py — Project co-pilot tool definitions."""

import copy
import os

PROJECT_TOOL_GREP = {
    "type": "function",
    "function": {
        "name": "grep_search",
        "description": (
            "Search project file content and return paths, line numbers, and matches. "
            "Prefer this over shell grep/rg: a persistent file index plus ripgrep over "
            "exact candidates avoids recursive walks on huge/network (FUSE) trees, "
            "skips ignored directories, and is case-insensitive by default. Prefer "
            "short literal keywords; regex uses Rust/ripgrep syntax (`A|B`, `\\.` for "
            "a literal dot, per-line `^`/`$`), not GNU BRE. `path` is one relative "
            "path or project root. For independent roots/patterns use one `searches` "
            "batch (max 20 entries); it runs together and avoids sequential model "
            "rounds."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Short literal substring preferred; Rust/ripgrep regex is supported."},
                "path": {"type": "string", "description": "One relative path; omit for project root."},
                "include": {"type": "string", "description": "Optional file glob such as `*.py`."},
                "context_lines": {"type": "integer", "description": "Lines before/after each match. Default 0, max 10; use 3-5 to avoid a follow-up read."},
                "max_results": {"type": "integer", "description": "Matching-line cap, default 50; use 5-20 for samples/existence checks."},
                "count_only": {"type": "boolean", "description": "Return the full matching-line count only; `max_results` is ignored in count_only mode."},
                "searches": {
                    "type": "array",
                    "description": "Batch operations, max 20 entries; extras are dropped. Faster than separate calls.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string", "description": "Search pattern"},
                            "path": {"type": "string", "description": "Relative path to search in (optional)"},
                            "include": {"type": "string", "description": "File glob filter (optional)"},
                            "context_lines": {"type": "integer", "description": "Context lines (optional)"},
                            "max_results": {"type": "integer", "description": "Max results per search (optional)"},
                            "count_only": {"type": "boolean", "description": "Count only mode (optional)"}
                        },
                        "required": ["pattern"]
                    }
                }
            }
        }
    }
}

PROJECT_TOOL_FIND = {
    "type": "function",
    "function": {
        "name": "find_files",
        "description": (
            "Find files by name pattern (glob) in the project. Useful for discovering "
            "test files, configs, etc.\n\n"
            "**Prefer this over ``run_command find``** — find_files is served from a "
            "persistent project file index (near-instant even on huge or network-mounted "
            "trees), supports ``max_results``, and auto-filters ignored dirs "
            "(node_modules, .venv, etc.).\n\n"
            "For MULTIPLE searches, provide a 'searches' array — each entry has the same "
            "fields as the top-level parameters. Batch mode cuts round trips."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "File name glob pattern, e.g. '*.test.py', 'Dockerfile', '*.config.*'"},
                "path": {"type": "string", "description": "Relative path to search in (optional)"},
                "max_results": {"type": "integer", "description": "Maximum number of files to return. Default 100. Use a small value (5-20) when you only need a quick sample."},
                "searches": {
                    "type": "array",
                    "description": "Array of find operations (for batch mode, max 20 entries — extras are dropped). Each entry has the same fields as the top-level parameters. Much faster than multiple separate find_files calls.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string", "description": "File name glob pattern"},
                            "path": {"type": "string", "description": "Relative path to search in (optional)"},
                            "max_results": {"type": "integer", "description": "Max results per search (optional)"}
                        },
                        "required": ["pattern"]
                    }
                }
            }
        }
    }
}

PROJECT_TOOL_WRITE_FILE = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Create or replace one whole file. Use for new files/major rewrites; "
            "use edit_file for targeted changes. Read an existing file first and "
            "supply its complete replacement—omitted lines are deleted. Relative "
            "paths use the current project. An absolute path outside registered "
            "roots needs allow_outside_workspace=true after the user confirms it "
            "(the containing directory then registers as a new root); system paths "
            "(/etc, /usr, $HOME itself) are refused outright. Give exactly one "
            "source: content (an empty "
            "string creates an empty file) or content_ref to reuse all/a slice of a "
            "previous tool result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short pre-write intent shown before execution."
                },
                "path": {
                    "type": "string",
                    "description": "Project-relative or allowed absolute file path."
                },
                "content": {
                    "type": "string",
                    "description": "Complete replacement; may be empty."
                },
                "content_ref": {
                    "type": "object",
                    "description": "Reuse prior tool output without regenerating it.",
                    "properties": {
                        "tool_round": {
                            "type": "integer", "description": "Prior roundNum."
                        },
                        "start": {
                            "type": "integer",
                            "description": "First character; default 0."
                        },
                        "end": {
                            "type": "integer",
                            "description": "Exclusive end; default content end."
                        }
                    },
                    "required": ["tool_round"]
                },
                "allow_outside_workspace": {
                    "type": "boolean",
                    "description": (
                        "Set true only after the user explicitly confirmed this "
                        "exact out-of-workspace destination; its containing "
                        "directory registers as a new root. Default false: "
                        "refused with an error naming this flag."
                    )
                }
            },
            "required": ["description", "path"],
            "oneOf": [
                {
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"]
                },
                {
                    "properties": {"content_ref": {"type": "object"}},
                    "required": ["content_ref"]
                }
            ],
            "additionalProperties": False
        }
    }
}

PROJECT_TOOL_APPLY_DIFF = {
    "type": "function",
    "function": {
        "name": "apply_diff",
        "description": (
            "Apply a single search-and-replace edit to a file. The 'search' string "
            "must match EXACTLY (including whitespace/indentation) in the file. Use "
            "read_files first to get the exact content.\n\n"
            "**Read-before-edit is enforced.** apply_diff is REJECTED when the target "
            "file has not been read (or written) earlier in the conversation. A "
            "sibling ``read_files`` issued in the SAME parallel batch as this "
            "apply_diff does NOT satisfy the gate — its result is not visible to this "
            "tool call. To edit a file you have not yet read: issue read_files this "
            "turn, then issue apply_diff in the NEXT turn.\n\n"
            "**Use apply_diff for small, targeted edits.** For new files or whole-file "
            "rewrites use write_file; for purely additive changes (adding a new function "
            "next to existing code without modifying it) prefer insert_content.\n\n"
            "For MULTIPLE edits in one call, use **apply_diffs** instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Brief description of the change (generated FIRST, before writing search/replace)"},
                "path": {"type": "string", "description": "Relative file path from project root"},
                "search": {"type": "string", "description": "Exact text to find in the file (must match precisely)"},
                "replace": {"type": "string", "description": "Replacement text"},
                "replace_all": {
                    "type": "boolean",
                    "description": "If true, replace ALL occurrences of 'search' in the file (not just the first). Default false — errors when multiple matches exist to prevent accidental mass edits."
                },
                "allow_outside_workspace": {
                    "type": "boolean",
                    "description": "Set true only after the user explicitly confirmed this exact out-of-workspace destination; its containing directory registers as a new root. Default false: refused."
                }
            },
            "required": ["description", "path", "search", "replace"]
        }
    }
}

PROJECT_TOOL_APPLY_DIFFS = {
    "type": "function",
    "function": {
        "name": "apply_diffs",
        "description": (
            "apply_diffs: Apply multiple search-and-replace edits in one call. Edits "
            "are applied sequentially so later edits see earlier changes. Much faster "
            "than multiple separate apply_diff calls.\n\n"
            "**Read-before-edit is enforced.** Every target file must have been read "
            "(or written) earlier in the conversation. A sibling ``read_files`` issued "
            "in the SAME parallel batch does NOT satisfy the gate.\n\n"
            "**Failure semantics**: if one edit fails (search not found, ambiguous "
            "match), the remaining edits STILL RUN — failures do not halt the batch "
            "and successful edits already applied are NOT rolled back. The summary "
            "reads ``Applied X/(X+Y) edits`` with per-edit OK/FAIL lines. After a "
            "partial failure, re-read the affected files before retrying."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "description": "Array of edit operations (max 30 per call — extras are dropped). Each entry has description, path, search, replace.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string", "description": "Brief description of this edit (generated FIRST, before writing search/replace)"},
                            "path": {"type": "string", "description": "Relative file path"},
                            "search": {"type": "string", "description": "Exact text to find"},
                            "replace": {"type": "string", "description": "Replacement text"},
                            "replace_all": {"type": "boolean", "description": "Replace ALL occurrences (default false)"}
                        },
                        "required": ["description", "path", "search", "replace"]
                    }
                },
                "description": {"type": "string", "description": "Brief description of the overall change"},
                "allow_outside_workspace": {
                    "type": "boolean",
                    "description": "Set true only after the user explicitly confirmed this exact out-of-workspace destination; its containing directory registers as a new root. Default false: refused."
                }
            },
            "required": ["edits"]
        }
    }
}

PROJECT_TOOL_INSERT_CONTENT = {
    "type": "function",
    "function": {
        "name": "insert_content",
        "description": (
            "Insert new content before or after an anchor string in a file. Unlike "
            "apply_diff (search-and-replace), this tool ADDS content without removing "
            "the anchor.\n\n"
            "**Read-before-edit is enforced.** insert_content is REJECTED when the "
            "target file has not been read (or written) earlier in the conversation. "
            "A sibling ``read_files`` issued in the SAME parallel batch does NOT "
            "satisfy the gate. To edit a file you have not yet read: issue read_files "
            "this turn, then issue insert_content in the NEXT turn.\n\n"
            "**Prefer insert_content over apply_diff when the change is purely "
            "additive** (adding new lines without modifying existing ones). Examples: "
            "adding an import, appending a new function/method/block before or after "
            "existing code, inserting a config entry. insert_content is simpler — no "
            "need to repeat the anchor in both search and replace — and less error-prone.\n\n"
            "The 'anchor' string must match EXACTLY once in the file (like apply_diff's "
            "search). If it matches multiple locations, the tool errors — make the "
            "anchor more specific.\n\n"
            "For MULTIPLE insertions in one call, use **insert_contents** instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Brief description of the insertion (generated FIRST, before writing anchor/content)"},
                "path": {"type": "string", "description": "Relative file path from project root"},
                "anchor": {
                    "type": "string",
                    "description": "Exact text to locate the insertion point (must match exactly once in the file)"
                },
                "content": {"type": "string", "description": "New content to insert"},
                "position": {
                    "type": "string",
                    "enum": ["before", "after"],
                    "description": "Insert before or after the anchor. Default: 'after'"
                },
                "allow_outside_workspace": {
                    "type": "boolean",
                    "description": "Set true only after the user explicitly confirmed this exact out-of-workspace destination; its containing directory registers as a new root. Default false: refused."
                }
            },
            "required": ["description", "path", "anchor", "content"]
        }
    }
}

PROJECT_TOOL_INSERT_CONTENTS = {
    "type": "function",
    "function": {
        "name": "insert_contents",
        "description": (
            "insert_contents: Insert content at multiple locations in one call. Each "
            "insertion adds content before or after an anchor string. Insertions are "
            "applied sequentially so later ones see earlier changes. Much faster than "
            "multiple separate insert_content calls.\n\n"
            "**Read-before-edit is enforced.** Every target file must have been read "
            "(or written) earlier in the conversation. A sibling ``read_files`` issued "
            "in the SAME parallel batch does NOT satisfy the gate.\n\n"
            "**Failure semantics**: if one insertion fails (anchor not found, ambiguous "
            "match), the remaining insertions STILL RUN — failures do not halt the "
            "batch and successful insertions are NOT rolled back. The summary reads "
            "``Inserted X/(X+Y) edits`` with per-edit OK/FAIL lines. After a partial "
            "failure, re-read the affected files before retrying."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "description": "Array of insertion operations (max 30 per call — extras are dropped). Each entry has description, path, anchor, content, and position.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string", "description": "Brief description of this insertion (generated FIRST, before writing anchor/content)"},
                            "path": {"type": "string", "description": "Relative file path"},
                            "anchor": {"type": "string", "description": "Exact text to locate the insertion point"},
                            "content": {"type": "string", "description": "New content to insert"},
                            "position": {
                                "type": "string", "enum": ["before", "after"],
                                "description": "Insert before or after the anchor. Default: 'after'"
                            }
                        },
                        "required": ["description", "path", "anchor", "content"]
                    }
                },
                "description": {"type": "string", "description": "Brief description of the overall insertion"},
                "allow_outside_workspace": {
                    "type": "boolean",
                    "description": "Set true only after the user explicitly confirmed this exact out-of-workspace destination; its containing directory registers as a new root. Default false: refused."
                }
            },
            "required": ["edits"]
        }
    }
}


# ``edit_file`` is the only edit surface shown to new model turns.  The four
# older schemas above deliberately remain module-level constants: persisted
# conversations, input repair and the rollback switch still need to understand
# their exact wire shapes, but making all five visible recreates the tool-choice
# competition this unified entry point exists to remove.
PROJECT_TOOL_EDIT_FILE = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "Apply 1–30 ordered anchored edits after reading/writing each target "
            "in an earlier round. Use insert_after/insert_before when the anchor "
            "stays; content is only new text—do not repeat its anchor or neighbor "
            "lines. Example: add B between A/C with insert_after, anchor A, content "
            "B. Safe insertion echoes are stripped; pure echoes and a replace that "
            "merely wraps its unchanged anchor are rejected as insertions. Use "
            "replace only to change/remove the anchor. Anchors match exactly once; "
            "replace_all permits multiple matches only for replace. Later edits see "
            "earlier changes; one failure neither rolls back nor stops the others."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 120,
                    "description": "Short pre-edit intent shown before execution.",
                },
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 30,
                    "description": "Shortest-unique-anchor operations, in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string", "minLength": 1,
                                "description": "Project-relative or allowed absolute path."
                            },
                            "operation": {
                                "type": "string",
                                "enum": ["insert_after", "insert_before", "replace"],
                                "description": "Insert keeps anchor; replace changes/removes it.",
                            },
                            "anchor": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Exact, normally unique existing text.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Only inserted/replacement text; do not echo context.",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": "Replace every match; ignored by inserts.",
                            },
                        },
                        "required": ["path", "operation", "anchor", "content"],
                    },
                },
                "allow_outside_workspace": {
                    "type": "boolean",
                    "description": (
                        "Set true only after the user explicitly confirmed this "
                        "exact out-of-workspace destination; its containing "
                        "directory registers as a new root. Default false: "
                        "refused."
                    ),
                },
            },
            "required": ["description", "edits"],
            "additionalProperties": False,
        },
    },
}

PROJECT_TOOL_RUN_COMMAND = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "Run a shell command for builds, tests, lint, installs, Git, or a "
            "pipeline, returning stdout/stderr. There is NO default timeout; use "
            "`timeout` only when the work should be abandoned. User Stop remains "
            "available. If the operator sends a message during a long wait, the "
            "command may move to the background and its result will arrive "
            "automatically. Avoid commands that wait for stdin.\n\n"
            "Each call is a fresh subprocess with no persistent shell: environment "
            "changes do not carry over. Prefer absolute paths and avoid `cd`; use "
            "`working_dir`.\n\n"
            "Use `read_files`, `grep_search`, `find_files`, `edit_file`, and "
            "`write_file` for repository data. Filesystem grep is redirected through "
            "a bounded FUSE-safe engine or refused with a `grep_search` translation; "
            "filtering another command's output is allowed. Use "
            "`browser_download_url_to_server` for browser-authenticated downloads. "
            "Text helpers include `sd`, `mlr`, and `goawk`.\n\n"
            "Never use a shell no-op as a placeholder (`true`, `:`, or `exit 0`); "
            "repeated placeholders are a tool loop."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command, for example `pytest` or `git status`."
                },
                "description": {
                    "type": "string",
                    "description": "Short user-language caption: what runs and why."
                },
                "timeout": {
                    "type": "integer",
                    "description": "Optional seconds before abandonment; omit or use 0 for unlimited."
                },
                "working_dir": {
                    "type": "string",
                    "description": (
                        "Optional directory. In multi-root workspaces use "
                        "`rootname:subdir`. STICKY: after setting it (or using "
                        "`cd`), later project run_command calls resume there. "
                        "Default: last conversation directory, then project root."
                    )
                },
                "credentials": {
                    "type": "array",
                    "description": "Vault entry names for THIS child process only; use names from <credential_vault>. Values never enter the command/chat.",
                    "items": {"type": "string"},
                    "maxItems": 16
                }
            },
            "required": ["command"]
        }
    }
}

# NOTE: read_files is NOT a project-scoped tool — it's registered globally
#   in lib/tasks_pkg/model_config.py (and timer.py) so the model can read
#   absolute local paths (images, PDFs, Office docs, text files) even when no
#   project is attached. Its handler is registered independently via
#   @tool_registry.tool('read_files', ...) in lib/tasks_pkg/handlers/project.py,
#   and its display entry is set explicitly in tool_display.py. It is NOT in
#   PROJECT_TOOLS or PROJECT_TOOL_NAMES.
READ_FILES_TOOL = {
    "type": "function",
    "function": {
        "name": "read_files",
        "description": (
            "Read one or more files with line numbers and optional ranges. Read WIDE "
            "within the round budget: for 1-2 relevant files request 200+ lines or a "
            "whole small file instead of repeated 50-line fragments; for 3+ files use "
            "focused ranges or `grep_search`. An explicit start/end range is "
            "authoritative and never widened. Whole-file reads above 512 KB are "
            "refused, but bounded ranges always work and satisfy read-before-edit. "
            "Batch up to 20 independent ranges while keeping total expected content "
            "near 24k tokens; use top-level `path` for one file. Prefer this over shell "
            "cat/head/tail/sed. Relative paths use the project; absolute, `~`, and "
            "`file://` paths are supported. Images are shown natively; PDFs extract "
            "layout text; Office files extract Markdown text/tables; text uses "
            "encoding detection."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reads": {
                    "type": "array",
                    "description": "Batch file/range specs: `{path, start_line?, end_line?}` (max 20).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Relative or absolute file path; supports `~`."
                            },
                            "start_line": {"type": "integer", "minimum": 1, "description": "Start line (1-based, optional)"},
                            "end_line": {"type": "integer", "minimum": 1, "description": "End line (inclusive, optional)"}
                        },
                        "required": ["path"]
                    }
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Single-file path; ignored when `reads` is present. "
                        "Relative, absolute, and `~` supported."
                    )
                },
                "start_line": {"type": "integer", "minimum": 1, "description": "Start line (1-based, optional) — only with top-level 'path'."},
                "end_line": {"type": "integer", "minimum": 1, "description": "End line (inclusive, optional) — only with top-level 'path'."}
            }
        }
    }
}

# read_files is intentionally NOT in PROJECT_TOOLS / PROJECT_TOOL_NAMES
#   — it's a global tool registered unconditionally by the orchestrator
#   so absolute-path file reads work regardless of project mode.
#   See READ_FILES_TOOL above.
#
# Note: project_history / project_diff / project_blame were retired in the
# Tier-3 redesign (2026-05-08).  Their shadow-git backend was replaced by
# the file-history copy-backup store (lib/file_history/), and the LLM-facing
# tools were dropped because the model rarely invoked them and the same
# information is available via reading conversation history (which the
# model already does).  Per-round undo/redo of file changes still works
# end-to-end through the file-history store.
def with_multiroot_hint(tools):
    """Compatibility copy; multi-root guidance now rides tail user context."""
    return copy.deepcopy(list(tools or ()))


def with_remote_hint(tools):
    """Compatibility copy; remote guidance now rides tail user context."""
    return copy.deepcopy(list(tools or ()))


PROJECT_TOOLS_UNIFIED = [
    PROJECT_TOOL_GREP, PROJECT_TOOL_FIND,
    PROJECT_TOOL_WRITE_FILE, PROJECT_TOOL_EDIT_FILE,
    PROJECT_TOOL_RUN_COMMAND,
]

PROJECT_TOOLS_LEGACY = [
    PROJECT_TOOL_GREP, PROJECT_TOOL_FIND,
    PROJECT_TOOL_WRITE_FILE, PROJECT_TOOL_APPLY_DIFF, PROJECT_TOOL_APPLY_DIFFS,
    PROJECT_TOOL_INSERT_CONTENT, PROJECT_TOOL_INSERT_CONTENTS,
    PROJECT_TOOL_RUN_COMMAND,
]


def project_tools_for_runtime():
    """Return the model-visible project surface.

    The environment switch is an emergency rollback for new turns. Existing
    conversations already latch their tool schema in the task runtime, so the
    switch never rewrites persisted tool history.
    """
    enabled = os.environ.get('TOFU_UNIFIED_EDIT_TOOL', '1').strip().lower()
    return (PROJECT_TOOLS_LEGACY if enabled in ('0', 'false', 'no', 'off')
            else PROJECT_TOOLS_UNIFIED)


# Default/static export used by schema introspection. Runtime assembly calls
# ``project_tools_for_runtime`` so operators retain the rollback switch.
PROJECT_TOOLS = PROJECT_TOOLS_UNIFIED
# Handler-registration names for the LIVE project tool surface. Historical
# create_project rounds still render in the UI, but the name is deliberately
# absent here so any new/latched call is refused as an unknown tool instead of
# executing a retired scaffold action.
PROJECT_TOOL_NAMES = {
    # ``list_dir`` remains dispatchable only for conversation-latched legacy
    # schemas. New tool epochs omit it and route simple ``run_command`` ls
    # requests through the same bounded directory reader instead.
    'list_dir', 'grep_search', 'find_files',
    'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
    'run_command',
}

__all__ = [
    'READ_FILES_TOOL',
    'PROJECT_TOOL_GREP', 'PROJECT_TOOL_FIND',
    'PROJECT_TOOL_WRITE_FILE', 'PROJECT_TOOL_EDIT_FILE',
    'PROJECT_TOOL_APPLY_DIFF', 'PROJECT_TOOL_APPLY_DIFFS',
    'PROJECT_TOOL_INSERT_CONTENT', 'PROJECT_TOOL_INSERT_CONTENTS',
    'PROJECT_TOOL_RUN_COMMAND',
    'PROJECT_TOOLS', 'PROJECT_TOOLS_UNIFIED', 'PROJECT_TOOLS_LEGACY',
    'PROJECT_TOOL_NAMES', 'project_tools_for_runtime', 'with_multiroot_hint',
    'with_remote_hint',
]
