"""Tests for budget-aware, user-message-preserving summary-input elision.

Owner sign-off (§10.1, 2026-07-18): the manual /compact was slow because the
whole cost is the single summary LLM call, and its input cap was 200k chars —
~3× larger than needed. Objective A: lower the cap to ~64k AND elide the
MIDDLE (not the tail) so section 6 of the summary prompt ("All User Messages —
MANDATORY") never loses a user instruction.

Two load-bearing invariants:
  1. When the rendered input exceeds the budget, EVERY ``[user]`` message
     survives verbatim — only assistant (non-user) content is elided.
  2. The elision happens in the MIDDLE, keeping the earliest goals and the most
     recent working state; a marker shows where content was dropped.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest -p no:napari \
        tests/test_summary_input_elision.py
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _u(text):
    return {'role': 'user', 'content': text}


def _a(text):
    return {'role': 'assistant', 'content': text}


# ── The new cap must be ~64k, well below the old 200k ceiling ──────────────

def test_summary_char_budget_ceiling_lowered_to_64k():
    """The hard ceiling that binds on large (1M) windows is now ~64k, not 200k.

    A 1M-context model has huge ``usable``, so the budget is clamped by the
    ceiling — which must be the tightened 64k, not the old 3×-redundant 200k."""
    from lib.tasks_pkg.compaction._layer2._prompt import _summary_input_char_budget
    big_window_task = {'config': {'model': 'claude-opus-4.6'}}  # 1M context
    budget = _summary_input_char_budget(big_window_task)
    assert budget <= 64_000, f'ceiling not tightened: {budget}'
    assert budget >= 40_000, f'ceiling too aggressive (would over-elide): {budget}'


# ── User messages are MANDATORY — elision must never drop one ──────────────

def test_elision_preserves_every_user_message():
    """When the input exceeds the budget, all [user] messages survive verbatim;
    only assistant content is elided (summary prompt §6 is MANDATORY)."""
    from lib.tasks_pkg.compaction._layer2._prompt import _format_messages_for_summary

    # 40 turns; each assistant carries a big (5k-char) blob, users are short but
    # each carries a UNIQUE, must-not-be-lost instruction token.
    msgs = []
    user_tokens = []
    for i in range(40):
        tok = f'USERREQ_{i:03d}'
        user_tokens.append(tok)
        msgs.append(_u(f'{tok}: please do step {i}'))
        msgs.append(_a('ASSISTANTBLOB ' + ('z' * 5000)))

    budget = 40_000
    out = _format_messages_for_summary(msgs, char_budget=budget)

    # (1) Every single user instruction token is still present.
    missing = [t for t in user_tokens if t not in out]
    assert not missing, f'elision dropped {len(missing)} user messages: {missing[:5]}'

    # (2) The output actually fit the budget (with a small marker allowance).
    assert len(out) <= budget + 500, f'elision did not honor budget: {len(out)}'

    # (3) Some assistant content WAS elided (a marker is present) — otherwise
    #     the test conversation was too small to exercise the path.
    assert 'omitted' in out.lower() or 'elided' in out.lower(), (
        'no elision marker — the budget path did not fire')


def test_elision_is_middle_not_tail():
    """The FIRST and LAST user turns must both survive — elision hits the
    middle, not the tail (a naive tail-truncate would drop the recent state,
    a naive head-truncate would drop the earliest goal)."""
    from lib.tasks_pkg.compaction._layer2._prompt import _format_messages_for_summary
    msgs = []
    for i in range(40):
        msgs.append(_u(f'USERREQ_{i:03d}: step {i}'))
        msgs.append(_a('BLOB ' + ('z' * 5000)))
    out = _format_messages_for_summary(msgs, char_budget=40_000)
    assert 'USERREQ_000' in out, 'earliest goal (head) was dropped'
    assert 'USERREQ_039' in out, 'most recent turn (tail) was dropped'


def test_no_budget_is_unbounded_backcompat():
    """Called with no char_budget (the existing signature), behavior is
    unchanged: full render, no elision marker."""
    from lib.tasks_pkg.compaction._layer2._prompt import _format_messages_for_summary
    msgs = [_u('short'), _a('also short')]
    out = _format_messages_for_summary(msgs)
    assert '[user] short' in out
    assert '[assistant] also short' in out
    assert 'omitted' not in out.lower()


def test_user_messages_kept_even_when_they_alone_exceed_budget():
    """Degenerate case: if the user messages ALONE exceed the budget, they are
    STILL all kept (correctness over budget — never silently drop an
    instruction). Assistant content is fully elided first."""
    from lib.tasks_pkg.compaction._layer2._prompt import _format_messages_for_summary
    msgs = []
    toks = []
    for i in range(30):
        tok = f'BIGUSER_{i:03d}'
        toks.append(tok)
        msgs.append(_u(f'{tok}: ' + ('u' * 3000)))   # each user msg is large
        msgs.append(_a('ASSTBLOB ' + ('z' * 5000)))
    out = _format_messages_for_summary(msgs, char_budget=20_000)
    missing = [t for t in toks if t not in out]
    assert not missing, f'dropped user messages under tight budget: {missing[:5]}'



# ── Audit trail: the §10.1 config_change must ACTUALLY fire (not swallowed) ──

def test_config_change_audit_fires_with_cap(monkeypatch):
    """Owner required an audit_log('config_change', summary_input_char_cap=...,
    approved_by='user') for the §10.1 cap change. Regression guard: a missing
    import of _SUMMARY_INPUT_CHAR_CAP would raise NameError INSIDE the
    try/except in _audit_config_once and be silently swallowed at debug — the
    audit entry would never fire and no one would notice. Assert the call
    actually lands with the cap value."""
    import lib.tasks_pkg.compaction._manual as man
    calls = []
    monkeypatch.setattr(man, 'audit_log', lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(man, '_CONFIG_CHANGE_LOGGED', False)
    man._audit_config_once()
    assert calls, ('config_change audit never fired — a NameError (e.g. a '
                   'missing constant import) was swallowed by the try/except')
    _args, kw = calls[0]
    assert kw.get('change') == 'manual_compaction_intra_turn', kw
    assert kw.get('approved_by') == 'user', kw
    assert kw.get('summary_input_char_cap') == 64_000, (
        f'cap not recorded in the sign-off audit entry: {kw}')
