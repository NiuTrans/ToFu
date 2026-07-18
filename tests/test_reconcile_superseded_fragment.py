"""tests/test_reconcile_superseded_fragment.py — regression for the
"truncated abort fragment shown as a finished turn" bug.

WHY
---
A Stop→Regenerate leaves TWO assistant messages for one user turn:

    [user,
     fragment  (924 chars of the aborted task, finishReason=None),  ← the ghost
     answer    (the regenerate's complete reply, finishReason=stop)]

The fragment was written by the aborted task's partial-checkpoint path
(status='running' → NO terminal fields), and its terminal sync was skipped by
the freshness guard, so it persisted with ``finishReason=None`` — an ambiguous
"settled but no terminal reason" husk. The frontend then renders it in full
completed chrome (action row, translate footer, branch button) even though it's
a truncated partial (the reported screenshot).

The fix is data-preserving: reconcile MARKS the fragment
``finishReason='aborted'`` (never deletes it) so the content survives but
renders as the aborted partial it truthfully is. This suite drives the PURE
``reconcile_conversation_messages`` and asserts the mark, plus a NEUTER control
that reverts ``is_superseded_incomplete_fragment`` to always-False and proves
the ambiguous husk survives unmarked — the mark pass is load-bearing.
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


def _frag(k, content='Compile gate green. T'):
    # The aborted-task partial-checkpoint shape: real content, NO finishReason.
    return _a(k, content=content, thinking='', toolRounds=[])


def _reconcile(messages, prefix=0):
    from lib.conversations.reconcile import reconcile_conversation_messages
    return reconcile_conversation_messages(copy.deepcopy(messages), prefix)


def _fr(messages, k):
    for m in messages:
        if m.get('k') == k:
            return m.get('finishReason')
    return '__absent__'


# ── POSITIVE: the exact two-answers-one-turn artifact is marked. ──
def test_superseded_fragment_before_settled_answer_is_marked():
    msgs = [_u('u0'), _frag('f1'), _a('r1', content='full answer', finishReason='stop')]
    out, changed = _reconcile(msgs)
    # Nothing deleted — content preserved.
    assert [m['k'] for m in out] == ['u0', 'f1', 'r1']
    assert changed is True
    # The fragment now carries a truthful terminal reason.
    assert _fr(out, 'f1') == 'aborted'
    # The real answer is untouched.
    assert _fr(out, 'r1') == 'stop'
    assert out[-1]['content'] == 'full answer'


def test_settled_answer_before_fragment_is_marked():
    # Ordering inversion (the observed DB state): the completed regenerate landed
    # BEFORE the lingering aborted fragment. Adjacency still proves same-turn.
    msgs = [_u('u0'), _a('r1', content='full answer', finishReason='stop'), _frag('f1')]
    out, changed = _reconcile(msgs)
    assert _fr(out, 'f1') == 'aborted'
    assert changed is True


def test_settled_via_toolrounds_sibling_also_marks():
    msgs = [
        _u('u0'), _frag('f1'),
        _a('r1', toolRounds=[{'status': 'done', 'toolName': 'run_command'}], finishReason='stop'),
    ]
    out, changed = _reconcile(msgs)
    assert _fr(out, 'f1') == 'aborted'
    assert changed is True


# ── NEGATIVE guards: only the exact artifact is marked. ──
def test_lone_incomplete_tail_fragment_is_not_marked():
    # A live/settling turn with no settled sibling — must NOT be stamped
    # (it may still be streaming; the backend owns its terminal reason).
    msgs = [_u('u0'), _frag('f1')]
    out, changed = _reconcile(msgs)
    assert _fr(out, 'f1') == '__absent__' or _fr(out, 'f1') is None
    assert changed is False


def test_fragment_separated_by_user_turn_is_not_marked():
    # A genuine prior-turn partial with a NEW user turn between it and the next
    # answer is a different exchange — adjacency broken, do not mark.
    msgs = [_u('u0'), _frag('f1'), _u('u1'), _a('r1', content='reply', finishReason='stop')]
    out, changed = _reconcile(msgs)
    assert _fr(out, 'f1') == '__absent__' or _fr(out, 'f1') is None


def test_already_finished_fragment_is_left_alone():
    # Two settled answers (e.g. a branch): neither lacks a reason → no mark.
    msgs = [
        _u('u0'),
        _a('a1', content='answer one', finishReason='aborted'),
        _a('r1', content='answer two', finishReason='stop'),
    ]
    out, changed = _reconcile(msgs)
    assert _fr(out, 'a1') == 'aborted'  # unchanged, not re-marked
    assert changed is False


def test_special_turn_fragment_is_never_marked():
    msgs = [
        _u('u0'),
        _a('vu', content='vu partial', _isVirtualUser=True),
        _a('r1', content='reply', finishReason='stop'),
    ]
    out, changed = _reconcile(msgs)
    assert _fr(out, 'vu') == '__absent__' or _fr(out, 'vu') is None
    assert changed is False


# ── CACHE-NEUTRALITY: marking finishReason must not shift the wire prefix. ──
def test_mark_is_wire_cache_neutral():
    """finishReason is NOT a wire-fingerprint field, so stamping it on an
    in-prefix fragment must leave the prompt-cache canonical form byte-identical
    (else moving reconcile onto the hot GET path would bust the cache). Proves
    the mark is safe to run even when the fragment sits inside the cached
    prefix."""
    from lib.conversations.reconcile import mark_superseded_incomplete_fragments
    from lib.tasks_pkg.wire_fingerprint import canonical_messages, diff_canonical
    msgs = [
        {'role': 'user', 'content': 'Q'},
        {'role': 'assistant', 'content': 'Compile gate green. T', 'thinking': '',
         'toolRounds': []},                                     # the fragment
        {'role': 'assistant', 'content': 'full answer', 'finishReason': 'stop'},
    ]
    before = canonical_messages(copy.deepcopy(msgs))
    out, marked = mark_superseded_incomplete_fragments(copy.deepcopy(msgs))
    assert marked == 1, 'expected the fragment to be marked'
    after = canonical_messages(out)
    assert diff_canonical(before, after) == [], (
        'marking finishReason changed the wire fingerprint — would bust cache')


# ── NEUTER: revert the mark pass → the ambiguous husk survives unmarked. ──
def test_neuter_disables_mark_and_husk_survives(monkeypatch):
    import lib.conversations.reconcile as rec
    monkeypatch.setattr(rec, 'is_superseded_incomplete_fragment',
                        lambda messages, idx: False)
    msgs = [_u('u0'), _frag('f1'), _a('r1', content='full answer', finishReason='stop')]
    out, changed = rec.reconcile_conversation_messages(copy.deepcopy(msgs), 0)
    # With the predicate neutered the fragment keeps finishReason=None — the
    # exact ambiguous husk the bug reports. Proves the mark pass has teeth.
    assert _fr(out, 'f1') == '__absent__' or _fr(out, 'f1') is None
