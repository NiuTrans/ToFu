"""Resource contract for opt-in real-Sidecar pytest plugins."""

from types import SimpleNamespace

import pytest

from tests.support.sidecar_fixtures import module_declares_plugin


pytestmark = pytest.mark.unit


def _request(declared=()):
    return SimpleNamespace(module=SimpleNamespace(pytest_plugins=declared))


def test_sidecar_plugin_runs_only_for_the_module_that_declared_it():
    plugin = 'tests._artifact_sidecar'

    assert module_declares_plugin(_request((plugin,)), plugin) is True
    assert module_declares_plugin(_request(plugin), plugin) is True
    assert module_declares_plugin(_request(('tests._other_plugin',)), plugin) is False
    assert module_declares_plugin(_request(), plugin) is False


def test_malformed_module_plugin_declaration_fails_closed():
    request = _request(declared=None)

    assert module_declares_plugin(request, 'tests._artifact_sidecar') is False
