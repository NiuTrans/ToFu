"""Plugin startup hooks: off the time-to-ready critical path.

``tofu.startup`` entry-point hooks launch background plugin workers that idle
behind their own feature flag — nothing they do at boot gates a request, and
under storage-authority starvation their boot-time state restores re-pay the
full authority round trip (2026-09-22: ~60s of phase 7 for a disabled
plugin). These tests pin the scheduling contract: deferred behind the
serving gate when one exists, inline for embedders and test harnesses that
have none.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import lib.knowledge.enrichment as enrichment
import lib.oauth.codex_catalog as codex_catalog
import routes
import routes.plugin_registry as plugins


pytestmark = pytest.mark.unit


@pytest.fixture
def hook_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        plugins, 'run_startup_hooks', lambda _app: calls.append('hooks') or 1)
    # Neighboring owners on the same paths stay out of this contract test.
    monkeypatch.setattr(
        enrichment, 'resume_visual_enrichment', lambda *, principal: 1)
    monkeypatch.setattr(
        codex_catalog, 'start_codex_catalog_refresher', lambda: False)
    monkeypatch.setattr(
        'lib.oauth.outbound.reconcile_oauth_providers', lambda: None)
    monkeypatch.setattr(
        'lib.model_routing.world_catalog.sync_world_model_catalog',
        lambda: None)
    monkeypatch.setattr(
        'runtime_guards.load_deployment_configuration',
        lambda: SimpleNamespace(mode='personal'))
    return calls


def test_runs_inline_without_a_serving_gate(hook_calls):
    app = SimpleNamespace(extensions={})
    started = routes.start_registered_background_services(
        app, process_role='worker')
    assert started == 2
    assert hook_calls == ['hooks']


def test_role_without_task_workers_defers_no_hooks(hook_calls):
    app = SimpleNamespace(extensions={})
    started = routes.start_registered_background_services(
        app, process_role='api')
    assert started == 0
    assert hook_calls == []


def test_defers_until_the_gate_opens(hook_calls):
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
        started = routes.start_registered_background_services(
            app, process_role='worker')
        # Only the still-inline owners count; the hooks land post-gate.
        assert started == 1
        assert hook_calls == []
        name, coro = collected[0]
        assert name == 'tofu-post-ready-plugin-startup-hooks'
        waiter = asyncio.get_running_loop().create_task(coro)
        await asyncio.sleep(0)
        assert hook_calls == []
        gate.set()
        await waiter
        assert hook_calls == ['hooks']

    asyncio.run(exercise())


def test_deferred_hook_is_skipped_when_shutdown_races(hook_calls):
    collected = []
    stop = asyncio.Event()
    stop.set()

    class _FakeRuntime:
        def create_task(self, coro, *, name):
            collected.append((name, coro))
            return None

    gate = asyncio.Event()
    app = SimpleNamespace(extensions={
        'tofu_production_startup_gate': gate,
        'tofu_shutdown_requested': stop,
        'tofu_serving_loop_lifecycle': {'runtime': _FakeRuntime()},
    })

    async def exercise():
        routes.start_registered_background_services(app, process_role='worker')
        _name, coro = collected[0]
        gate.set()
        await coro
        assert hook_calls == []

    asyncio.run(exercise())
