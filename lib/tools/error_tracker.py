"""lib/tools/error_tracker.py — Error log inspection & resolution tool definitions.

These tools let the LLM check error logs for unresolved bugs and mark them
as resolved after fixing them.

**Universal**: All error scanning uses `lib.project_error_tracker` —
the single, project-agnostic module.  When a project is open, these tools
scan THAT project's log files (auto-discovered by common conventions).
When no project is open, the executor falls back to the application's own
root directory (same code path, same module).

Resolution state is stored as `.chatui/error_resolutions.json` inside each
project — portable, no external DB dependency, can be committed or gitignored.
"""

import logging

logger = logging.getLogger(__name__)

CHECK_ERROR_LOGS_TOOL = {
    "type": "function",
    "function": {
        "name": "check_error_logs",
        "description": (
            "Inspect the current project's error logs for unresolved (unfixed) bugs. "
            "Automatically discovers log files in the project directory by scanning "
            "common log locations (logs/, log/, *.log, nohup.out, etc.) and parses "
            "multiple log formats (Python, Java/logback, Node.js/Winston/Pino, Go slog, "
            "Rails, syslog, and generic timestamp-level patterns).\n\n"
            "Returns error patterns grouped by fingerprint, with occurrence counts, "
            "severity levels, originating module, source log file, and sample messages. "
            "Each group has a unique fingerprint ID that can be used with resolve_error "
            "to mark it as fixed.\n\n"
            "Resolution state is stored per-project in `.chatui/error_resolutions.json` "
            "— portable across machines and teammates.\n\n"
            "Use this when the user asks about:\n"
            "- Recent errors or bugs in the project/application\n"
            "- Unresolved/unfixed issues in the logs\n"
            "- Error statistics or health status\n"
            "- What needs to be debugged or fixed\n"
            "- 'Check the logs for bugs'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "description": (
                        "Query mode:\n"
                        "- 'unresolved' (default): grouped unresolved error patterns\n"
                        "- 'stats': resolution statistics (total, resolved, unresolved, rate)\n"
                        "- 'recent': raw recent errors with resolved/unresolved annotations\n"
                        "- 'all_resolutions': list all previously resolved error fingerprints\n"
                        "- 'discover': show which log files were found in the project"
                    ),
                    "enum": ["unresolved", "stats", "recent", "all_resolutions", "discover"],
                },
                "logger_filter": {
                    "type": "string",
                    "description": (
                        "Optional: filter errors by logger/module name prefix "
                        "(e.g. 'lib.llm_client', 'com.example.service'). "
                        "Only used with mode='unresolved' or mode='recent'."
                    )
                },
                "n": {
                    "type": "integer",
                    "description": (
                        "Number of log lines to scan per log file (from end). "
                        "Default 2000. Increase for deeper history."
                    )
                }
            },
            "required": []
        }
    }
}

RESOLVE_ERROR_TOOL = {
    "type": "function",
    "function": {
        "name": "resolve_error",
        "description": (
            "Mark an error fingerprint as 'bug resolved' in the current project — "
            "meaning the underlying bug has been fixed and this error pattern is no "
            "longer actionable. Resolution state is stored in the project's "
            "`.chatui/error_resolutions.json` file.\n\n"
            "Use this AFTER you have actually fixed the bug (e.g. via apply_diff "
            "or write_file), not just to silence errors.\n\n"
            "You can also mark multiple fingerprints at once by passing a list, "
            "or resolve all errors from a specific module/logger, "
            "or resolve errors matching a message pattern."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fingerprint": {
                    "type": "string",
                    "description": (
                        "The 8-char hex fingerprint of the error to resolve "
                        "(from check_error_logs output). "
                        "Use this for resolving a single specific error pattern."
                    )
                },
                "fingerprints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of fingerprints to resolve at once. "
                        "Alternative to single 'fingerprint' for batch resolution."
                    )
                },
                "logger_name": {
                    "type": "string",
                    "description": (
                        "Resolve ALL errors from this logger/module (prefix match). "
                        "E.g. 'lib.llm_client' or 'com.example.auth' resolves all "
                        "errors from that module. "
                        "Use when you've done a major refactor of an entire module."
                    )
                },
                "message_pattern": {
                    "type": "string",
                    "description": (
                        "Regex pattern to match against error messages. "
                        "Resolves all errors whose message matches this pattern. "
                        "E.g. 'timeout.*fetch' resolves all fetch timeout errors."
                    )
                },
                "notes": {
                    "type": "string",
                    "description": "Description of what was fixed and how."
                },
                "ticket": {
                    "type": "string",
                    "description": "Optional issue tracker reference (e.g. 'PROJ-123')."
                }
            },
            "required": []
        }
    }
}

ERROR_TRACKER_TOOLS = [CHECK_ERROR_LOGS_TOOL, RESOLVE_ERROR_TOOL]
ERROR_TRACKER_TOOL_NAMES = {'check_error_logs', 'resolve_error'}

__all__ = [
    'CHECK_ERROR_LOGS_TOOL', 'RESOLVE_ERROR_TOOL',
    'ERROR_TRACKER_TOOLS', 'ERROR_TRACKER_TOOL_NAMES',
]
