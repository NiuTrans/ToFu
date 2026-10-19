"""Retained launch evidence shared by assign and stable-message activation.

Ordinary Swarm owns admission/execution. These pure helpers classify its reply
and update the existing retained CAS record; they never schedule or retry work.
Lookup handles are evidence addresses, not proof of admission.
"""
from __future__ import annotations

from dataclasses import dataclass
import json


# These ordinary-launch errors are emitted before this wave can be admitted.
# Legacy envelopes omit submission_state; an explicit unknown always wins.
_PRE_ADMISSION_ERRORS = frozenset({
    'execution_budget_exhausted', 'invalid_agents', 'invalid_agent',
    'retained_agent_binding', 'swarm_not_found', 'orchestration_session_mode_conflict',
    'swarm_session_conflict', 'agent_dependency_cycle',
})


@dataclass(frozen=True)
class LaunchOutcome:
    state: str
    handle: dict


def classify_launch(reply, *, run_id: str, swarm_key: str,
                    conversation_id: str) -> LaunchOutcome:
    """Accept only the exact single requested child with a real session handle."""
    try:
        handle = json.loads(reply)
    except (TypeError, ValueError):
        handle = None
    if not isinstance(handle, dict):
        return LaunchOutcome('unknown', {})
    submission = handle.get('submission_state')
    if handle.get('code') == 'swarm_launch_unknown' or submission == 'unknown':
        return LaunchOutcome('unknown', handle)
    if handle.get('status') == 'error':

        if submission not in (None, 'rejected'):
            return LaunchOutcome('unknown', handle)
        code = handle.get('code')
        if (isinstance(code, str) and (
                code in _PRE_ADMISSION_ERRORS
                or (submission == 'rejected'
                    and handle.get('phase') in ('preparation', 'validation')))):
            return LaunchOutcome('rejected', handle)
        return LaunchOutcome('unknown', handle)
    children = handle.get('agents')
    session_id = handle.get('swarm_run_id')
    if (handle.get('status') == 'async_launched'
            and handle.get('code') in (None, '')
            and submission in (None, 'accepted')
            and isinstance(session_id, str) and bool(session_id.strip())
            and handle.get('swarm_key', swarm_key) == swarm_key
            and isinstance(children, list) and len(children) == 1
            and isinstance(children[0], dict) and children[0].get('id') == run_id
            and children[0].get('swarm_run_id', session_id) == session_id
            and children[0].get('conversation_id', conversation_id) == conversation_id):
        return LaunchOutcome('accepted', handle)
    return LaunchOutcome('unknown', handle)


def launch_lookup(*, run_id: str, swarm_key: str, session_id: str = '',
                  handle: dict | None = None) -> dict:
    """Keep original requested IDs/key plus any returned session lookup name."""
    lookup = {'swarm_key': swarm_key, 'requested_agent_ids': [run_id]}
    reported = (handle or {}).get('swarm_run_id')
    if isinstance(reported, str) and reported.strip():
        session_id = reported
    if session_id:
        lookup['swarm_run_id'] = session_id
    return lookup


def apply_launch_outcome(value: dict, *, run_id: str, generation: int,
                         outcome: LaunchOutcome) -> dict:
    """CAS transform fenced against completion, abandonment and a newer run."""
    if (value.get('state') != 'busy' or value.get('run_id') != run_id
            or value.get('generation') != generation):
        return value
    if outcome.state == 'rejected':
        value.update(state='idle', run_id='', swarm_key='', run_session_id='',
                     run_conversation_id='', run_launch_state='', run_launch_lookup={})
    else:
        value['run_launch_state'] = outcome.state
        value['run_launch_lookup'] = launch_lookup(
            run_id=run_id, swarm_key=value['swarm_key'],
            session_id=value.get('run_session_id') or '', handle=outcome.handle)
        if outcome.state == 'accepted':
            value['run_session_id'] = outcome.handle['swarm_run_id']
    return value
