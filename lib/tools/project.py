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
                "from_line": {"type": "integer", "minimum": 1, "description": "Only report matches on lines ≥ N (per-file floor for paging)."},
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
                            "from_line": {"type": "integer", "minimum": 1, "description": "Per-file line floor (optional)"},
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

PROJECT_TOOL_REPO_MAP = {
    "type": "function",
    "function": {
        "name": "repo_map",
        "description": (
            "Return a compact structural outline of the project: which source "
            "files define which functions/classes/methods, ranked by how often "
            "other files reference them. Use this FIRST when you need to orient "
            "in an unfamiliar area — it is much cheaper than repeated ls/grep "
            "rounds. The map contains ONLY definition lines (signature + 1-based "
            "line number), never bodies; hand its paths and line numbers to "
            "read_files to see the actual code. Read-only, makes no changes.\n\n"
            "Filters: `path` scopes the map to one subdirectory (e.g. "
            "\"lib/storage\"); `focus` keeps only files whose path contains "
            "the given text; `max_tokens` caps output size (default 2048, the "
            "most useful tradeoff). If the map reports '(N more files…)', "
            "raise max_tokens or switch to a smaller `path`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Project-relative subdirectory to map (default: whole "
                        "project). Use this on huge repos to zoom in."
                    )
                },
                "focus": {
                    "type": "string",
                    "description": (
                        "Case-insensitive substring filter on file paths (e.g. "
                        "\"storage\" or \"_test.py\"). Optional."
                    )
                },
                "max_tokens": {
                    "type": "integer",
                    "description": (
                        "Rough token budget for the output (default 2048). "
                        "Lower it for a quick glance; raise it only when the "
                        "default cut off files you need."
                    ),
                    "default": 2048
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
            "Create or replace one whole file, or append to its end. For new "
            "files/major rewrites; use edit_file for mid-file changes. Set "
            "append=true to append to the end (a missing file is created): send "
            "only the new bytes, no read or anchor needed. Read an existing file "
            "first and supply its complete replacement—omitted lines are deleted. "
            "Relative paths use the current project. An absolute path outside "
            "registered roots needs allow_outside_workspace=true after the user "
            "confirms it (the containing directory then registers as a new root); "
            "system paths (/etc, /usr, $HOME itself) are refused outright. Give "
            "exactly one source: content (an empty string creates an empty file) "
            "or content_ref to reuse all/a slice of a previous tool result."
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
                "append": {
                    "type": "boolean",
                    "description": (
                        "Append content to the end of the file instead of "
                        "replacing it. Default false."
                    )
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
            "should match exactly — use read_files first to get the precise "
            "content. Matching is exact-first, then tolerant of line-ending, "
            "trailing-whitespace, uniform-indentation and unicode-escape drift; "
            "on a miss you get the closest-matching block to re-anchor from. "
            "Always include enough context lines to match EXACTLY ONCE.\n\n"
            "**Read-before-edit gate**: REJECTED unless the target file was read or "
            "written in an earlier turn; a read_files call in the SAME batch does "
            "not count. To edit an unread file: read_files this turn, apply_diff "
            "the NEXT.\n\n"
            "**Use for small, targeted edits.** New files or whole-file rewrites: "
            "write_file; purely additive changes: insert_content. MULTIPLE edits "
            "in one call: **apply_diffs**."
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
                    "description": "Set true only after the user explicitly confirmed this exact out-of-workspace destination; its containing directory registers as a new root. Default false: refused. Unneeded for a git worktree of a registered repo."
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
            "apply_diffs: Apply multiple search-and-replace edits in one call, "
            "sequentially — later edits see earlier changes. Matching follows "
            "the same exact-first, then drift-tolerant ladder as apply_diff.\n\n"
            "**Read-before-edit gate**: every target file must have been read or "
            "written in an earlier turn; a read_files call in the SAME batch does "
            "not count.\n\n"
            "**Failure semantics**: by default a failed edit (search not found, "
            "ambiguous) does NOT halt the batch and applied edits are NOT rolled "
            "back — the summary reads ``Applied X/(X+Y) edits`` with per-edit "
            "OK/FAIL lines; re-read affected files before retrying. Set "
            "atomic=true for all-or-nothing: every edit is pre-flight checked "
            "and when ANY edit fails, NOTHING is written — the report shows "
            "what would have applied so you can fix the failures and re-send."
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
                "atomic": {
                    "type": "boolean",
                    "description": "All-or-nothing mode (default false). When true, every edit is pre-flight validated and if ANY edit fails, NO file is written; the report marks each edit as would-apply or FAIL so the batch can be fixed and re-sent. Use for interdependent edits where a partial batch would leave the file inconsistent."
                },
                "allow_outside_workspace": {
                    "type": "boolean",
                    "description": "Set true only after the user explicitly confirmed this exact out-of-workspace destination; its containing directory registers as a new root. Default false: refused. Unneeded for a git worktree of a registered repo."
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
            "the anchor. Prefer it over apply_diff for purely additive changes "
            "(imports, new functions, config entries) — no need to repeat the "
            "anchor in search and replace.\n\n"
            "**Read-before-edit gate**: REJECTED unless the target file was read or "
            "written in an earlier turn; a read_files call in the SAME batch does "
            "not count. To edit an unread file: read_files this turn, insert_content "
            "the NEXT.\n\n"
            "The 'anchor' must match EXACTLY once — on multiple matches the tool "
            "errors; make the anchor more specific. MULTIPLE insertions in one "
            "call: **insert_contents**."
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
                    "description": "Set true only after the user explicitly confirmed this exact out-of-workspace destination; its containing directory registers as a new root. Default false: refused. Unneeded for a git worktree of a registered repo."
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
            "insert_contents: Insert content at multiple locations in one call, "
            "sequentially — later insertions see earlier changes. Each insertion "
            "adds content before or after an anchor string.\n\n"
            "**Read-before-edit gate**: every target file must have been read or "
            "written in an earlier turn; a read_files call in the SAME batch does "
            "not count.\n\n"
            "**Failure semantics**: a failed insertion (anchor not found, ambiguous) "
            "does NOT halt the batch and applied insertions are NOT rolled back. "
            "The summary reads ``Inserted X/(X+Y) edits`` with per-edit OK/FAIL "
            "lines; re-read affected files before retrying."
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
                    "description": "Set true only after the user explicitly confirmed this exact out-of-workspace destination; its containing directory registers as a new root. Default false: refused. Unneeded for a git worktree of a registered repo."
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
            "Apply 1–30 ordered anchored edits; read/write targets in an earlier round. "
            "For mid-file changes; append at end-of-file with write_file(append=true). "
            "Replace uses full content even when it retains the anchor; never adds "
            "line breaks. Prefer insert_after/insert_before for new lines: content is "
            "only new text; do not repeat anchor/neighbor lines. Example: add B between "
            "A/C: anchor A, content B. Safe insertion echoes are stripped; pure echoes "
            "rejected. Anchors match exactly once; "
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
                    "description": (
                        "Short pre-edit intent shown before execution "
                        "(max 120 chars)."),
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
                                "description": "Insert keeps anchor; replace substitutes it with content.",
                            },
                            "anchor": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Exact, normally unique existing text.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Insert: only new text. Replace: full replacement, empty to delete.",
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
            "pipeline, returning stdout/stderr. There is NO timeout: a command "
            "runs until it finishes, the user stops it, or you stop a "
            "backgrounded one with `kill`. If the operator sends a message "
            "during a long wait, the command may move to the background and its "
            "result will arrive automatically. Avoid commands that wait for "
            "stdin.\n\n"
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
                "working_dir": {
                    "type": "string",
                    "description": (
                        "Optional directory. In multi-root workspaces use "
                        "`rootname:subdir`. STICKY: after setting it (or using "
                        "`cd`), later project run_command calls resume there. "
                        "Default: last conversation directory, then project root."
                    )
                },
                "frozen_input_paths": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": {"type":"string","minLength":1,"maxLength":1024},
                    "description": "Optional self-evolution executor observation: map frozen digest fields (dataset_digest, split_digest, candidate_digest, baseline_digest) to project-relative files. Server derives expected hashes from your owned protocol and refuses drift before launch. Uses noninteractive stdin. Read facts are retained with command originals; tracing changes timing and is not semantic input-use or scientific acceptance proof."
                },
                "credentials": {
                    "type": "array",
                    "description": "Vault entry names for THIS child process only; use names from <credential_vault>. Values never enter the command/chat.",
                    "items": {"type": "string"},
                    "maxItems": 16
                }
,
                "background": {
                    "type": "boolean",
                    "description": (
                        "Start in background and return a command_id handle. "
                        "Completion is delivered automatically — end the turn "
                        "or do other work. `poll` reads its status or waits "
                        "for it; `kill` stops it."
                    )
                }
            },
            "required": ["command"]
        }
    }
}

PROJECT_TOOL_CHECK_BACKGROUND = {
    "type": "function",
    "function": {
        "name": "check_background_command",
        "description": (
            "Read a background command's status, result, and elapsed time. "
            "Set wait_s to block until it completes; omit for an instant snapshot."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command_id": {
                    "type": "string",
                    "description": "The command_id returned by run_command(background=true)."
                },
                "wait_s": {
                    "type": "integer",
                    "description": (
                        "Seconds to block waiting for completion (clamped to 60). "
                        "On cap, returns the running snapshot; call again to keep waiting."
                    )
                }
            },
            "required": ["command_id"]
        }
    }
}

PROJECT_TOOL_KILL_BACKGROUND = {
    "type": "function",
    "function": {
        "name": "kill_background_command",
        "description": (
            "Kill a running background command by its command_id. "
            "The process tree receives SIGTERM then SIGKILL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command_id": {
                    "type": "string",
                    "description": "The command_id to kill."
                }
            },
            "required": ["command_id"]
        }
    }
}

# NOTE: read_files is NOT a project-scoped tool
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


# PROJECT_TOOL_CHECK_BACKGROUND / PROJECT_TOOL_KILL_BACKGROUND are retired
# from the model-visible surface: the unified ``poll`` / ``kill`` tools
# (lib/tools/poll.py) absorb them. The schema dicts stay defined below for
# conversation-latched history, and their handlers stay registered.
PROJECT_TOOLS_UNIFIED = [
    PROJECT_TOOL_GREP, PROJECT_TOOL_FIND, PROJECT_TOOL_REPO_MAP,
    PROJECT_TOOL_WRITE_FILE, PROJECT_TOOL_EDIT_FILE,
    PROJECT_TOOL_RUN_COMMAND,
]

PROJECT_TOOLS_LEGACY = [
    PROJECT_TOOL_GREP, PROJECT_TOOL_FIND, PROJECT_TOOL_REPO_MAP,
    PROJECT_TOOL_WRITE_FILE, PROJECT_TOOL_APPLY_DIFF, PROJECT_TOOL_APPLY_DIFFS,
    PROJECT_TOOL_INSERT_CONTENT, PROJECT_TOOL_INSERT_CONTENTS,
    PROJECT_TOOL_RUN_COMMAND,
]


def with_command_input_observation_scope(tools, *, evolution=None, project_ready=False):
    """Expose frozen input selection only to bound project executors.

    This request-local projection grants no authority; command admission still
    validates the current owner, run, task and frozen protocol.
    """
    binding = evolution or {}
    if project_ready and binding.get('run_id') and binding.get('stage') == 'executing':
        return list(tools)
    projected = []
    for tool in tools:
        if tool.get('function', {}).get('name') == 'run_command':
            tool = copy.deepcopy(tool)
            tool['function']['parameters']['properties'].pop('frozen_input_paths', None)
        projected.append(tool)
    return projected


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
    'list_dir', 'grep_search', 'find_files', 'repo_map',
    'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
    'run_command', 'check_background_command', 'kill_background_command',
}

__all__ = [
    'READ_FILES_TOOL',
    'PROJECT_TOOL_GREP', 'PROJECT_TOOL_FIND', 'PROJECT_TOOL_REPO_MAP',
    'PROJECT_TOOL_WRITE_FILE', 'PROJECT_TOOL_EDIT_FILE',
    'PROJECT_TOOL_APPLY_DIFF', 'PROJECT_TOOL_APPLY_DIFFS',
    'PROJECT_TOOL_INSERT_CONTENT', 'PROJECT_TOOL_INSERT_CONTENTS',
    'PROJECT_TOOL_RUN_COMMAND',
    'PROJECT_TOOLS', 'PROJECT_TOOLS_UNIFIED', 'PROJECT_TOOLS_LEGACY',
    'PROJECT_TOOL_NAMES', 'project_tools_for_runtime',
    'with_command_input_observation_scope', 'with_multiroot_hint',
    'with_remote_hint',
]
