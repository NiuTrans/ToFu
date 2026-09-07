"""Slow reconstructible maintenance cannot monopolize SQLite's writer."""

from __future__ import annotations

import pytest

from lib.storage.errors import StorageError


pytestmark = pytest.mark.unit


def test_drained_task_event_retention_returns_to_five_minute_cadence(
        monkeypatch):
    from lib.tasks_pkg import event_log
    import lib.storage

    class StopAfterThreeCycles:
        def __init__(self):
            self.cycles = 0

        def wait(self, _seconds):
            self.cycles += 1
            return self.cycles > 3

        def is_set(self):
            return False

    class Client:
        def __init__(self):
            self.task_event_tiers = []

        def command(self, operation, payload, _command_id, **_kwargs):
            if operation == 'event.prune':
                self.task_event_tiers.append(payload['retention_class'])
                return {'deleted': 0}
            return {}

        def maintenance(self, _operation, _payload, **_kwargs):
            return {'deleted': 0, 'hasMore': False}

    client = Client()
    monkeypatch.setattr(event_log, '_SIDECAR_MAINTENANCE_STOP',
                        StopAfterThreeCycles())
    monkeypatch.setattr(event_log, '_TASK_EVENT_PRUNE_INTERVAL_S', 300.0)
    monkeypatch.setattr(event_log, '_BACKLOG_MAINTENANCE_INTERVAL_S', 30.0)
    monkeypatch.setattr(event_log, '_ATTEMPT_EVENT_TTL_MS', 0)
    monkeypatch.setattr(event_log, '_CONVERSATION_SYNC_REPLAY_TTL_MS', 0)
    monkeypatch.setattr(event_log, '_RECLAIM_PAGES', 0)
    monkeypatch.setattr(event_log.time, 'monotonic', lambda: 100.0)
    monkeypatch.setattr(lib.storage, 'get_storage_client',
                        lambda **_kwargs: client)

    event_log._sidecar_maintenance_loop()

    assert client.task_event_tiers == ['streaming', 'structural']


def test_reclaim_timeout_opens_process_lifetime_circuit(monkeypatch):
    from lib.tasks_pkg import event_log
    import lib.storage

    class StopAfterTwoCycles:
        def __init__(self):
            self.cycles = 0

        def wait(self, _seconds):
            self.cycles += 1
            return self.cycles > 2

        def is_set(self):
            return False

    class Client:
        def __init__(self):
            self.reclaim_calls = 0

        def command(self, operation, _payload, _command_id, **_kwargs):
            if operation == 'event.prune':
                return {'deleted': 0}
            if operation == 'system.reclaim':
                self.reclaim_calls += 1
                raise StorageError(
                    'database_timeout',
                    'Storage transaction exceeded its watchdog', True, 25)
            return {}

    client = Client()
    monkeypatch.setattr(event_log, '_SIDECAR_MAINTENANCE_STOP',
                        StopAfterTwoCycles())
    monkeypatch.setattr(event_log, '_ATTEMPT_EVENT_TTL_MS', 0)
    monkeypatch.setattr(event_log, '_TASK_EVENT_PRUNE_INTERVAL_S', 0.0)
    monkeypatch.setattr(event_log, '_CONVERSATION_SYNC_REPLAY_TTL_MS', 0)
    monkeypatch.setattr(event_log, '_TOOL_RESULT_ARTIFACT_PRUNE_INTERVAL_S',
                        999.0)
    monkeypatch.setattr(event_log, '_RECLAIM_PAGES', 1)
    monkeypatch.setattr(event_log, '_RECLAIM_INTERVAL_S', 0.0)
    monkeypatch.setattr(event_log.time, 'monotonic', lambda: 1.0)
    monkeypatch.setattr(lib.storage, 'get_storage_client',
                        lambda **_kwargs: client)

    event_log._sidecar_maintenance_loop()

    assert client.reclaim_calls == 1


def test_bulk_reclaim_verdict_disables_only_reclaim_until_restart(monkeypatch):
    from lib.tasks_pkg import event_log
    import lib.storage

    class StopAfterTwoCycles:
        def __init__(self):
            self.cycles = 0

        def wait(self, _seconds):
            self.cycles += 1
            return self.cycles > 2

        def is_set(self):
            return False

    class Client:
        def __init__(self):
            self.reclaim_calls = 0
            self.event_prune_calls = 0

        def command(self, operation, _payload, _command_id, **_kwargs):
            if operation == 'event.prune':
                self.event_prune_calls += 1
                return {'deleted': 0}
            if operation == 'system.reclaim':
                self.reclaim_calls += 1
                return {
                    'reclaimed': 0,
                    'freelist': 2_000_000,
                    'offline_required': True,
                    'freelist_bytes': 8_192_000_000,
                    'file_bytes': 12_288_000_000,
                    'freelist_ratio': 2 / 3,
                }
            return {}

    client = Client()
    monkeypatch.setattr(event_log, '_SIDECAR_MAINTENANCE_STOP',
                        StopAfterTwoCycles())
    monkeypatch.setattr(event_log, '_ATTEMPT_EVENT_TTL_MS', 0)
    monkeypatch.setattr(event_log, '_TASK_EVENT_PRUNE_INTERVAL_S', 0.0)
    monkeypatch.setattr(event_log, '_CONVERSATION_SYNC_REPLAY_TTL_MS', 0)
    monkeypatch.setattr(event_log, '_TOOL_RESULT_ARTIFACT_PRUNE_INTERVAL_S',
                        999.0)
    monkeypatch.setattr(event_log, '_RECLAIM_PAGES', 8192)
    monkeypatch.setattr(event_log, '_RECLAIM_INTERVAL_S', 0.0)
    monkeypatch.setattr(event_log.time, 'monotonic', lambda: 1.0)
    monkeypatch.setattr(lib.storage, 'get_storage_client',
                        lambda **_kwargs: client)

    event_log._sidecar_maintenance_loop()

    assert client.reclaim_calls == 1
    assert client.event_prune_calls >= 2, (
        'bulk compaction must not disable unrelated bounded retention')


def test_retention_timeout_stops_later_maintenance_cycles(monkeypatch):
    from lib.tasks_pkg import event_log
    import lib.storage

    class StopAfterTwoCycles:
        def __init__(self):
            self.cycles = 0

        def wait(self, _seconds):
            self.cycles += 1
            return self.cycles > 2

        def is_set(self):
            return False

    class Client:
        def __init__(self):
            self.attempt_prune_calls = 0

        def command(self, operation, _payload, _command_id, **_kwargs):
            if operation == 'event.prune':
                return {'deleted': 0}
            if operation == 'turn.events.prune':
                self.attempt_prune_calls += 1
                raise StorageError(
                    'database_timeout',
                    'Storage writer acquisition timed out', True, 25)
            return {}

    client = Client()
    monkeypatch.setattr(event_log, '_SIDECAR_MAINTENANCE_STOP',
                        StopAfterTwoCycles())
    monkeypatch.setattr(event_log, '_ATTEMPT_EVENT_TTL_MS', 1)
    monkeypatch.setattr(event_log, '_TASK_EVENT_PRUNE_INTERVAL_S', 0.0)
    monkeypatch.setattr(event_log, '_ATTEMPT_EVENT_PRUNE_INTERVAL_S', 0.0)
    monkeypatch.setattr(event_log, '_CONVERSATION_SYNC_REPLAY_TTL_MS', 0)
    monkeypatch.setattr(event_log, '_TOOL_RESULT_ARTIFACT_PRUNE_INTERVAL_S',
                        999.0)
    monkeypatch.setattr(event_log, '_RECLAIM_PAGES', 0)
    monkeypatch.setattr(event_log.time, 'monotonic', lambda: 1.0)
    monkeypatch.setattr(lib.storage, 'get_storage_client',
                        lambda **_kwargs: client)

    event_log._sidecar_maintenance_loop()

    assert client.attempt_prune_calls == 1
