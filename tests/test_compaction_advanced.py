"""Tests for the generalized multi-form compaction framework (Stage B).

Covers:
  * registration with kind / needs
  * run_steps capability gating (transform host rejects structural/LLM)
  * MessageEditor turn detection + protections (in-flight, cache prefix)
  * structural example: drop_superseded_turns (pure-tool turns dropped)
  * LLM example: summarize_oldest_turn with a stubbed ctx.summarize
  * advanced host wiring through task['config']['compaction']['advanced_steps']
  * default off: no advanced_steps ⇒ pipeline unchanged

Run:  pytest tests/test_compaction_advanced.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── registration + gating ───────────────────────────────────────────────

@pytest.mark.unit
def test_kinds_and_needs_registered():
    from lib.tasks_pkg.compaction.api import get_step_spec, list_steps
    assert 'drop_superseded_turns' in list_steps()
    assert 'summarize_oldest_turn' in list_steps()

    spec_struct = get_step_spec('drop_superseded_turns')
    assert spec_struct.kind == 'structural'

    spec_llm = get_step_spec('summarize_oldest_turn')
    assert spec_llm.kind == 'structural'
    assert 'llm' in spec_llm.needs

    # A plain transform stays transform / no needs.
    spec_t = get_step_spec('strip_thinking')
    assert spec_t.kind == 'transform'
    assert spec_t.needs == ()


@pytest.mark.unit
def test_l1_host_rejects_structural_and_llm_steps():
    """The transform-only host (run_steps defaults) must SKIP a structural
    or LLM step rather than run it — keeping L1 cheap + LLM-free."""
    from lib.tasks_pkg.compaction.api import micro_compact

    # Naming an advanced step in the L1 'steps' list must be a safe no-op.
    msgs = [{'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': 'hello'}]
    saved = micro_compact(msgs, conv_id='',
                          steps=['drop_superseded_turns', 'summarize_oldest_turn'])
    assert saved == 0
    assert len(msgs) == 2  # nothing dropped — host refused the kind


# ── MessageEditor ────────────────────────────────────────────────────────

def _mk_ctx(messages, *, cache_prefix_count=0, ignore_cache_prefix=False):
    from lib.tasks_pkg.compaction.api import CompactionContext, MessageEditor
    import lib.tasks_pkg.compaction._constants as constants
    ctx = CompactionContext(messages=messages, conv_id='t', constants=constants,
                            cache_prefix_count=cache_prefix_count,
                            ignore_cache_prefix=ignore_cache_prefix)
    ctx.edit = MessageEditor(ctx)
    return ctx


@pytest.mark.unit
def test_editor_turn_detection_and_protections():
    msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'turn 1'},
        {'role': 'assistant', 'content': 'a1'},
        {'role': 'user', 'content': 'turn 2'},
        {'role': 'assistant', 'content': 'a2'},
        {'role': 'user', 'content': 'turn 3 (in-flight)'},
        {'role': 'assistant', 'content': 'a3'},
    ]
    ctx = _mk_ctx(msgs)
    turns = ctx.edit.turns()
    assert len(turns) == 3                 # system excluded
    evictable = ctx.edit.evictable_turns()
    assert len(evictable) == 2             # in-flight (last) excluded

    # Dropping all evictable keeps system + in-flight turn.
    ctx.edit.drop_turns(evictable)
    roles = [m['content'] for m in msgs]
    assert 'sys' in roles
    assert 'turn 3 (in-flight)' in roles
    assert 'turn 1' not in roles and 'turn 2' not in roles


@pytest.mark.unit
def test_editor_respects_cache_prefix():
    msgs = [
        {'role': 'user', 'content': 'turn 1'},
        {'role': 'assistant', 'content': 'a1'},
        {'role': 'user', 'content': 'turn 2'},
        {'role': 'assistant', 'content': 'a2'},
        {'role': 'user', 'content': 'turn 3'},
        {'role': 'assistant', 'content': 'a3'},
    ]
    # Cache prefix covers the first turn (indices 0-1).
    ctx = _mk_ctx(msgs, cache_prefix_count=2)
    evictable = ctx.edit.evictable_turns()
    starts = {t.start for t in evictable}
    assert 0 not in starts, 'turn inside cache prefix must not be evictable'

    # ignore_cache_prefix lifts the protection.
    ctx2 = _mk_ctx([dict(m) for m in msgs], cache_prefix_count=2,
                   ignore_cache_prefix=True)
    starts2 = {t.start for t in ctx2.edit.evictable_turns()}
    assert 0 in starts2


# ── structural example ─────────────────────────────────────────────────

@pytest.mark.unit
def test_drop_superseded_turns_drops_pure_tool_turns():
    from lib.tasks_pkg.compaction.api import advanced_compact

    msgs = [{'role': 'system', 'content': 'sys'}]
    # Turn 1: pure tool activity (no prose) → droppable.
    msgs += [
        {'role': 'user', 'content': 'explore'},
        {'role': 'assistant', 'content': None,
         'tool_calls': [{'id': 'a', 'function': {'name': 'grep_search',
                                                 'arguments': '{}'}}]},
        {'role': 'tool', 'name': 'grep_search', 'tool_call_id': 'a',
         'content': 'x' * 500},
    ]
    # Turn 2: has assistant prose → kept.
    msgs += [
        {'role': 'user', 'content': 'why?'},
        {'role': 'assistant', 'content': 'Because the root cause is X.'},
    ]
    # Turn 3: in-flight.
    msgs += [
        {'role': 'user', 'content': 'fix it'},
        {'role': 'assistant', 'content': None,
         'tool_calls': [{'id': 'b', 'function': {'name': 'apply_diff',
                                                 'arguments': '{}'}}]},
        {'role': 'tool', 'name': 'apply_diff', 'tool_call_id': 'b',
         'content': 'ok'},
    ]
    before = len(msgs)
    saved = advanced_compact(msgs, conv_id='t', task={'convId': 't'},
                             advanced_steps=['drop_superseded_turns'])
    contents = [m.get('content') for m in msgs]
    assert 'explore' not in contents, 'pure-tool turn should be dropped'
    assert 'Because the root cause is X.' in contents, 'prose turn kept'
    assert 'fix it' in contents, 'in-flight turn kept'
    assert len(msgs) < before
    assert saved > 0


# ── LLM example (stubbed summarize) ─────────────────────────────────────

@pytest.mark.unit
def test_summarize_oldest_turn_uses_granted_llm(monkeypatch):
    import lib.tasks_pkg.compaction._advanced as _advanced

    # Stub the cheap-model summary so the test is hermetic + LLM-free.
    monkeypatch.setattr(_advanced, '_make_summarize_fn',
                        lambda conv_id, task: (
                            lambda text, *, instruction='', max_tokens=512:
                            'STUB SUMMARY of earlier work'))

    msgs = [{'role': 'system', 'content': 'sys'}]
    msgs += [
        {'role': 'user', 'content': 'long earlier request ' * 30},
        {'role': 'assistant', 'content': 'long earlier answer ' * 30},
    ]
    msgs += [
        {'role': 'user', 'content': 'current'},
        {'role': 'assistant', 'content': 'working'},
    ]
    saved = _advanced.advanced_compact(
        msgs, conv_id='t', task={'convId': 't'},
        advanced_steps=['summarize_oldest_turn'])

    joined = ' '.join(m.get('content') or '' for m in msgs)
    assert 'STUB SUMMARY' in joined, 'summary message should be spliced in'
    assert 'long earlier request' not in joined, 'old turn should be dropped'
    assert 'current' in joined


@pytest.mark.unit
def test_summarize_raises_without_grant():
    """ctx.summarize must raise when the host didn't grant the llm cap —
    proves the capability is opt-in by construction."""
    from lib.tasks_pkg.compaction.api import CompactionContext
    import lib.tasks_pkg.compaction._constants as constants
    ctx = CompactionContext(messages=[], constants=constants)
    with pytest.raises(RuntimeError):
        ctx.summarize('anything')


# ── pipeline wiring ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_pipeline_runs_advanced_steps_from_config():
    from lib.tasks_pkg.compaction.api import run_compaction_pipeline

    msgs = [{'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'explore'},
            {'role': 'assistant', 'content': None,
             'tool_calls': [{'id': 'a', 'function': {'name': 'grep_search',
                                                     'arguments': '{}'}}]},
            {'role': 'tool', 'name': 'grep_search', 'tool_call_id': 'a',
             'content': 'x' * 500},
            {'role': 'user', 'content': 'now fix'},
            {'role': 'assistant', 'content': 'on it'}]
    task = {'convId': '', '_userId': 1,
            'config': {'model': 'gpt-4', 'compaction': {
        'advanced_steps': ['drop_superseded_turns']}}}
    run_compaction_pipeline(msgs, current_round=2, task=task)
    assert 'explore' not in [m.get('content') for m in msgs]


@pytest.mark.unit
def test_pipeline_default_no_advanced():
    """No advanced_steps ⇒ no structural change from Stage B."""
    from lib.tasks_pkg.compaction.api import run_compaction_pipeline

    msgs = [{'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'explore'},
            {'role': 'assistant', 'content': None,
             'tool_calls': [{'id': 'a', 'function': {'name': 'grep_search',
                                                     'arguments': '{}'}}]},
            {'role': 'tool', 'name': 'grep_search', 'tool_call_id': 'a',
             'content': 'short'},
            {'role': 'user', 'content': 'now'},
            {'role': 'assistant', 'content': 'ok'}]
    before = len(msgs)
    task = {'convId': '', '_userId': 1, 'config': {'model': 'gpt-4'}}
    run_compaction_pipeline(msgs, current_round=2, task=task)
    assert len(msgs) == before  # Stage B did not run
