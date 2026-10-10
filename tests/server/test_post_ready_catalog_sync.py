"""Post-readiness catalogue sync: off the time-to-ready critical path.

The OAuth-template reconcile and world-model catalogue sync are idempotent
projections consumed only after readiness. These tests pin the scheduling
contract: deferred behind the serving gate when one exists, inline for
embedders and test harnesses that have none.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import routes


pytestmark = pytest.mark.unit


@pytest.fixture
def sync_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        'lib.oauth.outbound.reconcile_oauth_providers',
        lambda: calls.append('oauth-reconcile'))
    monkeypatch.setattr(
        'lib.model_routing.world_catalog.sync_world_model_catalog',
        lambda: calls.append('world-catalog'))
    monkeypatch.setattr(
        'runtime_guards.load_deployment_configuration',
        lambda: SimpleNamespace(mode='personal'))
    return calls


def test_runs_inline_without_a_serving_gate(sync_calls):
    app = SimpleNamespace(extensions={})
    scheduled = routes.start_post_ready_catalog_sync(app, process_role='all')
    assert scheduled is False
    assert sync_calls == ['oauth-reconcile', 'world-catalog']


def test_role_without_request_services_does_nothing(sync_calls):
    app = SimpleNamespace(extensions={})
    assert routes.start_post_ready_catalog_sync(
        app, process_role='worker') is False
    assert sync_calls == []


def test_defers_until_the_gate_opens(sync_calls):
    collected = []

    class _FakeRuntime:
        def create_task(self, coro, *, name):
            collected.append((name, coro))
            return None

    gate = asyncio.Event()
    app = SimpleNamespace(extensions={
        'tofu_production_startup_gate': gate,
        'tofu_serving_loop_lifecycle': {'runtime': _FakeRuntime()},
    })

    async def exercise():
        scheduled = routes.start_post_ready_catalog_sync(
            app, process_role='all')
        assert scheduled is True
        assert sync_calls == []
        name, coro = collected[0]
        assert name == 'tofu-post-ready-catalog-sync'
        waiter = asyncio.get_running_loop().create_task(coro)
        await asyncio.sleep(0)
        assert sync_calls == []
        gate.set()
        await waiter
        assert sync_calls == ['oauth-reconcile', 'world-catalog']

    asyncio.run(exercise())
