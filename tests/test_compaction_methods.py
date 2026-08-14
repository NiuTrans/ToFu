"""Tests for experimental compaction methods M1 (latest_state_dedup) and
M2 (fold_observations) in lib/tasks_pkg/compaction/_methods.py.

Run:  pytest tests/test_compaction_methods.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Parser/compaction fixture values, not anchors to shipped repository files.
_AUDIT_SYNTHETIC_REPO_PATHS = {
    'lib/a.py', 'lib/b.py', 'lib/c.py', 'lib/other.py', 'lib/server.py',
}


def _read_result(path: str, body_chars: int = 3000) -> str:
    return f'File: {path} (320 lines, 12.1KB)\n' + ('x' * body_chars)


def _mk_read_msg(tcid: str, path: str, body_chars: int = 3000) -> list:
    return [
        {'role': 'assistant', 'content': f'reading {path}',
         'tool_calls': [{'id': tcid,
                         'function': {'name': 'read_files', 'arguments': '{}'}}]},
        {'role': 'tool', 'name': 'read_files', 'tool_call_id': tcid,
         'content': _read_result(path, body_chars)},
    ]


def _mk_grep_msg(tcid: str, n_matches: int = 5) -> list:
    body = '\n'.join(f'lib/foo.py:{i}: def thing_{i}()' for i in range(n_matches))
    return [
        {'role': 'assistant', 'content': 'searching',
         'tool_calls': [{'id': tcid,
                         'function': {'name': 'grep_search', 'arguments': '{}'}}]},
        {'role': 'tool', 'name': 'grep_search', 'tool_call_id': tcid,
         'content': body + '\n' + ('pad ' * 800)},  # exceed threshold
    ]


# ── M1 ──────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_m1_registered():
    from lib.tasks_pkg.compaction import list_steps
    assert 'latest_state_dedup' in list_steps()
    assert 'fold_observations' in list_steps()


@pytest.mark.unit
def test_m1_supersedes_stale_reads_keeps_latest():
    from lib.tasks_pkg.compaction import micro_compact

    msgs = [{'role': 'user', 'content': 'go'}]
    # Pad with 60 unrelated tool results so the file reads fall outside the
    # hot tail and become cold.
    for i in range(60):
        msgs.append({'role': 'assistant', 'content': f'p{i}',
                     'tool_calls': [{'id': f'p{i}',
                                     'function': {'name': 'noop', 'arguments': '{}'}}]})
        msgs.append({'role': 'tool', 'name': 'noop', 'tool_call_id': f'p{i}',
                     'content': 'short'})
    # Three reads of the SAME file, then one of a different file.
    msgs += _mk_read_msg('r1', 'lib/server.py')
    msgs += _mk_read_msg('r2', 'lib/server.py')
    msgs += _mk_read_msg('r3', 'lib/server.py')
    msgs += _mk_read_msg('r4', 'lib/other.py')

    micro_compact(msgs, conv_id='',
                  steps=['latest_state_dedup'],
                  constant_overrides={'MICRO_HOT_TAIL': 2})

    def content_of(tcid):
        for m in msgs:
            if m.get('tool_call_id') == tcid:
                return m['content']
        return None

    # r1, r2 superseded; r3 (latest server.py) and r4 kept verbatim.
    assert 'superseded' in content_of('r1')
    assert 'superseded' in content_of('r2')
    assert 'superseded' not in content_of('r3'), 'latest read must stay verbatim'
    assert content_of('r3').startswith('File: lib/server.py')
    # r4 is within hot-tail(2) so untouched anyway, and a distinct path.
    assert 'superseded' not in content_of('r4')


@pytest.mark.unit
def test_m1_does_not_supersede_distinct_paths():
    from lib.tasks_pkg.compaction import micro_compact

    msgs = [{'role': 'user', 'content': 'go'}]
    for i in range(60):
        msgs.append({'role': 'assistant', 'content': f'p{i}',
                     'tool_calls': [{'id': f'p{i}',
                                     'function': {'name': 'noop', 'arguments': '{}'}}]})
        msgs.append({'role': 'tool', 'name': 'noop', 'tool_call_id': f'p{i}',
                     'content': 'short'})
    msgs += _mk_read_msg('a', 'lib/a.py')
    msgs += _mk_read_msg('b', 'lib/b.py')
    msgs += _mk_read_msg('c', 'lib/c.py')

    micro_compact(msgs, conv_id='',
                  steps=['latest_state_dedup'],
                  constant_overrides={'MICRO_HOT_TAIL': 1})

    superseded = sum(1 for m in msgs if isinstance(m.get('content'), str)
                     and 'superseded' in m['content'])
    assert superseded == 0, 'distinct file paths must never be superseded'


# ── M2 ──────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_m2_folds_grep_to_structured_fact():
    from lib.tasks_pkg.compaction import micro_compact

    msgs = [{'role': 'user', 'content': 'go'}]
    msgs += _mk_grep_msg('g1', n_matches=7)  # oldest → cold
    for i in range(60):
        msgs.append({'role': 'assistant', 'content': f'p{i}',
                     'tool_calls': [{'id': f'p{i}',
                                     'function': {'name': 'noop', 'arguments': '{}'}}]})
        msgs.append({'role': 'tool', 'name': 'noop', 'tool_call_id': f'p{i}',
                     'content': 'short'})

    micro_compact(msgs, conv_id='',
                  steps=['fold_observations'],
                  constant_overrides={'MICRO_HOT_TAIL': 1})

    g = next(m['content'] for m in msgs if m.get('tool_call_id') == 'g1')
    assert g.startswith('[grep_search folded'), g
    assert '7 match line(s)' in g, g
    assert 'first:' in g


@pytest.mark.unit
def test_m2_respects_hot_tail():
    from lib.tasks_pkg.compaction import micro_compact

    # Only one grep result, within the default hot tail → untouched.
    msgs = [{'role': 'user', 'content': 'go'}] + _mk_grep_msg('g1', n_matches=5)
    micro_compact(msgs, conv_id='', steps=['fold_observations'])
    g = next(m['content'] for m in msgs if m.get('tool_call_id') == 'g1')
    assert not g.startswith('[grep_search folded'), 'hot-tail result must survive'


@pytest.mark.unit
def test_m1_m2_are_llm_free():
    """Guard the zero-cost invariant for the new methods too."""
    import inspect
    import lib.tasks_pkg.compaction._methods as m
    src = inspect.getsource(m)
    assert 'dispatch_chat' not in src
    assert 'dispatch_stream' not in src
