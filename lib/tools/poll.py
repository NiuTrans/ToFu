"""Unified async-handle tools: ``poll`` and ``kill``.

One contract for everything that runs in the background and reports back
later: background shell commands (``run_command`` with background=true),
swarm sub-agents (``spawn_agents``), and tasks in other conversations.

Design contract (owner ruling):

* ``poll()`` with no id is a snapshot dashboard of everything this user
  owns; ``poll(id, wait_s)`` blocks (clamped to 60s) until that handle
  finishes. There is no partial-result spam — a running handle reports a
  compact status, a finished handle reports its full result once.
* ``kill`` is THE way background work stops. run_command has no timeout:
  commands run to completion unless killed or stopped by the user.
* The retired per-family surfaces (``check_background_command``,
  ``await_agents``, ``await_task``, ``kill_background_command``) stay
  dispatchable for conversation-latched schemas; fresh name mistakes are
  routed here by name repair.
"""

POLL_TOOL = {
    "type": "function",
    "function": {
        "name": "poll",
        "description": (
            "Check or wait on background work: background shell commands "
            "(bg_* handles from run_command background=true), swarm "
            "sub-agents from spawn_agents, or tasks running in other "
            "conversations. With no id, returns a snapshot of everything "
            "currently running. With an id, returns that handle's status — "
            "and its full result once finished. Set wait_s to block until "
            "completion (clamped to 60s; on cap returns the running "
            "snapshot — call again to keep waiting). Background command "
            "completions are also delivered automatically, so only wait "
            "when there is nothing else to do."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "One handle: a bg_* command id, a swarm agent id, or "
                        "a task id. Legacy aliases command_id / agent_id / "
                        "task_id are accepted."
                    )
                },
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Several handles in one call.",
                },
                "wait_s": {
                    "type": "integer",
                    "description": (
                        "Seconds to block waiting for completion (clamped to "
                        "60). Omit for an instant snapshot."
                    )
                },
                "mode": {
                    "type": "string",
                    "enum": ["any", "all"],
                    "description": (
                        "With multiple swarm-agent ids: 'any' (default) "
                        "returns as soon as one finishes; 'all' waits for "
                        "every match."
                    )
                },
            },
        },
    },
}

KILL_TOOL = {
    "type": "function",
    "function": {
        "name": "kill",
        "description": (
            "Stop background work by handle: a bg_* background command "
            "(its process tree receives SIGTERM then SIGKILL) or a RUNNING "
            "swarm sub-agent. There is no timeout auto-kill — this is how "
            "background work is stopped. Tasks in other conversations "
            "cannot be killed from here; use that conversation's Stop "
            "control. Call poll with no arguments first if unsure what is "
            "running."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "Handle to stop: a bg_* command id or a swarm agent "
                        "id. Legacy alias command_id is accepted."
                    )
                },
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Several handles in one call.",
                },
            },
        },
    },
}

BACKGROUND_TOOL_NAMES = frozenset({'poll', 'kill'})

__all__ = ['POLL_TOOL', 'KILL_TOOL', 'BACKGROUND_TOOL_NAMES']
