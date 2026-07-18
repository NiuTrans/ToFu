"""Ground-truth corpus regression for the BACKEND ghost reconcile
(``lib.conversations.reconcile.reconcile_conversation_messages``).

HISTORY
-------
This suite originally proved byte-equivalence between the BACKEND reconcile and
the FRONTEND Case-D sequence (``_sweepBuriedGhostAssistants`` then
``_classifyGhostTail`` in ``static/js/main/main_init_tasks.js``) over a shared
fixture corpus — the gate that had to pass BEFORE the JS sweep could be safely
deleted. That migration is now COMPLETE: the buried-ghost sweep
(``_isBuriedEmptyGhost`` / ``_sweepBuriedGhostAssistants``) was removed from the
frontend (the verdict lives on the backend GET path,
``routes/conversations.py::_reconcile_conv_on_get_blocking``), so there is no
longer a live JS implementation to compare against.

Rather than lose the 15-fixture corpus, this suite now FREEZES the authentic JS
golden output (captured from the last commit that still shipped the JS
classifiers, confirmed byte-identical to the backend at capture time) as the
reference. The backend must continue to reproduce EXACTLY what the retired JS
produced — so a future change to the backend reconcile that would have silently
diverged from the frontend's historical behaviour is still caught.

EQUIVALENCE AXIS — ``cache_prefix_count=0``
-------------------------------------------
The retired JS had NO prompt-cache-prefix guard; the backend has one
(``cache_prefix_count``). The golden was captured at ``cache_prefix_count=0`` —
the documented byte-identical-behaviour default. The LIVE GET path passes the
real prefix count, so the backend deliberately will NOT sweep a buried ghost
sitting inside the immutable cache prefix (a ghost the JS WOULD have swept).
That is a FEATURE (cache-neutrality), pinned by
``test_in_prefix_ghost_divergence_is_prefix_gated`` below.

The compared shape is the APPLIED RESULT (surviving message keys in order +
each survivor's ``finishReason`` + a ``changed`` flag), NOT the raw verdict
string — the backend verdict token is ``'interrupt'`` while the JS token was
``'interrupted'``, but BOTH apply the same mutation (stamp
``finishReason='interrupted'``, keep the message), so the applied output is
identical.
"""

from __future__ import annotations

import copy
import json

import pytest

pytestmark = pytest.mark.unit


# ── The shared corpus (single source of truth) ────────────────────────────
# Each message carries a unique 'k' key so we compare surviving SETS by identity
# (not by content). 'k' is ignored by every reconcile predicate, so it does not
# perturb the verdict.
def _u(k, content='Q'):
    return {'k': k, 'role': 'user', 'content': content}


def _a(k, **kw):
    m = {'k': k, 'role': 'assistant', 'content': ''}
    m.update(kw)
    return m


FIXTURES = [
    # 1. Bare empty tail → delete.
    {'name': 'bare_empty_tail', 'messages': [_u('u0'), _a('a1')]},
    # 2. Thinking-only tail → interrupt (stamp, keep).
    {'name': 'thinking_only_tail', 'messages': [_u('u0'), _a('a1', thinking='I')]},
    # 3. Settled (finishReason+usage) tail → kept, no change.
    {'name': 'settled_tail_finish',
     'messages': [_u('u0'), _a('a1', finishReason='stop', usage={'input_tokens': 5})]},
    # 4. Content tail → kept, no change.
    {'name': 'content_tail',
     'messages': [_u('u0'), _a('a1', content='hi', finishReason='stop')]},
    # 5. The mr3jfcw10pianj shape: 4 buried empties + a settled tail.
    {'name': 'buried_4_ghosts', 'messages': [
        _u('u0'),
        _a('g1', finishReason='aborted', usage={}),  # buried ghost (bodyless badge)
        _a('g2'),                                     # buried ghost
        _a('g3'),                                     # buried ghost
        _a('r1', content='real reply', finishReason='stop'),  # keep
        _u('u1', content='Q2'),                       # keep
        _a('g4'),                                     # buried ghost
        _a('t1', content='tail', finishReason='stop'),  # tail keep
    ]},
    # 6. Buried settled-but-bodyless bubble → swept (buried is more aggressive
    #    than the tail: it removes even a finishReason-bearing empty).
    {'name': 'buried_settled_bodyless', 'messages': [
        _u('u0'),
        _a('g1', finishReason='aborted', usage={'input_tokens': 3}),  # buried → swept
        _u('u1', content='Q2'),
        _a('t1', content='reply', finishReason='stop'),  # tail keep
    ]},
    # 7. Buried SPECIAL turns (VU / endpoint planner) → never swept.
    {'name': 'special_turns_kept', 'messages': [
        _u('u0'),
        _a('vu', _isVirtualUser=True),        # buried special → keep
        _a('ep', _isEndpointPlanner=True),    # buried special → keep
        _a('t1', content='reply', finishReason='stop'),  # tail keep
    ]},
    # 8. Buried real tool round → kept.
    {'name': 'buried_real_round_kept', 'messages': [
        _u('u0'),
        _a('g1', toolRounds=[{'status': 'done', 'toolName': 'run_command'}]),  # keep
        _a('t1', content='reply', finishReason='stop'),
    ]},
    # 9. Buried error envelope NOT superseded (followed by a USER turn, i.e. a
    #    genuine prior-exchange error the user should keep seeing) → kept.
    #    (The SUPERSEDED shape [u, error, real-assistant] is collapsed by the
    #    new pass and is covered by tests/test_reconcile_error_husk_collapse.py,
    #    NOT here — this corpus freezes the RETIRED JS behaviour, which had no
    #    collapse pass, so its fixtures must be collapse-neutral.)
    {'name': 'buried_error_kept', 'messages': [
        _u('u0'),
        _a('g1', error={'kind': 'internal'}),  # keep (renders an error block)
        _u('u1', content='Q2'),                # user turn → error NOT superseded
        _a('t1', content='reply', finishReason='stop'),
    ]},
    # 10. Buried thinking-only → kept (recovered reasoning renders a block).
    {'name': 'buried_thinking_kept', 'messages': [
        _u('u0'),
        _a('g1', thinking='some recovered reasoning'),  # keep
        _a('t1', content='reply', finishReason='stop'),
    ]},
    # 11. _epIteration:0 is NON-special in BOTH implementations (backend
    #     `!=0` clause, JS `0` falsy) → an empty _epIteration:0 buried turn is
    #     swept. Locks that parity edge.
    {'name': 'ep_iteration_zero_nonspecial', 'messages': [
        _u('u0'),
        _a('g1', _epIteration=0),  # non-special empty → buried ghost → swept
        _a('t1', content='reply', finishReason='stop'),
    ]},
    # 12. All buried ghosts then an empty tail → sweep all buried, delete tail.
    {'name': 'all_ghosts_empty_tail', 'messages': [_a('g1'), _a('t1')]},
    # 13. Single empty assistant (no user) → sweep skipped (<2), tail delete.
    {'name': 'single_empty', 'messages': [_a('a1')]},
    # 14. Empty list → no-op.
    {'name': 'empty_list', 'messages': []},
    # 15. Tail with a real tool round but empty content → kept.
    {'name': 'tail_real_round_kept', 'messages': [
        _u('u0'), _a('a1', toolRounds=[{'status': 'searching', 'toolContent': 'out'}])]},
]


# ── FROZEN JS GOLDEN ────────────────────────────────────────────────────────
# Captured by replaying the EXACT retired Case-D sequence (sweep buried ghosts,
# then classify + apply the tail verdict) of the LAST commit that shipped
# ``_sweepBuriedGhostAssistants`` / ``_classifyGhostTail``, over the corpus
# above, and confirmed byte-identical to the backend at capture time. This is
# the historical frontend behaviour the backend must continue to reproduce.
JS_GOLDEN = {
    'bare_empty_tail': {'survivors': ['u0'], 'finish': {'u0': None}, 'changed': True},
    'thinking_only_tail': {'survivors': ['u0', 'a1'], 'finish': {'u0': None, 'a1': 'interrupted'}, 'changed': True},
    'settled_tail_finish': {'survivors': ['u0', 'a1'], 'finish': {'u0': None, 'a1': 'stop'}, 'changed': False},
    'content_tail': {'survivors': ['u0', 'a1'], 'finish': {'u0': None, 'a1': 'stop'}, 'changed': False},
    'buried_4_ghosts': {'survivors': ['u0', 'r1', 'u1', 't1'], 'finish': {'u0': None, 'r1': 'stop', 'u1': None, 't1': 'stop'}, 'changed': True},
    'buried_settled_bodyless': {'survivors': ['u0', 'u1', 't1'], 'finish': {'u0': None, 'u1': None, 't1': 'stop'}, 'changed': True},
    'special_turns_kept': {'survivors': ['u0', 'vu', 'ep', 't1'], 'finish': {'u0': None, 'vu': None, 'ep': None, 't1': 'stop'}, 'changed': False},
    'buried_real_round_kept': {'survivors': ['u0', 'g1', 't1'], 'finish': {'u0': None, 'g1': None, 't1': 'stop'}, 'changed': False},
    'buried_error_kept': {'survivors': ['u0', 'g1', 'u1', 't1'], 'finish': {'u0': None, 'g1': None, 'u1': None, 't1': 'stop'}, 'changed': False},
    'buried_thinking_kept': {'survivors': ['u0', 'g1', 't1'], 'finish': {'u0': None, 'g1': None, 't1': 'stop'}, 'changed': False},
    'ep_iteration_zero_nonspecial': {'survivors': ['u0', 't1'], 'finish': {'u0': None, 't1': 'stop'}, 'changed': True},
    'all_ghosts_empty_tail': {'survivors': [], 'finish': {}, 'changed': True},
    'single_empty': {'survivors': [], 'finish': {}, 'changed': True},
    'empty_list': {'survivors': [], 'finish': {}, 'changed': False},
    'tail_real_round_kept': {'survivors': ['u0', 'a1'], 'finish': {'u0': None, 'a1': None}, 'changed': False},
}


def _normalize(messages, changed):
    return {
        'survivors': [m['k'] for m in messages],
        'finish': {m['k']: m.get('finishReason') for m in messages},
        'changed': bool(changed),
    }


def _backend_results(fixtures, prefix=0):
    from lib.conversations.reconcile import reconcile_conversation_messages
    res = {}
    for fx in fixtures:
        out, changed = reconcile_conversation_messages(
            copy.deepcopy(fx['messages']), prefix)
        res[fx['name']] = _normalize(out, changed)
    return res


def test_backend_matches_frozen_js_golden():
    """Backend == the frozen historical JS golden on every fixture."""
    backend = _backend_results(FIXTURES, prefix=0)
    assert set(backend) == set(JS_GOLDEN)
    mismatches = {name: {'backend': backend[name], 'golden': JS_GOLDEN[name]}
                  for name in backend if backend[name] != JS_GOLDEN[name]}
    assert not mismatches, (
        'backend reconcile DIVERGED from the frozen JS golden on:\n'
        + json.dumps(mismatches, indent=2))


def test_neuter_special_turn_guard_detects_divergence(monkeypatch):
    """NC-1 (teeth): if the backend's buried predicate DROPS the special-turn
    guard, it sweeps the buried VU/planner turns the golden keeps → the
    comparison MUST detect divergence on `special_turns_kept`. Proves the test
    isn't a tautology."""
    import lib.conversations.reconcile as rec

    def _fake_buried(msg):
        # Same as the real predicate MINUS the _is_special_turn guard.
        if not isinstance(msg, dict) or msg.get('role') != 'assistant':
            return False
        if (msg.get('content') or '').strip():
            return False
        if (msg.get('thinking') or '').strip():
            return False
        if msg.get('error'):
            return False
        if rec._has_real_round(msg):
            return False
        return True

    monkeypatch.setattr(rec, 'is_buried_empty_ghost', _fake_buried)
    backend = _backend_results(FIXTURES, prefix=0)
    assert backend['special_turns_kept'] != JS_GOLDEN['special_turns_kept'], (
        'neuter dropped the special-turn guard but the comparison failed to '
        'notice — the test has no teeth')


def test_neuter_thinking_tail_detects_divergence(monkeypatch):
    """NC-2 (teeth): if the backend tail classifier IGNORES `thinking` (deletes
    a thinking-only husk instead of stamping interrupt), it diverges from the
    golden on `thinking_only_tail` → the comparison MUST catch it."""
    import lib.conversations.reconcile as rec
    _orig = rec.classify_ghost_tail

    def _fake_tail(msg):
        v = _orig(msg)
        return 'delete' if v == 'interrupt' else v  # collapse interrupt→delete

    monkeypatch.setattr(rec, 'classify_ghost_tail', _fake_tail)
    backend = _backend_results(FIXTURES, prefix=0)
    assert backend['thinking_only_tail'] != JS_GOLDEN['thinking_only_tail'], (
        'neuter collapsed interrupt→delete but the comparison failed to notice')


def test_in_prefix_ghost_divergence_is_prefix_gated():
    """Constraint #1: a buried ghost INSIDE the cache prefix is NOT swept by the
    backend (cache-neutrality), but IS swept once the prefix is 0. Same input,
    two prefix values — proves the divergence from the golden is purely
    prefix-gated and SELF-HEALS when the prefix resets (server restart / cache
    eviction → next idle GET sweeps it). It is never stranded forever."""
    from lib.conversations.reconcile import reconcile_conversation_messages
    msgs = [_u('u0'),
            _a('g1'),  # buried empty ghost at idx 1
            _a('t1', content='reply', finishReason='stop')]

    # prefix=0 → swept (matches the golden).
    out0, changed0 = reconcile_conversation_messages(copy.deepcopy(msgs), 0)
    assert [m['k'] for m in out0] == ['u0', 't1']
    assert changed0 is True

    # prefix=2 → idx<2 protected → the buried ghost survives (intended,
    # cache-neutral). Nothing swept, settled tail → no change.
    out2, changed2 = reconcile_conversation_messages(copy.deepcopy(msgs), 2)
    assert [m['k'] for m in out2] == ['u0', 'g1', 't1']
    assert changed2 is False


def test_known_whitespace_thinking_tail_divergence():
    """DOCUMENTED, INTENTIONAL non-equivalence (found while checking predicate
    parity): a tail whose `thinking` is WHITESPACE-ONLY.

    Backend ``classify_ghost_tail`` uses ``(thinking or '').strip()`` → '   '
    → falsy → DELETE. The retired JS ``_classifyGhostTail`` used truthy
    ``lastMsg.thinking`` (no trim) → '   ' truthy → INTERRUPTED (keep+stamp).
    The backend is stricter and more correct (whitespace is not real reasoning),
    and since the backend is the AUTHORITATIVE reconciler this is an improvement,
    not a regression. Pinned here so the difference is RECORDED, not hidden —
    this is the one predicate-level divergence in the corpus, and it is on a
    shape that does not occur in practice (reasoning deltas are never pure
    whitespace)."""
    from lib.conversations.reconcile import reconcile_conversation_messages
    fx = [_u('u0'), _a('a1', thinking='   ')]
    out, _changed = reconcile_conversation_messages(copy.deepcopy(fx), 0)
    assert [m['k'] for m in out] == ['u0'], 'backend deletes whitespace-thinking tail'
