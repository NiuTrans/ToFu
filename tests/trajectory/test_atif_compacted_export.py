"""A compacted run's ATIF export must contain every pre-compaction round.

12 of 88 TB2.1 baseline trajectories compacted, and their exports contained
only the post-compaction working view — the folded rounds were missing, so the
contamination audit could not see the calls that fetched hidden material.
``_archive_transcript`` now records each fold on the task, and the exporter
re-prepends the archived prefixes with the compaction point marked.
"""
from copy import deepcopy

import pytest

from lib.trajectory import flatten, _full_history_messages

pytestmark = pytest.mark.unit


def _round(call_id, path, result, text='inspect'):
    return [
        {'role': 'assistant', 'content': text, 'tool_calls': [{
            'id': call_id, 'function': {'name': 'read_files',
                                        'arguments': '{"path": "%s"}' % path}}]},
        {'role': 'tool', 'tool_call_id': call_id, 'content': result},
    ]


def _compacted_task():
    # 20 folded rounds survive only as an archive record; the working view is
    # the summary plus the last 2 rounds.
    folded = []
    for i in range(20):
        folded.extend(_round(f'old{i}', f'f{i}.py', f'content of f{i}'))
    working = [{'role': 'system', '_isMeta': True, 'content': 'summary of earlier rounds'}]
    working.extend(_round('new0', 'g0.py', 'content of g0'))
    working.extend(_round('new1', 'g1.py', 'content of g1'))
    working.append({'role': 'assistant', 'content': 'final'})
    return {
        'id': 'compacted', 'status': 'done', 'finishReason': 'stop',
        'messages': working,
        '_messages_include_current_run': True,
        'content': 'final',
        'usage': {'prompt_tokens': 100, 'completion_tokens': 10},
        '_compaction_archives': [{
            'messages': folded, 'trigger': 'window', 'round_num': 20,
            'msgs_before': len(folded), 'msgs_after': len(working),
        }],
    }


def test_every_precompaction_round_is_exported():
    task = _compacted_task()
    before = deepcopy(task)
    doc = flatten(task, 'atif')['trajectory']
    steps = doc['steps']
    call_ids = [c['tool_call_id'] for s in steps for c in (s.get('tool_calls') or [])]
    # All 20 folded calls plus the 2 post-compaction calls are present.
    assert call_ids == [f'old{i}' for i in range(20)] + ['new0', 'new1']
    # Every call still has its own observation content.
    results = [r['content'] for s in steps for r in (s.get('observation') or {}).get('results', [])]
    assert 'content of f0' in results and 'content of f19' in results
    assert 'content of g1' in results
    assert len(results) == 22
    assert task == before


def test_the_compaction_point_is_marked():
    doc = flatten(_compacted_task(), 'atif')['trajectory']
    markers = [s for s in doc['steps']
               if s.get('source') == 'system' and 'compaction' in (s.get('message') or '')]
    assert markers, 'expected a system step marking the fold'
    assert 'window' in markers[0]['message']


def test_a_run_without_compaction_is_unchanged():
    task = _compacted_task()
    task.pop('_compaction_archives')
    steps = flatten(task, 'atif')['trajectory']['steps']
    call_ids = [c['tool_call_id'] for s in steps for c in (s.get('tool_calls') or [])]
    assert call_ids == ['new0', 'new1']


def test_multiple_compactions_are_restored_in_order():
    task = _compacted_task()
    first = task['_compaction_archives'][0]['messages']
    second = []
    for i in range(5):
        second.extend(_round(f'mid{i}', f'm{i}.py', f'content of m{i}'))
    task['_compaction_archives'].append({
        'messages': second, 'trigger': 'reactive', 'round_num': 25,
        'msgs_before': len(second), 'msgs_after': 5,
    })
    steps = flatten(task, 'atif')['trajectory']['steps']
    call_ids = [c['tool_call_id'] for s in steps for c in (s.get('tool_calls') or [])]
    assert call_ids == ([f'old{i}' for i in range(20)]
                        + [f'mid{i}' for i in range(5)] + ['new0', 'new1'])


def test_retained_tail_and_preamble_are_exported_once():
    """Compaction archives the FULL pre-compaction transcript, and the next view
    repeats the system prompt, the task and the retained tail (TB2.1 smoke,
    make-mips-interpreter: 8 calls and 4 preamble steps exported twice)."""
    preamble = [{'role': 'system', 'content': 'system prompt'},
                {'role': 'user', 'content': 'the task'}]
    rounds = []
    for i in range(6):
        rounds.extend(_round(f'c{i}', f'f{i}.py', f'content of f{i}'))
    archived = preamble + rounds
    # The working view keeps the last two rounds; L1 clearing may already have
    # replaced a retained receipt, which must not make it look new.
    tail = [dict(m) for m in rounds[-4:]]
    tail[1]['content'] = '[cleared]'
    working = (preamble + [{'role': 'user', 'content': '<compaction_handoff> summary'}] + tail
               + _round('n0', 'g0.py', 'content of g0') + [{'role': 'assistant', 'content': 'final'}])
    task = {'id': 't', 'status': 'done', 'messages': working, '_messages_include_current_run': True,
            'content': 'final', '_compaction_archives': [{'messages': archived, 'trigger': 'window',
                                                           'round_num': 6}]}
    steps = flatten(task, 'atif')['trajectory']['steps']
    call_ids = [c['tool_call_id'] for s in steps for c in (s.get('tool_calls') or [])]
    assert call_ids == [f'c{i}' for i in range(6)] + ['n0']
    texts = [s.get('message') for s in steps if s.get('source') in ('system', 'user')]
    assert texts.count('system prompt') == 1 and texts.count('the task') == 1
    assert '<compaction_handoff> summary' in texts
    results = {r['source_call_id']: r['content'] for s in steps
               for r in (s.get('observation') or {}).get('results', [])}
    # The archived, uncleared receipt is the one exported.
    assert results['c4'] == 'content of f4'


def test_archive_recording_keeps_messages_on_the_task():
    """The fold must be recorded even if the archive row cannot be written."""
    from lib.tasks_pkg.compaction import _archive
    messages = [{'role': 'user', 'content': 'x'}]
    task = {'id': 't1', 'convId': 'c1', 'model': 'm'}

    class _Store:
        def archive_transcript(self, *args, **kwargs):
            return None  # archive write failed

        def prune_archives(self, *args, **kwargs):
            return None

    from lib.agent_core import store as store_mod
    original = store_mod.get_conversation_store
    store_mod.get_conversation_store = lambda: _Store()
    try:
        _archive._archive_transcript('c1', messages, task=task, user_id=1,
                                     emit_event=False)
    finally:
        store_mod.get_conversation_store = original
    records = task.get('_compaction_archives')
    assert records and records[0]['messages'] == messages
    # The recorded copy is not an alias of the caller's list.
    assert records[0]['messages'] is not messages
