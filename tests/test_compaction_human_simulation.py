"""Long-horizon compaction evaluation with a deterministic agent/user oracle.

This is not a substitute for SWE-bench. It isolates the production incident's
mechanism: a tool-dominated turn is compacted, then an agent either sees the
authoritative checklist and working evidence or spends the next cycle
replanning/re-reading. Three user-facing task shapes run for up to 40
compaction cycles each (120 simulated opportunities to regress).
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


SCENARIOS = (
    {
        'name': 'code-investigation',
        'tool': 'grep_search',
        'args': {'pattern': 'force_compact_if_needed', 'path': 'lib'},
        'result': 'lib/compaction.py:42:def force_compact_if_needed',
        'required_fact': 'lib/compaction.py:42',
    },
    {
        'name': 'verification',
        'tool': 'run_command',
        'args': {'cmd': 'pytest tests/test_compaction.py -q'},
        'result': '29 passed in 3.70s',
        'required_fact': '29 passed in 3.70s',
    },
    {
        'name': 'delegated-research',
        'tool': 'get_agent_result',
        'args': {'id': 'agent-a'},
        'result': 'Root cause: the summary omitted every tool result.',
        'required_fact': 'summary omitted every tool result',
    },
)


def _todos() -> list[dict]:
    return [
        {'id': 'inspect', 'content': 'Inspect current state',
         'status': 'in_progress'},
        {'id': 'fix', 'content': 'Implement the focused fix',
         'status': 'pending'},
        {'id': 'verify', 'content': 'Verify the user outcome',
         'status': 'pending'},
    ]


def _tool_history(scenario: dict) -> list[dict]:
    return [
        {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'call-1', 'type': 'function',
            'function': {
                'name': scenario['tool'],
                'arguments': json.dumps(scenario['args']),
            },
        }]},
        {'role': 'tool', 'tool_call_id': 'call-1',
         'name': scenario['tool'], 'content': scenario['result']},
    ]


def _visible_text(messages: list[dict]) -> str:
    return '\n'.join(
        str(message.get('content') or '') for message in messages)


def _simulated_agent_action(messages: list[dict], required_fact: str) -> str:
    """A conservative coding-agent policy matching the incident behaviour."""
    visible = _visible_text(messages)
    if '## Active Task Checklist' not in visible:
        return 'replan_with_todo_write'
    if required_fact not in visible:
        return 'rediscover_with_tool'
    return 'advance_declared_work'


def _advance(todos: list[dict]) -> None:
    active = next((todo for todo in todos
                   if todo['status'] == 'in_progress'), None)
    if active:
        active['status'] = 'completed'
    pending = next((todo for todo in todos if todo['status'] == 'pending'), None)
    if pending:
        pending['status'] = 'in_progress'


def _simulate(scenario: dict, *, restoration: str,
              max_cycles: int = 40) -> dict:
    from lib.tasks_pkg.attachments import (
        compute_turn_attachments, inject_attachments)
    from lib.tasks_pkg.compaction._evidence import (
        bound_evidence_ledger, build_evidence_ledger,
        format_evidence_ledger)

    task = {'_todos': _todos()}
    evidence_source = _tool_history(scenario)
    model_tool_calls = 0
    actions = []

    for cycle in range(max_cycles):
        # Shape immediately after L2 replaced the cold tool round.
        compact_result = 'Earlier work was summarized without tool evidence.'
        if restoration == 'full':
            ledger = bound_evidence_ledger(
                build_evidence_ledger(evidence_source, task), 16_000)
            compact_result += '\n\n' + format_evidence_ledger(ledger)
        messages = [
            {'role': 'user', 'content': 'Fix the compaction regression.'},
            {'role': 'tool', 'name': 'context_compact',
             'content': compact_result},
        ]
        if restoration in ('todo_only', 'full'):
            attachments = compute_turn_attachments(
                messages, task, round_num=cycle + 1,
                conv_id=f"sim-{scenario['name']}")
            inject_attachments(messages, attachments)

        action = _simulated_agent_action(
            messages, scenario['required_fact'])
        actions.append(action)
        model_tool_calls += 1
        if action == 'advance_declared_work':
            _advance(task['_todos'])
        # Replanning/rediscovery recreates information that the following
        # compaction loses again, so declared work itself does not advance.
        if all(todo['status'] == 'completed' for todo in task['_todos']):
            break

    completed = all(
        todo['status'] == 'completed' for todo in task['_todos'])
    # Simulated human acceptance: outcome first, then interaction cost. A fast
    # incomplete task is not a win; a completed three-step task within ten
    # model tool calls is a good experience.
    human_score = 5 if completed and model_tool_calls <= 10 else 1
    return {
        'completed': completed,
        'modelToolCalls': model_tool_calls,
        'humanScore': human_score,
        'actions': actions,
    }


@pytest.mark.parametrize('scenario', SCENARIOS,
                         ids=[row['name'] for row in SCENARIOS])
def test_compaction_state_continuity_beats_legacy_replanning_loop(scenario):
    legacy = _simulate(scenario, restoration='none')
    todo_only = _simulate(scenario, restoration='todo_only')
    fixed = _simulate(scenario, restoration='full')

    assert legacy == {
        'completed': False,
        'modelToolCalls': 40,
        'humanScore': 1,
        'actions': ['replan_with_todo_write'] * 40,
    }
    assert todo_only == {
        'completed': False,
        'modelToolCalls': 40,
        'humanScore': 1,
        'actions': ['rediscover_with_tool'] * 40,
    }
    assert fixed['completed'] is True
    assert fixed['modelToolCalls'] == 3
    assert fixed['humanScore'] == 5
    assert fixed['actions'] == ['advance_declared_work'] * 3
