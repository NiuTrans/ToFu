"""Complete tool evidence preserves operation/result pairing and multiplicity."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from lib.agent_core.native_process import ProgressLimits, ProgressRequest
from lib.agent_core.progress_ledger import ProgressLedgerV2
from lib.agent_loop.agent_loop import AbortSignal, run_agent_loop
from lib.identity import PrincipalContext
from lib.tasks_pkg.native_progress import NativeGatewayProgress

pytestmark = pytest.mark.unit


def tool_call(path, identity):
    return {'id': identity, 'function': {
        'name': 'read', 'arguments': json.dumps({'path': path})}}


def native_observations(rounds, *, reverse_completion=False):
    principal = PrincipalContext.user(subject_id='result-evidence', owner_user_id=7)
    gateway = SimpleNamespace(_closed=False, owner=7, task_id='task',
        task={'id': 'task', '_userId': 7, 'status': 'running'})
    progress = NativeGatewayProgress(gateway, ProgressLimits(2, 2), 4096)
    try:
        for round_num, entries in enumerate(rounds):
            if reverse_completion and round_num % 2:
                entries = list(reversed(entries))
            for call, result in entries:
                progress.record(round_num, call, result)
            yield asyncio.run(progress.observe(ProgressRequest(
                principal, 'task', round_num, len(entries), threading.Event())))
    finally:
        progress.close()


@pytest.mark.parametrize('adapter', ['serial', 'native'])
@pytest.mark.parametrize('change', ['operation_results', 'repeated_result_counts'])
def test_changed_result_assignment_or_counts_remain_productive(adapter, change):
    paths = ['a', 'b'] if change == 'operation_results' else ['a', 'a', 'a']
    values = [('old', 'new'), ('new', 'old')] if change == 'operation_results' else [
        ('old', 'old', 'new'), ('old', 'new', 'new')]
    rounds = [[(tool_call(path, f'{rnd}-{index}'), result)
               for index, (path, result) in enumerate(zip(paths, values[rnd % 2]))]
              for rnd in range(6)]
    if adapter == 'native':
        ledger = ProgressLedgerV2()
        for entries, observation in zip(rounds, native_observations(rounds)):
            decision = ledger.observe([call for call, _ in entries],
                evidence_ids=observation['evidenceIds'],
                evidence_complete=observation['resultEvidenceComplete'])
            assert decision['noProgressStreak'] == 0
    else:
        position = 0
        def dispatch(rnd, _tools):
            if rnd == len(rounds):
                return {'role': 'assistant', 'content': 'done'}, 'stop', {}
            return {'role': 'assistant', 'tool_calls': [call for call, _ in rounds[rnd]]}, 'tool_calls', {}
        def execute(rnd, _call):
            nonlocal position
            result = rounds[rnd][position % len(paths)][1]
            position += 1
            return result
        outcome = run_agent_loop(abort=AbortSignal.never(), round_tools=[],
            dispatch=dispatch, execute_tool=execute, max_consecutive_no_progress_rounds=2)
        assert outcome.completed, outcome.exit_reason
        assert position == len(rounds) * len(paths)


def test_native_completion_order_and_transport_ids_do_not_invent_progress():
    rounds = [[(tool_call(path, f'{rnd}-{path}'), value)
               for path, value in [('a', 'old'), ('b', 'new')]] for rnd in range(3)]
    ledger = ProgressLedgerV2()
    for rnd, (entries, observation) in enumerate(zip(
            rounds, native_observations(rounds, reverse_completion=True))):
        decision = ledger.observe([call for call, _ in entries],
            evidence_ids=observation['evidenceIds'],
            evidence_complete=observation['resultEvidenceComplete'])
        assert decision['noProgressStreak'] == rnd


def test_operation_identity_normalizes_argument_encoding_and_preserves_target():
    from lib.agent_core.progress_evidence import tool_result_evidence
    original = tool_call('a', 'original')
    reformatted = tool_call('a', 'replacement')
    reformatted['function']['arguments'] = ' { "path" : "a" } '
    decoded = {'function': {'name': 'read', 'arguments': {'path': 'a'}}}
    evidence = tool_result_evidence(original, ['same', {'text': 'visible'}])
    assert evidence == tool_result_evidence(reformatted, ['same', {'text': 'visible'}])
    assert evidence == tool_result_evidence(decoded, ['same', {'text': 'visible'}])
    assert evidence != tool_result_evidence(tool_call('b', 'original'), ['same', {'text': 'visible'}])


@pytest.mark.parametrize('source', ['note', 'probe'])
@pytest.mark.parametrize('malformation', ['missing', 'scalar', 'mapping', 'bad_item', 'flag'])
def test_malformed_complete_evidence_cannot_stop_a_run(source, malformation):
    def dispatch(rnd, _tools):
        if rnd == 4:
            return {'role': 'assistant', 'content': 'done'}, 'stop', {}
        return {'role': 'assistant', 'tool_calls': [tool_call('a', str(rnd))]}, 'tool_calls', {}
    evidence_key, complete_key = (('progress_evidence_ids', 'result_evidence_complete')
        if source == 'note' else ('evidenceIds', 'resultEvidenceComplete'))
    value = {evidence_key: ['same'], complete_key: True}
    if malformation == 'missing':
        value.pop(evidence_key)
    elif malformation == 'scalar':
        value[evidence_key] = 'same'
    elif malformation == 'mapping':
        value[evidence_key] = {'same': True}
    elif malformation == 'bad_item':
        value[evidence_key] = [None]
    else:
        value[complete_key] = 'true'
    outcome = run_agent_loop(abort=AbortSignal.never(), round_tools=[], dispatch=dispatch,
        execute_tools=lambda *_: value if source == 'note' else None,
        progress_probe=lambda: value if source == 'probe' else {},
        max_consecutive_no_progress_rounds=2)
    assert outcome.completed, outcome.exit_reason


@pytest.mark.parametrize('source', ['note', 'probe'])
@pytest.mark.parametrize('container', [list, tuple, set])
def test_valid_evidence_still_stops_unchanged_work_with_malformed_other_source(source, container):
    note = {'progress_evidence_ids': container(['same']), 'result_evidence_complete': True}
    probe = {'evidenceIds': container(['same']), 'resultEvidenceComplete': True}
    if source == 'note':
        probe['evidenceIds'] = 'invalid'
    else:
        note['progress_evidence_ids'] = 'invalid'
    def dispatch(rnd, _tools):
        return {'role': 'assistant', 'tool_calls': [tool_call('a', str(rnd))]}, 'tool_calls', {}
    outcome = run_agent_loop(abort=AbortSignal.never(), round_tools=[], dispatch=dispatch,
        execute_tools=lambda *_: note, progress_probe=lambda: probe,
        max_consecutive_no_progress_rounds=2)
    assert outcome.halted and outcome.exit_reason == 'no_progress'
    assert outcome.rounds == 3


def test_recovered_streak_resets_when_result_targets_change_without_replaying_tools():
    saved = []
    executed = []
    class Interrupted(Exception):
        pass
    def dispatch(rnd, _tools):
        if rnd == 4:
            return {'role': 'assistant', 'content': 'done'}, 'stop', {}
        return {'role': 'assistant', 'tool_calls': [tool_call(path, f'{rnd}-{path}')
            for path in ['a', 'b']]}, 'tool_calls', {}
    def execute(rnd, call):
        path = json.loads(call['function']['arguments'])['path']
        executed.append((rnd, path))
        return 'old' if (path == 'a') == (rnd < 2) else 'new'
    def checkpoint(value):
        if value['next_round'] == 2:
            saved.append(value)
            raise Interrupted()
    arguments = dict(abort=AbortSignal.never(), round_tools=[], dispatch=dispatch,
        execute_tool=execute, max_consecutive_no_progress_rounds=2)
    with pytest.raises(Interrupted):
        run_agent_loop(**arguments, on_checkpoint=checkpoint)
    assert saved[0]['progress']['no_progress_streak'] == 1
    outcome = run_agent_loop(**arguments, resume_checkpoint=saved[0])
    assert outcome.completed, outcome.exit_reason
    assert executed == [(rnd, path) for rnd in range(4) for path in ['a', 'b']]


def test_older_result_only_checkpoint_rebaselines_without_rewinding_rounds():
    from lib.agent_core.progress_evidence import visible_result_evidence
    from tests.agent_core.test_agent_loop_recovery import checkpoint_after, turn
    snapshot, _ = checkpoint_after(max_consecutive_no_progress_rounds=2)
    previous = ProgressLedgerV2()
    for rnd in range(2):
        previous.observe(turn(rnd, [])[0]['tool_calls'],
            evidence_ids=[visible_result_evidence('same')], evidence_complete=True)
    snapshot['progress'] = previous.snapshot()
    executed = []
    outcome = run_agent_loop(abort=AbortSignal.never(), round_tools=[], dispatch=turn,
        execute_tool=lambda rnd, call: executed.append((rnd, call['id'])) or 'same',
        max_consecutive_no_progress_rounds=2, resume_checkpoint=snapshot)
    assert outcome.halted and outcome.exit_reason == 'no_progress'
    assert [rnd for rnd, _ in executed] == [2, 3, 4]
    assert len({identity for _, identity in executed}) == 3
