"""Host scratch placement is independent of guest paths and tempfile globals."""
from __future__ import annotations

import ast
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from rootless_vm import host_paths
from rootless_vm.qemu import QemuRuntime

pytestmark = pytest.mark.unit


def test_resolution_precedence_and_environment_independence(host_tmp_path, monkeypatch):
    # Simulate tempfile having cached a root before policy was configured. No
    # allocation is made in that cached location.
    monkeypatch.setattr(tempfile, "tempdir", "/tmp")
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, "/tmp")
    monkeypatch.setenv("TOFU_EVAL_ROOT", str(host_tmp_path / "eval"))
    monkeypatch.delenv("ROOTLESS_VM_TMPDIR", raising=False)
    before = dict(os.environ)
    assert host_paths.private_temporary_root() == host_tmp_path / "eval/.runtime/tmp"
    assert dict(os.environ) == before
    assert tempfile.tempdir == "/tmp"
    monkeypatch.setenv("ROOTLESS_VM_TMPDIR", str(host_tmp_path / "explicit"))
    assert host_paths.private_temporary_root() == host_tmp_path / "explicit"
    monkeypatch.setenv("ROOTLESS_VM_TMPDIR", str(host_tmp_path / "second"))
    assert host_paths.private_temporary_root() == host_tmp_path / "second"


def test_checkout_default_and_owner_precedence(host_tmp_path, monkeypatch):
    monkeypatch.delenv("ROOTLESS_VM_TMPDIR", raising=False)
    monkeypatch.delenv("TOFU_EVAL_ROOT", raising=False)
    monkeypatch.setattr(host_paths, "__file__", str(host_tmp_path / "rootless_vm/host_paths.py"))
    assert host_paths.private_temporary_root() == host_tmp_path / ".eval-runs/.runtime/tmp"
    monkeypatch.setenv("ROOTLESS_VM_TMPDIR", "/tmp")
    assert host_paths.private_temporary_root(owner_root=host_tmp_path / "trial") == host_tmp_path / "trial/.runtime/tmp"


@pytest.mark.parametrize("path", ["/tmp", "/tmp/a", "/var/tmp/a", "/dev/shm/a", "/", "relative", "/safe/../tmp/a"])
def test_reject_shared_and_ambiguous_paths(path):
    with pytest.raises(ValueError):
        host_paths.validate_host_path(path)


@pytest.mark.parametrize("key", ["ROOTLESS_VM_TMPDIR", "TOFU_EVAL_ROOT"])
def test_explicit_invalid_environment_never_falls_back(host_tmp_path, monkeypatch, key):
    monkeypatch.delenv("ROOTLESS_VM_TMPDIR", raising=False)
    monkeypatch.setenv(key, "")
    with pytest.raises(ValueError):
        host_paths.private_temporary_root()


def test_reject_symlink_components_and_unsafe_private_roots(host_tmp_path, monkeypatch):
    link = host_tmp_path / "link"
    link.symlink_to(host_tmp_path / "missing", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        host_paths.validate_host_path(link / "scratch")
    link.unlink()
    link.symlink_to("/tmp", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        host_paths.validate_host_path(link / "scratch")
    root = host_tmp_path / "public"
    root.mkdir(mode=0o755)

    root.chmod(0o755)
    monkeypatch.setenv("ROOTLESS_VM_TMPDIR", str(root))
    with pytest.raises(PermissionError, match="private"):
        host_paths.private_temporary_root()
    assert root.stat().st_mode & 0o777 == 0o755
    root.chmod(0o777)
    with pytest.raises(PermissionError, match="world-writable"):
        host_paths.private_temporary_root()
    root.chmod(0o700)
    monkeypatch.setattr(host_paths.os, "geteuid", lambda: root.stat().st_uid + 1)
    with pytest.raises(PermissionError, match="owned"):
        host_paths.private_temporary_root()


def test_trial_scratch_is_unique_and_cleanup_survives_failure(host_tmp_path):
    roots = [host_paths.private_temporary_root(owner_root=host_tmp_path / trial) for trial in ("a", "b")]
    paths = []
    with pytest.raises(RuntimeError):
        with tempfile.TemporaryDirectory(dir=roots[0]) as first, tempfile.TemporaryDirectory(dir=roots[0]) as second, tempfile.TemporaryDirectory(dir=roots[1]) as third:
            paths = [Path(first), Path(second), Path(third)]
            assert len(set(paths)) == 3
            assert all(p.stat().st_mode & 0o777 == 0o700 for p in paths)
            raise RuntimeError("injected transfer failure")
    assert all(not p.exists() for p in paths)
    assert all(not list(root.iterdir()) for root in roots)


def test_qemu_probe_uses_private_root_and_cleans_failure(host_tmp_path, monkeypatch):
    monkeypatch.setenv("ROOTLESS_VM_TMPDIR", str(host_tmp_path / "scratch"))
    runtime = QemuRuntime(qemu=Path("/bin/false"), unshare=Path("/bin/false"))
    seen = []

    def fail(command, **kwargs):
        seen.append(Path(command[-1]))
        assert seen[-1].parent == host_tmp_path / "scratch"
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr("rootless_vm.qemu.subprocess.run", fail)
    assert runtime.supports_host_confinement() is False
    assert seen and not seen[0].exists()


def test_qemu_preflight_cleans_spawn_failure(host_tmp_path, monkeypatch):
    monkeypatch.setenv("ROOTLESS_VM_TMPDIR", str(host_tmp_path / "scratch"))
    for name, result in [("version", (11, 1, 0)), ("accelerators", ("tcg",)), ("supports_sandbox", True), ("supports_user_network", True), ("supports_host_confinement", True)]:
        monkeypatch.setattr(QemuRuntime, name, lambda self, value=result: value)
    runtime = QemuRuntime(qemu=Path("/bin/false"))

    def fail(command, **kwargs):
        index = command.index("--host-tmpdir")
        assert Path(command[index + 1]).parent == host_tmp_path / "scratch"
        raise OSError("injected spawn failure")

    monkeypatch.setattr("rootless_vm.qemu.subprocess.Popen", fail)
    with pytest.raises(OSError, match="injected"):
        runtime.preflight()
    assert not list((host_tmp_path / "scratch").iterdir())


def test_host_tempfile_calls_have_explicit_placement():
    # Architecture boundary: future adapters must not consult host-global
    # tempfile defaults. Guest strings are intentionally not inspected.
    root = Path(host_paths.__file__).parent
    for source in root.glob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "tempfile" and node.func.attr in {"mkdtemp", "mkstemp", "TemporaryDirectory", "NamedTemporaryFile", "TemporaryFile"}:
                assert any(k.arg == "dir" for k in node.keywords), source.name


@pytest.mark.parametrize("module_name,class_name", [
    ("rootless_vm.harbor_tofu_agent", "TofuHostAgent"),
    ("rootless_vm.harbor_deepseek_minimal_agent", "DeepSeekMinimalHostAgent"),
])
def test_dispatch_defaults_resolve_at_construction(host_tmp_path, monkeypatch, module_name, class_name):
    import importlib
    pytest.importorskip("harbor")
    agent_type = getattr(importlib.import_module(module_name), class_name)
    for trial in ("first", "second"):
        root = host_tmp_path / trial
        monkeypatch.setenv("ROOTLESS_VM_TMPDIR", str(root))
        agent = agent_type(logs_dir=host_tmp_path / "logs", model_name="synthetic/model")
        assert agent._dispatch_gate_dir == root / "tofu-harbor-dispatch-gate"
    with pytest.raises(ValueError):
        agent_type(logs_dir=host_tmp_path / "logs", model_name="synthetic/model", dispatch_gate_dir="/tmp/gate")


def test_guest_jail_tmp_semantics_unchanged():
    from rootless_vm.qemu_launcher import _sanitized_environment
    assert _sanitized_environment()["TMPDIR"] == "/tmp"
