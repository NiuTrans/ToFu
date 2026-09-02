"""Focused regressions for the 2026-08-16 runtime warning cluster."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1


def test_project_feed_clamps_to_sidecar_contract(monkeypatch):
    import lib.conversations.project_feed as feed
    seen = {}

    class Client:
        def query(self, operation, payload):
            seen.update(operation=operation, payload=payload)
            return {'events': [], 'maxSeq': 0}
    monkeypatch.setattr(feed, 'get_storage_client', lambda **_kw: Client())
    assert feed.read_project_feed('/project', limit=500, user_id=TEST_OWNER_USER_ID)['events'] == []
    assert seen['operation'] == 'project.feed.list'
    assert seen['payload']['limit'] == 200


def test_queue_reap_command_id_matches_exact_payload(monkeypatch):
    import lib.message_queue as queue_mod

    calls = []

    class Client:
        def command(self, operation, payload, command_id):
            calls.append((operation, dict(payload), command_id))
            return {'conv_ids': []}

        def query(self, operation, payload):
            assert operation == 'queue.conversations.list_all'
            assert payload == {}
            return []

    times = iter((1000.001, 1000.002))
    client = Client()
    monkeypatch.setattr(queue_mod, '_queue_client', lambda **_kw: client)
    monkeypatch.setattr(queue_mod, 'time', SimpleNamespace(
        time=lambda: next(times)))
    queue_mod.reap_expired_queue_leases()
    queue_mod.reap_expired_queue_leases()
    assert calls[0][2] != calls[1][2]
    for _, payload, command_id in calls:
        assert str(payload['now_ms']) in command_id


def test_subscription_probe_failure_is_debug_but_request_failure_warns(caplog):
    from lib.subscription_routes import ProbeResult, Route, RouteManager

    route = Route('direct', 'direct', 'direct')
    manager = RouteManager(probe=lambda _url, _route: ProbeResult('network_fail'),
                           jitter=lambda value: value)
    try:
        with caplog.at_level(logging.DEBUG):
            manager._run_probe('https://chatgpt.com/x', 'chatgpt.com', route,
                               manager._generation)
        assert not any(rec.levelno >= logging.WARNING and
                       '[SubscriptionRoute]' in rec.message
                       for rec in caplog.records)
        assert any('probe:' in rec.message for rec in caplog.records)

        caplog.clear()
        with caplog.at_level(logging.WARNING):
            manager.report('https://chatgpt.com/x', route, False)
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)
    finally:
        manager.close()


def test_log_aggregate_row_is_projected_to_storage_bounds():
    from lib.log_aggregates import _sanitize_storage_row

    row = _sanitize_storage_row({
        'fingerprint': 'f' * 100,
        'level': 'L' * 100,
        'logger': 'x' * 1000,
        'template': 't' * 1000,
        'sample': 's' * 3000,
        'count': 2_000_000_000,
        'first_seen': -1,
        'last_seen': -2,
    })
    assert len(row['fingerprint']) == 64
    assert len(row['level']) == 32
    assert len(row['logger']) == 256
    assert len(row['template']) == 200
    assert len(row['sample']) == 2000
    assert row['count'] == 1_000_000_000
    assert row['first_seen'] == row['last_seen'] == 0


def test_conversation_revision_projection_uses_owner_scoped_authority(monkeypatch):
    from lib.conversations.repository import ConversationRepository

    class Client:
        def query(self, operation, payload):
            assert operation == 'conversation.list'
            assert payload['ids'] == ['conv-a', 'conv-missing']
            assert payload['include_messages'] is False
            assert payload['derive_messages'] is False
            assert payload['user_id'] == TEST_OWNER_USER_ID
            return [{'metadata': {'id': 'conv-a', 'rev': 17}, 'messages': []}]

    snapshots = ConversationRepository(
        lambda *, write=False: Client()).list(
        ids=['conv-a', 'conv-missing'],
        user_id=TEST_OWNER_USER_ID,
        include_messages=False,
    )
    assert [(item['id'], item['rev']) for item in snapshots] == [('conv-a', 17)]


def test_conversation_affinity_is_scoped_by_logical_route():
    from lib.llm_dispatch import conv_affinity as affinity

    affinity._conv_keys.clear()
    affinity.record_conv_key('conv-a', 'main-key', route_key='kimi-k3')
    affinity.record_conv_key('conv-a', 'aux-key', route_key='gpt-5.6-luna')
    assert affinity.get_preferred_key(
        'conv-a', route_key='kimi-k3') == 'main-key'
    assert affinity.get_preferred_key(
        'conv-a', route_key='gpt-5.6-luna') == 'aux-key'
