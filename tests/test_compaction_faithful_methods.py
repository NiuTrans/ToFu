"""Tests for per-system faithful compaction baselines (_faithful_methods.py).

Verifies each system's OWN trigger threshold + protected-region sizing
(they must NOT be shared), the OpenCode prune constants, and Hermes
informative stubs + iterative running summary.

Run:  pytest tests/test_compaction_faithful_methods.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mk_tools(n, chars, name='read_files'):
    # Realistic agent structure: one user turn per round so backward
    # turn-counting (OpenCode skip-2-recent) has real turn boundaries.
    msgs = [{'role': 'user', 'content': 'solve the issue'}]
    for i in range(n):
        msgs.append({'role': 'assistant', 'content': None,
                     'tool_calls': [{'id': f't{i}',
                                     'function': {'name': name, 'arguments': '{}'}}]})
        msgs.append({'role': 'tool', 'name': name, 'tool_call_id': f't{i}',
                     'content': 'x' * chars})
        msgs.append({'role': 'user', 'content': f'continue {i}'})
    return msgs


def _n_with(msgs, marker):
    return sum(1 for m in msgs if m.get('role') == 'tool'
               and isinstance(m.get('content'), str) and marker in m['content'])


# ── registration + forms ────────────────────────────────────────────────

@pytest.mark.unit
def test_per_system_steps_registered():
    from lib.tasks_pkg.compaction import get_step_spec, list_steps
    steps = list_steps()
    for name in ('prune_tool_outputs_opencode', 'prune_tool_outputs_hermes',
                 'summarize_opencode', 'summarize_hermes', 'summarize_openclaw'):
        assert name in steps, name
    assert get_step_spec('prune_tool_outputs_opencode').kind == 'transform'
    for s in ('summarize_opencode', 'summarize_hermes', 'summarize_openclaw'):
        spec = get_step_spec(s)
        assert spec.kind == 'structural' and 'llm' in spec.needs, s


# ── OpenCode prune: 40k/20k, skip 2 recent turns, 2000-char threshold ────

@pytest.mark.unit
def test_opencode_prune_constants_and_skip_recent():
    from lib.tasks_pkg.compaction import micro_compact
    # 30 tool results × ~3k tokens (12k chars). Protect 40k tail + skip the
    # 2 most-recent turns; prune older (reclaim >> 20k).
    msgs = _mk_tools(30, 12_000)
    micro_compact(msgs, conv_id='', steps=['prune_tool_outputs_opencode'])
    pruned = _n_with(msgs, 'pruned to save context')
    assert pruned >= 8, f'expected many pruned, got {pruned}'
    last = [m for m in msgs if m.get('role') == 'tool'][-1]
    assert 'pruned' not in last['content'][:40], 'recent tail protected'


@pytest.mark.unit
def test_opencode_prune_skips_below_20k_minimum():
    from lib.tasks_pkg.compaction import micro_compact
    msgs = _mk_tools(10, 12_000)  # ~30k total < 40k protect → reclaim<20k
    micro_compact(msgs, conv_id='', steps=['prune_tool_outputs_opencode'])
    assert _n_with(msgs, 'pruned') == 0


@pytest.mark.unit
def test_opencode_prune_respects_2000_char_threshold():
    from lib.tasks_pkg.compaction import micro_compact
    # Many SMALL (<2000 char) tool results — none should be pruned even if cold.
    msgs = _mk_tools(60, 500)
    micro_compact(msgs, conv_id='', steps=['prune_tool_outputs_opencode'])
    assert _n_with(msgs, 'pruned') == 0


# ── Hermes informative stubs ─────────────────────────────────────────────

@pytest.mark.unit
def test_hermes_prune_informative_stub():
    from lib.tasks_pkg.compaction import micro_compact
    msgs = _mk_tools(40, 5000, name='grep_search')
    micro_compact(msgs, conv_id='', steps=['prune_tool_outputs_hermes'],
                  constant_overrides={'HERMES_PRUNE_PROTECT': 5_000})
    # Stub names the tool + char/line counts (informative), not generic "pruned".
    stubbed = [m['content'] for m in msgs if m.get('role') == 'tool'
               and isinstance(m.get('content'), str) and 'cleared' in m['content']]
    assert stubbed, 'expected informative stubs'
    assert any('grep_search' in s and 'chars' in s for s in stubbed), stubbed[0]


# ── Per-system trigger thresholds DIFFER (the key fidelity property) ─────

@pytest.mark.unit
def test_triggers_are_per_system_not_shared(monkeypatch):
    """OpenCode/Hermes/OpenClaw must compute DIFFERENT trigger thresholds
    from the same 128k context — proving they're not collapsed into one."""
    import lib.tasks_pkg.compaction._faithful_methods as fm

    class FakeCtx:
        task = {'config': {'model': 'deepseek-v4-flash'}}
        constants = type('C', (), {})()
        messages = []
        conv_id = 'x'
    ctx = FakeCtx()
    monkeypatch.setattr(fm, '_raw_context_limit', lambda c: 128_000)
    monkeypatch.setattr(fm, '_max_output_tokens', lambda c: 8192)

    oc = fm._oc_usable(ctx)                       # 128k - min(20k,8192)=8192 → 119808
    hermes = int(128_000 * fm._HERMES_THRESHOLD_PCT)   # 64000
    openclaw = 128_000 - fm._OPENCLAW_RESERVE_FLOOR    # 108000
    assert oc == 128_000 - 8192
    assert hermes == 64_000
    assert openclaw == 108_000
    assert len({oc, hermes, openclaw}) == 3, 'triggers must differ per system'


# ── overflow gating: under threshold → no summarize call ─────────────────

@pytest.mark.unit
def test_summarizers_skip_under_threshold(monkeypatch):
    from lib.tasks_pkg.compaction import advanced_compact
    import lib.tasks_pkg.compaction._faithful_methods as fm
    monkeypatch.setattr(fm, '_raw_context_limit', lambda c: 128_000)
    monkeypatch.setattr(fm, '_tok', lambda m, t: 5_000)  # well under all thresholds
    for step in ('summarize_opencode', 'summarize_hermes', 'summarize_openclaw'):
        msgs = _mk_tools(3, 200)
        saved = advanced_compact(msgs, conv_id=f'u-{step}', task={'convId': 'u'},
                                 advanced_steps=[step])
        assert saved == 0, f'{step} fired under threshold'


# ── Hermes iterative running summary persists + updates ──────────────────

@pytest.mark.unit
def test_hermes_iterative_summary_updates(monkeypatch):
    from lib.tasks_pkg.compaction import advanced_compact
    import lib.tasks_pkg.compaction._faithful_methods as fm
    import lib.tasks_pkg.compaction._advanced as adv

    fm.reset_running_summary('c-it')
    monkeypatch.setattr(fm, '_raw_context_limit', lambda c: 10_000)  # threshold 5k
    monkeypatch.setattr(fm, '_tok', lambda m, t: 9_999)
    monkeypatch.setattr(fm, '_cooldown_ok', lambda cid: True)
    monkeypatch.setattr(fm, '_select_middle_turns',
                        lambda ctx, keep_recent_tokens, protect_first_n, protect_last_n=0:
                        ([t for t in ctx.edit.turns()[1:-1]], 'MIDDLE ' * 100))
    seen = []
    monkeypatch.setattr(adv, '_make_summarize_fn', lambda conv_id, task:
                        (lambda text, *, instruction='', max_tokens=512:
                         (seen.append(instruction), f'SUMMARY v{len(seen)}')[1]))

    def mk():
        m = [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'orig ' * 50}]
        for i in range(6):
            m += [{'role': 'assistant', 'content': f'w{i} ' * 50},
                  {'role': 'user', 'content': f'c{i}'}]
        m += [{'role': 'user', 'content': 'tail'}]
        return m

    advanced_compact(mk(), conv_id='c-it', task={'convId': 'c-it'},
                     advanced_steps=['summarize_hermes'])
    assert fm._running_summaries['c-it'] == 'SUMMARY v1'
    assert 'CURRENT RUNNING SUMMARY' not in seen[0]

    advanced_compact(mk(), conv_id='c-it', task={'convId': 'c-it'},
                     advanced_steps=['summarize_hermes'])
    assert fm._running_summaries['c-it'] == 'SUMMARY v2'
    assert any('UPDATE the running summary' in s for s in seen[1:])
    fm.reset_running_summary('c-it')


@pytest.mark.unit
def test_overflow_context_limit_override_pins_trigger():
    """OVERFLOW_CONTEXT_LIMIT (experiment knob) pins the trigger budget
    WITHOUT touching the real model context limit. A 1M model with the
    override set to 128k must compute the same triggers as a 128k model."""
    import lib.tasks_pkg.compaction._faithful_methods as fm

    class Ctx1M:
        task = {'config': {'model': 'deepseek-v4-pro'}}
        constants = type('C', (), {'OVERFLOW_CONTEXT_LIMIT': 128_000})()
        messages = []
        conv_id = 'p'
    # With the override, _raw_context_limit returns the pinned 128k, NOT 1M.
    assert fm._raw_context_limit(Ctx1M()) == 128_000
    assert int(fm._raw_context_limit(Ctx1M()) * fm._HERMES_THRESHOLD_PCT) == 64_000

    class CtxNoOverride:
        task = {'config': {'model': 'deepseek-v4-pro'}}
        constants = type('C', (), {})()  # no override
        messages = []
        conv_id = 'p'
    # Absent the override, falls through to the real model limit (unchanged).
    from lib.tasks_pkg.compaction._tokens import _get_context_limit
    assert fm._raw_context_limit(CtxNoOverride()) == _get_context_limit(
        {'config': {'model': 'deepseek-v4-pro'}})


@pytest.mark.unit
def test_prune_steps_llm_free():
    import inspect
    import lib.tasks_pkg.compaction._faithful_methods as m
    for fn in (m.prune_tool_outputs_opencode, m.prune_tool_outputs_hermes):
        src = inspect.getsource(fn)
        assert 'summarize' not in src and 'dispatch_chat' not in src
