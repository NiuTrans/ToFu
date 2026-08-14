"""tests/test_schema_registry.py — unit tests for lib/database/schema_registry.py.

Pure in-memory: no DB connection, no init_db(). Verifies the registry's
contract — optional-domain registration, the active_domains() cache key,
run_registered ordering, and idempotent register/unregister.
"""

import pytest

from lib.database import schema_registry as sr

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_registry():
    """Snapshot + restore the module-global registry around each test."""
    saved = dict(sr._INITIALIZERS)
    sr._INITIALIZERS.clear()
    yield
    sr._INITIALIZERS.clear()
    sr._INITIALIZERS.update(saved)


def test_register_and_active_domains_sorted():
    sr.register_schema_initializer('trading', lambda conn: None)
    sr.register_schema_initializer('alpha', lambda conn: None)
    assert sr.active_domains() == ['alpha', 'trading']


def test_duplicate_ignored_without_replace():
    first = lambda conn: None
    second = lambda conn: None
    sr.register_schema_initializer('trading', first)
    sr.register_schema_initializer('trading', second)  # ignored
    assert sr._INITIALIZERS['trading'] is first
    sr.register_schema_initializer('trading', second, replace=True)
    assert sr._INITIALIZERS['trading'] is second


def test_empty_domain_rejected():
    sr.register_schema_initializer('', lambda conn: None)
    assert sr.active_domains() == []


def test_run_registered_runs_each_in_sorted_order():
    calls = []
    sr.register_schema_initializer('trading', lambda conn: calls.append('trading'))
    sr.register_schema_initializer('alpha', lambda conn: calls.append('alpha'))
    sr.run_registered(conn=object())
    assert calls == ['alpha', 'trading']


def test_run_registered_propagates_initializer_error():
    def boom(conn):
        raise RuntimeError('ddl failed')
    sr.register_schema_initializer('bad', boom)
    with pytest.raises(RuntimeError, match='ddl failed'):
        sr.run_registered(conn=object())


def test_unregister():
    sr.register_schema_initializer('trading', lambda conn: None)
    sr.unregister_schema_initializer('trading')
    assert sr.active_domains() == []
    # idempotent — removing a missing domain is a no-op
    sr.unregister_schema_initializer('trading')


def test_discover_is_fail_soft_with_no_plugins(monkeypatch):
    # With no tofu.schema entry points → returns 0, never raises. Isolate
    # from the AMBIENT environment: this host has the real tofu_trading
    # plugin installed, which would otherwise leak into the discovery.
    import importlib.metadata
    monkeypatch.setattr(importlib.metadata, 'entry_points',
                        lambda *a, **k: [])
    assert sr.discover_schema_plugins() == 0


def test_db_init_discovery_is_once_and_test_isolated(monkeypatch):
    from lib.database import _core

    calls = []
    monkeypatch.setattr(sr, 'discover_schema_plugins',
                        lambda: calls.append('scan') or 0)
    monkeypatch.setattr(_core, '_OPTIONAL_DOMAINS_DISCOVERED', False)
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'unit::schema')
    monkeypatch.delenv('TOFU_TEST_SCHEMA_PLUGIN_DISCOVERY', raising=False)

    _core._register_optional_domains()
    assert calls == [], 'ordinary tests must not load ambient installed plugins'

    monkeypatch.setenv('TOFU_TEST_SCHEMA_PLUGIN_DISCOVERY', '1')
    _core._register_optional_domains()
    _core._register_optional_domains()
    assert calls == ['scan'], 'metadata discovery must be process-once'
