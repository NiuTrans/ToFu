#!/usr/bin/env python3
"""The conversation must READ AS BUSY while its autopilot VU turn runs.

WHY (production incident, conv ms34u49egqwhug, 2026-07-27)
----------------------------------------------------------
The user opened a conversation whose autopilot was mid-flight. The backend was
demonstrably working (the VU carrier ran 8 LLM rounds over ~7 minutes), yet the
sidebar dot and the composer both showed IDLE — "generation complete" — so the
user believed autopilot was stuck and could neither watch nor stop it.

Root cause is NOT "the carrier is filtered from a snapshot". It is that ONE
FACT has THREE READERS and only one of them honours it:

    parent task['status'] = 'done'      (orchestrator/_finalize.py, at the
                                         terminal flip)
        ...but the SAME synchronous call stack then runs maybe_run_autopilot()
        which executes the whole VU turn (tens of seconds to minutes) BEFORE
        the done event is ever appended.

  reader 1  SSE live-tick (lib/chat_dispatch.py) — HONOURS it, via the
            ``_finalize_started_at`` latch: holds the LATE-done so the stream
            stays open across the VU window. This is why the stream works.
  reader 2  the busy projection (snapshot_running_by_conv → notify_conv_changed
            → sidebar dot + composer Send/Stop) — does NOT honour it.
  reader 3  the reconnect view (/api/chat/active) — does NOT honour it
            (separate concern: discoverability, tracked separately).

So during the VU window BOTH candidate tasks drop out of the busy set, for two
DIFFERENT reasons, and the set is empty:

    parent  status == 'done'      -> excluded by the status filter
    carrier _vu_subtask == True   -> excluded by the carrier filter

This suite pins the OUTCOME, not the mechanism (charter 2026-07-27: behaviour
guards MUST assert results, so they keep biting after a reasonable reimpl):

    "While a VU carrier is running for conv X, the authoritative busy signal
     the client receives for conv X is non-empty."

It deliberately asserts through the PUBLIC seam — the ``notify_conv_changed``
push payload the client actually consumes — and NOT on
``snapshot_running_by_conv`` returning a particular task id. Any fix that makes
the client see the conversation as busy satisfies it; no particular internal
shape is mandated.

NEUTER (must bite): drop the finalize/VU-window term from the busy predicate
→ ``test_busy_signal_nonempty_during_vu_window`` goes red.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

CONV = 'conv-vu-busy'


@pytest.fixture
def captured(monkeypatch):
    """Capture the outbound notify frames (the wire the client reads)."""
    frames = []

    def _fake_push_event(channel, task_id, payload):
        frames.append({'channel': channel, 'taskId': task_id, 'payload': payload})

    import lib.agent_core.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event', _fake_push_event)
    return frames


@pytest.fixture
def vu_window():
    """Install the EXACT production registry shape of the VU window.

    Reproduces the ms34u49egqwhug state at the moment the user looked:

      * parent  — already flipped to ``status='done'`` by the terminal flip,
                  and its finalize latch is ALREADY STALE: the user opened the
                  conversation ~3 MINUTES into the VU window (the real turn ran
                  8 rounds over ~7 minutes). Using a stale latch here is
                  deliberate and load-bearing — a fixture with a FRESH latch
                  passes against a naive time-bounded fix while production
                  still shows an idle sidebar, which is exactly the false-green
                  this suite must not produce.
      * carrier — the VU sub-task, genuinely ``status='running'``, marked
                  ``_vu_subtask`` + ``_inline_messages``, registered under the
                  REAL convId (the pt_8dc03017 cutover).

    Yields the two task ids, then removes both from the shared registry.
    """
    from lib.tasks_pkg.manager._state import tasks as reg, tasks_lock as lock

    now = time.time()
    parent_id = 'tid-parent-vu'
    carrier_id = 'tid-carrier-vu'

    parent = {
        'id': parent_id, 'convId': CONV,
        # The terminal flip already happened (orchestrator/_finalize.py) ...
        'status': 'done', 'aborted': False,
        # ... and finalize has been running for 3 MINUTES (real incident
        # timing). Any fix that only tolerates a short fixed window is
        # still broken here — the VU turn is the long pole, by design.
        '_finalize_started_at': now - 180,
        '_vu_carrier_id': carrier_id,
        'created_at': now - 400, '_t_last_event': now - 180,
        '_dispatch_heartbeat': now - 180,
    }
    carrier = {
        'id': carrier_id, 'convId': CONV,
        'status': 'running', 'aborted': False,
        '_vu_subtask': True, '_inline_messages': True,
        '_autopilotParent': parent_id,
        'created_at': now - 180, '_t_last_event': now,
        '_dispatch_heartbeat': now,
    }
    with lock:
        reg[parent_id] = parent
        reg[carrier_id] = carrier
    try:
        yield parent_id, carrier_id
    finally:
        with lock:
            reg.pop(parent_id, None)
            reg.pop(carrier_id, None)


def _busy_ids_for(frames, conv_id):
    """The busy set the CLIENT ends up with for ``conv_id`` — read off the wire."""
    for f in reversed(frames):
        p = f.get('payload') or {}
        if p.get('convId') == conv_id and p.get('type') == 'conv_changed':
            return p.get('runningTaskIds', [])
    return None


# ─────────────────────────────────────────────────────────────────────
#  THE OUTCOME (this is the whole point of the suite)
# ─────────────────────────────────────────────────────────────────────
def test_busy_signal_nonempty_during_vu_window(captured, vu_window):
    """While the VU turn runs, the client MUST be told the conv is busy.

    This is the user-visible contract: sidebar dot lit, composer offering
    Stop rather than Send. Asserted as a RESULT — "the busy list is not
    empty" — so any correct implementation passes and the mechanism stays
    free to change.
    """
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed(CONV, rev=None)

    busy = _busy_ids_for(captured, CONV)
    assert busy is not None, 'no conv_changed frame reached the client at all'
    assert busy, (
        'the conversation reads as IDLE while its autopilot VU turn is '
        'actively running — this is the ms34u49egqwhug incident: the sidebar '
        'and composer showed "generation complete" for ~7 minutes of real '
        'backend work. The parent is status=done (terminal flip already ran) '
        'AND the carrier is filtered as _vu_subtask, so both candidates drop '
        'out and the busy set is empty. The SSE reader already honours this '
        'window via task["_finalize_started_at"]; the busy projection must '
        'honour the same fact.')


def test_busy_signal_survives_a_long_vu_turn(captured):
    """A VU turn running for MINUTES must keep the conv busy the whole time.

    The production turn ran 8 LLM rounds over ~7 minutes. A fix bounded by a
    fixed wall-clock window (e.g. the 30s ceiling the SSE reader uses on the
    finalize latch) would report busy for the first seconds and then go idle
    mid-turn — reproducing the incident for any user who looks a minute in.
    The window must therefore be bounded by the CARRIER'S LIVENESS, not by a
    timeout.
    """
    from lib.tasks_pkg.manager._state import tasks as reg, tasks_lock as lock
    from lib.conversations.meta_cache import notify_conv_changed

    now = time.time()
    parent_id, carrier_id = 'tid-p-long', 'tid-c-long'
    with lock:
        reg[parent_id] = {
            'id': parent_id, 'convId': 'conv-long-vu',
            'status': 'done', 'aborted': False,
            # 7 minutes into finalize — far outside any sane fixed ceiling.
            '_finalize_started_at': now - 420,
            '_vu_carrier_id': carrier_id,
            'created_at': now - 600,
        }
        reg[carrier_id] = {
            'id': carrier_id, 'convId': 'conv-long-vu',
            'status': 'running', 'aborted': False,
            '_vu_subtask': True, '_inline_messages': True,
            'created_at': now - 420,
        }
    try:
        notify_conv_changed('conv-long-vu', rev=None)
        busy = _busy_ids_for(captured, 'conv-long-vu')
        assert busy, (
            'the conv went IDLE part-way through a long VU turn — the busy '
            'window must end when the carrier is discarded, not when an '
            'arbitrary timer expires (the real turn ran ~7 minutes)')
    finally:
        with lock:
            reg.pop(parent_id, None)
            reg.pop(carrier_id, None)


def test_busy_without_attachable_id_is_not_wire_idle(captured):
    """'Busy, no attachable worker' must NOT look identical to 'idle' on the wire.

    The client treats an EMPTY runningTaskIds list as idle
    (computeConvBusy → ``_authoritativeActiveTaskIds.size > 0``). So a conv
    whose ONLY live worker is a VU carrier (not independently reconnectable)
    must surface something non-empty — a marker the reducer strips into the
    busy Set — never an empty list. This is the exact shape the owner's
    report hit: the backend fact was fixed but the wire still read idle.
    """
    from lib.tasks_pkg.manager._state import tasks as reg, tasks_lock as lock
    from lib.conversations.meta_cache import notify_conv_changed

    now = time.time()
    carrier_id = 'tid-carrier-marker'
    with lock:
        reg[carrier_id] = {
            'id': carrier_id, 'convId': 'conv-marker',
            'status': 'running', 'aborted': False,
            '_vu_subtask': True, '_inline_messages': True,
            'created_at': now,
        }
    try:
        notify_conv_changed('conv-marker', rev=None)
        busy = _busy_ids_for(captured, 'conv-marker')
        assert busy, (
            'a conv whose only worker is a VU carrier emitted an EMPTY busy '
            'list — on the wire that is indistinguishable from idle, which is '
            'precisely the incident shape. Busy and idle must not look the '
            'same to the client.')
        # And the non-attachable nature must be MARKED, not a bare id that a
        # reconnect would try (and hang) on.
        assert any(isinstance(x, str) and x.endswith('#vu') for x in busy), (
            'the carrier id must carry a non-attachable marker so the '
            'reducer can light the dot without offering a dangling attach '
            'target')
    finally:
        with lock:
            reg.pop(carrier_id, None)


def test_busy_signal_nonempty_when_only_the_carrier_survives(captured):
    """The PRODUCTION shape: parent gone, only the VU carrier left in the registry.

    The parent leaves the registry once its finalize returns; the VU turn
    outlives it by minutes. Anchoring busy-ness on a parent ``_vu_carrier_id``
    back-pointer evaporates exactly during the window it must cover. The busy
    fact must therefore be anchored on the CARRIER itself.
    """
    from lib.tasks_pkg.manager._state import tasks as reg, tasks_lock as lock
    from lib.conversations.meta_cache import notify_conv_changed

    now = time.time()
    carrier_id = 'tid-carrier-alone'
    with lock:
        reg[carrier_id] = {
            'id': carrier_id, 'convId': 'conv-carrier-alone',
            'status': 'running', 'aborted': False,
            '_vu_subtask': True, '_inline_messages': True,
            '_autopilotParent': 'tid-parent-EVICTED',
            'created_at': now - 200,
        }
    try:
        notify_conv_changed('conv-carrier-alone', rev=None)
        busy = _busy_ids_for(captured, 'conv-carrier-alone')
        assert busy, (
            'the parent is gone and only the VU carrier survives, yet the '
            'conv reads IDLE — this is the production path, not an edge case')
        assert any(isinstance(x, str) and x.endswith('#vu') for x in busy)
    finally:
        with lock:
            reg.pop(carrier_id, None)


def test_busy_signal_empty_when_conv_is_genuinely_idle(captured):
    """Control: a conv with NO live work must still read as idle.

    Without this, "always report busy" would trivially satisfy the test
    above. This is the discriminating half — it proves the signal carries
    information rather than being pinned on.
    """
    from lib.conversations.meta_cache import notify_conv_changed
    notify_conv_changed('conv-genuinely-idle', rev=None)

    busy = _busy_ids_for(captured, 'conv-genuinely-idle')
    assert busy == [], (
        'an idle conversation must report an EMPTY busy set — otherwise the '
        'busy dot can never be dismissed')


def test_busy_signal_clears_once_the_vu_window_closes(captured):
    """The window is bounded: once finalize is over and the carrier is gone,
    the conv goes back to idle.

    The production failure mode this rules out is the mirror image of the
    incident — a dot that lights correctly but then never extinguishes,
    which is exactly what ``_endpoint_managed`` carriers caused before the
    terminal-row fix (pt_8a491f9d). Here the carrier has been discarded
    (as ``run_virtual_user``'s finally does) and the latch is stale.
    """
    from lib.tasks_pkg.manager._state import tasks as reg, tasks_lock as lock
    from lib.conversations.meta_cache import notify_conv_changed

    now = time.time()
    parent_id = 'tid-parent-settled'
    # Finalize long over (latch far in the past), carrier already discarded.
    parent = {
        'id': parent_id, 'convId': 'conv-settled',
        'status': 'done', 'aborted': False,
        '_finalize_started_at': now - 3600,
        'created_at': now - 3600,
        '_t_last_event': now - 3600, '_dispatch_heartbeat': now - 3600,
    }
    with lock:
        reg[parent_id] = parent
    try:
        notify_conv_changed('conv-settled', rev=None)
        busy = _busy_ids_for(captured, 'conv-settled')
        assert busy == [], (
            'a settled conversation still reads as BUSY — a stale '
            'finalize latch must not pin the dot on forever (the window is '
            'bounded; the SSE reader caps the same latch at 30s)')
    finally:
        with lock:
            reg.pop(parent_id, None)


def test_aborted_carrier_does_not_hold_the_conv_busy(captured):
    """A carrier the user STOPPED must extinguish the dot immediately.

    Abort flips ``aborted=True`` while status can still read 'running' for a
    moment; the busy signal must follow the abort, not the status, so the
    composer returns to Send the instant Stop is pressed.
    """
    from lib.tasks_pkg.manager._state import tasks as reg, tasks_lock as lock
    from lib.conversations.meta_cache import notify_conv_changed

    now = time.time()
    carrier_id = 'tid-carrier-aborted'
    carrier = {
        'id': carrier_id, 'convId': 'conv-aborted',
        'status': 'running', 'aborted': True,
        '_vu_subtask': True, '_inline_messages': True,
        'created_at': now, '_t_last_event': now, '_dispatch_heartbeat': now,
    }
    with lock:
        reg[carrier_id] = carrier
    try:
        notify_conv_changed('conv-aborted', rev=None)
        busy = _busy_ids_for(captured, 'conv-aborted')
        assert busy == [], (
            'an ABORTED VU carrier must not keep the conversation busy — '
            'the user pressed Stop and must get Send back immediately')
    finally:
        with lock:
            reg.pop(carrier_id, None)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
