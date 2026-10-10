"""Environment contract: guest exits, transport loss and phase cancellation differ."""
import asyncio
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("harbor")
from rootless_vm.guest_agent import GuestExecResult
from rootless_vm.harbor_environment import RootlessQemuEnvironment

pytestmark = pytest.mark.unit


def environment(monkeypatch, outcome):
    instance = RootlessQemuEnvironment.__new__(RootlessQemuEnvironment)
    instance._container_backend = "docker"
    instance._virtual_time_shift = None
    monkeypatch.setattr(instance, "_resolve_user", lambda user: user)
    monkeypatch.setattr(instance, "_merge_env", lambda env: env)
    monkeypatch.setattr(instance, "_command_network_environment", lambda **kw: {})
    monkeypatch.setattr(instance, "_output_callback", lambda: None)
    operation = AsyncMock(**({"side_effect": outcome} if isinstance(outcome, BaseException)
                             else {"return_value": outcome}))
    monkeypatch.setattr(instance, "_outer_exec", operation)
    return instance, operation


def test_transport_timeout_never_fabricates_guest_exit(monkeypatch):
    instance, operation = environment(monkeypatch, TimeoutError("lost guest channel"))
    with pytest.raises(RuntimeError, match="termination is unconfirmed") as raised:
        asyncio.run(instance.exec("sleep 100", timeout_sec=1))
    assert type(raised.value).__name__ == "GuestChannelTimeoutError"
    assert isinstance(raised.value.__cause__, TimeoutError)
    assert not isinstance(raised.value, TimeoutError)
    assert operation.await_count == 1  # A lost acknowledgement cannot authorize replay.


def test_confirmed_guest_timeout_remains_an_ordinary_exit(monkeypatch):
    instance, _ = environment(monkeypatch, GuestExecResult(
        return_code=124, stdout=b"partial output", stderr=b"deadline reached"))
    result = asyncio.run(instance.exec("sleep 100", timeout_sec=1))
    assert result.return_code == 124
    assert result.stdout == "partial output"
    assert result.stderr == "deadline reached"


def test_phase_cancellation_propagates(monkeypatch):
    instance, operation = environment(monkeypatch, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(instance.exec("sleep 100", timeout_sec=1))
    assert operation.await_count == 1
