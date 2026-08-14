"""Lifecycle contracts for the request-triggered pricing refresh worker."""

from __future__ import annotations

import threading

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_pricing_worker():
    import lib.pricing._refresh as refresh
    from lib.observability import reset_for_tests

    reset_for_tests()
    refresh._refresh_stop.set()
    refresh.stop_pricing_refresh(timeout=1.0)
    if refresh._refresh_lock.locked():
        refresh._refresh_lock.release()
    refresh._refresh_stop.clear()
    reset_for_tests()
    yield
    refresh._refresh_stop.set()
    refresh.stop_pricing_refresh(timeout=1.0)
    if refresh._refresh_lock.locked():
        refresh._refresh_lock.release()
    refresh._refresh_stop.clear()


def test_refresh_has_one_named_owner_and_deduplicates(monkeypatch):
    import lib.pricing._refresh as refresh

    entered = threading.Event()
    release = threading.Event()

    def update():
        entered.set()
        release.wait(2.0)

    monkeypatch.setattr(refresh, '_do_update_pricing', update)

    refresh.refresh_pricing_async()
    assert entered.wait(1.0)
    owner = refresh._refresh_thread
    assert owner is not None
    assert owner.name == 'pricing-refresh'

    refresh.refresh_pricing_async()
    assert refresh._refresh_thread is owner

    release.set()
    owner.join(timeout=1.0)
    assert not owner.is_alive()
    assert refresh._refresh_thread is None
    assert not refresh._refresh_lock.locked()


def test_stop_signals_and_releases_only_a_finished_owner(monkeypatch):
    import lib.pricing._refresh as refresh

    class Thread:
        alive = True
        joined = []

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            self.joined.append(timeout)
            self.alive = False

    owner = Thread()
    monkeypatch.setattr(refresh, '_refresh_thread', owner)

    assert refresh.stop_pricing_refresh(timeout=0.125) is True
    assert refresh._refresh_stop.is_set()
    assert owner.joined == [0.125]
    assert refresh._refresh_thread is None


def test_stop_retains_live_owner_after_timeout(monkeypatch):
    import lib.pricing._refresh as refresh

    class Thread:
        def is_alive(self):
            return True

        def join(self, timeout):
            assert timeout == 0.25

    owner = Thread()
    monkeypatch.setattr(refresh, '_refresh_thread', owner)

    assert refresh.stop_pricing_refresh(timeout=0.25) is False
    assert refresh._refresh_thread is owner
    # Do not leave the deliberately immortal fake installed for the autouse
    # fixture's real-owner teardown.
    monkeypatch.setattr(refresh, '_refresh_thread', None)


def test_shutdown_gate_skips_network_and_database(monkeypatch):
    import lib.pricing._refresh as refresh

    called = []
    monkeypatch.setattr(
        refresh, '_fetch_exchange_rate', lambda: called.append('exchange'))
    monkeypatch.setattr(
        refresh, '_fetch_model_pricing_online',
        lambda _model: called.append('pricing'))
    refresh._refresh_stop.set()

    refresh._do_update_pricing()

    assert called == []


def test_refresh_persists_through_semantic_sidecar_operation(monkeypatch):
    import lib
    import lib.pricing._refresh as refresh

    calls = []

    class Client:
        def command(self, operation, payload, command_id, **kwargs):
            calls.append((operation, payload, command_id, kwargs))
            return {'version': 1}

    monkeypatch.setattr(refresh, '_storage', lambda **_kwargs: Client())
    monkeypatch.setattr(refresh, '_fetch_exchange_rate', lambda: 7.2)
    monkeypatch.setattr(
        refresh, '_fetch_model_pricing_online', lambda _model: None)
    monkeypatch.setattr(lib, 'LLM_MODEL', 'test-unknown-model')

    refresh._do_update_pricing()

    assert len(calls) == 1
    operation, payload, command_id, kwargs = calls[0]
    assert operation == 'record.put'
    assert payload['namespace'] == 'pricing_cache'
    assert payload['key'] == 'pricing'
    assert payload['value']['usdToCny'] == 7.2
    assert command_id.startswith('pricing-cache:')
    assert kwargs['priority'] == 'event'


def test_worker_lifecycle_is_exposed_without_dynamic_labels(monkeypatch):
    import lib.pricing._refresh as refresh
    from lib.observability import prometheus_lines

    monkeypatch.setattr(refresh, '_do_update_pricing', lambda: None)
    refresh.refresh_pricing_async()
    owner = refresh._refresh_thread
    assert owner is not None
    owner.join(timeout=1.0)

    text = '\n'.join(prometheus_lines())
    assert 'tofu_background_jobs_started_total{kind="pricing_refresh"} 1.0' in text
    assert 'tofu_background_jobs_active{kind="pricing_refresh"} 0.0' in text
    assert ('tofu_background_jobs_completed_total'
            '{kind="pricing_refresh",outcome="success"} 1.0') in text
    assert 'request_id=' not in text
    assert 'task_id=' not in text
