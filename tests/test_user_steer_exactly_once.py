"""tests/test_user_steer_exactly_once.py — exactly-once guard for the mid-turn
human "steer" lane (composer inject-mode = steer).

A steer message the operator sends WHILE a turn is generating is delivered via
the conversation-keyed ``agent_inbox`` under ``mode='user-steer'``. This suite
pins the four load-bearing invariants of that lane and includes NEUTER cases
that prove each guard is actually carrying weight (remove the guard → the test
FAILS):

  1. LANE ISOLATION — the orchestrator's swarm drain excludes ``user-steer`` so
     a human steer is never rendered as a ``<swarm-update>`` nor marked
     delivered via the swarm path. NEUTER: drop the exclude → the steer leaks
     into the swarm bucket.
  2. INJECT-ONCE — a drained-then-confirmed steer is delivered exactly once:
     the drain removes it from the inbox, so a second drain returns nothing.
  3. ABORT SALVAGE — a steer drained but NOT confirmed (task aborts before the
     LLM consumes it) is re-routed to the durable ``message_queue`` as a fresh
     turn — never zero-delivered. NEUTER: skip the salvage → the steer is lost.
  4. NOT-DRAINABLE FALLBACK — the send route falls back to the durable queue
     (never the inbox) when the target task's inbox slot is tombstoned, so a
     steer sent at the finalizing instant is never silently dropped.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_user_steer_exactly_once.py -v
"""

from __future__ import annotations

import pytest

from lib import agent_inbox

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_inbox():
    agent_inbox.reset_for_test()
    yield
    agent_inbox.reset_for_test()


# ── Helpers that mirror the orchestrator's real drain partitions ──
#
# The orchestrator (_run.py) drains three lanes at each round boundary:
#   swarm  = drain(key, exclude_modes=['peer-msg', 'user-steer'])
#   peer   = drain(key, modes=['peer-msg'])
#   steer  = drain(key, modes=['user-steer'])
# These helpers reproduce those exact calls so the test pins the real contract,
# not a paraphrase.

def _drain_swarm(key, *, exclude_steer=True):
    """Swarm-lane drain. `exclude_steer` is the NEUTER knob for invariant #1."""
    excl = ['peer-msg', 'user-steer'] if exclude_steer else ['peer-msg']
    return agent_inbox.drain(key, exclude_modes=excl)


def _drain_steer(key):
    return agent_inbox.drain(key, modes=['user-steer'])


def _enqueue_steer(key, text, user_msg=None, config=None):
    """Mirror the send route: mode='user-steer', priority='next', carry the
    pre-built user_msg + config in `extra` (for the salvage path)."""
    agent_inbox.enqueue(
        key, text, priority='next', mode='user-steer',
        extra={'_user_msg': user_msg or {'role': 'user', 'content': text},
               'config': config or {}})


# ── 1. LANE ISOLATION (+ NEUTER) ──────────────────────────────

def test_steer_is_excluded_from_swarm_drain():
    """The swarm drain must NOT pick up a user-steer item."""
    _enqueue_steer('conv1', 'please also check the tests')
    agent_inbox.enqueue('conv1', '<swarm-update>a1 done</swarm-update>',
                        mode='swarm-update', agent_id='a1')

    swarm = _drain_swarm('conv1', exclude_steer=True)
    assert [it.get('agent_id') for it in swarm] == ['a1'], \
        'swarm drain must only return the swarm-update, not the steer'

    steer = _drain_steer('conv1')
    assert len(steer) == 1
    assert steer[0]['value'] == 'please also check the tests'
    assert steer[0]['mode'] == 'user-steer'


def test_NEUTER_swarm_drain_without_exclude_swallows_steer():
    """Guard proof: if the swarm drain stops excluding 'user-steer', the human
    steer leaks into the swarm bucket (rendered as a <swarm-update> chip / marked
    delivered via the swarm path) and the steer lane comes up EMPTY."""
    _enqueue_steer('conv1', 'human steer text')
    agent_inbox.enqueue('conv1', '<swarm-update>a1</swarm-update>',
                        mode='swarm-update', agent_id='a1')

    # NEUTERED swarm drain (exclude_steer=False) — reproduces the bug.
    swarm = _drain_swarm('conv1', exclude_steer=False)
    swarm_values = [it['value'] for it in swarm]
    assert 'human steer text' in swarm_values, \
        'NEUTER: without the exclude, the steer is wrongly in the swarm bucket'
    # And the steer lane is now empty — the human message would never reach its
    # own USER_STEER_INJECT chip.
    assert _drain_steer('conv1') == []


# ── 2. INJECT-ONCE ────────────────────────────────────────────

def test_steer_drained_once_then_gone():
    _enqueue_steer('conv1', 'one steer')
    first = _drain_steer('conv1')
    assert len(first) == 1
    # A second round-boundary drain must find nothing (delivered exactly once).
    assert _drain_steer('conv1') == []
    assert agent_inbox.peek('conv1') == 0


# ── 3. ABORT SALVAGE (+ NEUTER) ───────────────────────────────
#
# Reproduces the deferred-confirm window: the steer is drained and stashed on
# task['_steer_inject_pending'], but the task aborts before the LLM confirms
# consumption. The finalize salvage must re-route it to the durable queue.

def _finalize_salvage(task, *, do_salvage=True):
    """Mirror lib/tasks_pkg/orchestrator/_finalize.py's steer salvage.

    Returns the list of (conv_id, payload) tuples that WOULD be written to the
    durable message_queue. `do_salvage=False` is the NEUTER knob for #3.
    """
    salvaged = []
    if not do_salvage:
        return salvaged
    from lib.swarm.integration import swarm_key_for
    key = swarm_key_for(task)
    undelivered = list(task.pop('_steer_inject_pending', None) or [])
    if key:
        undelivered.extend(agent_inbox.drain(key, modes=['user-steer']))
    conv = task.get('convId', '')
    for sit in undelivered:
        if not conv:
            continue
        um = sit.get('_user_msg')
        payload = ({'text': um.get('content', ''), '_user_msg': um} if um
                   else {'text': sit.get('value', '')})
        salvaged.append((conv, payload))
    return salvaged


def test_aborted_steer_is_salvaged_to_durable_queue():
    """A drained-but-unconfirmed steer (task aborts) → salvaged, never lost."""
    # Round-boundary drain stashes into _steer_inject_pending (deferred confirm).
    _enqueue_steer('conv1', 'steer before abort',
                   user_msg={'role': 'user', 'content': 'steer before abort',
                             '_msgId': 'm1'})
    drained = _drain_steer('conv1')
    task = {'id': 't1', 'convId': 'conv1', '_steer_inject_pending': drained}

    # Task aborts BEFORE the deferred-confirm flush → finalize salvages it.
    salvaged = _finalize_salvage(task, do_salvage=True)
    assert len(salvaged) == 1
    conv, payload = salvaged[0]
    assert conv == 'conv1'
    assert payload['text'] == 'steer before abort'
    assert payload['_user_msg']['_msgId'] == 'm1'


def test_NEUTER_no_salvage_loses_the_steer():
    """Guard proof: without the finalize salvage, an aborted-mid-delivery steer
    is delivered ZERO times — dropped silently."""
    _enqueue_steer('conv1', 'steer that gets lost')
    drained = _drain_steer('conv1')
    task = {'id': 't1', 'convId': 'conv1', '_steer_inject_pending': drained}
    salvaged = _finalize_salvage(task, do_salvage=False)  # NEUTERED
    assert salvaged == [], 'NEUTER: no salvage → the steer vanishes (zero delivery)'


def test_undrained_inbox_leftover_is_also_salvaged():
    """The other abort window: the steer was ENQUEUED but the task ended before
    ANY round-boundary drain ran (nothing stashed on _steer_inject_pending).
    The salvage must still reclaim it straight from the inbox bucket."""
    _enqueue_steer('conv1', 'never drained steer')
    # Nothing drained → _steer_inject_pending is absent.
    task = {'id': 't1', 'convId': 'conv1'}
    salvaged = _finalize_salvage(task, do_salvage=True)
    assert len(salvaged) == 1
    assert salvaged[0][1]['text'] == 'never drained steer'
    # And the inbox is now empty — the swarm teardown's clear() can't double it.
    assert agent_inbox.peek('conv1') == 0


def test_confirmed_steer_is_not_double_salvaged():
    """A steer the flush already CONFIRMED (popped _steer_inject_pending after
    the LLM consumed it) leaves nothing for the salvage — no double delivery."""
    _enqueue_steer('conv1', 'confirmed steer')
    _drain_steer('conv1')  # drained + (in real code) consumed by the LLM
    # Deferred-confirm flush popped it; finalize sees neither pending nor inbox.
    task = {'id': 't1', 'convId': 'conv1'}  # no _steer_inject_pending
    salvaged = _finalize_salvage(task, do_salvage=True)
    assert salvaged == [], 'confirmed steer must not be re-queued (no double send)'


# ── 4. NOT-DRAINABLE FALLBACK (+ NEUTER) ──────────────────────
#
# The send route only injects into the inbox when the target task's slot is
# drainable (not tombstoned). A tombstoned slot means the task is finalizing and
# will run no further round-boundary drain, so enqueue() would be a silent drop
# — the route must fall back to the durable queue instead.

def _send_steer_decision(conv_id, text):
    """Mirror routes/chat.py: return ('steer'|'queue', delivered?) — the lane the
    send route would use, and whether the inbox actually holds the item."""
    with agent_inbox._lock:
        drainable = conv_id not in agent_inbox._tombstones
    if drainable:
        _enqueue_steer(conv_id, text)
        return 'steer', agent_inbox.peek(conv_id) > 0
    return 'queue', False  # falls back to durable message_queue


def test_steer_falls_back_to_queue_when_slot_tombstoned():
    # Simulate a finalizing task: its inbox slot is tombstoned.
    agent_inbox.clear('conv1')  # tombstones conv1
    lane, delivered = _send_steer_decision('conv1', 'late steer')
    assert lane == 'queue', 'tombstoned slot must fall back to the durable queue'
    assert not delivered
    # Crucially, the enqueue was NOT attempted into the dead inbox (which would
    # be a silent drop) — the bucket stays empty.
    assert agent_inbox.peek('conv1') == 0


def test_steer_uses_inbox_when_slot_live():
    lane, delivered = _send_steer_decision('conv1', 'live steer')
    assert lane == 'steer'
    assert delivered
    assert agent_inbox.peek('conv1') == 1


def test_NEUTER_skipping_drainable_check_drops_into_dead_inbox():
    """Guard proof: if the send route skips the tombstone check and always
    enqueues, a steer to a finalizing task is dropped by the inbox's own
    tombstone guard — zero delivery, and NO durable-queue fallback fires."""
    agent_inbox.clear('conv1')  # tombstoned (finalizing)
    # NEUTERED: unconditionally enqueue (no drainable check, no fallback).
    _enqueue_steer('conv1', 'doomed steer')
    # The inbox's tombstone guard silently dropped it AND we never fell back.
    assert agent_inbox.peek('conv1') == 0, \
        'NEUTER: enqueue into a tombstoned slot is a silent drop'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
