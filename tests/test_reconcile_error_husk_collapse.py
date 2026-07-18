"""tests/test_reconcile_error_husk_collapse.py — regression for the
"user → error-bubble → agent" duplicate (the late-task-start artifact).

WHY
---
When an edit/regen/send client safety timer fires AFTER the ~30s recovery
window has already synced a visible error bubble, the server task can still
appear later; a subsequent orphan-recovery reconnect then appends the REAL
assistant right below the error bubble. The persisted list becomes

    [user, error-husk (empty content, error set), real-assistant (settled)]

— a "user → error → agent" duplicate for a SINGLE logical exchange. This is
the last orchestration duplicate-source, and its correct home is the backend
GET-path reconcile (``reconcile_conversation_messages``), NOT the frontend
ghost-classifiers (separation-of-concerns directive).

This suite drives the PURE reconcile function directly and asserts the
superseded error husk collapses to ``[user, real-assistant]``. The NEUTER
monkeypatches ``is_superseded_error_husk`` to always-False (reverting the
collapse pass) and proves the duplicate survives — so the collapse pass is
load-bearing, not a tautology.
"""

from __future__ import annotations

import copy

import pytest

pytestmark = pytest.mark.unit


def _u(k, content='Q'):
    return {'k': k, 'role': 'user', 'content': content}


def _a(k, **kw):
    m = {'k': k, 'role': 'assistant', 'content': ''}
    m.update(kw)
    return m


def _err_husk(k, msg='Regenerate timed out'):
    # Exactly the errAssistant shape the frontend pushes on timeout/abort
    # (main_regen_continue.js / edit_message.js / main_send_pipeline.js).
    return _a(k, error={'kind': 'internal', 'message': msg}, thinking='', toolRounds=[])


def _survivors(messages):
    return [m['k'] for m in messages]


def _reconcile(messages, prefix=0):
    from lib.conversations.reconcile import reconcile_conversation_messages
    return reconcile_conversation_messages(copy.deepcopy(messages), prefix)


# ── POSITIVE: the exact late-recovery artifact collapses. ──
def test_superseded_error_husk_collapses_to_single_reply():
    msgs = [_u('u0'), _err_husk('e1'), _a('r1', content='real reply', finishReason='stop')]
    out, changed = _reconcile(msgs)
    assert _survivors(out) == ['u0', 'r1'], (
        'superseded error husk should collapse to [user, real-assistant]')
    assert changed is True
    # The surviving reply is the real one, untouched.
    assert out[-1]['content'] == 'real reply'
    assert out[-1].get('error') is None


def test_settled_via_toolrounds_also_collapses():
    # 'settled' includes a result-bearing tool round, not just content.
    msgs = [
        _u('u0'), _err_husk('e1'),
        _a('r1', toolRounds=[{'status': 'done', 'toolName': 'run_command'}], finishReason='stop'),
    ]
    out, changed = _reconcile(msgs)
    assert _survivors(out) == ['u0', 'r1']
    assert changed is True


# ── NEGATIVE guards: only the exact artifact collapses, nothing else. ──
def test_error_husk_followed_by_user_is_kept():
    # A genuine prior-exchange error (next turn is a NEW user message) — keep it.
    msgs = [_u('u0'), _err_husk('e1'), _u('u1'), _a('r1', content='reply', finishReason='stop')]
    out, changed = _reconcile(msgs)
    assert 'e1' in _survivors(out), 'a non-superseded error husk must be kept'
    assert changed is False


def test_tail_error_husk_is_kept():
    # An error at the very tail (no reply after it) is real user-visible info.
    msgs = [_u('u0'), _err_husk('e1')]
    out, changed = _reconcile(msgs)
    assert _survivors(out) == ['u0', 'e1']
    assert changed is False


def test_error_husk_followed_by_empty_placeholder_is_kept():
    # Next turn is a still-empty placeholder (not settled) → not superseded.
    msgs = [_u('u0'), _err_husk('e1'), _a('p1')]
    out, changed = _reconcile(msgs)
    assert 'e1' in _survivors(out), 'error not superseded by an unsettled placeholder'
    # (the trailing empty placeholder p1 is itself deleted by the tail pass,
    #  but the error husk stays.)


def test_error_husk_with_content_is_not_a_husk():
    # An assistant with BOTH error and real content is a real reply, not a husk.
    msgs = [
        _u('u0'),
        _a('e1', content='partial answer', error={'kind': 'internal'}),
        _a('r1', content='reply', finishReason='stop'),
    ]
    out, changed = _reconcile(msgs)
    assert 'e1' in _survivors(out), 'an error WITH content is a real reply, keep it'
    assert changed is False


def test_special_turn_error_is_never_collapsed():
    # A VU / endpoint turn carrying an error is never 'clutter'.
    msgs = [
        _u('u0'),
        _a('vu', error={'kind': 'internal'}, _isVirtualUser=True),
        _a('r1', content='reply', finishReason='stop'),
    ]
    out, changed = _reconcile(msgs)
    assert 'vu' in _survivors(out)
    assert changed is False


def test_collapse_is_cache_prefix_gated():
    # An error husk INSIDE the immutable cache prefix is NOT collapsed
    # (removing it would shift prefix bytes and bust the prompt cache).
    msgs = [_u('u0'), _err_husk('e1'), _a('r1', content='reply', finishReason='stop')]
    # prefix=2 protects idx 0,1 → e1 (idx 1) survives.
    out, changed = _reconcile(msgs, prefix=2)
    assert _survivors(out) == ['u0', 'e1', 'r1'], 'in-prefix husk must survive (cache-neutral)'
    assert changed is False
    # prefix=0 → collapses (self-heals once the cache prefix resets).
    out0, changed0 = _reconcile(msgs, prefix=0)
    assert _survivors(out0) == ['u0', 'r1']
    assert changed0 is True


# ── NEUTER: revert the collapse pass → the duplicate survives (teeth). ──
def test_neuter_disables_collapse_and_duplicate_survives(monkeypatch):
    import lib.conversations.reconcile as rec
    monkeypatch.setattr(rec, 'is_superseded_error_husk', lambda messages, idx: False)
    msgs = [_u('u0'), _err_husk('e1'), _a('r1', content='real reply', finishReason='stop')]
    out, changed = rec.reconcile_conversation_messages(copy.deepcopy(msgs), 0)
    assert _survivors(out) == ['u0', 'e1', 'r1'], (
        'neuter disabled the collapse but the husk vanished anyway — '
        'the collapse pass is not the thing under test (no teeth)')
    assert changed is False
