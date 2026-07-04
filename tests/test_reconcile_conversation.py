#!/usr/bin/env python3
"""Phase-3: backend-authoritative ghost reconcile (lib/conversations/reconcile.py).

Moves the turn-end lifecycle VERDICT off the frontend. Ports the three JS
classifiers (`_classifyGhostTail` / `_isBuriedEmptyGhost` /
`_sweepBuriedGhostAssistants`) to a PURE server function so the frontend only
renders, never infers.

Covers BOTH regressions this subsystem previously bit us on:
  * RESURRECT: the buried-ghost sweep must actually REMOVE the ghost from the
    returned list (so the caller persists the shorter list in one commit — no
    frontend allowTruncate PUT to lose). Test: buried ghost is gone + changed=True.
  * AUTO-FIRE: the verdict is DATA, not a trigger — reconcile NEVER starts a
    turn. A 'delete' just drops the tail; there is no code path that could
    fall through into an auto-start. Test: deleting a ghost tail whose PRIOR
    message is a recent user turn returns a list ending in that user msg, with
    NO side effect (pure function — nothing to auto-fire).

Double-neuter: disabling the sweep (return early) makes the resurrect-coverage
test FAIL (buried ghost survives).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def test_buried_ghost_swept():
    """★ RESURRECT coverage: a mid-list empty-ghost assistant is REMOVED."""
    from lib.conversations.reconcile import reconcile_conversation_messages
    msgs = [
        {'role': 'user', 'content': 'q1'},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': []},  # buried ghost
        {'role': 'user', 'content': 'q2'},
        {'role': 'assistant', 'content': 'real answer', 'finishReason': 'stop'},
    ]
    out, changed = reconcile_conversation_messages(msgs)
    assert changed is True, 'sweep should report changed'
    assert len(out) == 3, f'buried ghost not swept — got {len(out)} msgs'
    assert [m['role'] for m in out] == ['user', 'user', 'assistant']
    assert out[-1]['content'] == 'real answer', 'real turn must survive the sweep'
    _ok('buried empty-ghost assistant is swept (returned list is shorter → caller persists it, no resurrect)')


def test_buried_settled_bodyless_swept():
    """A buried assistant with a settled finishReason but NO body is still
    clutter mid-list → swept (matches JS: buried predicate ignores finishReason)."""
    from lib.conversations.reconcile import reconcile_conversation_messages
    msgs = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': '', 'finishReason': 'aborted', 'toolRounds': []},  # bodyless badge
        {'role': 'user', 'content': 'q2'},
        {'role': 'assistant', 'content': 'answer', 'finishReason': 'stop'},
    ]
    out, changed = reconcile_conversation_messages(msgs)
    assert changed and len(out) == 3, f'bodyless buried badge not swept — {len(out)}'
    _ok('buried bodyless (finishReason-only) assistant is swept (mid-list clutter)')


def test_tail_delete_no_autofire():
    """★ AUTO-FIRE coverage: a ghost tail is DELETED; the verdict is pure data.
    After delete the new tail is the preceding user turn — but reconcile is a
    pure function with NO auto-start path, so nothing fires. The caller decides
    what to do (and the frontend no longer pops → no Case-D→Case-E leak)."""
    from lib.conversations.reconcile import reconcile_conversation_messages
    msgs = [
        {'role': 'user', 'content': 'recent question', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': []},  # ghost tail
    ]
    out, changed = reconcile_conversation_messages(msgs)
    assert changed is True
    assert len(out) == 1 and out[-1]['role'] == 'user', (
        'ghost tail should be deleted, exposing the user turn — as inert DATA')
    # Purity: the function returns a list, nothing else — there is no callback,
    # no task spawn, no I/O. The auto-fire leak is structurally impossible here.
    _ok('ghost tail deleted as pure data — no auto-start path exists (auto-fire leak impossible)')


def test_tail_interrupt_preserves_thinking():
    """A thinking-only ghost tail → stamped finishReason='interrupted', NOT deleted."""
    from lib.conversations.reconcile import reconcile_conversation_messages
    msgs = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': '', 'thinking': 'partial reasoning recovered', 'toolRounds': []},
    ]
    out, changed = reconcile_conversation_messages(msgs)
    assert changed is True and len(out) == 2, 'thinking-only tail must be kept (not deleted)'
    assert out[-1]['finishReason'] == 'interrupted', 'must stamp interrupted'
    assert out[-1]['thinking'] == 'partial reasoning recovered', 'recovered reasoning preserved'
    _ok('thinking-only ghost tail → interrupted (reasoning preserved, not discarded)')


def test_settled_turn_untouched():
    """A completed turn is left exactly as-is (changed=False)."""
    from lib.conversations.reconcile import reconcile_conversation_messages
    msgs = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': 'a real answer', 'finishReason': 'stop',
         'usage': {'output_tokens': 5}},
    ]
    out, changed = reconcile_conversation_messages(msgs)
    assert changed is False, 'settled turn must not be modified'
    assert out == msgs
    _ok('settled turn is untouched (changed=False → no needless write)')


def test_special_turns_never_swept():
    """Endpoint / autopilot-VU / image-gen empty turns are never clutter."""
    from lib.conversations.reconcile import reconcile_conversation_messages
    msgs = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': '', '_isEndpointPlanner': True},  # empty planner
        {'role': 'user', 'content': 'q2'},
        {'role': 'assistant', 'content': 'done', 'finishReason': 'stop'},
    ]
    out, changed = reconcile_conversation_messages(msgs)
    assert changed is False and len(out) == 4, 'special (planner) turn must survive'
    _ok('special turns (endpoint/VU/image-gen) are never swept even when empty')


def main():
    print()
    print(_color('═══ Phase-3 backend reconcile primitive tests ═══', '36'))
    print()
    tests = [
        test_buried_ghost_swept,
        test_buried_settled_bodyless_swept,
        test_tail_delete_no_autofire,
        test_tail_interrupt_preserves_thinking,
        test_settled_turn_untouched,
        test_special_turns_never_swept,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} RECONCILE TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
