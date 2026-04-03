"""lib/scheduler/tool_defs.py — LLM tool schema definitions for the scheduler."""

SCHEDULE_TOOL_CREATE = {
    "type": "function",
    "function": {
        "name": "schedule_create",
        "description": (
            "Create a scheduled task that runs automatically at specified times. "
            "Tasks persist across server restarts. Use cron expressions for recurring tasks, "
            "or 'once:YYYY-MM-DD HH:MM' for one-time tasks.\n\n"
            "Cron format: minute hour day_of_month month day_of_week\n"
            "Examples:\n"
            "  '*/5 * * * *'     — every 5 minutes\n"
            "  '0 9 * * *'       — daily at 9:00 AM\n"
            "  '0 9 * * 1-5'     — weekdays at 9:00 AM\n"
            "  '30 8,12,18 * * *' — at 8:30, 12:30, 18:30 daily\n"
            "  '0 0 1 * *'       — first day of month at midnight\n"
            "  'once:2026-03-15 14:00' — one-time at that specific time\n\n"
            "Task types:\n"
            "  'command' — run a shell command\n"
            "  'python'  — run Python code\n"
            "  'prompt'  — simple LLM inference (no tools)\n"
            "  'agent'   — ★ PROACTIVE AGENT: periodically polls (cheap model) to decide\n"
            "              if conditions are met, then executes a full agentic task with\n"
            "              ALL tools in the target conversation. The execution is visible\n"
            "              to the user as a normal assistant response with tool calls.\n"
            "              Each poll is independent (no cross-poll history, saving tokens).\n"
            "              Use this for: monitoring, recurring analysis, event-driven actions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human-readable task name, e.g. 'Monitor training completion', 'Weekly code review'"
                },
                "schedule": {
                    "type": "string",
                    "description": "Cron expression or 'once:YYYY-MM-DD HH:MM'"
                },
                "command": {
                    "type": "string",
                    "description": (
                        "For command/python/prompt: the shell command, Python code, or LLM prompt.\n"
                        "For agent: the standing instruction — what to check and what to do when conditions are met. "
                        "Be specific about both the TRIGGER CONDITION and the ACTION."
                    )
                },
                "task_type": {
                    "type": "string",
                    "enum": ["command", "python", "prompt", "agent"],
                    "description": (
                        "'command' for shell (default), 'python' for Python code, "
                        "'prompt' for LLM inference, 'agent' for proactive agentic task"
                    )
                },
                "description": {
                    "type": "string",
                    "description": "What this task does (for documentation)"
                },
                "max_runtime": {
                    "type": "integer",
                    "description": "Max seconds before killing (default 300, not used for 'agent')",
                    "default": 300
                },
                "target_conv_id": {
                    "type": "string",
                    "description": (
                        "For agent type: the conversation ID to execute in. "
                        "Use 'current' to use this conversation. Required for agent type."
                    )
                },
                "tools_config": {
                    "type": "object",
                    "description": (
                        "For agent type: tool settings for execution. Keys: "
                        "searchMode, fetchEnabled, projectPath, codeExecEnabled, "
                        "browserEnabled, skillsEnabled, swarmEnabled, imageGenEnabled, model. "
                        "Omitted keys inherit from the target conversation's saved settings."
                    )
                },
                "max_executions": {
                    "type": "integer",
                    "description": "Auto-disable after this many executions (0=unlimited, default 0). Use 1 for one-shot proactive tasks.",
                    "default": 0
                },
                "expires_at": {
                    "type": "string",
                    "description": "ISO datetime after which the task auto-disables (e.g. '2026-04-01 00:00')"
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
            "Create a Timer Watcher — a blocking inline poller that periodically checks "
            "whether conditions are met, then returns the result as a tool output so you "
            "can continue your workflow.\n\n"
            "Use this when a long-running process has been started (e.g. remote experiment, "
            "build, deployment) and you want to automatically proceed once it completes.\n\n"
            "IMPORTANT: This tool call BLOCKS until conditions are met (or max_polls "
            "is exhausted). The user sees each poll check as a collapsible progress "
            "indicator. When conditions are met, the result is returned and you can "
            "continue generating as normal.\n\n"
            "★ TOOL-CAPABLE: The timer poll LLM has access to the SAME tools as you "
            "(web_search, fetch_url, run_command, list_dir, read_files, grep_search, "
            "find_files, etc.). It can actively gather information to evaluate conditions "
            "— not just passively read check_command output. This means the check_instruction "
            "can describe complex conditions that require file reading, web requests, "
            "or command execution to verify.\n\n"
            "How it works:\n"
            "  1. You set up the timer with a check instruction + continuation message.\n"
            "  2. The tool blocks — each poll is shown to the user as a live progress check.\n"
            "  3. A poll LLM evaluates the check instruction each poll (with tools, "
            "independent per poll, no cross-poll history).\n"
            "  4. Optionally, a shell command runs before each poll for grounded status data.\n"
            "  5. When conditions are met, the result is returned as this tool's output.\n"
            "  6. You then proceed with the continuation instructions.\n"
            "  7. The timer auto-disables after triggering (single-shot).\n\n"
            "Example: After submitting a training job, create a timer that checks\n"
            "'tail -5 /path/to/train.log | grep DONE' every 60 seconds, and when\n"
            "detected, continues with 'The training job has completed. Please analyze\n"
            "the results in /path/to/results/ and summarize the metrics.'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "check_instruction": {
                    "type": "string",
                    "description": (
                        "Natural-language instruction for the poll LLM explaining what "
                        "conditions to check and what 'ready' means. Be specific. "
                        "Example: 'Check if the training job output contains DONE or FINISHED. "
                        "Also check for ERROR — if error is found, still trigger so we can handle it.'"
                    )
                },
                "continuation_message": {
                    "type": "string",
                    "description": (
                        "The user message to inject into this conversation when conditions "
                        "are met. This will appear as a user message and trigger a full "
                        "agentic task with all tools. Write it as an instruction for what "
                        "to do next. Example: 'The training job has completed. Please read "
                        "the results file and summarize the performance metrics.'"
                    )
                },
                "check_command": {
                    "type": "string",
                    "description": (
                        "Optional shell command to run before each poll. Its output is fed "
                        "to the LLM for grounded decision-making. Example: "
                        "'tail -20 /path/to/experiment.log' or "
                        "'ssh server \"cat /tmp/job_status.txt\"'. "
                        "If omitted, the LLM decides based on the check_instruction alone "
                        "(less reliable for external processes)."
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
                        "Maximum number of polls before giving up (status→exhausted). "
                        "Default 120. Set to 0 for unlimited (use with caution)."
                    ),
                    "default": 120
                }
            },
            "required": ["check_instruction", "continuation_message"]
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
