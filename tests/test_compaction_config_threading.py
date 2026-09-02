"""Stage-3 config-threading tests for compaction strategy selection.

Verifies that ``task['config']['compaction']`` selects a non-default L1
strategy purely as data — no global mutation — so experiment arms can run
concurrently.  Covers:

  * absent config  → byte-identical to the default step list
  * ``steps=[...]`` → exactly those steps run (ablation)
  * ``ignore_cache_prefix`` → steps compact inside the cache prefix
  * ``constant_overrides`` → per-call tunable overlay, NOT a global write
  * concurrency: an override on one call does not leak to another

Run:  pytest tests/test_compaction_config_threading.py -v
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _big(n: int, ch: str = 'x') -> str:
    return ch * n


def _mk_conv(n_tool: int = 41, tool_chars: int = 3000):
    msgs = [{'role': 'user', 'content': 'go'}]
    for i in range(n_tool):
        msgs.append({'role': 'assistant', 'content': f's{i}',
                     'tool_calls': [{'id': f't{i}',
                                     'function': {'name': 'grep_search',
                                                  'arguments': '{}'}}]})
        msgs.append({'role': 'tool', 'name': 'grep_search',
                     'tool_call_id': f't{i}', 'content': _big(tool_chars)})
    return msgs


def _count_compacted_tools(msgs) -> int:
    return sum(1 for m in msgs if m.get('role') == 'tool'
               and isinstance(m.get('content'), str)
               and 'compacted' in m['content'][:80])


# ── 1. Absent config == default behavior ───────────────────────────────

@pytest.mark.unit
def test_pipeline_absent_config_matches_default():
    from lib.tasks_pkg.compaction.api import micro_compact

    base = _mk_conv()
    direct = copy.deepcopy(base)
    via_default = copy.deepcopy(base)

    saved_direct = micro_compact(direct, conv_id='')
    saved_default = micro_compact(via_default, conv_id='', steps=None)

    assert saved_direct == saved_default
    assert _count_compacted_tools(direct) == _count_compacted_tools(via_default)


# ── 2. Explicit steps list = ablation ───────────────────────────────────

@pytest.mark.unit
def test_explicit_steps_ablation():
    from lib.tasks_pkg.compaction.api import micro_compact

    msgs = _mk_conv()
    # Run ONLY strip_thinking — no tool compaction should happen.
    micro_compact(msgs, conv_id='', steps=['strip_thinking'])
    assert _count_compacted_tools(msgs) == 0, (
        'compact_tool_results should NOT have run when excluded from steps')

    # Now run only compact_tool_results — tools compact.
    msgs2 = _mk_conv()
    micro_compact(msgs2, conv_id='', steps=['compact_tool_results'])
    assert _count_compacted_tools(msgs2) == 1


# ── 3. ignore_cache_prefix lets steps compact inside the prefix ─────────

@pytest.mark.unit
def test_ignore_cache_prefix(monkeypatch):
    import lib.tasks_pkg.compaction._layer1 as _layer1
    from lib.tasks_pkg.compaction.api import micro_compact

    # Force a large cache prefix so the default (skip) arm protects most
    # cold tool results and compacts few; the aggressive arm compacts more.
    import lib.tasks_pkg.cache_tracking._prefix as ct
    # Stub mirrors the real signature: get_cache_prefix_count now takes an
    # optional current_msg_count (the history-shrink clamp). Return a fixed
    # large prefix regardless so the skip-arm protects most cold results.
    monkeypatch.setattr(ct, 'get_cache_prefix_count',
                        lambda _cid, user_id=None,
                        current_msg_count=None: 100)

    skip_msgs = _mk_conv()
    aggr_msgs = _mk_conv()

    micro_compact(skip_msgs, conv_id='c-skip')
    micro_compact(aggr_msgs, conv_id='c-aggr', ignore_cache_prefix=True)

    skip_n = _count_compacted_tools(skip_msgs)
    aggr_n = _count_compacted_tools(aggr_msgs)
    assert aggr_n > skip_n, (
        f'aggressive ({aggr_n}) should compact more than skip ({skip_n}) '
        f'when a large cache prefix is present')


# ── 4. constant_overrides applies per-call WITHOUT global mutation ──────

@pytest.mark.unit
def test_constant_overrides_no_global_leak():
    import lib.tasks_pkg.compaction._constants as comp
    from lib.tasks_pkg.compaction.api import micro_compact

    orig_hot_tail = comp.MICRO_HOT_TAIL

    # With a tiny hot tail, far more tool results become cold → compacted.
    msgs = _mk_conv(n_tool=10, tool_chars=3000)
    micro_compact(msgs, conv_id='',
                  constant_overrides={'MICRO_HOT_TAIL': 2})
    overridden_n = _count_compacted_tools(msgs)

    # The global MUST be untouched (concurrency safety).
    assert comp.MICRO_HOT_TAIL == orig_hot_tail, (
        'constant_overrides leaked into the global package namespace')

    # Sanity: with default hot tail (40) and only 10 tools, nothing is cold.
    msgs_default = _mk_conv(n_tool=10, tool_chars=3000)
    micro_compact(msgs_default, conv_id='')
    assert _count_compacted_tools(msgs_default) == 0
    assert overridden_n > 0, 'override should have made some tools cold'


# ── 5. Pipeline reads task['config']['compaction'] ─────────────────────

@pytest.mark.unit
def test_pipeline_reads_compaction_config():
    from lib.tasks_pkg.compaction.api import run_compaction_pipeline

    msgs = _mk_conv()
    task = {'convId': '', '_userId': 1, 'config': {'model': 'gpt-4',
                                     'compaction': {'steps': ['strip_thinking']}}}
    run_compaction_pipeline(msgs, current_round=5, task=task)
    # strip_thinking only → no tool compaction via the config-selected arm.
    assert _count_compacted_tools(msgs) == 0


def _mk_thinking_conv(n: int):
    """Cold assistant turns each carrying reasoning_content + a tool call."""
    msgs = [{'role': 'user', 'content': 'go'}]
    for i in range(n):
        msgs.append({'role': 'assistant', 'content': f's{i}',
                     'reasoning_content': 'R' * 500,
                     'tool_calls': [{'id': f't{i}',
                                     'function': {'name': 'g', 'arguments': '{}'}}]})
        msgs.append({'role': 'tool', 'name': 'g',
                     'tool_call_id': f't{i}', 'content': 'x' * 100})
    return msgs


def _count_blanked_reasoning(msgs) -> int:
    return sum(1 for m in msgs if m.get('role') == 'assistant'
               and m.get('reasoning_content') == '')


# ── 6. DeepSeek thinking mode: reasoning_content must NOT be stripped ───

@pytest.mark.unit
def test_strip_thinking_skips_deepseek():
    """DeepSeek V4 thinking mode rejects an assistant turn whose
    reasoning_content was emptied (HTTP 400). strip_thinking must skip it,
    while still stripping for OpenAI-compatible models."""
    import lib.tasks_pkg.compaction._constants as comp
    from lib.tasks_pkg.compaction.api import micro_compact

    n = comp._THINKING_HOT_TAIL + 5  # ensure some cold thinking exists

    ds = _mk_thinking_conv(n)
    micro_compact(ds, conv_id='',
                  task={'model': 'deepseek-v4-flash', '_userId': 1})
    assert _count_blanked_reasoning(ds) == 0, (
        'DeepSeek reasoning_content must be preserved for thinking replay')

    gpt = _mk_thinking_conv(n)
    micro_compact(gpt, conv_id='', task={'model': 'gpt-4', '_userId': 1})
    assert _count_blanked_reasoning(gpt) > 0, (
        'non-DeepSeek models should still strip cold reasoning_content')


@pytest.mark.unit
def test_disable_default_l1_and_force_compact_flags():
    """REPLACEMENT-mode arms: disableDefaultL1 skips the built-in L1 pass,
    disableForceCompact skips chatui L2 — so an external method runs alone."""
    from lib.tasks_pkg.compaction.api import run_compaction_pipeline

    # disableDefaultL1 with NO steps → no tool compaction happens at all.
    msgs = _mk_conv()
    task = {'convId': '', '_userId': 1, 'config': {'model': 'gpt-4',
            'compaction': {'disableDefaultL1': True, 'disableForceCompact': True}}}
    run_compaction_pipeline(msgs, current_round=5, task=task)
    assert _count_compacted_tools(msgs) == 0, 'disableDefaultL1 must skip L1'

    # disableDefaultL1 but WITH explicit steps → the arm's own steps still run.
    msgs2 = _mk_conv()
    task2 = {'convId': '', '_userId': 1, 'config': {'model': 'gpt-4',
             'compaction': {'disableDefaultL1': True, 'disableForceCompact': True,
                            'steps': ['compact_tool_results']}}}
    run_compaction_pipeline(msgs2, current_round=5, task=task2)
    assert _count_compacted_tools(msgs2) == 1, "arm's own steps must still run"


@pytest.mark.unit
def test_make_constants_identity_when_empty():
    """make_constants preserves the constants owner when no overlay exists."""
    import lib.tasks_pkg.compaction._constants as comp
    from lib.tasks_pkg.compaction._steps import make_constants

    assert make_constants(comp, None) is comp
    assert make_constants(comp, {}) is comp
    view = make_constants(comp, {'MICRO_HOT_TAIL': 5})
    assert view is not comp
    assert view.MICRO_HOT_TAIL == 5
    # Non-overridden attrs fall through to the constants owner.
    assert view.MICRO_COMPACT_THRESHOLD == comp.MICRO_COMPACT_THRESHOLD
