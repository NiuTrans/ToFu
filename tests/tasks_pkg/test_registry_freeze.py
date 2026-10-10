"""D2 freeze contract: the fallback mutates only inside audited windows."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def registry():
    from lib.tasks_pkg.executor._registry import ToolRegistry
    return ToolRegistry()


def test_frozen_registry_refuses_every_mutation_kind(registry):
    from lib.tasks_pkg.executor._registry import RegistryFrozenError
    registry.register('alpha', object())
    registry.freeze()
    assert registry.frozen is True
    with pytest.raises(RegistryFrozenError):
        registry.register('beta', object())
    with pytest.raises(RegistryFrozenError):
        registry.register_set({'gamma', 'delta'}, object())
    with pytest.raises(RegistryFrozenError):
        registry.register_special('__special__', object())
    with pytest.raises(RegistryFrozenError):
        registry.unregister('alpha')
    # Reads stay open: freezing seals mutation, never resolution.
    assert registry.lookup('alpha') is not None


def test_registration_window_is_explicit_audited_and_restores(registry):
    from lib.tasks_pkg.executor._registry import RegistryFrozenError
    registry.freeze()
    with registry.registration_window(reason='plugin lifecycle'):
        registry.register('beta', object())
        assert registry.frozen is False
    assert registry.frozen is True
    assert registry.lookup('beta') is not None
    with pytest.raises(RegistryFrozenError):
        registry.unregister('beta')


def test_frozen_singleton_refuses_request_scoped_mutation():
    """After the startup freeze (app assembly), resolution is unaffected and
    a request-scoped mutation is refused typed; restored after the test."""
    from lib.tasks_pkg.executor import tool_registry
    from lib.tasks_pkg.executor._registry import RegistryFrozenError
    snap = tool_registry.snapshot()
    try:
        tool_registry.freeze()
        assert tool_registry.lookup('grep_search') is not None
        with pytest.raises(RegistryFrozenError):
            tool_registry.register('request-scoped-probe', object())
    finally:
        tool_registry.restore(snap)
