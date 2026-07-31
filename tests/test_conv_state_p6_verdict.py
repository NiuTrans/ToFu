#!/usr/bin/env python3
"""pt_conv_state_ssot P6 VERDICT GUARD — the three branches must NOT be deleted.

WHY THIS FILE EXISTS
--------------------
P6 as originally scoped says "sweep 3 redundant branches, each deletion
commit-body naming which new frame replaces its old scenario" (owner hard
constraint #5). Working it produced the opposite finding: **there is no such
frame for any of the three**, so the sweep must not happen. This guard pins
that verdict in executable form, because the epic text still reads like a
deletion task and the next session to pick it up would otherwise re-derive
(or worse, skip) the analysis and delete live safety nets.

THE ARGUMENT, IN ONE LINE
-------------------------
P2 BIFURCATED busy state (owner hard constraint #2):

    conv.activeTaskId                 -> LOCAL OPTIMISTIC   (this tab's send)
    conv._authoritativeActiveTaskIds  -> SERVER AUTHORITATIVE (frames only)

``computeConvBusy`` reads the UNION. The new conv-state channel — notify
frames + connect snapshot — writes and clears ONLY the authoritative half.
Nothing in the new channel can clear a stale LOCAL OPTIMISTIC pin. The three
"redundant" branches are precisely what covers that half plus the offline
case, so deleting them re-opens the symptom the epic was created to fix
("sidebar busy dot outlives the work" / phone shows N generating, PC fewer).

WHY THE task_ids DRIFT EVIDENCE DOES NOT AUTHORIZE DELETION
------------------------------------------------------------
The P5 probe's ``taskIds`` dimension reads ``_authoritativeActiveTaskIds``.
Sustained zero divergence there proves the AUTHORITATIVE half converges — it
says nothing about the optimistic half. Using it as the admission criterion
for deleting an optimistic-half safety net is a category error, and was the
plan until this analysis. Measured: 8 consecutive probe cycles, zero
``kind=task_ids`` divergences, while the optimistic-pin sweep remained
load-bearing.

Run: python3 tests/test_conv_state_p6_verdict.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CROSS_TAB = os.path.join(REPO, 'static', 'js', 'core', 'cross_tab_sync.js')
HEALTH = os.path.join(REPO, 'static', 'js', 'core', 'health_stream_timer.js')
REDUCER = os.path.join(REPO, 'static', 'js', 'core', 'conv_state_reducer.js')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _source_scan import js_function_body, strip_comments  # noqa: E402

try:
    import pytest
except ImportError:
    pytest = None


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _strip_comments(src):
    """Delegate to the SINGLE shared implementation (charter #24).

    This used to be a local regex pair. That is the exact duplication the
    shared module exists to prevent — incident 3 in its docstring is a ratchet
    that was fixed in one copy and stayed broken in the other. Kept as a thin
    alias only so the call sites below read unchanged.
    """
    return strip_comments(src, lang='js')


#: Assignment TO the server-authoritative set — the thing the sweep must never
#: do. Matches ``x._authoritativeActiveTaskIds = …`` and its compound/delete
#: forms, but NOT a read such as ``const b = conv._authoritativeActiveTaskIds``.
#:
#: The distinction is the whole point (see the guard below): the sweep is
#: REQUIRED to read that set — carrier-aware liveness depends on it — and
#: forbidden to write it.
_AUTHORITATIVE_WRITE_RE = re.compile(
    r'(?:delete\s+\w+\.)?_authoritativeActiveTaskIds\w*\s*'
    r'(?:=(?!=)|\+=|\|\|=|\?\?=)'
    r'|\._authoritativeActiveTaskIds\w*\.(?:add|delete|clear)\s*\(')


# ── Branch 1: the conv-agnostic stale optimistic-pin sweep ───────────────

@_unit
def test_stale_pin_sweep_is_defined_AND_actually_invoked():
    """Both must hold. A definition with no call site is a silent hole.

    (An earlier pass mis-reported this function as uncalled dead code after a
    grep whose parenthesis was swallowed. Asserting BOTH facts here makes that
    mistake impossible to repeat.)
    """
    code = _strip_comments(_read(CROSS_TAB))
    assert 'function _reconcileStuckActiveTaskPins' in code, (
        'the optimistic-pin sweep was DELETED — P6 verdict is that it must '
        'stay; see this file\'s docstring')
    # A call is any occurrence that is not the definition itself.
    calls = [m for m in re.finditer(r'_reconcileStuckActiveTaskPins\s*\(', code)
             if not code[:m.start()].rstrip().endswith('function')]
    assert calls, (
        'the sweep is defined but never invoked — the background orphan-pin '
        'lane is silently unguarded')


@_unit
def test_sweep_clears_the_OPTIMISTIC_field_not_the_authoritative_one():
    """This is the whole reason the branch is not redundant.

    ★ TWO DEFECTS IN THIS GUARD'S OWN HISTORY (pt_852527ce031e4f93), both of
    which made it accuse innocent code:

    1. IT SCANNED RAW TEXT (charter #24 violation, incident 4 of that family).
       The function's design comment legitimately names the authoritative set
       while explaining why it reads the reducer's output instead of re-parsing
       the wire. Prose was therefore able to VIOLATE the guard — the mirror of
       the vacuous-guard failure, and just as misleading.

    2. IT FORBADE ANY MENTION, WHEN THE INVARIANT IS ABOUT WRITES.
       Stripping comments alone does NOT make this pass, and measuring that is
       what corrected the fix: the sweep contains a live
       ``const _busy = conv._authoritativeActiveTaskIds;``. That read is not a
       violation, it is REQUIRED — ``/api/v1/chat/active`` hides autopilot VU
       carriers by design, so without the reducer's carrier-inclusive busy set
       the sweep is carrier-blind and stamps ``interrupted`` on a turn the
       backend is still generating (the d6e8bdb3 defect).

    Hard constraint #3 says the sweep must not become a second WRITER of
    server-owned state. So the assertion is narrowed to writes, which is both
    what the constraint says and what leaves the required read legal.
    """
    body = js_function_body(_read(CROSS_TAB), '_reconcileStuckActiveTaskPins')
    assert 'activeTaskId' in body, (
        'the sweep must operate on the LOCAL OPTIMISTIC pin (conv.activeTaskId)')

    writes = _AUTHORITATIVE_WRITE_RE.findall(body)
    assert not writes, (
        'the sweep WRITES the server-authoritative set, making it a second '
        'writer of server-owned state (hard constraint #3). Reading it is '
        'required and fine; assigning to it is not. Matches: %r' % (writes,))

    # The required READ must still be present: losing it silently restores the
    # carrier-blind sweep that stamped `interrupted` on live VU turns.
    assert '_authoritativeActiveTaskIds' in body, (
        'the sweep no longer consults the reducer\'s carrier-inclusive busy '
        'set — it is carrier-blind again and will stamp `interrupted` on an '
        'autopilot VU turn the backend is still generating (d6e8bdb3)')


# ── Branch 2: the shared background reclaim it delegates into ────────────

@_unit
def test_background_orphan_reclaim_still_present():
    code = _read(HEALTH)
    assert 'Background orphan-pin clear' in code, (
        'the background reclaim arm was removed; the sweep delegates into it, '
        'so deleting it guts branch 1 as well')
    assert 'probe.background' in _strip_comments(code), (
        'the background gate is what keeps the FOREGROUND timer path '
        'byte-identical — losing it changes live-stream behaviour')


# ── Branch 3: offline recovery polling ──────────────────────────────────

@_unit
def test_offline_recovery_polling_still_present():
    code = _strip_comments(_read(CROSS_TAB))
    assert 'function _startOfflineRecoveryPolling' in code, (
        'offline recovery polling was deleted — when the server is offline the '
        'new frame channel cannot deliver either, so nothing replaces it')


# ── The load-bearing asymmetry itself ───────────────────────────────────

@_unit
def test_new_channel_clears_only_the_authoritative_half():
    """Pins the fact the whole verdict rests on.

    If a future change makes the snapshot clear ``activeTaskId`` too, THEN the
    branches genuinely become redundant and P6 can be revisited — this test
    failing is the signal to re-open that question, not to delete the guard.

    Same two corrections as the sibling guard above: brace-matched body instead
    of a blind 4000-byte window (measured: that window overshot
    ``applyConvStateSnapshot``'s real 3391-byte body by 609 bytes and swallowed
    the next declaration), and comments stripped so prose can neither satisfy
    nor violate it. This one had not yet gone red — it was the same latent bug
    waiting for the neighbouring code to mention ``conv.activeTaskId =``.
    """
    body = js_function_body(_read(REDUCER), 'applyConvStateSnapshot')
    assert '_authoritativeActiveTaskIds = new Set()' in body, (
        'snapshot clear semantics changed; re-derive the P6 verdict')
    assert not re.search(r'\bconv\.activeTaskId\s*=(?!=)', body), (
        'the snapshot now writes the OPTIMISTIC pin. That would change the P6 '
        'verdict (branches may become redundant) AND violate hard constraint '
        '#2 (fields never merge). Re-open the analysis deliberately.')


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('ok  ', name)
            except AssertionError as e:
                failures += 1
                print('FAIL', name)
                print('     ', e)
    print('ALL PASSED' if not failures else f'{failures} FAILED')
    sys.exit(1 if failures else 0)
