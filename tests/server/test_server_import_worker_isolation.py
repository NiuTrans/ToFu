"""Importing the app must register routes without launching real workers."""

from __future__ import annotations

import lib.server.server_background_services as background_services

import inspect

import pytest

pytestmark = pytest.mark.unit


def test_process_services_import_without_loading_http_graph():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, '-c',
         'import sys\n'
         'import lib.server.server_background_services\n'
         'import lib.server.server_reexec\n'
         'import lib.app.plugin_registry\n'
         'assert not any(name == "routes" or name.startswith("routes.") '
         'for name in sys.modules)\n'],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_server_import_defers_route_workers_to_serving_lifecycle():
    import server
    import lib.server.server_background_services as background_services

    source = inspect.getsource(server)
    worker = inspect.getsource(background_services.start_background_services)
    assert 'start_registered_background_services(' in worker
    assert 'process_role=process_role' in worker
    assert 'start_janitor()' not in worker
    assert 'bootstrap_personal_key()' in worker
    assert 'load_saved_proxy_config()' in worker
    # Best-effort sweeps/probes run post-gate, still inside the serving
    # lifecycle — never at import time.
    deferred = inspect.getsource(
        background_services.start_deferred_background_services)
    assert 'start_prober()' in deferred
    wrapper = inspect.getsource(server._start_background_workers)
    assert 'start_background_services(' in wrapper
    module_prefix = source[:source.index('def _start_background_workers')]
    assert 'start_janitor()' not in module_prefix
    assert 'start_prober()' not in module_prefix


def test_saved_proxy_state_is_not_applied_during_module_import():
    import server

    source = inspect.getsource(server)
    function_start = source.index('def _load_saved_proxy_config():')
    worker_start = source.index('def _start_background_workers(')

    assert function_start < worker_start
    assert '_read_server_config()' not in source[:function_start]
    assert 'set_proxy_config(' not in source[:function_start]
    assert '_load_saved_proxy_config()' not in source[worker_start:]
    assert 'load_saved_proxy_config=_load_saved_proxy_config' \
        in source[worker_start:]


@pytest.mark.parametrize('failed_owner', [None, 'coalescer', 'maintenance', 'listener', 'aggregate'])
def test_async_logging_workers_are_owned_by_quart_lifecycle(monkeypatch, failed_owner):
    from types import SimpleNamespace
    import server

    source = inspect.getsource(server)
    start_function = source.index('def _start_logging_runtime():')
    assert '_log_listener.start()' not in source[:start_function]
    assert 'tofu.logging.startup' in server.app.extensions[
        'tofu_lifecycle']['startup_handlers']
    assert 'tofu.logging.shutdown' in server.app.extensions[
        'tofu_lifecycle']['shutdown_handlers']

    calls = []

    class Thread:
        def is_alive(self):
            return True

    class Listener:
        _thread = None

        def start(self):
            calls.append('listener-start')
            self._thread = Thread()

        def stop(self, timeout):
            calls.append(('listener-stop', timeout))
            self._thread = None
            return failed_owner != 'listener'

    listener = Listener()
    monkeypatch.setattr(server, '_LOG_UNDER_PYTEST', False)
    monkeypatch.setattr(server, '_log_listener', listener)
    # This specification owns lifecycle order, not real filesystem retention
    # or delayed flush workers. Those owners have their own join specifications.
    monkeypatch.delenv('TOFU_EXTERNAL_CONSOLE_LOG', raising=False)
    def sink(_record):
        pass
    monkeypatch.setattr(server, '_queue_handler', SimpleNamespace(emit=sink))
    monkeypatch.setattr(server, '_coalescing_filter', SimpleNamespace(
        start_pending_flush=lambda emit: calls.append(('coalescer-start', emit)),
        stop_pending_flush=lambda **kwargs: (
            calls.append(('coalescer-stop', kwargs)) or failed_owner != 'coalescer'),
    ))
    monkeypatch.setattr(server, '_start_log_maintenance',
                        lambda *_args: calls.append('maintenance-start'))
    monkeypatch.setattr(server, '_stop_log_maintenance',
                        lambda **kwargs: calls.append(('maintenance-stop', kwargs))
                        or failed_owner != 'maintenance')
    monkeypatch.setattr(server, '_log_agg_enabled', lambda: True)
    monkeypatch.setattr(server, '_log_agg_start_flusher',
                        lambda: calls.append('aggregate-start'))
    monkeypatch.setattr(
        server, '_log_agg_stop_flusher',
        lambda **kwargs: calls.append(('aggregate-stop', kwargs))
        or failed_owner != 'aggregate')

    assert server._start_logging_runtime() is True
    assert server._start_logging_runtime() is False
    assert calls == ['listener-start', ('coalescer-start', sink), 'maintenance-start']
    server._start_log_aggregate_runtime_after_recovery()
    assert server._stop_logging_runtime(timeout=0.25) is (failed_owner is None)
    assert calls == [
        'listener-start', ('coalescer-start', sink), 'maintenance-start',
        'aggregate-start',
        ('coalescer-stop', {'timeout': 0.25, 'final_flush': True}),
        ('maintenance-stop', {'timeout': 0.25}),
        ('listener-stop', 0.25),
        ('aggregate-stop', {'final_flush': True, 'timeout': 0.25}),
    ]


class _QuartLikeApp:
    """The registration surface ``register_all`` uses on a Quart app."""

    def __init__(self):
        # Quart's name -> blueprint registry. register_all reads it so a
        # plugin cannot shadow a blueprint already mounted on this app.
        self.blueprints = {}
        self.extensions = {}
        # register_all consults the API-version policy via app.config on
        # any Quart-like surface; an empty dict keeps v4 routes dark.
        self.config = {}

    def register_blueprint(self, bp):
        self.blueprints[bp.name] = bp


def test_register_all_only_mounts_blueprints(monkeypatch):
    import routes
    import lib.app.plugin_registry as plugin_registry

    monkeypatch.setattr(plugin_registry, 'discover_blueprint_plugins', lambda: [])
    calls = []
    monkeypatch.setattr(background_services, 'start_registered_background_services',
                        lambda app: calls.append(app) or 0)

    app = _QuartLikeApp()
    routes.register_all(app)
    assert app.blueprints and calls == []

    # Embedders explicitly enter the same lifecycle as production after storage.
    background_services.start_registered_background_services(app)
    assert calls == [app]


def test_plugin_blueprint_cannot_shadow_one_already_mounted(monkeypatch):
    # The guard reads the app's own registry, so a blueprint an embedder
    # mounted before register_all is protected like an in-tree one.
    from quart import Blueprint

    import routes
    import lib.app.plugin_registry as plugin_registry

    app = _QuartLikeApp()
    embedder = Blueprint('embedder_owned', __name__, url_prefix='/embedder')
    app.register_blueprint(embedder)
    shadow = Blueprint('embedder_owned', __name__, url_prefix='/plugin-shadow')
    fresh = Blueprint('fresh_plugin', __name__, url_prefix='/plugin-fresh')
    monkeypatch.setattr(plugin_registry, 'discover_blueprint_plugins',
                        lambda: [shadow, fresh])

    routes.register_all(app)

    assert app.blueprints['embedder_owned'] is embedder
    assert app.blueprints['fresh_plugin'] is fresh


def test_background_service_start_is_per_app_idempotent(monkeypatch):
    import threading
    from types import SimpleNamespace

    import lib.oauth.codex_catalog as codex_catalog
    import lib.oauth.outbound as oauth_outbound
    import lib.scheduler.manager as scheduler_manager
    import routes
    import lib.app.plugin_registry as plugin_registry
    import runtime_guards
    import lib.channels.dx.supervisor as channel_supervisor

    calls = []
    monkeypatch.setattr(channel_supervisor, '_ADAPTERS', {})
    monkeypatch.setattr(channel_supervisor, '_WORKERS', set())
    monkeypatch.setattr(channel_supervisor, '_STOPPING', threading.Event())
    channel_supervisor._STOPPING.set()
    monkeypatch.setattr(
        runtime_guards, 'load_deployment_configuration',
        lambda: SimpleNamespace(mode='personal'))
    def start_scheduler_worker(*, principal):
        assert not channel_supervisor._STOPPING.is_set()
        assert principal.kind == 'system'
        assert principal.owner_user_id == 1
        assert principal.scopes == frozenset({'scheduler:run'})
        calls.append('scheduler')

    monkeypatch.setattr(
        scheduler_manager, 'start_scheduler_worker', start_scheduler_worker)
    monkeypatch.setattr(oauth_outbound, 'reconcile_oauth_providers',
                        lambda: calls.append('oauth-reconcile'))
    monkeypatch.setattr(codex_catalog, 'start_codex_catalog_refresher',
                        lambda: calls.append('codex-catalog') or True)
    monkeypatch.setattr(plugin_registry, 'run_startup_hooks',
                        lambda app: calls.append('plugin') or 1)

    class App:
        extensions = {}

    app = App()
    assert background_services.start_registered_background_services(app) == 3
    assert background_services.start_registered_background_services(app) == 0
    assert calls == [
        'scheduler', 'oauth-reconcile', 'codex-catalog', 'plugin',
    ]


def test_background_service_shutdown_is_paired_and_idempotent(monkeypatch):
    import lib.channels.dx.supervisor as channel_supervisor
    import lib.channels.github.supervisor as github_supervisor
    import lib.evolution.automation as evolution_automation
    import lib.knowledge.enrichment as knowledge_enrichment
    import lib.knowledge.image_ocr_jobs as knowledge_ocr
    import lib.oauth.codex_catalog as codex_catalog
    import lib.scheduler.manager as scheduler_manager
    import routes
    import lib.app.plugin_registry as plugin_registry

    calls = []

    def stopped(name):
        return lambda **kwargs: calls.append((name, kwargs)) or True

    monkeypatch.setattr(
        evolution_automation, 'stop_terminal_checks',
        stopped('evolution-terminal-checks'))
    monkeypatch.setattr(
        knowledge_enrichment, 'stop_visual_enrichment', stopped('knowledge'))
    monkeypatch.setattr(
        knowledge_ocr, 'stop_attachment_image_ocr', stopped('knowledge-ocr'))
    monkeypatch.setattr(
        scheduler_manager, 'stop_scheduler_worker', stopped('scheduler'))
    monkeypatch.setattr(
        codex_catalog, 'stop_codex_catalog_refresher',
        stopped('codex-catalog'))
    monkeypatch.setattr(
        channel_supervisor, 'stop_channel_bridges', stopped('channel-bridges'))
    monkeypatch.setattr(
        github_supervisor, 'stop_github_sessions', stopped('github-sessions'))
    monkeypatch.setattr(
        plugin_registry, 'run_shutdown_hooks',
        lambda app: calls.append(('plugin', {'app': app})) or 1)

    class App:
        extensions = {'tofu_registered_background_services': True}

    app = App()
    assert background_services.stop_registered_background_services(app, timeout=0.25) == 8
    assert background_services.stop_registered_background_services(app, timeout=0.25) == 0
    # Channel sessions stop after the scheduler, whose tick restarts them.
    assert calls == [
        ('plugin', {'app': app}),
        ('evolution-terminal-checks', {'timeout': 0.25}),
        ('knowledge', {'timeout': 0.25}),
        ('knowledge-ocr', {'timeout': 0.25}),
        ('codex-catalog', {'timeout': 0.25}),
        ('scheduler', {'timeout': 0.25}),
        ('channel-bridges', {'timeout': 0.25}),
        ('github-sessions', {'timeout': 0.25}),
    ]


def test_background_service_shutdown_retains_latch_on_timeout(monkeypatch):
    import lib.channels.dx.supervisor as channel_supervisor
    import lib.channels.github.supervisor as github_supervisor
    import lib.evolution.automation as evolution_automation
    import lib.knowledge.enrichment as knowledge_enrichment
    import lib.knowledge.image_ocr_jobs as knowledge_ocr
    import lib.oauth.codex_catalog as codex_catalog
    import lib.scheduler.manager as scheduler_manager
    import routes
    import lib.app.plugin_registry as plugin_registry

    monkeypatch.setattr(
        evolution_automation, 'stop_terminal_checks', lambda **_kwargs: True)
    monkeypatch.setattr(
        knowledge_enrichment, 'stop_visual_enrichment', lambda **_kwargs: True)
    monkeypatch.setattr(
        knowledge_ocr, 'stop_attachment_image_ocr', lambda **_kwargs: True)
    monkeypatch.setattr(
        scheduler_manager, 'stop_scheduler_worker', lambda **_kwargs: False)
    monkeypatch.setattr(
        codex_catalog, 'stop_codex_catalog_refresher', lambda **_kwargs: True)
    monkeypatch.setattr(
        channel_supervisor, 'stop_channel_bridges', lambda **_kwargs: True)
    monkeypatch.setattr(
        github_supervisor, 'stop_github_sessions', lambda **_kwargs: True)
    monkeypatch.setattr(plugin_registry, 'run_shutdown_hooks', lambda _app: 0)

    class App:
        extensions = {'tofu_registered_background_services': True}

    app = App()
    assert background_services.stop_registered_background_services(app) == 6
    assert app.extensions['tofu_registered_background_services'] is True
