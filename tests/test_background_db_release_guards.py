"""Long-lived/ephemeral background loops must release DB leases while idle."""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('module_name,function_name', [
    ('routes.common', '_refresh_db_probe'),
    ('lib.daily_report.scheduler', '_scheduler_loop'),
])
def test_background_function_has_explicit_connection_release(
        module_name, function_name):
    module = __import__(module_name, fromlist=[function_name])
    src = inspect.getsource(getattr(module, function_name))
    assert 'close_thread_db' in src
    assert 'finally:' in src


def test_health_probe_releases_even_when_query_fails(monkeypatch):
    import lib.database as dbmod
    import routes.common as common

    released = []

    def _fail():
        raise RuntimeError('probe failed')

    monkeypatch.setattr(dbmod, 'get_thread_db', _fail)
    monkeypatch.setattr(dbmod, 'close_thread_db', lambda: released.append(True))
    monkeypatch.setattr(common, '_db_probe_cache', {
        'at': 0.0, 'responsive': None, 'error': '', 'ever': False})

    common._refresh_db_probe()

    assert released == [True]
    assert common._db_probe_cache['responsive'] is False
