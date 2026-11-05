"""Shared pytest fixtures for the tofu-search test suite.

All tests here are offline: nothing hits the network. Engine/HTTP paths are
exercised via monkeypatching, and the pure helpers (dedup, rerank, vertical
intent detection, URL guards) run directly.
"""

import sys
from pathlib import Path

import pytest

# These tests certify this checkout's package source, never an editable
# install that points at another checkout.
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PACKAGE_ROOT))

import tofu_search  # noqa: E402
import tofu_search.config as _config  # noqa: E402
import tofu_search.providers as _providers  # noqa: E402

if not Path(tofu_search.__file__).resolve().is_relative_to(_PACKAGE_ROOT):
    raise pytest.UsageError(
        f'tofu_search was imported from {tofu_search.__file__}, not {_PACKAGE_ROOT}')


@pytest.fixture(autouse=True)
def _reject_unmocked_httpx_network(monkeypatch):
    """Fail even if the product catches an unexpected offline-test request."""
    import httpx

    attempts = []
    def reject(self, request):
        attempts.append(request.method)
        raise AssertionError('Unmocked HTTPX network request in offline search test')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', reject)
    yield
    assert not attempts, 'Offline test reached the real HTTPX network transport'


@pytest.fixture(autouse=True)
def _reset_engine_breakers():
    """Breaker outcomes are process-global and shared by every engine module.

    A failure leaked from an earlier test would make that engine an un-awaited
    probe in a later search.
    """
    from tofu_search.search import _common

    _common.engine_circuit._state.clear()
    yield
    _common.engine_circuit._state.clear()


@pytest.fixture(autouse=True)
def _reset_global_config():
    """Snapshot and restore the global SearchConfig around every test.

    configure() mutates a process-global singleton; without this an early
    test could leak settings into a later one.
    """
    saved = _config._global_config
    _config._global_config = _config.SearchConfig()
    try:
        yield
    finally:
        _config._global_config = saved


@pytest.fixture(autouse=True)
def _reset_optional_providers():
    """Provider seams are process-global but must remain optional per test."""
    saved = (
        _providers.get_browser_provider(),
        _providers.get_site_search_provider(),
        _providers.get_auth_source_provider(),
        _providers.get_site_knowledge_provider(),
    )
    _providers.register_browser_provider(None)
    _providers.register_site_search_provider(None)
    _providers.register_auth_source_provider(None)
    _providers.register_site_knowledge_provider(None)
    try:
        yield
    finally:
        _providers.register_browser_provider(saved[0])
        _providers.register_site_search_provider(saved[1])
        _providers.register_auth_source_provider(saved[2])
        _providers.register_site_knowledge_provider(saved[3])
