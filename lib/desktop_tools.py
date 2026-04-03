"""
Desktop Agent Tool Definitions — JSON Schema for LLM tool_use.
"""

DESKTOP_TOOL_LIST_FILES = {
    "type": "function",
    "function": {
        "name": "desktop_list_files",
        "description": (
            "List files and directories on the user's LOCAL computer. "
            "This operates on the user's actual machine (desktop/laptop), not the server. "
            "Use this to browse their local file system — Documents, Desktop, Downloads, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list. Use ~ for home directory. Examples: '~/Documents', '~/Desktop', '/tmp'"
                }
            },
            "required": ["path"]
        }
    }
}

DESKTOP_TOOL_READ_FILE = {
    "type": "function",
    "function": {
        "name": "desktop_read_file",
        "description": (
            "Read a file from the user's LOCAL computer. "
            "Returns the full text content. Useful for reading local configs, documents, code, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full file path on user's computer"
                },
                "maxSize": {
                    "type": "integer",
                    "description": "Max file size in bytes (default 500KB)",
                    "default": 500000
                }
            },
            "required": ["path"]
        }
    }
}

DESKTOP_TOOL_WRITE_FILE = {
    "type": "function",
    "function": {
        "name": "desktop_write_file",
        "description": (
            "Write content to a file on the user's LOCAL computer. "
            "Creates the file if it doesn't exist. Overwrites if it does."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full file path to write"
                },
                "content": {
                    "type": "string",
                    "description": "File content to write"
                },
                "createDirs": {
                    "type": "boolean",
                    "description": "Create parent directories if they don't exist",
                    "default": False
                }
            },
            "required": ["path", "content"]
        }
    }
}

DESKTOP_TOOL_OPEN_FILE = {
    "type": "function",
    "function": {
        "name": "desktop_open_file",
        "description": (
            "Open a file on the user's computer with its default application. "
            "Like double-clicking the file. Works for PDFs, images, Word docs, spreadsheets, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to open"
                }
            },
            "required": ["path"]
        }
    }
}

DESKTOP_TOOL_OPEN_APP = {
    "type": "function",
    "function": {
        "name": "desktop_open_app",
        "description": (
            "Launch an application on the user's computer. "
            "Examples: 'code' (VS Code), 'notepad', 'chrome', 'spotify', etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "Application name or full path"
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command-line arguments to pass to the app"
                }
            },
            "required": ["app"]
        }
    }
}

DESKTOP_TOOL_RUN_COMMAND = {
    "type": "function",
    "function": {
        "name": "desktop_run_command",
        "description": (
            "Run a shell command on the user's LOCAL computer (not the server). "
            "Use for local operations: 'ls', 'pip install', 'git status', 'docker ps', etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute"
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (optional)"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                    "default": 30
                }
            },
            "required": ["command"]
        }
    }
}

DESKTOP_TOOL_SCREENSHOT = {
    "type": "function",
    "function": {
        "name": "desktop_screenshot",
        "description": (
            "Take a screenshot of the user's ENTIRE desktop (all monitors). "
            "Unlike browser_screenshot which only captures a tab, this captures everything — "
            "other apps, taskbar, desktop icons, etc. Useful for seeing what the user is looking at."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional [x, y, width, height] to capture a specific region"
                },
                "maxDimension": {
                    "type": "integer",
                    "description": "Max pixel dimension (auto-downscale). Default 1920",
                    "default": 1920
                }
            }
        }
    }
}

DESKTOP_TOOL_GUI_ACTION = {
    "type": "function",
    "function": {
        "name": "desktop_gui_action",
        "description": (
            "Perform GUI automation on the user's desktop — click, type, hotkey, drag, scroll "
            "at specific screen coordinates. This works on ANY application, not just the browser. "
            "Use desktop_screenshot first to see the screen, then use coordinates to interact. "
            "Actions: click, doubleclick, type, hotkey, moveto, scroll, drag, locate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "doubleclick", "type", "hotkey", "moveto", "scroll", "drag", "locate"],
                    "description": "GUI action to perform"
                },
                "x": {"type": "integer", "description": "X screen coordinate (for click/doubleclick/moveto/scroll)"},
                "y": {"type": "integer", "description": "Y screen coordinate (for click/doubleclick/moveto/scroll)"},
                "text": {"type": "string", "description": "Text to type (for 'type' action)"},
                "keys": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Key combination (for 'hotkey'). Examples: ['ctrl', 'c'], ['alt', 'tab'], ['command', 's']"
                },
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button (default: left)"},
                "clicks": {"type": "integer", "description": "Number of clicks (default: 1)"},
                "amount": {"type": "integer", "description": "Scroll amount (negative=down, positive=up)"},
                "x1": {"type": "integer"}, "y1": {"type": "integer"},
                "x2": {"type": "integer"}, "y2": {"type": "integer"},
                "duration": {"type": "number", "description": "Animation duration in seconds"}
            },
            "required": ["action"]
        }
    }
}

DESKTOP_TOOL_CLIPBOARD = {
    "type": "function",
    "function": {
        "name": "desktop_clipboard",
        "description": (
            "Read or write the user's system clipboard. "
            "Useful for: copying results to clipboard, reading what user just copied, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "write"],
                    "description": "'read' to get clipboard content, 'write' to set it"
                },
                "content": {
                    "type": "string",
                    "description": "Content to write (for 'write' action)"
                }
            },
            "required": ["action"]
        }
    }
}

DESKTOP_TOOL_SYSTEM_INFO = {
    "type": "function",
    "function": {
        "name": "desktop_system_info",
        "description": (
            "Get system information from the user's computer: "
            "CPU, memory, disk usage, running processes, etc. "
            "Can also kill processes by PID."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["overview", "processes", "kill"],
                    "description": "'overview' for system stats, 'processes' for top processes, 'kill' to terminate a process"
                },
                "top": {"type": "integer", "description": "Number of top processes to return (default 15)"},
                "pid": {"type": "integer", "description": "Process ID to kill (for 'kill' type)"}
            },
            "required": ["type"]
        }
    }
}


# ══════════════════════════════════════════════════════════
#  Aggregated lists
# ══════════════════════════════════════════════════════════

DESKTOP_TOOLS = [
    DESKTOP_TOOL_LIST_FILES,
    DESKTOP_TOOL_READ_FILE,
    DESKTOP_TOOL_WRITE_FILE,
    DESKTOP_TOOL_OPEN_FILE,
    DESKTOP_TOOL_OPEN_APP,
    DESKTOP_TOOL_RUN_COMMAND,
    DESKTOP_TOOL_SCREENSHOT,
    DESKTOP_TOOL_GUI_ACTION,
    DESKTOP_TOOL_CLIPBOARD,
    DESKTOP_TOOL_SYSTEM_INFO,
]

DESKTOP_TOOL_NAMES = {
    'desktop_list_files',
    'desktop_read_file',
    'desktop_write_file',
    'desktop_move_file',
    'desktop_open_file',
    'desktop_open_app',
    'desktop_run_command',
    'desktop_screenshot',
    'desktop_gui_action',
    'desktop_clipboard',
    'desktop_system_info',
}
