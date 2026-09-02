"""Contracts for the bounded run-command Python bytecode cache."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

import pytest

from lib.project_mod import portable_sandbox
from lib.project_mod import python_bytecode_cache as cache
from lib.storage_sidecar.storage_capabilities import MountDescription


pytestmark = pytest.mark.unit


def _mount(storage_class: str) -> MountDescription:
    return MountDescription(
        filesystem_type=(
            "fuse.beegfs" if storage_class == "network-filesystem" else "ext4"
        ),
        mount_point="/",
        storage_class=storage_class,
        persistence=(
            "unknown" if storage_class == "network-filesystem" else "ephemeral"
        ),
    )


def _policy(root: Path, *, max_bytes: int = 8 * 1024 * 1024) \
        -> cache.PythonBytecodeCachePolicy:
    return cache.PythonBytecodeCachePolicy(
        mode="on",
        cache_root=str(root),
        max_bytes=max_bytes,
        max_files=10_000,
        max_namespaces=8,
        reserve_bytes=0,
        ttl_seconds=3600,
    )


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPYCACHEPREFIX", None)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    return environment


def _make_network_local_probe(monkeypatch, workspace: Path) -> None:
    workspace_real = os.path.realpath(workspace)

    def describe(path):
        return _mount(
            "network-filesystem"
            if os.path.realpath(path) == workspace_real
            else "local-block"
        )

    monkeypatch.setattr(cache, "describe_mount", describe)


@pytest.mark.parametrize(
    ("command", "eligible"),
    [
        ("python3 script.py", True),
        ("python3 -m pytest -q", True),
        (f"{sys.executable} -u script.py --label 'two words'", True),
        ("python3 -c 'import json'", False),
        ("python3 -S script.py", False),
        ("python3 -OB script.py", False),
        ("python3 -I script.py", False),
        ("python3 -X pycache_prefix=/tmp/custom script.py", False),
        ("python3 script.py | head", False),
        ("python3 script.py && echo done", False),
        ("pypy3 script.py", False),
    ],
)
def test_parser_only_accepts_static_import_capable_cpython_shapes(
        command, eligible):
    assert (cache.parse_python_workload(command) is not None) is eligible


def test_activation_requires_explicit_owner_and_remote_source(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _make_network_local_probe(monkeypatch, workspace)
    environment = _environment()
    policy = _policy(tmp_path / "cache")
    command = f"{sys.executable} script.py"

    assert cache.prepare_python_bytecode_cache(
        command, str(workspace), {}, environment, policy=policy
    ) is None

    monkeypatch.setattr(cache, "describe_mount", lambda _path: _mount("local-block"))
    assert cache.prepare_python_bytecode_cache(
        command,
        str(workspace),
        {"_userId": 7},
        environment,
        policy=policy,
    ) is None
    assert "PYTHONPYCACHEPREFIX" not in environment


def test_existing_python_bytecode_policy_is_never_overridden(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _make_network_local_probe(monkeypatch, workspace)
    environment = _environment()
    environment["PYTHONPYCACHEPREFIX"] = "/operator/cache"

    activation = cache.prepare_python_bytecode_cache(
        f"{sys.executable} script.py",
        str(workspace),
        {"_userId": 7},
        environment,
        policy=_policy(tmp_path / "cache"),
    )

    assert activation is None
    assert environment["PYTHONPYCACHEPREFIX"] == "/operator/cache"


def test_broad_temp_root_is_rejected_without_changing_permissions(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _make_network_local_probe(monkeypatch, workspace)
    broad_root = Path(tempfile.gettempdir())
    mode_before = broad_root.stat().st_mode & 0o777
    policy = _policy(broad_root)

    activation = cache.prepare_python_bytecode_cache(
        f"{sys.executable} script.py",
        str(workspace),
        {"_userId": 8},
        _environment(),
        policy=policy,
    )

    assert activation is None
    assert broad_root.stat().st_mode & 0o777 == mode_before


def test_auto_mode_avoids_cold_tax_for_one_shot_workloads(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _make_network_local_probe(monkeypatch, workspace)
    policy = replace(_policy(tmp_path / "cache"), mode="auto")

    one_shot = cache.prepare_python_bytecode_cache(
        f"{sys.executable} script.py",
        str(workspace),
        {"_userId": 9},
        _environment(),
        policy=policy,
    )
    informational = cache.prepare_python_bytecode_cache(
        f"{sys.executable} -m pytest --version",
        str(workspace),
        {"_userId": 9},
        _environment(),
        policy=policy,
    )
    repeated_first = cache.prepare_python_bytecode_cache(
        f"{sys.executable} -m pytest -q tests",
        str(workspace),
        {"_userId": 9},
        _environment(),
        policy=policy,
    )
    repeated_second = cache.prepare_python_bytecode_cache(
        f"{sys.executable} -m pytest -q tests",
        str(workspace),
        {"_userId": 9},
        _environment(),
        policy=policy,
    )

    assert one_shot is None
    assert informational is None
    assert repeated_first is None
    assert repeated_second is not None
    cache.release_python_bytecode_cache(repeated_second)


def test_real_python_writes_to_owner_workspace_interpreter_namespace(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "helper.py").write_text("VALUE = 42\n", encoding="utf-8")
    script = workspace / "main.py"
    script.write_text("import helper\nprint(helper.VALUE)\n", encoding="utf-8")
    _make_network_local_probe(monkeypatch, workspace)
    environment = _environment()

    policy = _policy(tmp_path / "cache")
    activation = cache.prepare_python_bytecode_cache(
        f"{sys.executable} main.py",
        str(workspace),
        {"_userId": 11},
        environment,
        policy=policy,
    )
    assert activation is not None
    try:
        completed = subprocess.run(
            [sys.executable, "main.py"],
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0
        assert completed.stdout.strip() == "42"
        assert list(Path(activation.pycache_prefix).rglob("*.pyc"))
        assert oct(Path(activation.namespace).stat().st_mode & 0o777) == "0o700"
    finally:
        cache.release_python_bytecode_cache(activation)

    warm_auto = cache.prepare_python_bytecode_cache(
        f"{sys.executable} main.py",
        str(workspace),
        {"_userId": 11},
        _environment(),
        policy=replace(policy, mode="auto"),
    )
    assert warm_auto is not None
    cache.release_python_bytecode_cache(warm_auto)


def test_owner_changes_select_a_different_namespace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _make_network_local_probe(monkeypatch, workspace)
    policy = _policy(tmp_path / "cache")
    activations = []
    try:
        for owner in (21, 22):
            activation = cache.prepare_python_bytecode_cache(
                f"{sys.executable} script.py",
                str(workspace),
                {"_userId": owner},
                _environment(),
                policy=policy,
            )
            assert activation is not None
            activations.append(activation)
        assert activations[0].namespace != activations[1].namespace
    finally:
        for activation in activations:
            cache.release_python_bytecode_cache(activation)


def test_over_budget_namespace_is_reclaimed_after_last_active_user(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _make_network_local_probe(monkeypatch, workspace)
    policy = _policy(tmp_path / "cache", max_bytes=2 * 1024 * 1024)
    first = cache.prepare_python_bytecode_cache(
        f"{sys.executable} script.py",
        str(workspace),
        {"_userId": 31},
        _environment(),
        policy=policy,
    )
    second = cache.prepare_python_bytecode_cache(
        f"{sys.executable} script.py",
        str(workspace),
        {"_userId": 31},
        _environment(),
        policy=policy,
    )
    assert first is not None and second is not None
    namespace = Path(first.namespace)
    (Path(first.pycache_prefix) / "oversized.pyc").write_bytes(
        b"x" * (3 * 1024 * 1024))

    cache.release_python_bytecode_cache(first)
    assert namespace.exists(), "one concurrent user still owns the namespace"
    cache.release_python_bytecode_cache(second)
    assert not namespace.exists(), "disposable bytes must return below the cap"


def test_entry_budget_and_ttl_reclaim_inactive_namespaces(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _make_network_local_probe(monkeypatch, workspace)
    policy = replace(
        _policy(tmp_path / "cache"),
        max_files=4,
        ttl_seconds=1,
    )
    first = cache.prepare_python_bytecode_cache(
        f"{sys.executable} script.py",
        str(workspace),
        {"_userId": 32},
        _environment(),
        policy=policy,
    )
    assert first is not None
    first_namespace = Path(first.namespace)
    (Path(first.pycache_prefix) / "one.pyc").write_bytes(b"x")
    cache.release_python_bytecode_cache(first)
    assert not first_namespace.exists(), "entry ceiling is enforced post-run"

    ttl_policy = replace(policy, max_files=10_000)
    stale = cache.prepare_python_bytecode_cache(
        f"{sys.executable} script.py",
        str(workspace),
        {"_userId": 33},
        _environment(),
        policy=ttl_policy,
    )
    assert stale is not None
    stale_namespace = Path(stale.namespace)
    cache.release_python_bytecode_cache(stale)
    old = time.time() - 60
    os.utime(stale_namespace / ".last-used", (old, old))

    current = cache.prepare_python_bytecode_cache(
        f"{sys.executable} script.py",
        str(workspace),
        {"_userId": 34},
        _environment(),
        policy=ttl_policy,
    )
    assert current is not None
    assert not stale_namespace.exists()
    cache.release_python_bytecode_cache(current)


def test_disk_reserve_failure_leaves_python_environment_unchanged(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _make_network_local_probe(monkeypatch, workspace)
    environment = _environment()

    class Usage:
        total = 1024
        used = 1023
        free = 1

    monkeypatch.setattr(cache.shutil, "disk_usage", lambda _path: Usage())
    activation = cache.prepare_python_bytecode_cache(
        f"{sys.executable} script.py",
        str(workspace),
        {"_userId": 34},
        environment,
        policy=_policy(tmp_path / "cache"),
    )

    assert activation is None
    assert "PYTHONPYCACHEPREFIX" not in environment
    assert not list((tmp_path / "cache").glob("ns-*"))


def test_cross_process_lease_blocks_cleanup_while_command_is_active(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _make_network_local_probe(monkeypatch, workspace)
    activation = cache.prepare_python_bytecode_cache(
        f"{sys.executable} script.py",
        str(workspace),
        {"_userId": 35},
        _environment(),
        policy=_policy(tmp_path / "cache"),
    )
    assert activation is not None
    root = Path(activation.cache_root)
    namespace = Path(activation.namespace)

    assert not cache._safe_remove_namespace(root, namespace)
    assert namespace.exists()
    cache.release_python_bytecode_cache(activation)
    assert cache._safe_remove_namespace(root, namespace)


def test_run_command_passes_one_mutated_environment_and_releases(
        tmp_path, monkeypatch):
    from lib.project_mod import run_command

    namespace = tmp_path / "cache" / (
        "ns-000000000000-0000000000000000-0000000000000000")
    namespace.mkdir(parents=True)
    namespace.chmod(0o700)
    policy = _policy(tmp_path / "cache")
    activation = cache.PythonBytecodeCacheActivation(
        cache_root=str(tmp_path / "cache"),
        namespace=str(namespace),
        pycache_prefix=str(namespace / "pycache"),
        policy=policy,
    )
    released = []

    def prepare(_command, _cwd, _task, environment):
        environment["TOFU_PYTHON_CACHE_TEST"] = "visible"
        return activation

    monkeypatch.setattr(cache, "prepare_python_bytecode_cache", prepare)
    monkeypatch.setattr(
        cache,
        "release_python_bytecode_cache",
        lambda value: released.append(value),
    )
    output = run_command.tool_run_command(
        str(tmp_path),
        f"{sys.executable} -c \"import os; "
        "print(os.environ.get('TOFU_PYTHON_CACHE_TEST'))\"",
        task={"_userId": 41},
    )

    assert "visible" in output
    assert released == [activation]


def test_bwrap_binds_only_the_validated_exact_cache_namespace(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    namespace = tmp_path / "cache" / (
        "ns-000000000000-0000000000000000-0000000000000000")
    namespace.mkdir(parents=True)
    namespace.chmod(0o700)
    monkeypatch.setattr(portable_sandbox, "_ENABLED", True)
    monkeypatch.setattr(portable_sandbox, "_probe_backend", lambda: "bwrap")

    wrapped = portable_sandbox.wrap_command(
        "python3 main.py",
        str(workspace),
        writable_cache_dir=str(namespace),
    )
    arguments = shlex.split(wrapped)
    bind_indexes = [
        index for index, value in enumerate(arguments) if value == "--bind"
    ]

    assert [arguments[index + 1:index + 3] for index in bind_indexes] == [
        [str(workspace), str(workspace)],
        [str(namespace), str(namespace)],
    ]
    assert str(namespace.parent) not in arguments
