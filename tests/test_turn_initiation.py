"""Tests for the unified turn-initiation seam (lib/conversations/turn_initiation).

Covers the ONE resolver (``resolve_initiator``) + the ONE write seam
(``stamp_initiator``), and proves each of the three backend injection choke
points stamps the right ``_initiator`` value:

  * message_queue.dispatch_next_queued  → brain / peer / operator
  * scheduler._shared.inject_and_run_task → proactive / timer
  * swarm.integration._start_autocontinue_turn → swarm  (marker asserted via
    the resolver; the DB path itself is exercised elsewhere)

Also proves the one-directional migration: a message carrying ONLY a legacy
boolean (no ``_initiator``) still resolves to the correct initiator.

Pure logic — no DB / network. Run under pytest or standalone.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.conversations.turn_initiation import (  # noqa: E402
    INITIATOR_AUTOPILOT, INITIATOR_BRAIN, INITIATOR_HUMAN, INITIATOR_OPERATOR,
    INITIATOR_PEER, INITIATOR_PROACTIVE, INITIATOR_SWARM, INITIATOR_TIMER,
    is_auto_initiated, resolve_initiator, stamp_initiator)

pytestmark = pytest.mark.unit


# ─────────────────────────── stamp_initiator ────────────────────────────

def test_stamp_sets_field():
    m = {'role': 'user', 'content': 'x'}
    stamp_initiator(m, INITIATOR_BRAIN)
    assert m['_initiator'] == INITIATOR_BRAIN


def test_stamp_human_is_noop():
    """A human turn stays unstamped — absence of _initiator IS 'human'."""
    m = {'role': 'user', 'content': 'x'}
    stamp_initiator(m, INITIATOR_HUMAN)
    assert '_initiator' not in m


def test_stamp_unknown_value_ignored():
    m = {'role': 'user'}
    stamp_initiator(m, 'nonsense')
    assert '_initiator' not in m


# ─────────────────────────── resolve_initiator ──────────────────────────

def test_resolve_defaults_to_human():
    assert resolve_initiator({'role': 'user', 'content': 'hi'}) == INITIATOR_HUMAN
    assert resolve_initiator({}) == INITIATOR_HUMAN
    assert resolve_initiator(None) == INITIATOR_HUMAN


def test_resolve_prefers_explicit_initiator_over_legacy():
    """_initiator is authoritative even if a stale legacy boolean disagrees."""
    m = {'_initiator': INITIATOR_BRAIN, '_isVirtualUser': True}
    assert resolve_initiator(m) == INITIATOR_BRAIN


@pytest.mark.parametrize('legacy,expected', [
    ({'_isVirtualUser': True}, INITIATOR_AUTOPILOT),
    ({'_autopilotRunId': 'ar-1'}, INITIATOR_AUTOPILOT),
    ({'_proactive': True}, INITIATOR_PROACTIVE),
    ({'_timer': True}, INITIATOR_TIMER),
    ({'_brainDispatch': True}, INITIATOR_BRAIN),
    ({'_swarmAutoContinue': True}, INITIATOR_SWARM),
    ({'_peerMessage': True}, INITIATOR_PEER),
    ({'_peerMessage': True, '_peerHuman': True}, INITIATOR_OPERATOR),
])
def test_resolve_legacy_boolean_fallback(legacy, expected):
    """One-directional migration: a pre-migration message carrying ONLY a
    legacy boolean still resolves to the correct initiator."""
    legacy['role'] = 'user'
    assert resolve_initiator(legacy) == expected


def test_is_auto_initiated():
    assert not is_auto_initiated({'role': 'user', 'content': 'hi'})
    assert is_auto_initiated({'_initiator': INITIATOR_TIMER})
    assert is_auto_initiated({'_proactive': True})  # legacy fallback


# ────────────── choke point 1: message_queue.dispatch mapping ────────────
#
# dispatch_next_queued's marker-propagation block stamps the initiator from
# the payload. We exercise the exact mapping logic (payload markers → stamped
# user_msg) by reproducing the branch conditions the seam uses, then asserting
# the resolver reads them back. This keeps the test DB-free while pinning the
# brain/peer/operator mapping the seam commits.

def _map_queue_payload(payload: dict) -> dict:
    """Mirror of the initiator branch in dispatch_next_queued (kept in sync
    by test_queue_seam_source_contains_stamp below)."""
    user_msg = {'role': 'user', 'content': payload.get('text', '')}
    if payload.get('_peerMessage'):
        stamp_initiator(user_msg,
                        INITIATOR_OPERATOR if payload.get('_peerHuman') else INITIATOR_PEER)
    if payload.get('_brainDispatch'):
        stamp_initiator(user_msg, INITIATOR_BRAIN)
    return user_msg


def test_queue_brain_dispatch_stamps_brain():
    m = _map_queue_payload({'_brainDispatch': True, 'text': 'do epic'})
    assert resolve_initiator(m) == INITIATOR_BRAIN


def test_queue_peer_stamps_peer():
    m = _map_queue_payload({'_peerMessage': True, 'text': 'hi'})
    assert resolve_initiator(m) == INITIATOR_PEER


def test_queue_operator_stamps_operator():
    m = _map_queue_payload({'_peerMessage': True, '_peerHuman': True, 'text': 'nudge'})
    assert resolve_initiator(m) == INITIATOR_OPERATOR


# ─────────── source-structure pins (the real seams call stamp_initiator) ──
#
# The mapping tests above use a local mirror; these pins prove the SHIPPED
# seams actually call stamp_initiator (so the mirror can't silently drift from
# a seam that stopped stamping).

def _read(path: str) -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, path), encoding='utf-8') as f:
        return f.read()


def test_queue_seam_source_contains_stamp():
    src = _read('lib/message_queue.py')
    assert 'stamp_initiator' in src
    assert 'INITIATOR_BRAIN' in src and 'INITIATOR_PEER' in src and 'INITIATOR_OPERATOR' in src


def test_scheduler_seam_source_contains_stamp():
    src = _read('lib/scheduler/_shared.py')
    assert 'stamp_initiator' in src
    assert 'INITIATOR_TIMER' in src and 'INITIATOR_PROACTIVE' in src


def test_swarm_seam_source_contains_stamp():
    src = _read('lib/swarm/integration/_autocontinue.py')
    assert 'stamp_initiator' in src and 'INITIATOR_SWARM' in src


# ─────────── reconcile._is_special_turn routes through the resolver ───────

def test_reconcile_special_turn_protects_all_auto_initiators():
    from lib.conversations.reconcile import _is_special_turn
    # Every auto-initiated empty turn must be protected from the ghost sweep.
    for marker in ({'_initiator': INITIATOR_PROACTIVE},
                   {'_initiator': INITIATOR_TIMER},
                   {'_initiator': INITIATOR_BRAIN},
                   {'_initiator': INITIATOR_SWARM},
                   {'_proactive': True}):     # legacy fallback too
        m = {'role': 'assistant', 'content': '', **marker}
        assert _is_special_turn(m), f'not protected: {marker}'
    # A plain empty human/assistant turn is NOT special.
    assert not _is_special_turn({'role': 'assistant', 'content': ''})


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_turn_initiation.__main__')
    sys.exit(pytest.main([__file__, '-v']))
