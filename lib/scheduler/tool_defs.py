"""lib/scheduler/tool_defs.py — LLM tool schema definitions for the scheduler."""

SCHEDULE_TOOL_CREATE = {
    "type": "function",
    "function": {
        "name": "schedule_create",
        "description": (
            "Create a durable task; returns its ID and next run (max 100 tasks). "
            "Use local-time five-field cron `minute hour day month weekday` for "
            "recurrence, or `once:YYYY-MM-DD HH:MM` for one run and auto-disable. "
            "Examples: `*/5 * * * *`, `0 9 * * 1-5`. Cron repeats until disabled; "
            "max_executions=1 also makes it one-shot. For approximate times avoid "
            ":00/:30 contention and choose an off-minute such as :07; honor an "
            "exact user-specified time.\n"
            "Types: command=shell and python=code (either may be disabled by "
            "deployment), prompt=one LLM call without tools, agent=independent "
            "polls followed by a visible full-tool turn in the target conversation. "
            "Use agent for monitoring, recurring analysis, or event triggers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short human-readable task name."
                },
                "schedule": {
                    "type": "string",
                    "description": "Local-time cron or once:YYYY-MM-DD HH:MM."
                },
                "command": {
                    "type": "string",
                    "description": (
                        "Shell/Python/prompt payload; for agent, the standing "
                        "trigger and action instruction. For a pure-code agent with "
                        "condition_command, pass an empty string."
                    )
                },
                "task_type": {
                    "type": "string",
                    "enum": ["command", "python", "prompt", "agent"],
                    "description": (
                        "command (default), python, prompt, or proactive agent."
                    )
                },
                "description": {
                    "type": "string",
                    "description": "Optional documentation."
                },
                "max_runtime": {
                    "type": "integer",
                    "description": "Max seconds before killing (default 300, not used for 'agent')",
                    "default": 300
                },
                "target_conv_id": {
                    "type": "string",
                    "description": (
                        "Required for agent: target conversation ID; use current "
                        "for this conversation."
                    )
                },
                "tools_config": {
                    "type": "object",
                    "description": (
                        "Agent execution settings. Keys: "
                        "searchMode, fetchEnabled, projectPath, codeExecEnabled, "
                        "browserEnabled, memoryEnabled, imageGenEnabled, model. "
                        "Missing keys inherit target-conversation settings."
                    )
                },
                "max_executions": {
                    "type": "integer",
                    "description": "Agent auto-disable count; 0=unlimited (default), 1=one-shot.",
                    "default": 0
                },
                "expires_at": {
                    "type": "string",
                    "description": "Agent auto-disable ISO datetime."
                },
                "condition_command": {
                    "type": "string",
                    "description": (
                        "Agent-only decisive shell predicate: exit 0 acts, or "
                        "condition_regex matches stdout. With empty command it is "
                        "zero-LLM code polling; with a standing instruction it is "
                        "hybrid and auto-promotes to code after agreement. Prefer "
                        "for deterministic triggers."
                    )
                },
                "condition_regex": {
                    "type": "string",
                    "description": (
                        "Agent-only readiness regex over predicate stdout; omit to "
                        "use exit code 0."
                    )
                }
            },
            "required": ["name", "schedule", "command"]
        }
    }
}

SCHEDULE_TOOL_LIST = {
    "type": "function",
    "function": {
        "name": "schedule_list",
        "description": "List all scheduled tasks with their status, next run time, and execution history.",
        "parameters": {
            "type": "object",
            "properties": {
                "include_disabled": {
                    "type": "boolean",
                    "description": "Include disabled tasks (default false)",
                    "default": False
                }
            }
        }
    }
}

SCHEDULE_TOOL_MANAGE = {
    "type": "function",
    "function": {
        "name": "schedule_manage",
        "description": (
            "Manage a scheduled task: run immediately, enable/disable, delete, or update.\n"
            "Actions: 'run' (trigger now), 'enable', 'disable', 'delete', 'update', 'log' (view execution log)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["run", "enable", "disable", "delete", "update", "log"],
                    "description": "Management action"
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID (not needed for 'log' action)"
                },
                "updates": {
                    "type": "object",
                    "description": "Fields to update (for 'update' action): name, schedule, command, task_type, description, max_runtime"
                }
            },
            "required": ["action"]
        }
    }
}

AWAIT_TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "await_task",
        "description": (
            "Wait for another conversation's task to finish before continuing. "
            "Use this when you need to block until a long-running task in another "
            "conversation completes.\n\n"
            "You can also list all currently active (running) tasks to discover "
            "which conversations are busy.\n\n"
            "Actions:\n"
            "  'list'  — show all currently running tasks (no task_id needed)\n"
            "  'wait'  — block until the specified task finishes (requires task_id)\n"
            "  'status' — check status of a task without blocking (requires task_id)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "wait", "status"],
                    "description": "Action to perform"
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID to wait for or check (not needed for 'list')"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum seconds to wait (default 600, max 3600)",
                    "default": 600
                },
                "poll_interval": {
                    "type": "integer",
                    "description": "Seconds between status checks (default 5)",
                    "default": 5
                }
            },
            "required": ["action"]
        }
    }
}

TIMER_TOOL_CREATE = {
    "type": "function",
    "function": {
        "name": "timer_create",
        "description": (
            "Create a durable single-shot background watcher and return "
            "immediately; do not wait or poll manually. It survives the current "
            "task and server restarts, exposes status/evidence through timer_manage, "
            "then starts one fresh authoritative turn with continuation_message and "
            "auto-disables when ready.\n"
            "Watch only self-resolving external conditions (CI/job/file/download), "
            "never a human-only action such as restarting/redeploying this Tofu "
            "server; stop and ask the user, then verify after they act. Polls are "
            "independent with no cross-poll history and can use the same search, "
            "shell, and file tools as this task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "check_instruction": {
                    "type": "string",
                    "description": (
                        "Optional readiness instruction for the tool-capable poll "
                        "LLM; define ready/error precisely. Omit only with "
                        "condition_command for a code-only watcher."
                    )
                },
                "continuation_message": {
                    "type": "string",
                    "description": (
                        "Required user message injected when ready; starts a full "
                        "tool-capable turn. State the next action."
                    )
                },
                "check_command": {
                    "type": "string",
                    "description": (
                        "Optional evidence command run before the poll LLM; its "
                        "output informs but does not decide readiness."
                    )
                },
                "condition_command": {
                    "type": "string",
                    "description": (
                        "Optional decisive shell predicate: exit 0 is ready, or "
                        "condition_regex matches stdout. Alone it is the cheapest, "
                        "zero-LLM code watcher; with check_instruction it is hybrid "
                        "and auto-promotes to code after agreement. Prefer for "
                        "deterministic conditions."
                    )
                },
                "condition_regex": {
                    "type": "string",
                    "description": (
                        "Readiness regex over condition_command stdout; omit to "
                        "use exit code 0."
                    )
                },
                "poll_interval": {
                    "type": "integer",
                    "description": "Seconds between polls. Minimum 10. Default 60.",
                    "default": 60
                },
                "max_polls": {
                    "type": "integer",
                    "description": (
                        "Polls before exhausted; default 120, 0=unlimited (caution)."
                    ),
                    "default": 120
                }
            },
            "required": ["continuation_message"],
            "anyOf": [
                {
                    "properties": {
                        "check_instruction": {"type": "string"}
                    },
                    "required": ["check_instruction"]
                },
                {
                    "properties": {
                        "condition_command": {"type": "string"}
                    },
                    "required": ["condition_command"]
                }
            ]
        }
    }
}

TIMER_TOOL_MANAGE = {
    "type": "function",
    "function": {
        "name": "timer_manage",
        "description": (
            "Manage Timer Watchers — cancel, check status, list active timers, "
            "or view the poll log."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["cancel", "status", "list", "log"],
                    "description": (
                        "'cancel' — cancel an active timer\n"
                        "'status' — get details of a specific timer\n"
                        "'list' — list all timers\n"
                        "'log' — view poll log for a timer"
                    )
                },
                "timer_id": {
                    "type": "string",
                    "description": "Timer ID (required for cancel/status/log)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max log entries to return (default 20)",
                    "default": 20
                }
            },
            "required": ["action"]
        }
    }
}

SCHEDULER_TOOLS = [
    SCHEDULE_TOOL_CREATE,
    SCHEDULE_TOOL_LIST,
    SCHEDULE_TOOL_MANAGE,
    AWAIT_TASK_TOOL,
    TIMER_TOOL_CREATE,
    TIMER_TOOL_MANAGE,
]

SCHEDULER_TOOL_NAMES = {
    'schedule_create',
    'schedule_list',
    'schedule_manage',
    'await_task',
    'timer_create',
    'timer_manage',
}


__all__ = [
    'SCHEDULE_TOOL_CREATE', 'SCHEDULE_TOOL_LIST', 'SCHEDULE_TOOL_MANAGE',
    'AWAIT_TASK_TOOL', 'TIMER_TOOL_CREATE', 'TIMER_TOOL_MANAGE',
    'SCHEDULER_TOOLS', 'SCHEDULER_TOOL_NAMES',
]
