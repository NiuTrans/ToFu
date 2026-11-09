"""The rootless Harbor environment keeps the reward channel root-only.

Harbor 0.25.0 prepares every writable mount (including ``/logs/verifier``)
with ``chmod 777`` at environment start, and empties + re-chmods that
directory right before verification, so a de-privileged process in the
container could forge ``reward.txt`` exactly as in the native runner. These
specs pin the adapter's overrides: the same commands now re-seal the verifier
directory to root:root 0700, the agent's own log mount keeps its writability,
and the verifier's exec still runs (as root unless the task names a user).
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

harbor = pytest.importorskip("harbor")

from harbor.models.task.config import EnvironmentConfig, TaskOS
from harbor.models.trial.paths import EnvironmentPaths

from rootless_vm.guest_agent import GuestExecResult
from rootless_vm.harbor_environment import RootlessQemuEnvironment

pytestmark = pytest.mark.unit


def environment(monkeypatch, backend="docker"):
    """The adapter without a VM: only the command construction and exec user are real."""
    instance = RootlessQemuEnvironment.__new__(RootlessQemuEnvironment)
    instance._container_backend = backend
    instance._virtual_time_shift = None
    instance._runc_user_cache = {"root": "0:0", "1000": "1000:1000"}
    instance.task_env_config = EnvironmentConfig(os=TaskOS.LINUX)
    instance.default_user = None
    monkeypatch.setattr(instance, "_resolve_user", lambda user: user)
    monkeypatch.setattr(instance, "_merge_env", lambda env: env)
    monkeypatch.setattr(instance, "_command_network_environment", lambda **kw: {})
    monkeypatch.setattr(instance, "_output_callback", lambda: None)
    monkeypatch.setattr(instance, "_outer_exec", AsyncMock(return_value=GuestExecResult(
        return_code=0, stdout=b"", stderr=b"")))
    return instance


def mounts():
    paths = EnvironmentPaths.for_os(TaskOS.LINUX)
    return [str(paths.verifier_dir), str(paths.agent_dir)]


def test_start_and_verifier_prep_seal_the_verifier_mount(monkeypatch):
    instance = environment(monkeypatch)
    for command in (instance._ensure_dirs_command(mounts(), chmod=True),
                    instance._empty_dirs_command(["/logs/verifier"], chmod=True),
                    instance._reset_dirs_command(remove_dirs=["/logs/verifier"],
                                                 create_dirs=["/logs/verifier"], chmod_dirs=["/logs/verifier"])):
        assert "chown 0:0 /logs/verifier" in command, command
        assert "chmod 0700 /logs/verifier" in command, command
        # Harbor's own preparation still runs first; the seal is what follows it.
        assert command.index("chmod 777 /logs/verifier") < command.index("chmod 0700 /logs/verifier")


def test_agent_log_mount_keeps_its_writability(monkeypatch):
    instance = environment(monkeypatch)
    command = instance._ensure_dirs_command(mounts(), chmod=True)
    assert "chmod 777 /logs/verifier /logs/agent" in command
    assert command.endswith("chown 0:0 /logs/verifier && chmod 0700 /logs/verifier")
    # No blanket tightening of the agent's directory, and no change to unrelated mounts.
    assert "chown 0:0 /logs/agent" not in command
    other = instance._ensure_dirs_command(["/logs/agent"], chmod=True)
    assert other == "mkdir -p /logs/agent && chmod 777 /logs/agent"


def test_verifier_exec_runs_as_root_and_keeps_the_guest_watchdog(monkeypatch):
    for backend in ("docker", "runc"):
        instance = environment(monkeypatch, backend)
        asyncio.run(instance.exec("bash /tests/test.sh > /logs/verifier/test-stdout.txt 2>&1", timeout_sec=30))
        argv = instance._outer_exec.await_args.args[0]
        expected = "--user 0 " if backend == "docker" else "--user 0:0 "
        assert expected in argv, argv
        assert "bash /tests/test.sh > /logs/verifier/test-stdout.txt 2>&1" in argv
        assert instance._outer_exec.await_count == 1, "no extra grant is needed for the root verifier"


def test_task_named_verifier_user_gets_the_directory_not_a_world_writable_one(monkeypatch):
    instance = environment(monkeypatch)
    asyncio.run(instance.exec("bash /tests/test.sh > /logs/verifier/test-stdout.txt 2>&1",
                              timeout_sec=30, user=1000))
    grant = instance._outer_exec.await_args_list[0].args[0]
    assert "chown -R 1000:1000 /logs/verifier" in grant
    assert "chmod 0700 /logs/verifier" in grant
    assert "chmod 777" not in grant
    run = instance._outer_exec.await_args_list[1].args[0]
    assert "--user 1000 " in run
    assert instance._outer_exec.await_count == 2


def test_agent_commands_do_not_touch_the_verifier_directory(monkeypatch):
    instance = environment(monkeypatch)
    asyncio.run(instance.exec("ls /logs/agent", timeout_sec=10))
    assert instance._outer_exec.await_count == 1
    assert "--user" not in instance._outer_exec.await_args.args[0]
