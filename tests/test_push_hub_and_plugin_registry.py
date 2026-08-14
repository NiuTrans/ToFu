"""Unit tests for two recently-relocated/added core seams that had no direct
coverage:

  * ``lib/agent_core/push.py`` (``PushHub`` / ``PushClient``) — the unified
    server-push fan-out relocated into ``agent_core`` in the 2026-06 leaf move.
    We exercise the no-event-loop delivery path (frames enqueued directly),
    per-channel/per-task and wildcard subscription routing, in-process listener
    fan-out + exception isolation, and the queue-full drop-oldest policy.

  * ``routes/plugin_registry.py`` — the Blueprint / startup-hook / TaskRuntime
    discovery seam added when the trading subsystem was extracted. We assert it
    is fail-soft: a broken entry point is logged and skipped (never raised),
    and discovery returns an empty list when no plugin is installed.

These are pure-logic tests — no DB, no network, no browser — so they run under
the ``unit`` marker.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from lib.agent_core.push import PushClient, PushHub


# ═══════════════════════════════════════════════════════════
#  PushHub fan-out
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPushHub:
    def test_push_routes_to_exact_task_subscriber(self):
        hub = PushHub()
        client = PushClient()
        hub.register(client)
        hub.subscribe(client, 'chat', 'task-1')

        # No event loop set → hub.push_event enqueues directly (the
        # synchronous fallback path used outside an async server).
        hub.push_event('chat', 'task-1', {'type': 'delta', 'content': 'hi'})

        frame = asyncio.run(client.drain())
        assert frame == {'channel': 'chat', 'taskId': 'task-1',
                         'type': 'delta', 'content': 'hi'}

    def test_wildcard_subscriber_receives_all_tasks_on_channel(self):
        hub = PushHub()
        client = PushClient()
        hub.register(client)
        hub.subscribe(client, 'paper', '*')

        hub.push_event('paper', 'whatever-task', {'type': 'progress'})
        frame = asyncio.run(client.drain())
        assert frame['channel'] == 'paper'
        assert frame['taskId'] == 'whatever-task'
        assert frame['type'] == 'progress'

    def test_non_subscriber_gets_nothing(self):
        hub = PushHub()
        client = PushClient()
        hub.register(client)
        hub.subscribe(client, 'chat', 'task-1')

        # Event on a task this client did NOT subscribe to.
        hub.push_event('chat', 'other-task', {'type': 'delta'})

        # drain() times out after 30s waiting for a frame; instead assert the
        # queue is empty without blocking.
        assert client._queue.empty()

    def test_unsubscribe_stops_delivery(self):
        hub = PushHub()
        client = PushClient()
        hub.register(client)
        hub.subscribe(client, 'chat', 'task-1')
        hub.unsubscribe(client, 'chat', 'task-1')

        hub.push_event('chat', 'task-1', {'type': 'delta'})
        assert client._queue.empty()

    def test_listener_receives_event_and_exception_is_isolated(self):
        hub = PushHub()
        seen = []

        def good_listener(channel, task_id, payload):
            seen.append((channel, task_id, payload))

        def bad_listener(channel, task_id, payload):
            raise RuntimeError('listener boom')

        # Register bad first so we prove a raising listener does not prevent
        # the good one (registered after) from running.
        hub.add_listener(bad_listener)
        hub.add_listener(good_listener)

        # No subscribers → no client fan-out, but listeners still fire.
        hub.push_event('notify', 'sys', {'type': 'config_change'})

        assert seen == [('notify', 'sys', {'type': 'config_change'})]

    def test_add_listener_is_idempotent(self):
        hub = PushHub()
        calls = []
        fn = lambda c, t, p: calls.append(1)  # noqa: E731
        hub.add_listener(fn)
        hub.add_listener(fn)  # second registration must be a no-op
        hub.push_event('notify', 'sys', {'type': 'x'})
        assert calls == [1]

    def test_remove_listener_missing_is_safe(self):
        hub = PushHub()
        # Removing a never-registered listener must not raise (logged at debug).
        missing = lambda c, t, p: None  # noqa: E731
        result = hub.remove_listener(missing)
        assert result is None
        assert hub._listeners == []

    def test_broadcast_reaches_all_registered_clients(self):
        hub = PushHub()
        c1, c2 = PushClient(), PushClient()
        hub.register(c1)
        hub.register(c2)

        hub.broadcast('notify', {'type': 'ping'})

        f1 = asyncio.run(c1.drain())
        f2 = asyncio.run(c2.drain())
        assert f1['type'] == 'ping' and f1['taskId'] == '*'
        assert f2['type'] == 'ping' and f2['taskId'] == '*'

    def test_unregister_removes_client_from_subscriptions(self):
        hub = PushHub()
        client = PushClient()
        hub.register(client)
        hub.subscribe(client, 'chat', 'task-1')
        hub.unregister(client)

        assert hub.client_count == 0
        hub.push_event('chat', 'task-1', {'type': 'delta'})
        assert client._queue.empty()

    def test_last_unsubscribe_prunes_task_and_channel_tombstones(self):
        hub = PushHub()
        client = PushClient()
        hub.subscribe(client, 'paper', 'task-once')
        hub.unsubscribe(client, 'paper', 'task-once')

        assert 'paper' not in hub._subscriptions

    def test_unknown_unsubscribe_does_not_create_registry_entry(self):
        hub = PushHub()
        hub.unsubscribe(PushClient(), 'never-seen', 'missing-task')

        assert dict(hub._subscriptions) == {}

    def test_unregister_prunes_all_last_owned_subscription_buckets(self):
        hub = PushHub()
        client = PushClient()
        hub.register(client)
        hub.subscribe(client, 'paper', 'p1')
        hub.subscribe(client, 'paper', 'p2')
        hub.subscribe(client, 'notify', '*')

        hub.unregister(client)

        assert dict(hub._subscriptions) == {}


@pytest.mark.unit
class TestPushClientQueue:
    def test_queue_full_drops_oldest_and_keeps_newest(self):
        client = PushClient()
        # Shrink the queue so we can saturate it cheaply.
        client._queue = asyncio.Queue(maxsize=2)
        client.enqueue({'n': 1})
        client.enqueue({'n': 2})
        # Third enqueue overflows → drop oldest ({'n': 1}), keep {'n': 2} + 3.
        client.enqueue({'n': 3})

        drained = [asyncio.run(client.drain()), asyncio.run(client.drain())]
        ns = [f['n'] for f in drained]
        assert ns == [2, 3], f'expected oldest dropped, got {ns}'

    def test_disconnected_client_ignores_enqueue(self):
        client = PushClient()
        client.disconnect()
        client.enqueue({'type': 'delta'})
        assert client._queue.empty()

    def test_drain_after_disconnect_returns_none(self):
        client = PushClient()
        client.disconnect()
        assert asyncio.run(client.drain()) is None


# ═══════════════════════════════════════════════════════════
#  plugin_registry — fail-soft discovery
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPluginRegistry:
    def test_discover_blueprints_returns_list(self):
        from routes.plugin_registry import discover_blueprint_plugins
        # Contract: discovery always returns a list (possibly empty on a
        # vanilla install, non-empty when plugins are installed) and never
        # raises. We assert the fail-soft return type rather than emptiness,
        # because the test environment may ship real ``tofu.blueprints``
        # plugins.
        assert isinstance(discover_blueprint_plugins(), list)

    def test_broken_blueprint_plugin_is_logged_and_skipped(self, monkeypatch, caplog):
        import routes.plugin_registry as pr

        class _FakeEP:
            name = 'boom'

            def load(self):
                raise ImportError('simulated broken plugin')

        monkeypatch.setattr(pr, 'entry_points', lambda group=None: [_FakeEP()],
                            raising=False)
        # The function imports entry_points lazily from importlib.metadata, so
        # patch there too.
        import importlib.metadata as md
        monkeypatch.setattr(md, 'entry_points', lambda group=None: [_FakeEP()])

        with caplog.at_level(logging.WARNING):
            result = pr.discover_blueprint_plugins()

        assert result == []  # broken plugin contributes nothing, no raise
        assert any('boom' in r.getMessage() for r in caplog.records), \
            'broken plugin should be logged at WARNING'

    def test_good_blueprint_plugin_contributes_blueprints(self, monkeypatch):
        import routes.plugin_registry as pr

        sentinel_bps = ['bp-a', 'bp-b']

        class _FakeEP:
            name = 'good'

            def load(self):
                return lambda: sentinel_bps

        import importlib.metadata as md
        monkeypatch.setattr(md, 'entry_points', lambda group=None: [_FakeEP()])

        assert pr.discover_blueprint_plugins() == sentinel_bps

    def test_startup_hook_failure_is_isolated(self, monkeypatch):
        import routes.plugin_registry as pr

        ran = {'good': False}

        class _BadEP:
            name = 'bad'

            def load(self):
                return lambda app: (_ for _ in ()).throw(RuntimeError('hook boom'))

        class _GoodEP:
            name = 'good'

            def load(self):
                def _hook(app):
                    ran['good'] = True
                return _hook

        import importlib.metadata as md
        monkeypatch.setattr(md, 'entry_points',
                            lambda group=None: [_BadEP(), _GoodEP()])

        n = pr.run_startup_hooks(app=object())
        # The good hook still runs despite the bad one raising.
        assert ran['good'] is True
        assert n == 1

    def test_shutdown_hook_failure_is_isolated(self, monkeypatch):
        import routes.plugin_registry as pr

        calls = []

        class _BadEP:
            name = 'bad'

            def load(self):
                def fail(_app):
                    raise RuntimeError('shutdown boom')
                return fail

        class _GoodEP:
            name = 'good'

            def load(self):
                return lambda app: calls.append(app)

        import importlib.metadata as md
        monkeypatch.setattr(
            md, 'entry_points', lambda group=None: [_BadEP(), _GoodEP()])

        app = object()
        assert pr.run_shutdown_hooks(app) == 1
        assert calls == [app]

    def test_task_runtime_plugin_flattens_list_and_skips_none(self, monkeypatch):
        import routes.plugin_registry as pr

        class _ListEP:
            name = 'multi'

            def load(self):
                return lambda: ['rt1', 'rt2']

        class _NoneEP:
            name = 'none'

            def load(self):
                return lambda: None

        class _SingleEP:
            name = 'single'

            def load(self):
                return lambda: 'rt3'

        import importlib.metadata as md
        monkeypatch.setattr(
            md, 'entry_points',
            lambda group=None: [_ListEP(), _NoneEP(), _SingleEP()])

        assert pr.discover_task_runtime_plugins() == ['rt1', 'rt2', 'rt3']

    def test_task_runtime_discovery_is_cached_per_loader(self, monkeypatch):
        """The hot generic-task endpoint must not rescan site-packages."""
        import importlib.metadata as md
        import routes.plugin_registry as pr

        calls = {'entry_points': 0, 'load': 0}

        class _EP:
            name = 'cached'

            def load(self):
                calls['load'] += 1
                return lambda: ['runtime-sentinel']

        def _entry_points(group=None):
            calls['entry_points'] += 1
            return [_EP()]

        monkeypatch.setattr(md, 'entry_points', _entry_points)
        first = pr.discover_task_runtime_plugins()
        first.append('caller-mutation')
        second = pr.discover_task_runtime_plugins()

        assert second == ['runtime-sentinel']
        assert calls == {'entry_points': 1, 'load': 1}
