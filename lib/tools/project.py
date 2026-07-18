"""lib/tools/project.py — Project co-pilot tool definitions."""

import copy

PROJECT_TOOL_LIST_DIR = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": (
            "List contents of a directory in the project. Shows files with line counts "
            "and sizes, and subdirectories with item counts.\n\n"
            "Flags data/binary files and notes size + line count per entry; use this "
            "before reading to avoid pulling a 20 MB file into context.\n\n"
            "Typical workflow: start a new task with list_dir('.') to map the project, "
            "then narrow with find_files / grep_search before reading specific files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from project root. Use '.' for root."}
            },
            "required": ["path"]
        }
    }
}

PROJECT_TOOL_GREP = {
    "type": "function",
    "function": {
        "name": "grep_search",
        "description": (
            "Search for a pattern across project files. Returns matching lines with file "
            "paths and line numbers. Very useful for finding function definitions, imports, "
            "usages, etc.\n\n"
            "**Prefer this over ``run_command grep/rg``** — grep_search uses ripgrep "
            "internally (5x faster than grep), auto-skips ignored dirs, and is "
            "case-insensitive by default. Supports ``max_results`` (like head -n) and "
            "``count_only`` (like grep -c).\n\n"
            "Use simple, short patterns for best results — e.g. 'handleRequest' instead "
            "of 'def handle_.*request'. If unsure of naming, search for a core keyword "
            "substring.\n\n"
            "**Regex flavor**: ripgrep / Rust regex (PCRE-like), NOT GNU grep BRE. "
            "Alternation is ``A|B`` (no backslash). ``.`` is the any-char metachar — "
            "escape as ``\\.`` to match a literal dot. Anchors ``^`` / ``$`` work per-line.\n\n"
            "``path`` accepts a SINGLE relative path (or omit for project root). To "
            "search multiple roots in one call, use the ``searches`` batch array with "
            "one entry per path — that's strictly faster than sequential calls.\n\n"
            "For MULTIPLE searches, provide a 'searches' array — each entry has the same "
            "fields as the top-level parameters. Batch mode runs them together and cuts "
            "round trips."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern — prefer short literal substrings (e.g. 'handleRequest', 'TODO', 'import foo'). Regex also supported."},
                "path": {"type": "string", "description": "Relative path to search in (optional, defaults to project root)"},
                "include": {"type": "string", "description": "File glob filter, e.g. '*.py' or '*.js' (optional)"},
                "context_lines": {"type": "integer", "description": "Number of context lines before and after each match (like grep -C). Default 0, max 10. Use 3-5 to see surrounding code without a separate read_files call."},
                "max_results": {"type": "integer", "description": "Maximum number of matching lines to return (like head -n). Default 50. Use a small value (5-20) when you only need a few examples or to check existence."},
                "count_only": {"type": "boolean", "description": "If true, return only the count of matching lines (like grep -c or wc -l), not the actual lines. Much faster for large result sets. NOTE: `max_results` is ignored in count_only mode — the full count is always returned."},
                "searches": {
                    "type": "array",
                    "description": "Array of search operations (for batch mode, max 20 entries — extras are dropped). Each entry has the same fields as the top-level parameters. Much faster than multiple separate grep_search calls.",
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
            "**Prefer this over ``run_command find``** — find_files supports "
            "``max_results`` and auto-filters ignored dirs (node_modules, .venv, etc.).\n\n"
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
            "Write content to a file in the project. Creates the file if it doesn't "
            "exist. Overwrites the entire file.\n\n"
            "**When to use which write tool:**\n"
            "  • write_file — new files, or major rewrites of an entire file\n"
            "  • apply_diff — small targeted changes to part of an existing file\n"
            "  • insert_content — purely additive changes (new import, new function, "
            "new config entry) where existing code is left intact\n\n"
            "IMPORTANT: Always read_files first to understand existing code before "
            "writing. Include ALL content — not just the changed parts. Otherwise the "
            "rest of the file is lost.\n\n"
            "**Paths:** a relative path resolves under the current project. An "
            "ABSOLUTE path (e.g. '/home/user/other-repo/src/main.py') also works "
            "directly — its containing directory is auto-registered as a workspace "
            "root on first write, so you do NOT need create_project first. Only "
            "genuine system paths (/etc, /usr, $HOME itself, …) are refused. The "
            "same applies to apply_diff / insert_content."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Brief description of what was changed (generated FIRST, before writing content)"},
                "path": {"type": "string", "description": "Relative file path from project root"},
                "content": {"type": "string", "description": "Complete file content to write"},
                "content_ref": {
                    "type": "object",
                    "description": (
                        "Reference to content from a previous tool result. Use INSTEAD of 'content' to avoid "
                        "re-generating large text that already exists in a previous tool round's output. "
                        "The referenced content will be resolved and written to the file."
                    ),
                    "properties": {
                        "tool_round": {"type": "integer", "description": "roundNum of the tool result whose output to use as file content"},
                        "start": {"type": "integer", "description": "Start character index for partial content (optional, default 0)"},
                        "end": {"type": "integer", "description": "End character index for partial content (optional, default end)"}
                    },
                    "required": ["tool_round"]
                }
            },
            "required": ["description", "path"]
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
                "description": {"type": "string", "description": "Brief description of the overall change"}
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
                "description": {"type": "string", "description": "Brief description of the overall insertion"}
            },
            "required": ["edits"]
        }
    }
}

PROJECT_TOOL_RUN_COMMAND = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "Execute a shell command in the project directory and return its output "
            "(stdout + stderr). Use this for running tests, linting, building, checking "
            "git status, installing packages — anything that needs a real shell.\n\n"
            "The command runs with the project root as working directory. A default "
            "timeout applies (see the `timeout` param: 60s for filesystem-heavy "
            "commands, 300s otherwise); pass `timeout=0` for a genuinely long-running "
            "process. Avoid interactive commands that require stdin input (they will "
            "hang).\n\n"
            "Each call runs in its own fresh subprocess — there is **no persistent "
            "shell**, so environment/shell state (exported variables, sourced "
            "profiles, activated virtualenvs) does NOT carry over between calls. "
            "**Prefer absolute paths and avoid `cd`** — use `cd` only when the user "
            "explicitly asks; to run in a different directory, pass `working_dir` "
            "instead of prefixing the command with `cd`.\n\n"
            "**WHEN TO USE run_command vs the dedicated tools:**\n"
            "  • Building / testing (`npm test`, `pytest`, `cargo build`) — use run_command\n"
            "  • Installing packages (`pip install`, `npm install`) — use run_command\n"
            "  • Git operations (`git status`, `git log`, `git push`) — use run_command\n"
            "  • Pipelines whose source is NOT a recursive search (`make 2>&1 | tail -50`, `pytest -k foo | grep PASS`) — use run_command\n\n"
            "**Do NOT use run_command for these — use the dedicated tools instead:**\n"
            "  • Reading files → use **read_files** (line numbers, batch reads, auto image/PDF/Office support)\n"
            "  • Searching file content → use **grep_search** (5x+ faster than `grep -r`, auto-respects .gitignore, batch mode)\n"
            "  • Finding files by name → use **find_files** (max_results, ignored-dir filter)\n"
            "  • Editing files → use **apply_diff / insert_content / write_file**\n"
            "Reaching for `cat` / `grep` / `find` / `sed` / `awk` is almost always a smell — there is a dedicated tool that's faster, safer, and easier for the user to review.\n"
            "**Pipelines do NOT excuse this** — `grep -rn 'foo' lib/ | head -20` is the WORST case: on a FUSE-mounted or large tree, the recursive `grep -rn` walks every untracked dir (caches, .project_sessions, vendor) and can take >120s, while `grep_search(pattern='foo', path='lib', max_results=20)` finishes in <1s. Use grep_search and pass `max_results` instead of piping to `head`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute, e.g. 'python -m pytest tests/', 'git status', 'npm test'"
                },
                "description": {
                    "type": "string",
                    "description": "ALWAYS provide a short one-line summary (in the user's language) of what this command does and why. It is rendered as a caption above the command in the UI so the user can grasp the intent at a glance without parsing the shell syntax — especially valuable for long pipelines. E.g. 'Run the auth test suite', 'Check installed package versions'."
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds. Default auto-detects (60s for FS-heavy, 300s otherwise). Set to 0 for NO timeout (unlimited) — only use when user explicitly requests it."
                },
                "working_dir": {
                    "type": "string",
                    "description": "Working directory for the command (optional). In multi-root workspaces, use 'rootname:subdir' to run in a specific root. STICKY: once you set it (or `cd` inside a command), later run_command calls in this conversation resume from that directory automatically — so you do NOT need to repeat `cd <project>` or use absolute paths for `python`/`pip` every call. Default: the conversation's last working directory, else project root."
                }
            },
            "required": ["command"]
        }
    }
}

PROJECT_TOOL_CREATE_PROJECT = {
    "type": "function",
    "function": {
        "name": "create_project",
        "description": (
            "create_project: Create a new, initially-empty project directory at the given path and register it "
            "as an EXTRA workspace root, and (optionally) give it a short root name.\n\n"
            "NOTE: You usually do NOT need this just to write files outside the current "
            "project — write_file / apply_diff / insert_content already accept absolute "
            "paths and auto-register the target directory on first write. Use "
            "create_project only when you want to (a) pre-create an empty directory "
            "before writing into it, or (b) assign an explicit short 'name:' prefix for "
            "a non-primary root. Example: 'scaffold a project under ~/projects/foo'.\n\n"
            "After this call, address files in the new project either as:\n"
            "  • an absolute path under the new directory (simplest), or\n"
            "  • the '<rootName>:<rel/path>' prefix shorthand\n\n"
            "The currently-open project is NOT replaced — it remains the primary root and can "
            "still be read for reference. System paths (e.g. /etc, /usr, /bin, $HOME itself) "
            "are rejected for safety."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute or ~-prefixed directory path where the new project will live. "
                        "Parent directories are created as needed. Examples: "
                        "'~/projects/my-new-repo', '/home/user/workspace/tool-X'."
                    )
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Optional short root name used as the 'name:' prefix in subsequent tool calls. "
                        "Defaults to the directory basename. If the name collides with an existing "
                        "root, a numeric suffix is appended."
                    )
                },
                "overwrite": {
                    "type": "boolean",
                    "description": (
                        "If true, allow registering a directory that already exists AND is not empty. "
                        "Existing files are NOT deleted — this flag only bypasses the non-empty guard "
                        "so the directory can still be registered as a workspace root. "
                        "Default: false (non-empty existing directories are rejected)."
                    )
                }
            },
            "required": ["path"]
        }
    }
}


# ★ NOTE: read_files is NOT a project-scoped tool — it's registered globally
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
            "Read the contents of one or more files. Returns file content with line "
            "numbers. Can read specific line ranges for large files.\n\n"
            "**Read WIDE, not narrow.** When examining a function or class, read 200+ "
            "lines in one shot — don't read 50-line fragments and come back for more. "
            "Prefer reading the WHOLE file (omit start_line / end_line) for files "
            "under 500 lines. Files under ~40 KB auto-expand to whole-file regardless "
            "of range, so don't worry about over-requesting.\n\n"
            "**Large files (>512 KB):** a whole-file read is refused with 'File too "
            "large', but a bounded ``start_line``/``end_line`` range ALWAYS works (the "
            "range caps the output, not the file size). Use grep_search to locate the "
            "line, then read that range. This is also the way to satisfy the "
            "read-before-edit gate before apply_diff on a large file.\n\n"
            "**Batch your reads.** When you need multiple files, put them all in one "
            "call — maximum 20 entries per batch. Each entry: ``{path, start_line?, "
            "end_line?}``. Batched reads cut round trips dramatically.\n\n"
            "For a SINGLE file you may instead pass top-level ``path`` (plus optional "
            "``start_line`` / ``end_line``) without the ``reads`` wrapper.\n\n"
            "**Prefer this over ``run_command cat/head/tail/sed``.** Dedicated reading "
            "is faster, includes line numbers, and lets the UI display the file nicely.\n\n"
            "**Supports BOTH relative project paths AND absolute paths:**\n"
            "  • Relative paths (e.g. 'src/main.py') resolve within the project.\n"
            "  • Absolute paths (e.g. '/home/user/report.pdf', '~/Documents/photo.png') "
            "read from the local filesystem with format auto-detection:\n"
            "    – **Images** (.png, .jpg, .gif, .webp, .bmp): Uploaded natively — you "
            "will SEE the image and can analyze its content.\n"
            "    – **PDFs** (.pdf): Extracts text with layout preservation.\n"
            "    – **Office docs** (.docx, .xlsx, .pptx): Extracts text and tables as "
            "Markdown.\n"
            "    – **Text files**: Reads with auto encoding detection.\n"
            "Also handles ``file://`` URIs — strip the prefix and pass the path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reads": {
                    "type": "array",
                    "description": "Array of file-read specs (batch mode). Each entry: {path, start_line?, end_line?}.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": (
                                    "File path — relative from project root (e.g. 'lib/server.py') "
                                    "or absolute (e.g. '/home/user/data.csv', '~/report.pdf'). "
                                    "Supports ~ expansion."
                                )
                            },
                            "start_line": {"type": "integer", "description": "Start line (1-based, optional)"},
                            "end_line": {"type": "integer", "description": "End line (inclusive, optional)"}
                        },
                        "required": ["path"]
                    }
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Single-file shorthand — file path (relative or absolute, ~ expansion "
                        "supported). Use INSTEAD of 'reads' when reading just one file. "
                        "Ignored when 'reads' is provided."
                    )
                },
                "start_line": {"type": "integer", "description": "Start line (1-based, optional) — only with top-level 'path'."},
                "end_line": {"type": "integer", "description": "End line (inclusive, optional) — only with top-level 'path'."}
            }
        }
    }
}

# ★ read_files is intentionally NOT in PROJECT_TOOLS / PROJECT_TOOL_NAMES
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
_MULTIROOT_PATH_HINT = (
    " In a multi-root workspace, target a non-primary root with either an "
    "ABSOLUTE path (simplest) or the 'rootname:' prefix (e.g. "
    "'otherroot:src/foo.py'); a bare relative path resolves under the PRIMARY "
    "root."
)


def _augment_path_descriptions(schema):
    """Recursively append the multi-root prefix hint to every ``path`` field.

    Walks an OpenAI-style JSON-schema ``properties`` tree in place, appending
    :data:`_MULTIROOT_PATH_HINT` to the ``description`` of any property literally
    named ``path`` (top-level or nested inside ``items``) that doesn't already
    mention the ``rootname:`` convention. Caller must pass a copy — this mutates.
    """
    if not isinstance(schema, dict):
        return
    props = schema.get('properties')
    if isinstance(props, dict):
        for key, sub in props.items():
            if not isinstance(sub, dict):
                continue
            if key == 'path':
                desc = sub.get('description', '') or ''
                if 'rootname:' not in desc:
                    sub['description'] = desc + _MULTIROOT_PATH_HINT
            # Recurse into nested object/array property schemas.
            _augment_path_descriptions(sub)
    items = schema.get('items')
    if isinstance(items, dict):
        _augment_path_descriptions(items)


def with_multiroot_hint(tools):
    """Return a deep copy of *tools* with the multi-root prefix hint on path fields.

    Called by the tool-assembly registry ONLY when more than one workspace root
    is active, so single-root sessions keep the byte-identical (prompt-cache
    friendly) schema. Each tool's ``path`` parameter gains a sentence telling the
    model to use the ``rootname:`` prefix for non-primary roots — placed where the
    model actually chooses the argument value, complementing the system-prompt
    multi-root table.
    """
    out = []
    for tool in tools:
        t = copy.deepcopy(tool)
        params = t.get('function', {}).get('parameters')
        _augment_path_descriptions(params)
        out.append(t)
    return out


PROJECT_TOOLS = [
    PROJECT_TOOL_LIST_DIR,
    PROJECT_TOOL_GREP, PROJECT_TOOL_FIND,
    PROJECT_TOOL_WRITE_FILE, PROJECT_TOOL_APPLY_DIFF, PROJECT_TOOL_APPLY_DIFFS,
    PROJECT_TOOL_INSERT_CONTENT, PROJECT_TOOL_INSERT_CONTENTS,
    PROJECT_TOOL_CREATE_PROJECT, PROJECT_TOOL_RUN_COMMAND,
]
PROJECT_TOOL_NAMES = {
    'list_dir', 'grep_search', 'find_files',
    'write_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
    'create_project', 'run_command',
}

__all__ = [
    'PROJECT_TOOL_LIST_DIR', 'READ_FILES_TOOL',
    'PROJECT_TOOL_GREP', 'PROJECT_TOOL_FIND',
    'PROJECT_TOOL_WRITE_FILE', 'PROJECT_TOOL_APPLY_DIFF', 'PROJECT_TOOL_APPLY_DIFFS',
    'PROJECT_TOOL_INSERT_CONTENT', 'PROJECT_TOOL_INSERT_CONTENTS',
    'PROJECT_TOOL_CREATE_PROJECT', 'PROJECT_TOOL_RUN_COMMAND',
    'PROJECT_TOOLS', 'PROJECT_TOOL_NAMES', 'with_multiroot_hint',
]
