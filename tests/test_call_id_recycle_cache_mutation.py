"""Regression: a recycled positional call id must never rewrite cached bytes.

conv msy8isz2qegcfo (2026-08-19, calls 10→15) logged EVERY round::

    [CacheTrack] ... PREFIX MUTATION DETECTED ... changed=[msg[34].content]
    [CacheTrack] ... WIRE PREFIX CHANGED ... changed=[tool_result(read_files_0).tool_result,
        tool_result(read_files_1).tool_result, ...] first_changed_idx=31
        inside_prior_cached_prefix=True

The changed set GREW each round, all of them ``tool_result`` contents for
read_files / grep_search / rep_search INSIDE the previously-cached prefix, and
``read_files_0`` appeared MORE THAN ONCE in one round's changed list — i.e.
two messages carried the same ``tool_call_id``. That is the signature of a
pass that re-binds results by tool NAME / call id across the WHOLE history.

Two defects combined into that loop:

1. ``execute_tool_pipeline``'s call_id CONFLICT recycler executed a fresh call
   under the SAME recycled model id (kimi-k3 mints ``{tool}_{index}``), so the
   conversation accumulated multiple ``tool_result(read_files_0)`` entries.
2. The per-round aggregate-budget apply-back loop iterated the WHOLE ``messages``
   list and re-bound ``msg['content']`` by ``tool_call_id`` — so the OLD
   already-cached result was overwritten with the NEW round's content every
   turn, directly re-billing the cached prefix.

The fix: the conflict lane re-mints a globally-unique id (and rewrites the wire
tool_call dict + round entry to keep the pair consistent), and the apply-back
loop only touches messages appended in the CURRENT round.
"""

import threading

import pytest

pytestmark = pytest.mark.unit


def _mk_task():
    return {
        'id': 'cache-mut-task',
        'convId': 'cv-cache-mut',
        '_userId': 1,
        'status': 'running',
        'aborted': False,
        'model': 'test-model',
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events': [],
        'events_lock': threading.Lock(),
        '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0,
        '_attended': False,
        'toolRounds': [],
    }


def _parsed_tc(tc_id, fn_name, args, seq):
    """A 7-tuple through the REAL round constructor (same as the settle suite)."""
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    _n, round_entry, _ev = _build_tool_round_entry(
        fn_name, args, tc_id, '{}', seq, False)
    tc = {'id': tc_id, 'type': 'function',
          'function': {'name': fn_name, 'arguments': '{}'}}
    return (tc, fn_name, tc_id, args, round_entry['roundNum'], round_entry, None)


def _fake_executor_factory(monkeypatch, result_text):
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    def fake(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
             cfg, project_path, project_enabled, all_tools=None):
        return tc_id, result_text, False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', fake, raising=False)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', fake, raising=False)
    return fake


def _run(task, parsed, messages):
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    execute_tool_pipeline(
        task, parsed, cfg={'autoApply': True}, project_path=None,
        project_enabled=False, tool_list=[], messages=messages,
        all_search_results_text=[], round_num=0, model='test-model')


def test_conflict_recycler_remints_globally_unique_id(monkeypatch):
    """A recycled id with DIFFERENT args gets a brand-new id — never reused.

    Pre-fix this executed under ``read_files_0`` again, so ``messages`` would
    hold two ``tool_result(read_files_0)`` entries and id-keyed writers could
    re-bind the old one.
    """
    from lib.tasks_pkg.tool_dispatch._flags import _call_id_signature

    task = _mk_task()
    old_args = {'path': 'old.txt'}
    # Simulate a prior round that already used read_files_0 with OLD args.
    task['_tool_call_id_receipts'] = {
        'read_files_0': {
            'signature': _call_id_signature('read_files', old_args),
            'name': 'read_files',
            'content': 'PRIOR RESULT',
            'status': 'done',
        },
    }
    _fake_executor_factory(monkeypatch, 'CURRENT RESULT')

    messages = [{'role': 'tool', 'tool_call_id': 'read_files_0',
                 'content': 'PRIOR RESULT'}]
    new_args = {'path': 'new.txt'}
    parsed = [_parsed_tc('read_files_0', 'read_files', new_args, 1)]

    _run(task, parsed, messages)

    assert messages[0]['content'] == 'PRIOR RESULT', (
        'the conflict lane (or a later id-keyed pass) rewrote the prior result')
    assert len(messages) == 2
    new_id = messages[1]['tool_call_id']
    assert new_id != 'read_files_0', (
        'recycled id was executed unchanged — the conversation now has a '
        'duplicate tool_call_id, re-opening the prefix-mutation loop')
    # The WIRE tool_call dict must match the new tool_result id (or the next
    # request hard-400s: tool_use id without a matching tool_result).
    assert parsed[0][0]['id'] == new_id
    assert parsed[0][5]['toolCallId'] == new_id
    ids = [m['tool_call_id'] for m in messages if m.get('tool_call_id')]
    assert len(ids) == len(set(ids)), (
        'the reminted id must be globally unique in the conversation')


def test_aggregate_budget_applyback_only_touches_current_round(monkeypatch):
    """The round-aggregate budget apply-back must not re-bind a PRIOR result.

    This isolates the second half even when a duplicate id reaches the
    pipeline through a path the remint lane did not intercept (no receipt, so
    no conflict → no remint). Pre-fix the ``for msg in messages`` loop
    overwrote the prior ``read_files_0`` content with the current round's.
    """
    task = _mk_task()
    _fake_executor_factory(monkeypatch, 'CURRENT RESULT')

    messages = [{'role': 'tool', 'tool_call_id': 'read_files_0',
                 'content': 'PRIOR RESULT'}]
    parsed = [_parsed_tc('read_files_0', 'read_files', {'path': 'x'}, 1)]

    _run(task, parsed, messages)

    assert messages[0]['content'] == 'PRIOR RESULT', (
        'aggregate-budget apply-back rewrote a prior (cached) tool_result in '
        'place — this is exactly the prefix-mutation re-bill loop')
    assert messages[-1]['tool_call_id'] == 'read_files_0'
    assert messages[-1]['content'] == 'CURRENT RESULT'
