"""Persist flow-visible phases as authoritative conversation turns.

Translation is scheduled once from the terminal turn event by
``lib.translate.terminal``. This module only projects executor-local flow
messages into stable visible turn identities for goal-mode autopilot and
user-selected Studio flows.
"""

from lib.conversation_sync.attempt_identity import is_conversation_attempt


def sync_flow_turns_to_conversation(task, flow_turns):
    """Idempotently project the accumulated flow phases into turn rows."""
    if not flow_turns or not is_conversation_attempt(task):
        return None
    from lib.turn_lifecycle import sync_visible_run_turns

    return sync_visible_run_turns(task, flow_turns)


def store_flow_turns_on_task(task, flow_turns):
    """Expose a copied phase snapshot to task polling and terminal projection."""
    task["_flow_turns"] = list(flow_turns or [])


__all__ = [
    "store_flow_turns_on_task",
    "sync_flow_turns_to_conversation",
]
