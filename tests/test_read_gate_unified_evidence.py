"""Tests for the read-before-edit gate's UNIFIED evidence paths.

Two production-observed false-refusal classes (2026-07-25, conv mrymx02ceap8l5
refused 8× in 12 min despite repeated reads of the same file):

A. **Aliased historical args.** ``task['messages']`` persists tool_call
   arguments VERBATIM as the model emitted them; the dispatch-time repair
   layer (lib/tool_input_repair) renames wrong-harness keys
   (``file_path``→``path``, ``paths``→``reads``, Claude MultiEdit
   ``{file_path, edits:[{old_string, …}]}`` → canonical ``apply_diffs``)
   only in its execution copy. The gate's satisfied-set collector used to
   read only canonical keys, so a successful read/write performed under an
   aliased key was invisible → apply_diff refused "must read first" right
   after the model DID read.

B. **Compaction/restart amnesia.** The collector scans ``task['messages']``
   + ``task['toolRounds']`` — both ephemeral (compaction rewrites messages
   mid-run; persisted histories get compacted to zero tool_calls; a new task
   starts with empty toolRounds). The write-freshness token store is keyed
   (conv, path), survives both, and is only written after a SUCCESSFUL
   read/write. A NON-STALE token is strictly stronger evidence than the
   message scan and now also satisfies the gate; a STALE token never does
   (the write stays refused — fail-closed — and the FreshGate owns the
   precise "changed on disk" message).
"""
from __future__ import annotations

import json
import os

import pytest


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    """Isolate the process-global freshness store + force both gates on."""
    monkeypatch.delenv('TOFU_APPLY_DIFF_READ_GATE', raising=False)
    monkeypatch.delenv('TOFU_WRITE_FRESHNESS_GATE', raising=False)
    from lib import write_freshness
    write_freshness._reset_for_tests()
    yield
    write_freshness._reset_for_tests()


@pytest.fixture
def workspace(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir()
    target = proj / 'a.py'
    target.write_text('def foo():\n    return 1\n')
    return {'project_path': str(proj), 'target_rel': 'a.py',
            'target_abs': str(target)}


def _make_task(messages=None, tool_rounds=None, conv_id='c1', task_id='t1'):
    return {
        'id': task_id,
        'convId': conv_id,
        'messages': list(messages or []),
        'toolRounds': list(tool_rounds or []),
    }


def _history_with_call(name, args_dict, result_text, tc_id='tc_1'):
    """A prior-turn assistant tool_call + its tool result, as persisted
    (arguments VERBATIM as the model emitted them — the whole point)."""
    return [
        {
            'role': 'assistant',
            'tool_calls': [{
                'id': tc_id,
                'function': {
                    'name': name,
                    'arguments': json.dumps(args_dict),
                },
            }],
        },
        {'role': 'tool', 'tool_call_id': tc_id, 'content': result_text},
    ]


def _check(task, workspace, fn_name='apply_diff'):
    from lib.tasks_pkg.handlers._read_gate import check_read_before_edit
    return check_read_before_edit(
        task, fn_name,
        {'path': workspace['target_rel'], 'search': 'return 1', 'replace': 'return 2'},
        workspace['project_path'],
    )


# ── A. aliased / foreign-shape historical args ─────────────────────────

@pytest.mark.unit
def test_aliased_read_args_satisfy_gate(workspace):
    """read_files(file_path=…) — Claude-Code key — repaired at dispatch,
    executed fine, model SAW the file. The stored args keep the alias."""
    msgs = _history_with_call(
        'read_files', {'file_path': workspace['target_rel']},
        '=== a.py ===\n  1: def foo():\n  2:     return 1\n')
    assert _check(_make_task(messages=msgs), workspace) is None


@pytest.mark.unit
def test_aliased_reads_array_satisfy_gate(workspace):
    """read_files(paths=[…]) — array alias for the canonical ``reads``."""
    msgs = _history_with_call(
        'read_files', {'paths': [workspace['target_rel']]},
        '=== a.py ===\n  1: def foo():\n')
    assert _check(_make_task(messages=msgs), workspace) is None


@pytest.mark.unit
def test_multiedit_shape_prior_apply_diffs_satisfies(workspace):
    """A prior apply_diffs in Claude MultiEdit shape
    (``{file_path, edits:[{old_string, new_string}]}``) succeeded — its
    foreign shape must still mark the file as seen."""
    msgs = _history_with_call(
        'apply_diffs',
        {'file_path': workspace['target_rel'],
         'edits': [{'old_string': 'return 0', 'new_string': 'return 1'}]},
        'Applied 1/1 edits\n[0] OK a.py: 1 lines changed (2L → 2L)')
    assert _check(_make_task(messages=msgs), workspace) is None


@pytest.mark.unit
def test_claude_write_shape_satisfies(workspace):
    """write_file(file_path=…, file_text=…) — Claude Write keys."""
    msgs = _history_with_call(
        'write_file',
        {'file_path': workspace['target_rel'], 'file_text': 'x = 1\n'},
        'File created: a.py')
    assert _check(_make_task(messages=msgs), workspace) is None


@pytest.mark.unit
def test_aliased_insert_content_satisfies(workspace):
    msgs = _history_with_call(
        'insert_content',
        {'file_path': workspace['target_rel'], 'anchor': 'def foo',
         'text': 'pass\n'},
        'Inserted content after anchor in a.py')
    assert _check(_make_task(messages=msgs), workspace) is None


# ── B. freshness-token coverage (compaction / restart amnesia) ─────────

@pytest.mark.unit
def test_fresh_token_satisfies_post_compaction(workspace):
    """The money case: messages compacted away (empty list), no rounds —
    but the freshness store proves this conv read the file and it is
    byte-unchanged since. Gate must allow."""
    from lib import write_freshness
    write_freshness.record('c1', workspace['target_abs'])
    task = _make_task()  # no messages, no rounds — post-compaction shape
    assert _check(task, workspace) is None


@pytest.mark.unit
def test_stale_token_does_NOT_satisfy(workspace):
    """Fail-closed: the token exists but the file CHANGED since (a sibling
    wrote it) — the write must still be refused (re-read first)."""
    from lib import write_freshness
    write_freshness.record('c1', workspace['target_abs'])
    with open(workspace['target_abs'], 'a', encoding='utf-8') as f:
        f.write('# sibling was here\n')
    err = _check(_make_task(), workspace)
    assert err is not None
    assert workspace['target_rel'] in err


@pytest.mark.unit
def test_other_conv_token_does_not_satisfy(workspace):
    """Tokens are per-conversation: convB's read says nothing about convA."""
    from lib import write_freshness
    write_freshness.record('convB', workspace['target_abs'])
    err = _check(_make_task(conv_id='c1'), workspace)
    assert err is not None


@pytest.mark.unit
def test_no_token_no_messages_still_refuses(workspace):
    """Regression pin: genuinely-unread file is still refused."""
    err = _check(_make_task(), workspace)
    assert err is not None
    assert 'must read each target file first' in err


@pytest.mark.unit
def test_subtask_falls_back_to_task_id_namespace(workspace):
    """A sub-task with convId='' (autopilot VU) uses its task id as the
    token namespace — same discipline as the FreshGate's _conv_key."""
    from lib import write_freshness
    write_freshness.record('t-vu-1', workspace['target_abs'])
    task = _make_task(conv_id='', task_id='t-vu-1')
    assert _check(task, workspace) is None


@pytest.mark.unit
def test_batch_partition_skips_only_uncovered_edit(workspace):
    """apply_diffs over [token-covered a.py, uncovered b.py] → skip b only."""
    from lib import write_freshness
    from lib.tasks_pkg.handlers._read_gate import partition_batch_edits
    other = os.path.join(workspace['project_path'], 'b.py')
    with open(other, 'w') as f:
        f.write('def bar():\n    return 2\n')
    write_freshness.record('c1', workspace['target_abs'])
    skip, unread = partition_batch_edits(
        _make_task(), 'apply_diffs',
        {'edits': [
            {'path': workspace['target_rel'], 'search': 'return 1', 'replace': 'return 9'},
            {'path': 'b.py', 'search': 'return 2', 'replace': 'return 8'},
        ]},
        workspace['project_path'])
    assert skip == [1]
    assert unread == ['b.py']


# ── has_token unit ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_has_token_lifecycle(workspace):
    from lib import write_freshness
    assert write_freshness.has_token('c1', workspace['target_abs']) is False
    write_freshness.record('c1', workspace['target_abs'])
    assert write_freshness.has_token('c1', workspace['target_abs']) is True
    write_freshness.drop('c1', workspace['target_abs'])
    assert write_freshness.has_token('c1', workspace['target_abs']) is False
    # Never raises on empty keys.
    assert write_freshness.has_token('', workspace['target_abs']) is False


# ── NEUTER proofs ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_neuter_alias_normalization_breaks_alias_evidence(workspace, monkeypatch):
    """Amputate the normalization helper → the aliased-read test's allow
    disappears (proving the allow flows through _normalize_historical_args,
    not some incidental state)."""
    import lib.tasks_pkg.handlers._read_gate as rg
    msgs = _history_with_call(
        'read_files', {'file_path': workspace['target_rel']},
        '=== a.py ===\n  1: def foo():\n')
    # Sanity: intact helper allows.
    assert _check(_make_task(messages=msgs), workspace) is None
    monkeypatch.setattr(rg, '_normalize_historical_args', lambda n, a: a)
    assert _check(_make_task(messages=msgs), workspace) is not None


@pytest.mark.unit
def test_neuter_staleness_check_breaks_token_evidence(workspace, monkeypatch):
    """Force every token stale → the token-covered allow disappears
    (proving the allow requires a NON-STALE token, not mere existence)."""
    from lib import write_freshness
    write_freshness.record('c1', workspace['target_abs'])
    assert _check(_make_task(), workspace) is None  # intact
    monkeypatch.setattr(write_freshness, 'is_stale', lambda k, p: True)
    assert _check(_make_task(), workspace) is not None
