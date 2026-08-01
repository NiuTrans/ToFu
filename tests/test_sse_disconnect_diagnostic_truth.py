#!/usr/bin/env python3
"""Regression: the SSE premature-disconnect diagnostic must not lie.

WHY (forensics, 2026-07-31 — epic pt_8a2f741ee4634cdc)
------------------------------------------------------
The epic was filed on this reading of the logs::

    SSE stream <id> DISCONNECTED PREMATURELY — 0 events sent in 0.1s,
    task status=running

…plus "``/api/chat/stream`` hits = 0" in access.log, concluding that EVERY
chat task's SSE dies instantly and 100% of traffic has degraded to the 1 Hz
poll fallback. **Both halves of that premise are false**, and the reason they
looked true is TWO diagnostic defects in the server. This suite pins the fix
for both, so the log can be trusted as evidence next time.

FALSIFICATION 1 — "``/api/chat/stream`` hits = 0" is a LOGGING artifact.
access.log is written by ``hypercorn.access``, which logs on RESPONSE
COMPLETION. An SSE stream is held open (``_MAX_SSE_DURATION = 7200``), so a
live stream has no access line until it closes — and a long one that outlives
the log window never appears at all. Measured: the string ``chat/stream``
appears **0 times in every access log ever rotated**, while ``routes/chat.py``
concurrently logs disconnects FOR THOSE SAME TASKS — proving the handler ran.
The client always sent the request.

FALSIFICATION 2 — SSE is NOT dead. Measured on the same day, the same task
``3ad51ceb`` produced BOTH::

    ev=0   t=0.2s     ← the "instant death" the epic was filed on
    ev=85  t=985.6s   ← the same task streaming happily for 16 minutes

and other tasks reached ev=797 / t=3333s. Of the 12 zero-event closes, **11
carry a ``superseded by newer reader (gen N→N+1)`` line in the same second**
(the 12th "match" is this epic's own board text, not an event).

THE TWO REAL DEFECTS (both fixed here)
--------------------------------------
1. ``_events_sent`` UNDERCOUNTS. The fresh-connection path yields the full
   state snapshot and then does NOT increment the counter, while the
   warm-resume path DOES increment for its equivalent frame. So a connection
   that correctly delivered a complete state snapshot reports "0 events sent"
   — which is what made the fleet look dead.

2. A DELIBERATE mechanism is logged as a FAILURE. ``next_live_tick`` returns
   ``kind='superseded'`` when a newer reader for the same task takes over
   (reconnect / second tab). That is the supersede design working; the old
   reader SHOULD close. But it fell into the ``_events_sent == 0`` branch and
   was reported at WARNING with "Client may lose data if poll fallback fails!"
   — alarming, and untrue: the newer reader owns the stream.

Net effect of the pair: a healthy, working transport that reported itself as
catastrophically broken, which is exactly how this epic came to be filed.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
        tests/test_sse_disconnect_diagnostic_truth.py -q
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CHAT_PY = os.path.join(ROOT, 'routes', 'chat.py')


def _chat_src() -> str:
    return open(CHAT_PY, encoding='utf-8').read()


def _generate_body(src: str) -> str:
    """Slice the ``async def generate():`` body (up to the next top-level def)."""
    start = src.index('    async def generate():')
    end = src.index('    async def generate_with_disconnect_log():', start)
    return src[start:end]


# ─────────────────────────────────────────────────────────────────────────
# DEFECT 1 — the fresh state snapshot must be counted
# ─────────────────────────────────────────────────────────────────────────

def test_fresh_state_snapshot_is_counted_as_an_event():
    """The fresh-connection state snapshot is a real delivered frame.

    It carries the entire accumulated content/thinking/toolRounds — for a
    reconnect it is the single most valuable frame on the wire. Yielding it
    without counting it is what produced the "0 events sent" verdict on
    connections that had in fact delivered a full snapshot.

    The warm-resume path already counts its equivalent frame, so this is also
    an internal-consistency fix: two sibling paths, one counter, same rule.
    """
    body = _generate_body(_chat_src())
    # The fresh snapshot yield must be followed by its counter increment.
    # Comment lines are allowed between them (the WHY of the fix lives there);
    # what must NOT appear in between is another yield — that would mean the
    # increment belongs to a different frame.
    m = re.search(
        r"yield f'data: \{_state_payload\}\\n\\n'"
        r"(?:\s*\n\s*#[^\n]*)*"
        r"\s*\n\s*_events_sent \+= 1",
        body)
    assert m, (
        'the fresh-connection state snapshot is yielded WITHOUT incrementing '
        '_events_sent. A connection that delivered a full state snapshot then '
        'reports "0 events sent" and is logged as a premature-disconnect '
        'FAILURE — the false signal this epic was filed on.')


def test_both_state_snapshot_paths_count_symmetrically():
    """Warm-resume and fresh-connection must not disagree about what counts.

    The asymmetry (warm counts, fresh does not) is the specific bug; asserting
    BOTH sides keeps a future edit from "fixing" it by removing the warm
    increment instead.
    """
    body = _generate_body(_chat_src())
    assert re.search(
        r"yield f'data: \{_resume_state_payload\}\\n\\n'\s*\n\s*_events_sent \+= 1",
        body), 'the warm-resume state frame lost its _events_sent increment'


# ─────────────────────────────────────────────────────────────────────────
# DEFECT 2 — a deliberate supersede must not be logged as data loss
# ─────────────────────────────────────────────────────────────────────────

def test_supersede_is_tracked_so_it_can_be_logged_honestly():
    """The driver must remember that it closed because a NEWER READER took
    over, so the teardown log can tell that apart from a real client drop."""
    src = _chat_src()
    assert '_superseded' in src, (
        "routes/chat.py does not track the supersede outcome — the teardown "
        "logger cannot distinguish 'a newer reader owns this stream now' "
        "(by design) from 'the client vanished and may lose data'.")
    assert re.search(r"if _tick\.kind == 'superseded':"
                     r"(?:\s*\n\s*#[^\n]*)*"
                     r"\s*\n\s*_superseded = True", src), (
        "the superseded branch does not record itself; it will keep falling "
        'into the zero-event WARNING branch.')
    # The flag is assigned inside the generate() closure but declared in the
    # enclosing request scope — without `nonlocal` the assignment silently
    # creates a LOCAL and the teardown logger never sees it (a no-op fix that
    # still reads correct). Caught exactly this way while writing the patch.
    assert re.search(r'nonlocal [^\n]*_superseded', src), (
        '_superseded is assigned inside generate() but not declared nonlocal '
        '— the assignment creates a function-local and the teardown logger '
        'always sees False, so the fix is inert while looking correct.')


def test_supersede_does_not_warn_about_losing_data():
    """A superseded reader must NOT be reported at WARNING with the
    'Client may lose data' phrasing.

    It is the supersede design working as intended: the newer reader owns the
    stream and will deliver everything. Logging it as a failure is what made a
    healthy transport look 100% broken, and it is the same class of defect this
    project keeps hitting — a message that asserts something no longer true.
    """
    src = _chat_src()
    # The supersede case must be handled BEFORE the zero-event warning.
    warn_idx = src.index('DISCONNECTED PREMATURELY')
    sup_log = re.search(
        r"if _superseded:.*?logger\.info\(", src[:warn_idx], re.S)
    assert sup_log, (
        'the teardown logger has no dedicated superseded branch ahead of the '
        'DISCONNECTED PREMATURELY warning — a deliberate handover is still '
        'reported as a premature disconnect.')


def test_zero_event_warning_still_fires_for_a_genuine_drop():
    """COMPLEMENT: the warning must survive for the case it was written for.

    Without this, both defects could be "fixed" by deleting the diagnostic —
    which would be worse than the false alarm, since a real stalled stream
    would then be invisible.
    """
    src = _chat_src()
    assert 'DISCONNECTED PREMATURELY' in src, (
        'the premature-disconnect diagnostic was deleted rather than made '
        'accurate — a genuinely dead stream would now be silent.')
    assert re.search(r'if _events_sent == 0:\s*\n\s*logger\.warning\(', src), (
        'the zero-event case no longer warns; a real "opened but delivered '
        'nothing" stream would be logged as routine.')


def test_access_log_blindness_is_documented_where_it_misleads():
    """The forensic trap must be written down at the place that causes it.

    `/api/chat/stream` never appears in access.log because hypercorn logs on
    response COMPLETION and SSE responses are long-lived. Anyone grepping
    access.log for stream traffic concludes "the client never sent it" — the
    exact wrong turn this epic took. The note has to live in the source, not
    only in a commit message.
    """
    src = _chat_src()
    assert 'access.log' in src, (
        'routes/chat.py does not warn that SSE requests are absent from '
        'access.log until the stream closes — the next investigator will '
        're-derive the same false conclusion.')
