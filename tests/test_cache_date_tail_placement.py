#!/usr/bin/env python3
"""Cache-prefix-stability regression suite for the "Current date → true tail"
move (A) and the mixed-TTL split invariant (B).

A — the ``Current date`` line changes once per UTC day. Baking it into the
cached static system block re-billed the WHOLE system prefix at every UTC-day
rollover (Anthropic's named "don't inject timestamps into the cached prompt"
anti-pattern). It now rides the TRUE tail via ``_refresh_tail_block`` (the same
cache-safe seam the digest / charter / board use), so the system floor stays
byte-identical across the day boundary and the volatile date rides the
already-5m tail for free.

B — the ``CACHE_EXTENDED_TTL`` toggle is deliberately KEPT (a legitimate
gateway-compat escape hatch — non-Claude gateways that don't honor the
extended-cache-ttl beta force it False). What must never silently regress is
the SPLIT: with extended TTL on, the stable system/tools blocks carry
``ttl='1h'`` and the volatile conversation tail stays at the default 5m
(writing the churning tail at the 2x 1h premium would be pure waste). There was
a real-world "1h silently regressed to 5m" incident upstream — this pins it.

Each behavioural assertion carries a NEUTER control proving the fix is
load-bearing.

Run DIRECTLY (env-guarded):
    python tests/test_cache_date_tail_placement.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


# ── Local helpers (self-contained — no cross-file test deps) ──

def _system_text_of(messages):
    """Concatenated text of the first system message (the hoisted floor)."""
    if not messages or messages[0].get('role') != 'system':
        return ''
    sc = messages[0].get('content', '')
    if isinstance(sc, str):
        return sc
    if isinstance(sc, list):
        return '\n\n'.join(b.get('text', '') for b in sc
                           if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def _run_inject(messages, *, model='claude-opus-4', mode='append'):
    from lib.tasks_pkg.system_context import _inject_system_contexts
    _inject_system_contexts(
        messages, project_path='/tmp/x', project_enabled=False,
        memory_enabled=False, search_enabled=False, swarm_enabled=False,
        has_real_tools=True, conv_id='', task={'config': {}}, model=model,
        system_prompt_mode=mode,
    )


def _ttl_of(block):
    cc = block.get('cache_control') if isinstance(block, dict) else None
    if not isinstance(cc, dict):
        return None
    return cc.get('ttl', '5m')  # bare ephemeral == default 5m


# ─────────────────────────────────────────────────────────────────────────────
#  A — Current date rides the TRUE tail, never the cached system floor
# ─────────────────────────────────────────────────────────────────────────────

def test_A_date_not_in_system_floor_append_mode():
    """After injection (append mode), the cached system floor must NOT carry a
    'Current date:' line — it rides the tail instead."""
    msgs = [{'role': 'system', 'content': 'BASE PROMPT'},
            {'role': 'user', 'content': 'hello'}]
    _run_inject(msgs)
    assert 'Current date:' not in _system_text_of(msgs), (
        'the date leaked into the cached system floor — the daily UTC rollover '
        'will re-bill the whole prefix')
    # It rode the last user message (the true volatile tail).
    last_user = next(m for m in reversed(msgs) if m.get('role') == 'user')
    body = last_user['content']
    txt = (body if isinstance(body, str)
           else '\n\n'.join(b.get('text', '') for b in body
                            if isinstance(b, dict)))
    assert 'Current date:' in txt, 'the date did not land on the true tail'


def test_A_system_floor_byte_identical_across_utc_day_boundary():
    """★ THE A INVARIANT. Two turns whose ONLY difference is the wall-clock date
    must produce a BYTE-IDENTICAL system floor (so cache_read survives the day
    rollover) — because the date rides the tail.

    NEUTER twin: with the date baked back into the static block (include_date
    True), the floor DIVERGES across the boundary."""
    from unittest.mock import patch

    def _floor_for_date(date_str):
        msgs = [{'role': 'system', 'content': 'BASE PROMPT'},
                {'role': 'user', 'content': 'hello'}]
        with patch('lib.tasks_pkg.system_prompt_cc.section_current_date',
                   return_value=f'Current date: {date_str}'):
            _run_inject(msgs)
        return _system_text_of(msgs)

    floor_day1 = _floor_for_date('2026-07-18')
    floor_day2 = _floor_for_date('2026-07-19')
    assert floor_day1 == floor_day2, (
        'A broken: the system floor changed across the UTC day boundary despite '
        'the date being moved to the tail')
    # And the floor genuinely has no date at all.
    assert 'Current date:' not in floor_day1

    # ── NEUTER: bake the date back into the static block → floor diverges ──
    from lib.tasks_pkg.system_prompt_cc import build_static_prompt
    with patch('lib.tasks_pkg.system_prompt_cc._build.section_current_date',
               return_value='Current date: 2026-07-18'):
        s1 = build_static_prompt(cwd='', is_git=False, model='m',
                                 include_date=True)
    with patch('lib.tasks_pkg.system_prompt_cc._build.section_current_date',
               return_value='Current date: 2026-07-19'):
        s2 = build_static_prompt(cwd='', is_git=False, model='m',
                                 include_date=True)
    assert s1 != s2, (
        'NEUTER expectation: baking the date into the static block (old '
        'behavior) MUST diverge the floor across days — proving the tail '
        'placement is load-bearing')


# ─────────────────────────────────────────────────────────────────────────────
#  B — mixed-TTL invariant: stable prefix = 1h, volatile tail = 5m
# ─────────────────────────────────────────────────────────────────────────────

def test_B_stable_is_1h_tail_is_5m_when_extended_on():
    """With CACHE_EXTENDED_TTL on, stable system/tools blocks are 1h and the
    conversation tail is 5m (default)."""
    import lib as _lib
    from lib.llm import add_cache_breakpoints
    _orig = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
    _lib.CACHE_EXTENDED_TTL = True
    try:
        body = {
            'model': 'claude-opus-4-20250514',
            'system': None,
            'tools': [{'function': {'name': 't', 'description': 'd',
                                    'parameters': {}}}],
            'messages': [
                {'role': 'system', 'content': [{'type': 'text', 'text': 'S'}]},
                {'role': 'user', 'content': 'u1'},
                {'role': 'assistant', 'content': 'a1'},
                {'role': 'user', 'content': 'u2 tail'},
            ],
        }
        add_cache_breakpoints(body)
        # System block → 1h
        sys_blk = body['messages'][0]['content'][0]
        assert _ttl_of(sys_blk) == '1h', f'stable system must be 1h, got {sys_blk}'
        # Tool definition → 1h
        tool_fn = body['tools'][-1]['function']
        assert _ttl_of(tool_fn) == '1h', f'stable tool must be 1h, got {tool_fn}'
        # Conversation tail (last user msg) → 5m (bare ephemeral)
        tail_blk = body['messages'][-1]['content'][-1]
        assert _ttl_of(tail_blk) == '5m', (
            f'volatile tail must stay 5m (never pay the 2x 1h write premium on '
            f'a block that changes every round), got {tail_blk}')
    finally:
        _lib.CACHE_EXTENDED_TTL = _orig


def test_B_all_5m_when_extended_off():
    """NEUTER / escape-hatch: with the toggle OFF (non-Claude gateway compat),
    every marker is bare 5m — no ttl key anywhere. Proves the toggle still
    disables 1h and the split is genuinely gated on it."""
    import lib as _lib
    from lib.llm import add_cache_breakpoints
    _orig = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
    _lib.CACHE_EXTENDED_TTL = False
    try:
        body = {
            'model': 'claude-opus-4-20250514',
            'tools': [{'function': {'name': 't', 'description': 'd',
                                    'parameters': {}}}],
            'messages': [
                {'role': 'system', 'content': [{'type': 'text', 'text': 'S'}]},
                {'role': 'user', 'content': 'u1'},
                {'role': 'assistant', 'content': 'a1'},
                {'role': 'user', 'content': 'u2 tail'},
            ],
        }
        add_cache_breakpoints(body)
        sys_blk = body['messages'][0]['content'][0]
        tail_blk = body['messages'][-1]['content'][-1]
        assert _ttl_of(sys_blk) == '5m', f'toggle off → system must be 5m: {sys_blk}'
        assert _ttl_of(tail_blk) == '5m', f'toggle off → tail must be 5m: {tail_blk}'
    finally:
        _lib.CACHE_EXTENDED_TTL = _orig


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
