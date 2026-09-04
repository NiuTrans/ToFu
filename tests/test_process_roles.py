"""Executable ownership contract for distributed process roles."""

from __future__ import annotations

import pytest

from lib.process_roles import (
    CAPABILITY_EVENT_MAINTENANCE,
    CAPABILITY_FRONTEND,
    CAPABILITY_REQUEST_SERVICES,
    CAPABILITY_SCHEDULED_JOBS,
    CAPABILITY_TASK_RECOVERY,
    CAPABILITY_TASK_WORKERS,
    capabilities_for_role,
    process_role_has,
)


pytestmark = pytest.mark.unit


def test_roles_have_disjoint_split_process_ownership():
    assert capabilities_for_role('api') == frozenset({
        CAPABILITY_FRONTEND,
        CAPABILITY_REQUEST_SERVICES,
        'network_configuration',
    })
    assert capabilities_for_role('worker') == frozenset({
        'network_configuration',
        CAPABILITY_TASK_RECOVERY,
        CAPABILITY_TASK_WORKERS,
    })
    assert capabilities_for_role('scheduler') == frozenset({
        CAPABILITY_SCHEDULED_JOBS,
        CAPABILITY_EVENT_MAINTENANCE,
    })
    assert capabilities_for_role('api').isdisjoint(
        capabilities_for_role('scheduler'))


def test_all_role_owns_every_split_capability():
    for role in ('api', 'worker', 'scheduler'):
        for capability in capabilities_for_role(role):
            assert process_role_has('all', capability)


def test_unknown_role_or_capability_is_rejected():
    with pytest.raises(ValueError, match='process role'):
        capabilities_for_role('web')
    with pytest.raises(ValueError, match='unknown process-role capability'):
        process_role_has('api', 'database_admin')


def test_route_background_owners_are_role_scoped(monkeypatch):
    from types import SimpleNamespace

    import lib.knowledge.enrichment as enrichment
    import lib.oauth.codex_catalog as codex_catalog
    import lib.scheduler.manager as scheduler_manager
    import routes
    import routes.plugin_registry as plugins
    import runtime_guards

    calls: list[str] = []
    scheduler_principals = []

    monkeypatch.setattr(
        runtime_guards, 'load_deployment_configuration',
        lambda: SimpleNamespace(mode='personal'))

    monkeypatch.setattr(
        scheduler_manager, 'start_scheduler_worker',
        lambda *, principal: (
            scheduler_principals.append(principal),
            calls.append('scheduler'),
        )[-1])
    monkeypatch.setattr(
        codex_catalog, 'start_codex_catalog_refresher',
        lambda: calls.append('codex') or True)
    monkeypatch.setattr(
        enrichment, 'resume_visual_enrichment',
        lambda *, principal: calls.append('enrichment') or 1)
    monkeypatch.setattr(
        plugins, 'run_startup_hooks',
        lambda _app: calls.append('plugins') or 1)

    class App:
        def __init__(self):
            self.extensions = {}

    api_app = App()
    assert routes.start_registered_background_services(
        api_app, process_role='api') == 1
    assert calls == ['codex']

    calls.clear()
    scheduler_app = App()
    assert routes.start_registered_background_services(
        scheduler_app, process_role='scheduler') == 1
    assert calls == ['scheduler']
    assert len(scheduler_principals) == 1
    assert scheduler_principals[0].kind == 'system'
    assert scheduler_principals[0].owner_user_id == 1
    assert scheduler_principals[0].scopes == frozenset({'scheduler:run'})

    calls.clear()
    worker_app = App()
    assert routes.start_registered_background_services(
        worker_app, process_role='worker') == 2
    assert calls == ['enrichment', 'plugins']


def test_distributed_role_does_not_invent_a_daily_report_owner(monkeypatch):
    from types import SimpleNamespace

    import lib.scheduler.manager as scheduler_manager
    import routes
    import runtime_guards

    calls = []
    scheduler_principals = []
    monkeypatch.setattr(
        runtime_guards, 'load_deployment_configuration',
        lambda: SimpleNamespace(mode='distributed'))
    monkeypatch.setattr(
        scheduler_manager, 'start_scheduler_worker',
        lambda *, principal: (
            scheduler_principals.append(principal),
            calls.append('scheduler'),
        )[-1])

    class App:
        extensions = {}

    assert routes.start_registered_background_services(
        App(), process_role='scheduler') == 1
    assert calls == ['scheduler']
    assert len(scheduler_principals) == 1
    assert scheduler_principals[0].kind == 'system'
    assert scheduler_principals[0].owner_user_id is None
    assert scheduler_principals[0].scopes == frozenset({'scheduler:run'})


def test_distributed_api_role_does_not_start_ownerless_codex_catalog(
        monkeypatch):
    from types import SimpleNamespace

    import lib.oauth.codex_catalog as codex_catalog
    import routes
    import runtime_guards

    calls = []
    monkeypatch.setattr(
        runtime_guards, 'load_deployment_configuration',
        lambda: SimpleNamespace(mode='distributed'))
    monkeypatch.setattr(
        codex_catalog, 'start_codex_catalog_refresher',
        lambda: calls.append('codex') or True)

    class App:
        def __init__(self):
            self.extensions = {}

    assert routes.start_registered_background_services(
        App(), process_role='api') == 0
    assert calls == []
