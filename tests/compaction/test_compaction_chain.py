"""Compression stages preserve execution facts while retiring obsolete context."""
from copy import deepcopy
import json

import pytest

from lib.tasks_pkg.compaction._continuity import build_continuity
from lib.tasks_pkg.compaction._layer1 import micro_compact
from lib.tasks_pkg.compaction._pipeline import recompose_context_after_compaction
from lib.tasks_pkg.context_composer import ComposeRequest, context_block
from lib.tasks_pkg.context_composer._render import render_context


pytestmark = pytest.mark.unit


def execution(name, arguments, result, identity):
    return [
        {'role': 'assistant', 'tool_calls': [{'id': identity, 'type': 'function',
         'function': {'name': name, 'arguments': json.dumps(arguments)}}]},
        {'role': 'tool', 'name': name, 'tool_call_id': identity, 'content': result},
    ]


@pytest.mark.parametrize('command', ['pytest tests/resume.py', 'python verify_epoch.py'])
def test_l1_cold_receipt_keeps_the_source_fact_for_l2(monkeypatch, command, record_property):
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda *a, **k: None)
    task = {'id': 'chain-task', '_userId': 7, 'config': {}}
    messages = [{'role': 'user', 'content': 'Fix the resume epoch without repeating completed work.'}]
    messages += execution('run_command', {'command': command},
                          'diagnostic detail\n' * 1500 +
                          'FAILED test_resume_epoch\n[exit code: 1]', 'verify')
    for index in range(45):
        messages += execution('read_files', {'path': f'file-{index}.py'},
                              'unrelated inspection', f'read-{index}')
    before = build_continuity(messages, task)
    original = deepcopy(messages)
    assert micro_compact(messages, task=task, ignore_cache_prefix=True) > 0
    assert messages[2]['content'] != original[2]['content']
    assert 'FAILED test_resume_epoch' in messages[2]['content']
    assert build_continuity(messages, task) == before
    assert len(messages[2]['content']) < len(original[2]['content'])
    record_property('stageMeasurements', json.dumps({
        'sourceResultChars': len(original[2]['content']),
        'l1ResultChars': len(messages[2]['content']),
        'exactOriginalObservationRetained': True,
    }, sort_keys=True))


def test_post_compaction_recomposition_retires_superseded_snapshots(monkeypatch):
    messages = [{'role': 'user', 'content': 'Continue the authorized work.'}]
    render_context(messages, [context_block('project_rules', 'OBSOLETE_RULE_SENTINEL'),
                              context_block('user_context', 'KEEP_OWNER_CONSTRAINT')],
                   ComposeRequest())
    monkeypatch.setattr('lib.tasks_pkg.context_composer.collect_context_blocks',
                        lambda *a, **k: [context_block('project_rules', 'CURRENT_RULE_SENTINEL'),
                                       context_block('user_context', 'KEEP_OWNER_CONSTRAINT')])
    task = {'id': 'chain-task', '_userId': 7, 'config': {}}
    recompose_context_after_compaction(messages, task)
    visible = json.dumps(messages, ensure_ascii=False)
    assert 'OBSOLETE_RULE_SENTINEL' not in visible
    assert visible.count('CURRENT_RULE_SENTINEL') == 1
    assert visible.count('KEEP_OWNER_CONSTRAINT') == 1
    frozen = deepcopy(messages)
    recompose_context_after_compaction(messages, task)
    assert messages == frozen


def compact_receipt(monkeypatch, *, result=None, command='python verify_epoch.py'):
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda *a, **k: None)
    task = {'id': 'chain-task', 'convId': 'chain-conversation', '_userId': 7, 'config': {}}
    messages = execution('run_command', {'command': command}, result or (
        'diagnostic detail\n' * 1500 + 'FAILED test_resume_epoch\n[exit code: 1]'), 'verify')
    for index in range(45):
        messages += execution('read_files', {'path': f'file-{index}.py'},
                              'unrelated inspection', f'read-{index}')
    micro_compact(messages, task=task, ignore_cache_prefix=True)
    return messages, task


@pytest.mark.parametrize('change', ['owner', 'text', 'arguments', 'record'])
def test_compacted_observation_rejects_changed_binding(monkeypatch, change):
    messages, task = compact_receipt(monkeypatch)
    if change == 'owner':
        task['convId'] = 'another-conversation'
    elif change == 'text':
        messages[1]['content'] += ' all checks passed'
    elif change == 'arguments':
        messages[0]['tool_calls'][0]['function']['arguments'] = '{"command":"true"}'
    else:
        messages[1]['_compactionObservation']['entry']['value'] = 'all checks passed'
    with pytest.raises(ValueError, match='compacted execution observation'):
        build_continuity(messages, task)


def test_l1_success_retires_same_invocation_failure_and_survives_route_reload(monkeypatch):
    from lib.tasks_pkg.compaction._continuity import attach_continuity
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    from lib.tasks_pkg.model_replay import _portable_messages
    messages, task = compact_receipt(monkeypatch, result='all checks passed\n' * 1500 + '[exit code: 0]')
    old = execution('run_command', {'command': 'python verify_epoch.py'},
                    'FAILED test_resume_epoch\n[exit code: 1]', 'old-failure')
    text, digest = attach_continuity('Historical failure.', old, task)
    messages.insert(0, make_compaction_handoff(text, phase='mid_turn', continuity_digest=digest))
    messages.append({'role': 'assistant', 'content': 'Continue the remaining work.'})
    restored = _portable_messages(json.loads(json.dumps(messages)))
    assert restored is not None
    assert build_continuity(restored, task)['observations'] == []


@pytest.mark.parametrize('provider', ['chat', 'responses-default', 'responses-codex', 'anthropic'])
def test_full_chain_preserves_next_action_evidence_and_reduces_wire(
        monkeypatch, provider, record_property):
    from lib.tasks_pkg.compaction._continuity import read_continuity
    from lib.tasks_pkg.compaction._layer2 import _compact as l2
    from lib.tasks_pkg.orchestrator._root_agent_loop import admit_round_context
    from tests.compaction.test_compaction_history_providers import _wire

    messages, task = compact_receipt(monkeypatch)
    task['config']['model'] = 'gpt-4o'
    task['_todos'] = [{'content': 'Fix the unresolved epoch failure', 'status': 'in_progress'}]
    messages.insert(0, {'role': 'user', 'content': 'Fix resume; preserve the public API.'})
    render_context(messages, [context_block('project_rules', 'OBSOLETE_RULE_SENTINEL')],
                   ComposeRequest())
    messages += execution('read_files', {'path': 'current.py'}, 'CURRENT_EXECUTION_PROOF', 'current')

    def wire_bytes(value):
        return len(json.dumps(_wire(value, provider), ensure_ascii=False).encode('utf-8'))

    sizes = {'afterL1WireBytes': wire_bytes(messages)}
    original_fact = build_continuity(messages, task)['observations']
    monkeypatch.setattr(l2, '_generate_query_aware_summary', lambda *a, **k: 'Continue verification.')
    monkeypatch.setattr(l2, '_extract_recently_accessed_files', lambda *a: [])
    meta = {}
    l2.execute_compact_tool(messages, task, preserve_budget_tokens=1,
                            _compaction_skip_archive=True, _result_meta=meta)
    assert meta['compacted'], meta
    sizes['afterL2WireBytes'] = wire_bytes(messages)
    monkeypatch.setattr('lib.tasks_pkg.context_composer.collect_context_blocks',
                        lambda *a, **k: [context_block('project_rules', 'CURRENT_RULE_SENTINEL')])
    recompose_context_after_compaction(messages, task)
    sizes['afterRecomposeWireBytes'] = wire_bytes(messages)
    messages.append({'role': 'user', 'content': 'LATEST_STEER: verify the failure before completing.'})
    monkeypatch.setattr('lib.tasks_pkg.compaction._prompt_admission._resolved_ceiling',
                        lambda *a, **k: (32000, 32000))
    admission = admit_round_context(task, messages, [], round_num=2, model='gpt-4o')
    rendered = json.dumps(_wire(messages, provider), ensure_ascii=False)
    state = read_continuity(messages, task)
    assert state['observations'] == original_fact
    assert state['checklist'] == task['_todos']
    for required in ('preserve the public API', 'FAILED test_resume_epoch',
                     'CURRENT_EXECUTION_PROOF', 'CURRENT_RULE_SENTINEL', 'LATEST_STEER'):
        assert required in rendered
    assert rendered.count('CURRENT_RULE_SENTINEL') == 1
    assert 'OBSOLETE_RULE_SENTINEL' not in rendered
    assert '_compactionObservation' not in rendered
    sizes['finalWireBytes'] = len(rendered.encode('utf-8'))
    sizes['finalRequestTokens'] = admission['totalTokens']
    assert sizes['finalWireBytes'] < sizes['afterL1WireBytes']
    record_property('stageMeasurements', json.dumps(sizes, sort_keys=True))
