"""Importing the app must register routes without launching real workers."""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit


def test_server_import_defers_route_workers_to_serving_lifecycle():
    import server
    import lib.app_assembly as app_assembly
    import lib.server_background_services as background_services

    source = inspect.getsource(server)
    assert 'register_all(app, start_workers=False)' in inspect.getsource(
        app_assembly)
    worker = inspect.getsource(background_services.start_background_services)
    assert 'start_registered_background_services(app)' in worker
    assert 'start_janitor()' in worker
    assert 'start_prober()' in worker
    assert 'bootstrap_personal_key()' in worker
    assert 'load_saved_proxy_config()' in worker
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


def test_async_logging_workers_are_owned_by_quart_lifecycle(monkeypatch):
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
            return True

    listener = Listener()
    monkeypatch.setattr(server, '_LOG_UNDER_PYTEST', False)
    monkeypatch.setattr(server, '_log_listener', listener)
    monkeypatch.setattr(server, '_log_agg_enabled', lambda: True)
    monkeypatch.setattr(server, '_log_agg_start_flusher',
                        lambda: calls.append('aggregate-start'))
    monkeypatch.setattr(
        server, '_log_agg_stop_flusher',
        lambda **kwargs: calls.append(('aggregate-stop', kwargs)) or True)

    assert server._start_logging_runtime() is True
    assert server._start_logging_runtime() is False
    assert server._stop_logging_runtime(timeout=0.25) is True
    assert calls == [
        'listener-start', 'aggregate-start',
        ('aggregate-stop', {'final_flush': True, 'timeout': 0.25}),
        ('listener-stop', 0.25),
    ]


def test_register_all_flag_controls_worker_start(monkeypatch):
    import routes
    import routes.plugin_registry as plugin_registry

    class App:
        def __init__(self):
            self.names = []
            self.extensions = {}

        def register_blueprint(self, bp):
            self.names.append(bp.name)

    monkeypatch.setattr(plugin_registry, 'discover_blueprint_plugins', lambda: [])
    calls = []
    monkeypatch.setattr(routes, 'start_registered_background_services',
                        lambda app: calls.append(app) or 0)

    deferred = App()
    routes.register_all(deferred, start_workers=False)
    assert deferred.names and calls == []

    eager = App()
    routes.register_all(eager)
    assert eager.names and calls == [eager]


def test_background_service_start_is_per_app_idempotent(monkeypatch):
    import lib.daily_report
    import lib.oauth.codex_catalog as codex_catalog
    import lib.llm_dispatch.model_catalog_sync as model_catalog_sync
    import lib.scheduler
    import routes
    import routes.plugin_registry as plugin_registry

    calls = []
    monkeypatch.setattr(lib.daily_report, 'start_report_scheduler',
                        lambda: calls.append('daily'))
    monkeypatch.setattr(lib.scheduler, 'start_scheduler_worker',
                        lambda: calls.append('scheduler'))
    monkeypatch.setattr(codex_catalog, 'start_codex_catalog_refresher',
                        lambda: calls.append('codex-catalog'))
    monkeypatch.setattr(model_catalog_sync, 'start_model_catalog_sync',
                        lambda: calls.append('model-catalog'))
    monkeypatch.setattr(plugin_registry, 'run_startup_hooks',
                        lambda app: calls.append('plugin') or 1)

    class App:
        extensions = {}

    app = App()
    assert routes.start_registered_background_services(app) == 5
    assert routes.start_registered_background_services(app) == 0
    assert calls == ['daily', 'scheduler', 'codex-catalog', 'model-catalog', 'plugin']


def test_background_service_shutdown_is_paired_and_idempotent(monkeypatch):
    import lib.daily_report
    import lib.knowledge.enrichment as knowledge_enrichment
    import lib.llm_dispatch.model_catalog_sync as model_catalog_sync
    import lib.oauth.codex_catalog as codex_catalog
    import lib.scheduler
    import routes
    import routes.plugin_registry as plugin_registry

    calls = []

    def stopped(name):
        return lambda **kwargs: calls.append((name, kwargs)) or True

    monkeypatch.setattr(
        lib.daily_report, 'stop_report_scheduler', stopped('daily'))
    monkeypatch.setattr(
        knowledge_enrichment, 'stop_visual_enrichment', stopped('knowledge'))
    monkeypatch.setattr(
        lib.scheduler, 'stop_scheduler_worker', stopped('scheduler'))
    monkeypatch.setattr(
        codex_catalog, 'stop_codex_catalog_refresher',
        stopped('codex-catalog'))
    monkeypatch.setattr(
        model_catalog_sync, 'stop_model_catalog_sync',
        stopped('model-catalog'))
    monkeypatch.setattr(
        plugin_registry, 'run_shutdown_hooks',
        lambda app: calls.append(('plugin', {'app': app})) or 1)

    class App:
        extensions = {'tofu_registered_background_services': True}

    app = App()
    assert routes.stop_registered_background_services(app, timeout=0.25) == 6
    assert routes.stop_registered_background_services(app, timeout=0.25) == 0
    assert calls == [
        ('plugin', {'app': app}),
        ('knowledge', {'timeout': 0.25}),
        ('model-catalog', {'timeout': 0.25}),
        ('codex-catalog', {'timeout': 0.25}),
        ('scheduler', {'timeout': 0.25}),
        ('daily', {'timeout': 0.25}),
    ]


def test_background_service_shutdown_retains_latch_on_timeout(monkeypatch):
    import lib.daily_report
    import lib.knowledge.enrichment as knowledge_enrichment
    import lib.llm_dispatch.model_catalog_sync as model_catalog_sync
    import lib.oauth.codex_catalog as codex_catalog
    import lib.scheduler
    import routes
    import routes.plugin_registry as plugin_registry

    monkeypatch.setattr(
        lib.daily_report, 'stop_report_scheduler', lambda **_kwargs: False)
    monkeypatch.setattr(
        knowledge_enrichment, 'stop_visual_enrichment', lambda **_kwargs: True)
    monkeypatch.setattr(
        lib.scheduler, 'stop_scheduler_worker', lambda **_kwargs: True)
    monkeypatch.setattr(
        codex_catalog, 'stop_codex_catalog_refresher', lambda **_kwargs: True)
    monkeypatch.setattr(
        model_catalog_sync, 'stop_model_catalog_sync', lambda **_kwargs: True)
    monkeypatch.setattr(plugin_registry, 'run_shutdown_hooks', lambda _app: 0)

    class App:
        extensions = {'tofu_registered_background_services': True}

    app = App()
    assert routes.stop_registered_background_services(app) == 4
    assert app.extensions['tofu_registered_background_services'] is True
