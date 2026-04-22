"""lib/tools/project.py — Project co-pilot tool definitions."""

PROJECT_TOOL_LIST_DIR = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "List contents of a directory in the project. Shows files with line counts and sizes, and subdirectories with item counts.",
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
            "Search for a pattern across project files. Returns matching lines with file paths and line numbers. "
            "Very useful for finding function definitions, imports, usages, etc.\n"
            "Search is case-insensitive. Uses ripgrep internally (5x faster than grep).\n"
            "Supports max_results to limit output (like head -n) and count_only for fast counting (like grep -c).\n"
            "Use simple, short patterns for best results — "
            "e.g. 'handleRequest' instead of 'def handle_.*request'. "
            "If unsure of naming, search for a core keyword substring.\n"
            "For MULTIPLE searches, provide a 'searches' array — each entry has the same fields as the top-level parameters. "
            "This is much faster than multiple separate grep_search calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern — prefer short literal substrings (e.g. 'handleRequest', 'TODO', 'import foo'). Regex also supported."},
                "path": {"type": "string", "description": "Relative path to search in (optional, defaults to project root)"},
                "include": {"type": "string", "description": "File glob filter, e.g. '*.py' or '*.js' (optional)"},
                "context_lines": {"type": "integer", "description": "Number of context lines before and after each match (like grep -C). Default 0, max 10. Use 3-5 to see surrounding code without a separate read_files call."},
                "max_results": {"type": "integer", "description": "Maximum number of matching lines to return (like head -n). Default 50. Use a small value (5-20) when you only need a few examples or to check existence."},
                "count_only": {"type": "boolean", "description": "If true, return only the count of matching lines (like grep -c or wc -l), not the actual lines. Much faster for large result sets."},
                "searches": {
                    "type": "array",
                    "description": "Array of search operations (for batch mode). Each entry has the same fields as the top-level parameters. Much faster than multiple separate grep_search calls.",
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
            "Find files by name pattern (glob) in the project. Useful for discovering test files, configs, etc.\n"
            "For MULTIPLE searches, provide a 'searches' array — each entry has the same fields as the top-level parameters. "
            "This is much faster than multiple separate find_files calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "File name glob pattern, e.g. '*.test.py', 'Dockerfile', '*.config.*'"},
                "path": {"type": "string", "description": "Relative path to search in (optional)"},
                "max_results": {"type": "integer", "description": "Maximum number of files to return. Default 100. Use a small value (5-20) when you only need a quick sample."},
                "searches": {
                    "type": "array",
                    "description": "Array of find operations (for batch mode). Each entry has the same fields as the top-level parameters. Much faster than multiple separate find_files calls.",
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
            "Write content to a file in the project. Creates the file if it doesn't exist. "
            "Overwrites the entire file. Use apply_diff for partial changes.\n"
            "IMPORTANT: Always read_files first to understand existing code before writing. "
            "Include ALL content — not just the changed parts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
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
                },
                "description": {"type": "string", "description": "Brief description of what was changed (shown to user)"}
            },
            "required": ["path"]
        }
    }
}

PROJECT_TOOL_APPLY_DIFF = {
    "type": "function",
    "function": {
        "name": "apply_diff",
        "description": (
            "Apply targeted search-and-replace edit(s) to file(s). "
            "The 'search' string must match EXACTLY (including whitespace/indentation) in the file. "
            "Use read_files first to get the exact content.\n"
            "For a SINGLE edit, provide path/search/replace at the top level.\n"
            "For MULTIPLE edits (same or different files), provide an 'edits' array — "
            "edits are applied sequentially so later edits see earlier changes. "
            "This is much faster than multiple separate apply_diff calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path from project root"},
                "search": {"type": "string", "description": "Exact text to find in the file (must match precisely)"},
                "replace": {"type": "string", "description": "Replacement text"},
                "description": {"type": "string", "description": "Brief description of the change"},
                "replace_all": {
                    "type": "boolean",
                    "description": "If true, replace ALL occurrences of 'search' in the file (not just the first). Default false — errors when multiple matches exist to prevent accidental mass edits."
                },
                "edits": {
                    "type": "array",
                    "description": "Array of edit operations (for batch mode). Each entry has path, search, replace, and optional description.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative file path"},
                            "search": {"type": "string", "description": "Exact text to find"},
                            "replace": {"type": "string", "description": "Replacement text"},
                            "replace_all": {"type": "boolean", "description": "Replace ALL occurrences (default false)"},
                            "description": {"type": "string", "description": "Brief description of this edit"}
                        },
                        "required": ["path", "search", "replace"]
                    }
                }
            }
        }
    }
}

PROJECT_TOOL_INSERT_CONTENT = {
    "type": "function",
    "function": {
        "name": "insert_content",
        "description": (
            "Insert new content before or after an anchor string in a file. "
            "Unlike apply_diff (search-and-replace), this tool ADDS content without removing the anchor.\n"
            "Use this when you need to add new code (imports, functions, config entries) "
            "next to existing code without replacing it.\n"
            "The 'anchor' string must match EXACTLY once in the file (like apply_diff's search). "
            "If it matches multiple locations, the tool errors — make the anchor more specific.\n"
            "For a SINGLE insertion, provide path/anchor/content/position at the top level.\n"
            "For MULTIPLE insertions (same or different files), provide an 'edits' array — "
            "edits are applied sequentially so later edits see earlier changes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
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
                "description": {"type": "string", "description": "Brief description of the insertion"},
                "edits": {
                    "type": "array",
                    "description": "Array of insertion operations (for batch mode). Each entry has path, anchor, content, position, and optional description.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative file path"},
                            "anchor": {"type": "string", "description": "Exact text to locate the insertion point"},
                            "content": {"type": "string", "description": "New content to insert"},
                            "position": {
                                "type": "string", "enum": ["before", "after"],
                                "description": "Insert before or after the anchor. Default: 'after'"
                            },
                            "description": {"type": "string", "description": "Brief description of this insertion"}
                        },
                        "required": ["path", "anchor", "content"]
                    }
                }
            }
        }
    }
}

PROJECT_TOOL_RUN_COMMAND = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "Execute a shell command in the project directory and return its output (stdout + stderr). "
            "Use this for running tests, linting, building, checking git status, installing packages, etc.\n"
            "The command runs with the project root as working directory.\n"
            "Commands run without a timeout by default — long-running processes are OK. "
            "Avoid interactive commands that require stdin input.\n\n"
            "WHEN TO USE run_command vs other tools:\n"
            "• Prefer run_command for: building/testing (npm, pytest), "
            "installing packages, git operations, and any task where a Unix pipeline is natural.\n"
            "• ALWAYS use grep_search instead of 'run_command grep/rg': it uses ripgrep internally (5x faster), "
            "auto-skips ignored dirs, and supports max_results (like head -n) and count_only (like wc -l).\n"
            "• ALWAYS use find_files instead of 'run_command find': it supports max_results and auto-filters ignored dirs.\n"
            "• Prefer read_files for: understanding code (returns with line numbers, supports batch reads of 20 files)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute, e.g. 'python -m pytest tests/', 'git status', 'npm test'"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds. Default auto-detects (60s for FS-heavy, 300s otherwise). Set to 0 for NO timeout (unlimited) — only use when user explicitly requests it."
                },
                "working_dir": {
                    "type": "string",
                    "description": "Working directory for the command (optional). In multi-root workspaces, use 'rootname:subdir' to run in a specific root. Default: project root."
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
            "Create a new, initially-empty project directory at the given path and register it "
            "as an EXTRA workspace root so subsequent write_file / apply_diff / insert_content / "
            "run_command / read_files calls can target it.\n\n"
            "Use this BEFORE trying to write any file that lives OUTSIDE the currently-open "
            "project — e.g. when the user asks you to 'generate a new repository at /some/path' "
            "or 'scaffold a project under ~/projects/foo while referencing the current repo'.\n\n"
            "After this call, address files in the new project either as:\n"
            "  • '<rootName>:<rel/path>'  (multi-root prefix — preferred)\n"
            "  • absolute path under the new directory\n\n"
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
            "Read the contents of one or more files. Can read specific line ranges for large files. "
            "Returns file content with line numbers.\n"
            "Each entry in the 'reads' array has 'path' (required), 'start_line' and 'end_line' (optional).\n"
            "When you need to read multiple files, put them all in one call — maximum 20 files per batch.\n"
            "Files under ~40KB are auto-expanded to whole-file regardless of range specified.\n\n"
            "Supports BOTH relative project paths AND absolute paths:\n"
            "• Relative paths (e.g. 'src/main.py') are resolved within the project.\n"
            "• Absolute paths (e.g. '/home/user/report.pdf', '~/Documents/photo.png') "
            "read from the local filesystem with format auto-detection:\n"
            "  - **Images** (.png, .jpg, .gif, .webp, .bmp): Uploaded natively as an image — "
            "you will SEE the image visually and can analyze its content.\n"
            "  - **PDFs** (.pdf): Extracts text content with layout preservation.\n"
            "  - **Office docs** (.docx, .xlsx, .pptx): Extracts text and tables as Markdown.\n"
            "  - **Text files**: Reads with auto encoding detection.\n"
            "Also handles file:// URIs — strip the file:// prefix and pass just the path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reads": {
                    "type": "array",
                    "description": "Array of file-read specs",
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
                }
            },
            "required": ["reads"]
        }
    }
}

# ★ read_files is intentionally NOT in PROJECT_TOOLS / PROJECT_TOOL_NAMES
#   — it's a global tool registered unconditionally by the orchestrator
#   so absolute-path file reads work regardless of project mode.
#   See READ_FILES_TOOL above.
PROJECT_TOOLS = [
    PROJECT_TOOL_LIST_DIR,
    PROJECT_TOOL_GREP, PROJECT_TOOL_FIND,
    PROJECT_TOOL_WRITE_FILE, PROJECT_TOOL_APPLY_DIFF, PROJECT_TOOL_INSERT_CONTENT,
    PROJECT_TOOL_CREATE_PROJECT, PROJECT_TOOL_RUN_COMMAND,
]
PROJECT_TOOL_NAMES = {
    'list_dir', 'grep_search', 'find_files',
    'write_file', 'apply_diff', 'insert_content', 'create_project', 'run_command',
}

__all__ = [
    'PROJECT_TOOL_LIST_DIR', 'READ_FILES_TOOL',
    'PROJECT_TOOL_GREP', 'PROJECT_TOOL_FIND',
    'PROJECT_TOOL_WRITE_FILE', 'PROJECT_TOOL_APPLY_DIFF', 'PROJECT_TOOL_INSERT_CONTENT',
    'PROJECT_TOOL_CREATE_PROJECT', 'PROJECT_TOOL_RUN_COMMAND',
    'PROJECT_TOOLS', 'PROJECT_TOOL_NAMES',
]
