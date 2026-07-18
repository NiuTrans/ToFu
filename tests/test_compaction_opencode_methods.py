"""Tests for OpenCode-inspired compaction steps in _methods.py:
prune_with_hysteresis and adaptive_hot_tail.

Run:  pytest tests/test_compaction_opencode_methods.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mk_tools(n, chars):
    msgs = [{'role': 'user', 'content': 'go'}]
    for i in range(n):
        msgs.append({'role': 'assistant', 'content': None,
                     'tool_calls': [{'id': f't{i}',
                                     'function': {'name': 'read_files', 'arguments': '{}'}}]})
        msgs.append({'role': 'tool', 'name': 'read_files', 'tool_call_id': f't{i}',
                     'content': 'x' * chars})
    return msgs


def _n_compacted(msgs, marker):
    return sum(1 for m in msgs if m.get('role') == 'tool'
               and isinstance(m.get('content'), str)
               and marker in m['content'][:80])


@pytest.mark.unit
def test_registered():
    from lib.tasks_pkg.compaction import list_steps
    assert 'prune_with_hysteresis' in list_steps()
    assert 'adaptive_hot_tail' in list_steps()


# ── prune_with_hysteresis ───────────────────────────────────────────────

@pytest.mark.unit
def test_prune_skips_below_minimum():
    """When reclaimable tokens are below PRUNE_MINIMUM, nothing is pruned."""
    from lib.tasks_pkg.compaction import micro_compact
    # Small protected tail + tiny reclaimable → below minimum.
    msgs = _mk_tools(3, 12_000)  # ~3k tokens each
    micro_compact(msgs, conv_id='', steps=['prune_with_hysteresis'],
                  constant_overrides={'PRUNE_PROTECT_TOKENS': 12_000,
                                      'PRUNE_MINIMUM_TOKENS': 50_000})
    assert _n_compacted(msgs, 'pruned') == 0


@pytest.mark.unit
def test_prune_acts_above_minimum_and_protects_tail():
    from lib.tasks_pkg.compaction import micro_compact
    # 10 results × ~3k tokens = 30k total. Protect 6k tail (~2 results),
    # minimum 4k → the other ~8 are pruned.
    msgs = _mk_tools(10, 12_000)
    micro_compact(msgs, conv_id='', steps=['prune_with_hysteresis'],
                  constant_overrides={'PRUNE_PROTECT_TOKENS': 6_000,
                                      'PRUNE_MINIMUM_TOKENS': 4_000})
    pruned = _n_compacted(msgs, 'pruned')
    assert pruned >= 6, f'expected most old results pruned, got {pruned}'
    # The very last tool result (newest) must be protected.
    last_tool = [m for m in msgs if m.get('role') == 'tool'][-1]
    assert 'pruned' not in last_tool['content'][:80], 'tail must be protected'


# ── adaptive_hot_tail ───────────────────────────────────────────────────

@pytest.mark.unit
def test_adaptive_hot_tail_compacts_outside_budget():
    from lib.tasks_pkg.compaction import micro_compact
    # 12 results × ~3k tokens. Budget 9k ≈ keep ~3 newest, compact the rest.
    msgs = _mk_tools(12, 12_000)
    micro_compact(msgs, conv_id='', steps=['adaptive_hot_tail'],
                  constant_overrides={'ADAPTIVE_TAIL_BUDGET': 9_000})
    compacted = _n_compacted(msgs, 'compacted')
    assert compacted >= 7, f'expected old results compacted, got {compacted}'
    last_tool = [m for m in msgs if m.get('role') == 'tool'][-1]
    assert 'compacted' not in last_tool['content'][:80], 'hot tail preserved'


@pytest.mark.unit
def test_adaptive_hot_tail_respects_cache_prefix(monkeypatch):
    from lib.tasks_pkg.compaction import micro_compact
    import lib.tasks_pkg.cache_tracking as ct
    monkeypatch.setattr(ct, 'get_cache_prefix_count',
                        lambda _cid, current_msg_count=None: 100)
    msgs = _mk_tools(12, 12_000)
    # Large cache prefix → most are protected; aggressive lifts it.
    micro_compact(msgs, conv_id='c1', steps=['adaptive_hot_tail'],
                  constant_overrides={'ADAPTIVE_TAIL_BUDGET': 9_000})
    skip_n = _n_compacted(msgs, 'compacted')

    msgs2 = _mk_tools(12, 12_000)
    micro_compact(msgs2, conv_id='c2', steps=['adaptive_hot_tail'],
                  ignore_cache_prefix=True,
                  constant_overrides={'ADAPTIVE_TAIL_BUDGET': 9_000})
    aggr_n = _n_compacted(msgs2, 'compacted')
    assert aggr_n > skip_n


@pytest.mark.unit
def test_opencode_methods_llm_free():
    import inspect
    import lib.tasks_pkg.compaction._methods as m
    src = inspect.getsource(m.prune_with_hysteresis) + inspect.getsource(m.adaptive_hot_tail)
    assert 'dispatch_chat' not in src
    assert 'summarize' not in src
