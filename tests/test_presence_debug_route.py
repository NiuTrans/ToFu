"""tests/test_presence_debug_route.py — the gated presence debug route.

``POST /api/push/debug/presence`` lets ``debug/presence_smoke.py --live`` fire
synthetic presence frames INSIDE the server process (the push hub is an
in-process singleton, so an external script can't reach a live browser). The
route MUST be OFF unless ``TOFU_PRESENCE_DEBUG=1`` — it is a debug affordance,
not a production surface. This pins both halves:

  • disabled (no env flag) → 403, and NOTHING is broadcast;
  • enabled → 200 + the registry actually emits 'presence' frames.

Uses the sync ``flask_client`` facade from conftest (calls are synchronous —
no event loop needed).
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.api, pytest.mark.auth_mode("open")]


def test_debug_route_disabled_by_default(flask_client, monkeypatch):
    """Without TOFU_PRESENCE_DEBUG the route refuses with 403 and emits nothing."""
    monkeypatch.delenv('TOFU_PRESENCE_DEBUG', raising=False)
    from lib.push import hub
    captured = []
    listener = lambda ch, tid, payload: captured.append(payload)  # noqa: E731
    hub.add_listener(listener)
    try:
        resp = flask_client.post('/api/push/debug/presence',
                                 json={'root': '/tmp/presence_test_root'})
        assert resp.status_code == 403
        assert resp.get_json()['ok'] is False
    finally:
        hub.remove_listener(listener)
    assert captured == [], 'disabled route must broadcast nothing'


def test_debug_route_enabled_emits_presence_frames(flask_client, monkeypatch,
                                                   tmp_path):
    """With the flag set, the route fires the scenario and broadcasts frames."""
    monkeypatch.setenv('TOFU_PRESENCE_DEBUG', '1')
    root = str(tmp_path / 'presence-enabled')
    from lib.push import hub
    captured = []
    listener = lambda ch, tid, payload: captured.append({'ch': ch, **payload})  # noqa: E731
    hub.add_listener(listener)
    try:
        resp = flask_client.post('/api/push/debug/presence',
                                 json={'root': root, 'action': 'scenario'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['ok'] is True
        assert data['activePeers'] == 2
        kinds = {}
        for f in captured:
            kinds[f.get('kind', '?')] = kinds.get(f.get('kind', '?'), 0) + 1
        assert all(f['ch'] == 'presence' for f in captured)
        assert kinds.get('update', 0) >= 2
        assert kinds.get('conflict', 0) >= 1, 'shared-file overlap must advise'
    finally:
        hub.remove_listener(listener)
        from lib import presence
        presence.depart(root, 'dbg-peer-1')
        presence.depart(root, 'dbg-peer-2')


def test_debug_route_subagents_emits_within_conv_conflict(flask_client,
                                                          monkeypatch,
                                                          tmp_path):
    """The 'subagents' action fires a within-conversation conflict + nested peers."""
    monkeypatch.setenv('TOFU_PRESENCE_DEBUG', '1')
    root = str(tmp_path / 'presence-subagents')
    from lib.push import hub
    captured = []
    listener = lambda ch, tid, payload: captured.append({'ch': ch, **payload})  # noqa: E731
    hub.add_listener(listener)
    try:
        resp = flask_client.post('/api/push/debug/presence',
                                 json={'root': root, 'action': 'subagents'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['ok'] is True
        # 1 conversation peer + 2 distinct sub-agent peers.
        assert data['activePeers'] == 3
        conflicts = [f for f in captured if f.get('kind') == 'conflict']
        assert conflicts, 'two sub-agents on one file must advise'
        peers = set(conflicts[-1]['conflict']['peers'])
        assert peers == {'dbg-swarm#agent-coder-1', 'dbg-swarm#agent-coder-2'}
    finally:
        hub.remove_listener(listener)
        from lib import presence
        presence.depart(root, 'dbg-swarm', agent_id='agent-coder-1')
        presence.depart(root, 'dbg-swarm', agent_id='agent-coder-2')
        presence.depart(root, 'dbg-swarm')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
