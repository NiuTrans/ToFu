"""Incident regression: conv mswu06rpir1hwv (2026-08-17, model kimi-k3).

kimi-k3 mints POSITIONAL call ids — ``{tool}_{index-in-message}`` — so every
first call of every assistant message is ``search_tools_0`` again. The old
cross-round receipt guard rejected each reuse with different args ("call_id
was already used ... Mint a new call_id") — an instruction a positional-id
model CANNOT follow. The model reverse-engineered a bogus "first call per
message is rejected" rule and began prepending sacrificial
``search_tools(query="noop ping placeholder")`` calls — 14 llmRounds of pure
token burn until the turn died.

The pin: same id + same args -> replay; same id + different args -> EXECUTE
(and the model must receive the FRESH result, never the stale one).
"""
import json
import threading

import pytest

pytestmark = pytest.mark.unit


def test_positional_call_id_model_is_never_locked_out(monkeypatch):
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    executions = []

    def fake_execute(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        executions.append((fn_name, dict(fn_args)))
        return tc_id, 'ran:%s' % fn_args.get('q'), False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', fake_execute)

    task = {
        'id': 'kimi-task', 'convId': 'mswu06rpir1hwv', 'status': 'running',
        '_userId': 1,
        'aborted': False, 'model': 'kimi-k3', 'events': [],
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events_lock': threading.Lock(), '_attended': False,
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0,
        'toolRounds': [],
    }

    seq = [0]

    def kimi_call(query):
        seq[0] += 1
        args = {'q': query}
        _, row, _ = _build_tool_round_entry(
            'side_effect_tool', args, 'search_tools_0',
            '{"q":%r}' % query, seq[0], False)
        task['toolRounds'].append(row)
        tc = {'id': 'search_tools_0', 'type': 'function',
              'function': {'name': 'side_effect_tool',
                           'arguments': '{"q":%r}' % query}}
        return (tc, 'side_effect_tool', 'search_tools_0', args,
                row['roundNum'], row, None)

    contents = []
    for q in ('first', 'noop ping placeholder', 'second', 'third'):
        messages = []
        execute_tool_pipeline(
            task, [kimi_call(q)], cfg={'autoApply': True},
            project_path=None, project_enabled=False, tool_list=[],
            messages=messages, all_search_results_text=[], round_num=0,
            model='kimi-k3')
        contents.append(messages[-1]['content'])

    # Every distinct call executed — none rejected, none served stale content.
    assert [args for _, args in executions] == [
        {'q': 'first'}, {'q': 'noop ping placeholder'}, {'q': 'second'},
        {'q': 'third'}]
    assert contents == ['ran:first', 'ran:noop ping placeholder',
                        'ran:second', 'ran:third']
    assert not any('already used' in c for c in contents)


def test_exact_reemit_after_state_change_returns_fresh_result(monkeypatch):
    """Incident regression: tasks f8149620 / 0c2e3a92 / d03690ec (2026-08-19,
    model kimi-k3) — the "results never change" loop.

    The cross-round call-id receipt layer replayed an EXACT re-emit (same
    recycled positional id + same args) from the stored receipt WITHOUT
    executing:

      * edit_file re-apply → reported the previous success, never ran (the
        model believed the filesystem had changed when it had not);
      * read_files re-read → returned pre-edit bytes (receipts bypass the
        dedup lane's FreshGate and are never write-invalidated);
      * run_command re-run → replayed stale output.

    The model looped: edit "succeeds" → re-read shows the old content →
    re-edit → replayed again. The pin: a completed call id is only a
    reuse DETECTOR — every re-emit remints a fresh id and EXECUTES; a
    re-read after a write sees the new bytes (write-invalidation) and a
    re-applied edit runs for real.
    """
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    fs = {'f': 'v1'}  # the "disk"
    executions = []

    def fake_execute(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        executions.append((fn_name, dict(fn_args)))
        if fn_name == 'read_files':
            return tc_id, 'file:%s' % fs['f'], False
        if fn_name == 'edit_file':
            fs['f'] = fn_args.get('content', fs['f'])
            return tc_id, 'edited:%s' % fs['f'], False
        return tc_id, 'ran', False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', fake_execute)

    task = {
        'id': 'stale-loop-task', 'convId': 'stale-loop-conv',
        '_userId': 1,
        'status': 'running', 'aborted': False, 'model': 'kimi-k3',
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events': [], 'events_lock': threading.Lock(), '_attended': False,
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0,
        'toolRounds': [],
    }

    seq = [0]

    def kimi_call(fn_name, args):
        seq[0] += 1
        tc_id = '%s_0' % fn_name  # kimi-k3 positional id: recycled every round
        _, row, _ = _build_tool_round_entry(
            fn_name, args, tc_id, json.dumps(args), seq[0], False)
        task['toolRounds'].append(row)
        tc = {'id': tc_id, 'type': 'function',
              'function': {'name': fn_name, 'arguments': json.dumps(args)}}
        return (tc, fn_name, tc_id, args, row['roundNum'], row, None)

    def run(item):
        messages = []
        execute_tool_pipeline(
            task, [item], cfg={'autoApply': True}, project_path=None,
            project_enabled=False, tool_list=[], messages=messages,
            all_search_results_text=[], round_num=0, model='kimi-k3')
        return messages[-1]['content'], item[5]

    read_args = {'path': 'f'}
    edit_args = {'path': 'f', 'content': 'v2'}

    # Round 1: read the file (v1).
    content, _ = run(kimi_call('read_files', dict(read_args)))
    assert content == 'file:v1'
    # Round 2: edit the file — executes, disk becomes v2.
    content, _ = run(kimi_call('edit_file', dict(edit_args)))
    assert content == 'edited:v2'
    # Round 3: EXACT re-emit of the round-1 read. Pre-fix this replayed the
    # receipt ('file:v1') without reading; it must re-read and see v2.
    content, read_row = run(kimi_call('read_files', dict(read_args)))
    assert content == 'file:v2'
    assert not read_row.get('_idempotentReplay')
    # Round 4: EXACT re-emit of the edit. Pre-fix this reported the round-2
    # success WITHOUT executing; it must run again (idempotent fake → same
    # text; the behavioural pin is the fourth execution).
    content, edit_row = run(kimi_call('edit_file', dict(edit_args)))
    assert content == 'edited:v2'
    assert not edit_row.get('_idempotentReplay')

    assert [(name, args.get('path')) for name, args in executions] == [
        ('read_files', 'f'), ('edit_file', 'f'),
        ('read_files', 'f'), ('edit_file', 'f')]
    # Every re-issued call ran under a reminted id — the wire never carries
    # two tool_call/tool_result pairs sharing one id across rounds.
    assert read_row['toolCallId'] != 'read_files_0'
    assert edit_row['toolCallId'] != 'edit_file_0'
