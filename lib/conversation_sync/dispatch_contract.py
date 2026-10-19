"""Durable classification for accepted-attempt executor recovery.

``dispatch_mode`` is stored with an attempt before request acknowledgement.
Only the canonical conversation executor mode may be auto-dispatched; an
empty mode belongs to external ingestion/manual lifecycle callers and is never
interpreted as permission to start billable model work.
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class AcceptedResumeLineage:
    """A checked relation from the selected Turn/Attempt authority."""
    owner_user_id: int
    conversation_id: str
    turn_id: str
    attempt_id: str
    task_id: str
    source_attempt_id: str
    source_task_id: str


def read_accepted_resume_lineage(task: dict) -> AcceptedResumeLineage:
    """Read the transaction-issued receipt; caller config never grants lineage.

    All reads use explicit ownership and the existing selected repository.
    Current replacement, missing bindings and foreign/stale sources refuse.
    This grants no journal takeover, model work or financial settlement.
    """
    from lib.identity import require_user_id
    from lib.turn_lifecycle import get_attempt, get_turn
    owner = require_user_id(task.get('_userId'), context='native resume lineage')
    config = task.get('config') or {}
    attempt_id = task.get('_attemptId') or config.get('_attemptId')
    turn_id = task.get('_turnId') or config.get('_turnId')
    conversation_id = task.get('convId')
    if not all(isinstance(value, str) and value for value in (attempt_id, turn_id, conversation_id, task.get('id'))):
        raise ValueError('resume lineage requires bound task identities')
    accepted = get_attempt(attempt_id, user_id=owner, include_execution_config=True)
    if (not isinstance(accepted, dict) or accepted.get('attemptId') != attempt_id
            or accepted.get('taskId') != task['id']
            or accepted.get('turnId') != turn_id or accepted.get('conversationId') != conversation_id
            or accepted.get('operation') not in ('continue', 'checkpoint_resume', 'answer_guidance')
            or accepted.get('status') != 'running'):
        raise ValueError('resume lineage target differs from accepted execution')
    source = _read_resume_source(accepted, owner=owner, conversation_id=conversation_id,
        turn_id=turn_id, attempt_id=attempt_id, task_id=task['id'])
    current = get_turn(conversation_id, turn_id, user_id=owner)
    if (not isinstance(current, dict) or current.get('turnId') != turn_id
            or current.get('conversationId') != conversation_id or current.get('currentAttemptId') != attempt_id
            or current.get('status') != 'running'):
        raise ValueError('resume lineage was superseded')
    return AcceptedResumeLineage(owner, conversation_id, turn_id, attempt_id, task['id'],
                                 source['attemptId'], source['taskId'])


def _read_resume_source(accepted, *, owner, conversation_id, turn_id, attempt_id, task_id):
    """One immutable accepted link, shared by immediate and producer ancestry."""
    from lib.turn_lifecycle import get_attempt
    receipt = (accepted.get('config') or {}).get('_resumeSource')
    if (not isinstance(receipt, dict) or set(receipt) != {'attemptId', 'taskId', 'status', 'cause'}
            or any(not isinstance(value, str) for value in receipt.values())
            or any(not isinstance(receipt.get(key), str) or not receipt[key] for key in ('attemptId', 'taskId'))
            or receipt['attemptId'] == attempt_id or receipt['taskId'] == task_id):
        raise ValueError('resume lineage source receipt is missing or ambiguous')
    source = get_attempt(receipt['attemptId'], user_id=owner)
    if (not isinstance(source, dict) or source.get('attemptId') != receipt['attemptId']
            or source.get('taskId') != receipt['taskId']
            or source.get('conversationId') != conversation_id or source.get('turnId') != turn_id
            or source.get('status') not in ('interrupted', 'truncated', 'failed')
            or source.get('status') != receipt['status']):
        raise ValueError('resume lineage source differs from accepted receipt')
    return source


def validate_resume_producer(task: dict, producer_task_id: str, *, byte_limit: int,
                             check_cancelled) -> None:
    """Prove an original effect producer through accepted, owner-scoped ancestry.

    Repeated crashed handoffs keep the original publication identity. Walk only
    immutable receipt links with bounded temporary IDs; no cached authority or
    caller-provided lineage. This authorizes inspection, never journal takeover.
    """
    from lib.turn_lifecycle import get_attempt
    if (not isinstance(producer_task_id, str) or not producer_task_id
            or type(byte_limit) is not int or byte_limit <= 0):
        raise ValueError('invalid resume producer proof budget')
    check_cancelled()
    relation = read_accepted_resume_lineage(task)
    attempt_id, task_id = relation.source_attempt_id, relation.source_task_id
    seen = {relation.attempt_id}
    remaining = byte_limit
    while True:
        check_cancelled()
        remaining -= len(attempt_id.encode()) + len(task_id.encode()) + 128
        if attempt_id in seen or remaining < 0:
            raise ValueError('resume producer ancestry is cyclic or exceeds its byte bound')
        seen.add(attempt_id)
        if task_id == producer_task_id:
            return
        accepted = get_attempt(attempt_id, user_id=relation.owner_user_id, include_execution_config=True)
        if (not isinstance(accepted, dict) or accepted.get('attemptId') != attempt_id
                or accepted.get('taskId') != task_id or accepted.get('turnId') != relation.turn_id
                or accepted.get('conversationId') != relation.conversation_id
                or accepted.get('status') not in ('interrupted', 'truncated', 'failed')
                or accepted.get('operation') not in ('continue', 'checkpoint_resume', 'answer_guidance')):
            raise ValueError('resume producer is outside accepted ancestry')
        source = _read_resume_source(accepted, owner=relation.owner_user_id,
            conversation_id=relation.conversation_id, turn_id=relation.turn_id,
            attempt_id=attempt_id, task_id=task_id)
        attempt_id, task_id = source['attemptId'], source['taskId']


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
    "AcceptedResumeLineage",
    "read_accepted_resume_lineage",
    "validate_resume_producer",
    "AttemptDispatchCancelled",
    "ATTEMPT_DISPATCH_REQUEST_STARTED_AT_MS_CONFIG_KEY",
    "RESUME_MODEL_SWITCH_FROM_CONFIG_KEY",
    "ATTEMPT_DISPATCH_MODES",
    "CONVERSATION_EXECUTOR_DISPATCH_MODE",
    "normalize_attempt_dispatch_mode",
]
