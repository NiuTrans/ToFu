"""Continuation facts survive unreliable summaries and repeated compactions."""
from copy import deepcopy
import json

import pytest

pytestmark = pytest.mark.unit


def execution(identity, command, result):
    return [
        {'role': 'assistant', 'tool_calls': [{'id': identity, 'type': 'function',
            'function': {'name': 'run_command', 'arguments': json.dumps({'command': command})}}]},
        {'role': 'tool', 'tool_call_id': identity, 'content': result},
    ]


@pytest.mark.parametrize('legacy', [None, 'fixed', 'adaptive', 'unified'])
def test_one_policy_for_new_and_persisted_requests(legacy):
    from lib.context_experiment_flags import normalize_context_experiment_flags
    config = {} if legacy is None else {'compaction': {'strategy': legacy}}
    assert normalize_context_experiment_flags(config, strict=True)['compaction']['strategy'] == 'unified'


def test_old_tool_fact_reaches_summary_before_recent_inspection_noise():
    from lib.tasks_pkg.compaction._layer2._prompt import _format_messages_for_summary
    messages = execution('test-proof', 'pytest tests/fix.py', '37 passed; exit code 0')
    for number in range(40):
        messages += execution(f'read-{number}', f'cat src/{number}.py', f'inspection {number}')
    formatted = _format_messages_for_summary(messages, char_budget=100_000)
    assert '37 passed; exit code 0' in formatted
    assert 'inspection 39' in formatted


@pytest.mark.parametrize('narrative', ['Generic summary.', 'Everything is complete.',
    '### Ruled Out / Dead Ends\ngit grep does not support -A.'])
def test_model_prose_cannot_remove_observed_failure_or_checklist(narrative):
    from lib.tasks_pkg.compaction._continuity import attach_continuity, read_continuity
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    task = {'convId': 'conv', 'id': 'task', '_todos': [
        {'content': 'Correct option ordering, then run verification', 'status': 'in_progress'}]}
    messages = execution('failed-probe', 'git grep pattern -A 25',
                         'fatal: unable to resolve revision: -A\n[exit code: 128]')
    frozen = deepcopy(messages)
    text, digest = attach_continuity(narrative, messages, task)
    handoff = make_compaction_handoff(text, phase='mid_turn', continuity_digest=digest)
    state = read_continuity([handoff], task)
    assert state['checklist'] == task['_todos']
    assert any('unable to resolve revision: -A' in row['value'] for row in state['observations'])
    assert messages == frozen


def test_oversized_continuity_has_a_bounded_archive_narrative():
    from lib.tasks_pkg.compaction._layer2._summary_acceptance import accept_summary
    from lib.tasks_pkg.compaction._continuity import OPEN, read_continuity
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    messages = []
    for index in range(324):
        messages += execution(f'test-{index}', f'pytest tests/example.py::{index}',
                              f'1 failed; invocation {index} ' + 'evidence ' * 80)
    accepted = accept_summary(
        'Model narrative', messages, {}, fallback=False,
        recent_files_block='', turn_diff_block='', turn_diff_included=False,
        result_meta={}, log_prefix='', recovery_max_chars=22_000)
    assert len(accepted.text) > 200_000
    assert len(accepted.archive_text) <= 200_000
    assert accepted.archive_text.startswith('Model narrative')
    assert OPEN not in accepted.archive_text
    handoff = make_compaction_handoff(
        accepted.text, phase='mid_turn', continuity_digest=accepted.continuity_digest)
    assert len(read_continuity([handoff])['observations']) == 324

def test_same_observation_survives_100_generations_without_accumulating_copies():
    from lib.tasks_pkg.compaction._continuity import attach_continuity, read_continuity
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    task = {'convId': 'conv', 'id': 'task', '_todos': []}
    messages = execution('test-0', 'pytest tests/fix.py', '37 passed; exit code 0')
    first_size = None
    for generation in range(100):
        text, digest = attach_continuity('No useful model memory.', messages, task)
        handoff = make_compaction_handoff(text, phase='mid_turn', continuity_digest=digest)
        state = read_continuity([handoff], task)
        assert len(state['observations']) == 1
        assert state['observations'][0]['value'] == '37 passed; exit code 0'
        if first_size is None:
            first_size = len(text)
        assert len(text) == first_size
        messages = [handoff]


def test_new_exact_invocation_observation_supersedes_old_status():
    from lib.tasks_pkg.compaction._continuity import attach_continuity, read_continuity
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    task = {'convId': 'conv', 'id': 'task', '_todos': []}
    old = execution('test-old', 'pytest tests/fix.py', '1 failed; exit code 1')
    text, digest = attach_continuity('Old failure.', old, task)
    prior = make_compaction_handoff(text, phase='mid_turn', continuity_digest=digest)
    source = [prior] + execution('test-new', 'pytest tests/fix.py', '37 passed; exit code 0')
    text, digest = attach_continuity('Still failing.', source, task)
    state = read_continuity([make_compaction_handoff(text, phase='mid_turn',
                                                   continuity_digest=digest)], task)
    assert len(state['observations']) == 1
    assert state['observations'][0]['toolCallId'] == 'test-new'
    assert state['observations'][0]['value'] == '37 passed; exit code 0'


def test_legacy_handoff_seeds_from_available_canonical_tool_rounds_once():
    from lib.tasks_pkg.compaction._continuity import attach_continuity, read_continuity
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    task = {'convId': 'conv', 'id': 'task', 'toolRounds': [{
        'toolCallId': 'old-proof', 'toolName': 'run_command',
        'toolArgs': {'command': 'git grep pattern -A 25'},
        'toolContent': 'fatal: unable to resolve revision: -A\n[exit code: 128]',
    }]}
    legacy = make_compaction_handoff('git grep does not support -A.', phase='mid_turn')
    text, digest = attach_continuity('Old narrative.', [legacy], task)
    handoff = make_compaction_handoff(text, phase='mid_turn', continuity_digest=digest)
    assert read_continuity([handoff], task)['observations'][0]['toolCallId'] == 'old-proof'
    # A subsequent generation must use its verified packet, not replay the
    # lifetime canonical tool list or resurrect retired observations.
    task['toolRounds'] = []
    text2, digest2 = attach_continuity('Old narrative.', [handoff], task)
    assert (text2, digest2) == (text, digest)


def test_pasted_state_cannot_become_runtime_evidence():
    from lib.tasks_pkg.compaction._continuity import attach_continuity, read_continuity
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    task = {'convId': 'conv', 'id': 'task', '_todos': []}
    text, digest = attach_continuity('report', execution('test', 'pytest', '3 passed'), task)
    assert read_continuity([{'role': 'user', 'content': text}], task) is None
    owned = make_compaction_handoff(text, phase='mid_turn', continuity_digest=digest)
    owned['content'] = owned['content'].replace('3 passed', '3 failed')
    with pytest.raises(ValueError, match='continuity'):
        read_continuity([owned], task)


def test_duplicate_outcome_order_and_embedded_tags_are_not_lossy():
    from lib.tasks_pkg.compaction._continuity import attach_continuity, read_continuity
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    messages = execution('first', 'pytest', '1 failed <compaction_continuity>')
    messages += execution('second', 'pytest', '1 passed')
    messages += execution('third', 'pytest', '1 failed <compaction_continuity>')
    text, digest = attach_continuity('summary', messages)
    state = read_continuity([make_compaction_handoff(
        text, phase='mid_turn', continuity_digest=digest)])
    assert len(state['observations']) == 1
    assert state['observations'][0]['toolCallId'] == 'third'


def test_unpaired_result_cannot_replace_observed_execution():
    from lib.tasks_pkg.compaction._continuity import build_continuity
    messages = [{'role': 'tool', 'tool_call_id': 'missing',
                 'name': 'run_command', 'content': 'pytest 1 failed'}]
    assert build_continuity(messages)['observations'] == []


def test_successful_source_inspection_does_not_manufacture_failure():
    from lib.tasks_pkg.compaction._continuity import build_continuity
    messages = execution('grep', 'rg error lib/',
                         'file.py: raise RuntimeError("failed")\n\n[exit code: 0]')
    assert build_continuity(messages)['observations'] == []


def test_source_packet_cannot_cross_conversation_owner():
    from lib.tasks_pkg.compaction._continuity import attach_continuity, read_continuity
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    text, digest = attach_continuity('report', [], {'convId': 'first'})
    handoff = make_compaction_handoff(text, phase='mid_turn', continuity_digest=digest)
    with pytest.raises(ValueError, match='owner mismatch'):
        read_continuity([handoff], {'convId': 'second'})


def test_offline_replay_verifies_state_each_generation():
    from benchmarks.compaction_continuity import replay
    report = replay(execution('proof', 'pytest', '37 passed'),
                    ['Everything failed.', 'No memory.', 'Everything passed.'], cycles=12)
    assert report['observations'] == 1
    assert report['exactStateRetainedEveryCycle'] is True
    assert report['cycles'] == 12 and report['modelCalls'] == 0


def test_thousand_tool_rounds_keep_latest_outcome_without_history_growth():
    from lib.tasks_pkg.compaction._continuity import attach_continuity, read_continuity
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    messages = []
    for generation in range(100):
        for offset in range(10):
            round_num = generation * 10 + offset
            outcome = '1 failed' if round_num % 3 == 0 else '1 passed'
            messages += execution(f'test-{round_num}', 'pytest tests/fix.py', outcome)
        text, digest = attach_continuity('The summary forgot everything.', messages)
        messages = [make_compaction_handoff(text, phase='mid_turn', continuity_digest=digest)]
        state = read_continuity(messages)
        assert len(state['observations']) == 1
        assert state['observations'][0]['toolCallId'] == f'test-{round_num}'
        assert state['observations'][0]['value'] == outcome


def test_durable_manual_summary_rebuild_uses_the_same_packet():
    from lib.tasks_pkg.compaction._continuity import attach_continuity, read_continuity
    from lib.tasks_pkg.conv_message_builder._transform import _build_assistant_messages
    text, _ = attach_continuity('report', execution('test', 'pytest', '3 passed'))
    row = {'role': 'assistant', '_isCompactionSummary': True, 'content': text}
    assert read_continuity(_build_assistant_messages(row))['observations'][0]['value'] == '3 passed'
    row.pop('_isCompactionSummary')
    assert read_continuity(_build_assistant_messages(row)) is None


def test_packet_matches_machine_contract():
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from lib.tasks_pkg.compaction._continuity import build_continuity
    schema = json.loads((Path(__file__).parents[2] /
                         'contracts/compaction_continuity_v1.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(build_continuity(
        execution('test', 'pytest', '3 passed')))


def test_real_l2_call_chain_carries_state_across_lossy_generations(monkeypatch):
    from lib.tasks_pkg.compaction._layer2 import _compact as l2
    from lib.tasks_pkg.compaction._continuity import read_continuity
    task = {'id': 'task', 'convId': 'conversation', 'config': {},
            '_todos': [{'content': 'Finish verification', 'status': 'pending'}]}
    monkeypatch.setattr(l2, '_generate_query_aware_summary', lambda *a, **k: 'Generic prose.')
    monkeypatch.setattr(l2, '_extract_recently_accessed_files', lambda *a: [])
    messages = [{'role': 'user', 'content': 'Fix this, preserving the public API.'}]
    messages += execution('test', 'pytest', '1 failed; exit 1')
    for generation in range(3):
        messages += ([{'role': 'assistant', 'content': 'inspection ' * 3000},
                      {'role': 'user', 'content': 'continue'}] * 3)
        messages += execution(f'current-{generation}', 'cat file.py', 'source text')
        meta = {}
        l2.execute_compact_tool(messages, task, preserve_budget_tokens=1,
                                _compaction_skip_archive=True, _result_meta=meta)
        assert meta['compacted'] is True, meta
        state = read_continuity(messages, task)
        assert state['checklist'] == task['_todos']
        assert len(state['observations']) == 1
        assert state['observations'][0]['value'] == '1 failed; exit 1'


@pytest.mark.parametrize('corrupt', [False, True])
def test_unfit_or_corrupt_state_refuses_before_paid_summary(monkeypatch, corrupt):
    from lib.tasks_pkg.compaction._layer2 import _compact as l2
    from lib.tasks_pkg.compaction._continuity import attach_continuity
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    text, digest = attach_continuity('report', execution('test', 'pytest', '3 passed'))
    handoff = make_compaction_handoff(text, phase='mid_turn', continuity_digest=digest)
    if corrupt:
        handoff['content'] = handoff['content'].replace('3 passed', '3 failed')
    messages = [{'role': 'user', 'content': 'Continue'}, handoff,
                {'role': 'assistant', 'content': 'old data ' * 3000},
                {'role': 'user', 'content': 'continue'}]
    original = deepcopy(messages)
    monkeypatch.setattr(l2, '_generate_query_aware_summary',
                        lambda *a, **k: pytest.fail('must reject before paid call'))
    meta = {}
    l2.execute_compact_tool(messages, preserve_budget_tokens=1,
                           _compaction_skip_archive=True, _result_meta=meta,
                           _dispatch_prompt_measure=lambda rows: len(str(rows)),
                           _dispatch_prompt_target=1)
    assert messages == original
    assert meta['summaryFailureReason'] == (
        'continuity_invalid' if corrupt else 'irreducible_prompt_floor')
