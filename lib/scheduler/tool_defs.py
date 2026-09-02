"""lib/scheduler/tool_defs.py — LLM tool schema definitions for the scheduler."""

SCHEDULE_TOOL_CREATE = {
    "type": "function",
    "function": {
        "name": "schedule_create",
        "description": (
            "Create a scheduled task that runs automatically at a future time. "
            "Tasks persist across server restarts. Use a cron expression for recurring "
            "tasks, or 'once:YYYY-MM-DD HH:MM' for a one-time task.\n\n"
            "Cron format (local time): minute hour day_of_month month day_of_week\n"
            "  '*/5 * * * *'      — every 5 minutes\n"
            "  '7 * * * *'        — hourly at :07 (see off-peak tip below)\n"
            "  '0 9 * * 1-5'      — weekdays at 09:00\n"
            "  '30 8,12,18 * * *' — at 08:30, 12:30 and 18:30 daily\n"
            "  '0 0 1 * *'        — first day of each month at midnight\n"
            "  'once:2026-03-15 14:00' — one time at that instant, then auto-disables\n\n"
            "ONE-SHOT vs RECURRING: a cron expression recurs forever until disabled. "
            "For a single future action prefer 'once:…', OR set max_executions=1 to "
            "auto-disable a cron after its first run.\n\n"
            "OFF-PEAK TIP: when the user's time is approximate ('around 9', 'hourly'), "
            "avoid minute 0 and 30 — pick an off-minute like '7 9 * * *' so many "
            "schedules don't all fire on the same wall-clock boundary. Only use :00/:30 "
            "when the user names that exact time.\n\n"
            "Task types:\n"
            "  'command' — run a shell command (may be DISABLED by deployment policy)\n"
            "  'python'  — run Python code (may be DISABLED by deployment policy)\n"
            "  'prompt'  — simple LLM inference (no tools)\n"
            "  'agent'   — PROACTIVE AGENT: periodically polls (cheap model) to decide\n"
            "              if conditions are met, then executes a full agentic task with\n"
            "              ALL tools in the target conversation. The execution is visible\n"
            "              to the user as a normal assistant response with tool calls.\n"
            "              Each poll is independent (no cross-poll history, saving tokens).\n"
            "              Use this for: monitoring, recurring analysis, event-driven actions.\n\n"
            "Returns the task ID and resolved next-run time. Max 100 tasks total."
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
                        "browserEnabled, memoryEnabled, imageGenEnabled, model. "
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
                },
                "condition_command": {
                    "type": "string",
                    "description": (
                        "For task_type='agent' ONLY: an optional shell PREDICATE that decides "
                        "whether to act this poll (exit code 0 = act, or match condition_regex). "
                        "Given ALONE it makes a zero-LLM 'code' agent (each poll just runs the "
                        "predicate); given WITH command (the standing instruction) it makes a "
                        "'hybrid' agent — the LLM decides but the predicate is reconciled and "
                        "AUTO-PROMOTED to pure code once it consistently agrees (poll cost → 0). "
                        "Use for deterministic triggers. Example: "
                        "'test $(cat /path/state) = ready'."
                    )
                },
                "condition_regex": {
                    "type": "string",
                    "description": (
                        "Optional regex matched against condition_command's stdout to decide. "
                        "If omitted, the exit code decides (0 = act). Agent tasks only."
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
            "Create a durable background Timer Watcher that periodically checks "
            "whether conditions are met, then starts a fresh conversation turn with "
            "the requested continuation.\n\n"
            "Use this when a long-running process has been started (e.g. remote experiment, "
            "build, deployment) and you want to automatically proceed once it completes.\n\n"
            "IMPORTANT: This tool returns immediately after durable creation. Do not "
            "wait or poll manually. The timer survives task completion and server "
            "restart; its status and poll evidence remain inspectable with timer_manage.\n\n"
            "⛔ DO NOT watch for something only a HUMAN can do — most importantly, never "
            "create a timer that waits for THIS Tofu server to restart / redeploy / pick "
            "up a code change. That is a human action, not an event that resolves on its "
            "own, so the watcher just burns polls doing nothing. If your work needs a "
            "server restart to take effect, simply STOP and tell the user; do the "
            "verification in your NEXT turn AFTER they restart. A timer is only for "
            "conditions that become true on their own (a remote CI run finishing, a file "
            "appearing, a download completing, a training job reaching a state).\n\n"
            "TOOL-CAPABLE: The timer poll LLM has access to the SAME tools as you "
            "(web_search, fetch_url, run_command, read_files, grep_search, "
            "find_files, etc.). It can actively gather information to evaluate conditions "
            "— not just passively read check_command output. This means the check_instruction "
            "can describe complex conditions that require file reading, web requests, "
            "or command execution to verify.\n\n"
            "How it works:\n"
            "  1. You set up the timer with a check instruction + continuation message.\n"
            "  2. This tool returns immediately and the current task may finish normally.\n"
            "  3. A poll LLM evaluates the check instruction each poll (with tools, "
            "independent per poll, no cross-poll history).\n"
            "  4. Optionally, a shell command runs before each poll for grounded status data.\n"
            "  5. When conditions are met, a new authoritative turn is created.\n"
            "  6. That turn proceeds with the continuation instructions.\n"
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
                        "'ssh server \"cat ~/job_status.txt\"'. "
                        "If omitted, the LLM decides based on the check_instruction alone "
                        "(less reliable for external processes)."
                    )
                },
                "condition_command": {
                    "type": "string",
                    "description": (
                        "Optional shell PREDICATE that DECIDES readiness by its result "
                        "(exit code 0 = ready, or match condition_regex) — distinct from "
                        "check_command, which only feeds output to the LLM. Giving this "
                        "ALONE runs a zero-LLM 'code' watcher (cheapest); giving it TOGETHER "
                        "with check_instruction runs a 'hybrid' watcher where the LLM decides "
                        "but the predicate is reconciled each poll and AUTO-PROMOTED to "
                        "pure code after it agrees with the LLM enough times (cost → 0). "
                        "Prefer this for deterministic conditions. Example: "
                        "'grep -q DONE /path/train.log'."
                    )
                },
                "condition_regex": {
                    "type": "string",
                    "description": (
                        "Optional regex matched against condition_command's stdout to decide "
                        "readiness. If omitted, the command's EXIT CODE decides (0 = ready). "
                        "Only meaningful alongside condition_command."
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
