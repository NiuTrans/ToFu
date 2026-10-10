"""Durable classification for accepted-attempt executor recovery.

``dispatch_mode`` is stored with an attempt before request acknowledgement.
Only the canonical conversation executor mode may be auto-dispatched; an
empty mode belongs to external ingestion/manual lifecycle callers and is never
interpreted as permission to start billable model work.
"""

from __future__ import annotations


class AttemptDispatchCancelled(RuntimeError):
    """Durable cancellation won before the executor could bind its task."""


CONVERSATION_EXECUTOR_DISPATCH_MODE = "conversation_executor"
ATTEMPT_DISPATCH_REQUEST_STARTED_AT_MS_CONFIG_KEY = (
    "_dispatchRequestStartedAtMs"
)
#: Model recorded on the submitted turn when a resume arrives carrying a
#: different one (pause → switch → continue). Stamped into the durable
#: attempt config at acceptance — BEFORE the submitted projection is
#: rebound — so a crashed dispatch still emits the switch banner on
#: recovery, and stamped before the rebind precisely because the rebind
#: would otherwise erase the evidence.
RESUME_MODEL_SWITCH_FROM_CONFIG_KEY = "_resumeModelSwitchFrom"
ATTEMPT_DISPATCH_MODES = frozenset({"", CONVERSATION_EXECUTOR_DISPATCH_MODE})


def normalize_attempt_dispatch_mode(value: object) -> str:
    mode = str(value or "")
    if mode not in ATTEMPT_DISPATCH_MODES:
        raise ValueError("Invalid conversation attempt dispatch mode")
    return mode


__all__ = [
    "AttemptDispatchCancelled",
    "ATTEMPT_DISPATCH_REQUEST_STARTED_AT_MS_CONFIG_KEY",
    "RESUME_MODEL_SWITCH_FROM_CONFIG_KEY",
    "ATTEMPT_DISPATCH_MODES",
    "CONVERSATION_EXECUTOR_DISPATCH_MODE",
    "normalize_attempt_dispatch_mode",
]
