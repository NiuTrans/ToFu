"""Shared pytest fixtures for the tofu-search test suite.

All tests here are offline: nothing hits the network. Engine/HTTP paths are
exercised via monkeypatching, and the pure helpers (dedup, rerank, vertical
intent detection, URL guards) run directly.
"""

import pytest

import tofu_search.config as _config
import tofu_search.providers as _providers


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
