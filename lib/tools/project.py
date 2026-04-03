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
            "Search is case-insensitive. Use simple, short patterns for best results — "
            "e.g. 'handleRequest' instead of 'def handle_.*request'. "
            "If unsure of naming, search for a core keyword substring."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern — prefer short literal substrings (e.g. 'handleRequest', 'TODO', 'import foo'). Regex also supported."},
                "path": {"type": "string", "description": "Relative path to search in (optional, defaults to project root)"},
                "include": {"type": "string", "description": "File glob filter, e.g. '*.py' or '*.js' (optional)"},
                "context_lines": {"type": "integer", "description": "Number of context lines before and after each match (like grep -C). Default 0, max 10. Use 3-5 to see surrounding code without a separate read_files call."}
            },
            "required": ["pattern"]
        }
    }
}

PROJECT_TOOL_FIND = {
    "type": "function",
    "function": {
        "name": "find_files",
        "description": "Find files by name pattern (glob) in the project. Useful for discovering test files, configs, etc.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "File name glob pattern, e.g. '*.test.py', 'Dockerfile', '*.config.*'"},
                "path": {"type": "string", "description": "Relative path to search in (optional)"}
            },
            "required": ["pattern"]
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
            "• Prefer run_command for: counting/statistics (wc, sort, uniq), building/testing (npm, pytest), "
            "installing packages, git operations, and any task where a Unix pipeline is natural.\n"
            "• Prefer grep_search for: finding code patterns (built-in fuzzy hints, context lines, case-insensitive).\n"
            "• Prefer read_files for: understanding code (returns with line numbers, supports batch reads of 20 files).\n"
            "• Prefer find_files for: locating files by name/glob (respects .gitignore-like filters)."
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

PROJECT_TOOL_READ_FILES = {
    "type": "function",
    "function": {
        "name": "read_files",
        "description": (
            "Read the contents of one or more files in the project. Can read specific line ranges for large files. "
            "Returns file content with line numbers.\n"
            "Each entry in the 'reads' array has 'path' (required), 'start_line' and 'end_line' (optional).\n"
            "When you need to read multiple files, put them all in one call — maximum 20 files per batch.\n"
            "Files under ~40KB are auto-expanded to whole-file regardless of range specified."
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
                            "path": {"type": "string", "description": "Relative file path from project root"},
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

PROJECT_TOOL_READ_LOCAL_FILE = {
    "type": "function",
    "function": {
        "name": "read_local_file",
        "description": (
            "Read a file from the local filesystem (any absolute path, not limited to the project). "
            "Supports multiple formats:\n"
            "• **Images** (.png, .jpg, .jpeg, .gif, .webp, .bmp): Uploaded natively as an image — "
            "you will SEE the image visually and can analyze its content.\n"
            "• **PDFs** (.pdf): Extracts text content with layout preservation.\n"
            "• **Office docs** (.docx, .doc, .xlsx, .xls, .pptx, .ppt): Extracts text and tables as Markdown.\n"
            "• **Text files** (.txt, .md, .csv, .json, .xml, .py, etc.): Reads with auto encoding detection.\n\n"
            "Use this when the user provides a file path outside the project, or when you need to "
            "read binary formats (images, PDFs, Office docs) that read_files cannot handle."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute file path (e.g. '/home/user/report.pdf', '~/Documents/photo.png'). "
                        "Supports ~ expansion."
                    )
                }
            },
            "required": ["path"]
        }
    }
}

PROJECT_TOOLS = [
    PROJECT_TOOL_LIST_DIR, PROJECT_TOOL_READ_FILES,
    PROJECT_TOOL_GREP, PROJECT_TOOL_FIND,
    PROJECT_TOOL_WRITE_FILE, PROJECT_TOOL_APPLY_DIFF, PROJECT_TOOL_RUN_COMMAND,
    PROJECT_TOOL_READ_LOCAL_FILE,
]
PROJECT_TOOL_NAMES = {
    'list_dir', 'read_files', 'grep_search', 'find_files',
    'write_file', 'apply_diff', 'run_command', 'read_local_file',
}

__all__ = [
    'PROJECT_TOOL_LIST_DIR', 'PROJECT_TOOL_READ_FILES',
    'PROJECT_TOOL_GREP', 'PROJECT_TOOL_FIND',
    'PROJECT_TOOL_WRITE_FILE', 'PROJECT_TOOL_APPLY_DIFF', 'PROJECT_TOOL_RUN_COMMAND',
    'PROJECT_TOOL_READ_LOCAL_FILE',
    'PROJECT_TOOLS', 'PROJECT_TOOL_NAMES',
]
