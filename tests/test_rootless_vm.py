from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from rootless_vm import (
    HarborRunSpec,
    NetworkMode,
    PreparedImageCache,
    PreparedImageSpec,
    QemuRuntime,
    QemuUnavailableError,
    SandboxSession,
    SandboxSpec,
    snapshot_tree,
    harbor_argv,
)
from rootless_vm.egress_proxy import (
    EgressProxy,
    _global_connection_slot,
    _is_global_address,
)
from rootless_vm.guest_agent import GuestAgent
from rootless_vm.image_store import resolve_image_store
from rootless_vm import qemu_launcher


pytestmark = pytest.mark.unit


def _executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def test_runtime_discovery_is_explicit_and_does_not_require_qemu_img(tmp_path, monkeypatch):
    qemu = _executable(tmp_path / "qemu-system-x86_64")
    monkeypatch.setenv("PATH", str(tmp_path))

    runtime = QemuRuntime.discover()

    assert runtime.qemu == qemu.resolve()
    assert runtime.qemu_img is None


def test_runtime_discovery_honors_explicit_environment(tmp_path, monkeypatch):
    qemu = _executable(tmp_path / "my-qemu")
    qemu_img = _executable(tmp_path / "my-qemu-img")
    monkeypatch.setenv("ROOTLESS_VM_QEMU", str(qemu))
    monkeypatch.setenv("ROOTLESS_VM_QEMU_IMG", str(qemu_img))
    monkeypatch.setenv("PATH", "")

    runtime = QemuRuntime.discover()

    assert runtime.qemu == qemu.resolve()
    assert runtime.qemu_img == qemu_img.resolve()


def test_qemu_child_does_not_inherit_provider_credentials(tmp_path, monkeypatch):
    captured = tmp_path / "child-environment"
    qemu = _executable(
        tmp_path / "qemu-system-x86_64",
        body=(
            "#!/bin/sh\n"
            f"env > {captured}\n"
            "echo 'QEMU emulator version 11.1.0'\n"
        ),
    )
    monkeypatch.setenv("FRIDAY_API_KEY", "must-not-reach-qemu")
    monkeypatch.setenv("MEITUAN_ACCESS_TOKEN", "also-must-not-reach-qemu")
    monkeypatch.setenv("HTTPS_PROXY", "http://credential-bearing-proxy.invalid")

    assert QemuRuntime(qemu=qemu).version() == (11, 1, 0)

    child_environment = captured.read_text(encoding="utf-8")
    assert "must-not-reach-qemu" not in child_environment
    assert "also-must-not-reach-qemu" not in child_environment
    assert "credential-bearing-proxy" not in child_environment


def test_session_command_has_no_network_host_mount_or_kvm(tmp_path):
    qemu = _executable(tmp_path / "qemu-system-x86_64")
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(b"kernel")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    session = SandboxSession(
        SandboxSpec(
            runtime=QemuRuntime(qemu=qemu),
            state_root=state,
            kernel=kernel,
            network=NetworkMode.NONE,
            require_qemu_sandbox=False,
        )
    )
    try:
        command = session.command(paused=True)
        rendered = " ".join(command)
        assert "-nic none" in rendered
        assert command[command.index("-accel") + 1].startswith("tcg,")
        assert command[command.index("-cpu") + 1] == "max,-svm,-vmx"
        assert "-enable-kvm" not in command
        assert not {"-virtfs", "-fsdev"} & set(command)
        assert str(Path.home()) not in rendered
        assert session.session_dir.stat().st_mode & 0o077 == 0
    finally:
        session.delete()


def test_session_icount_uses_single_threaded_tcg_and_virtual_clock(tmp_path):
    qemu = _executable(tmp_path / "qemu-system-x86_64")
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(b"kernel")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    session = SandboxSession(
        SandboxSpec(
            runtime=QemuRuntime(qemu=qemu),
            state_root=state,
            kernel=kernel,
            require_qemu_sandbox=False,
            virtual_time_shift=0,
        )
    )
    try:
        command = session.command(paused=True)
        assert command[command.index("-accel") + 1].startswith("tcg,thread=single")
        assert command[command.index("-icount") + 1] == "shift=0,sleep=off"
        assert command[command.index("-rtc") + 1] == "clock=vm"
    finally:
        session.delete()


def test_public_network_only_exposes_authenticated_restricted_proxy(
    tmp_path, monkeypatch
):
    qemu = _executable(tmp_path / "qemu-system-x86_64")
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(b"kernel")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(QemuRuntime, "supports_user_network", lambda self: True)
    monkeypatch.setattr(QemuRuntime, "supports_sandbox", lambda self: True)
    session = SandboxSession(
        SandboxSpec(
            runtime=QemuRuntime(qemu=qemu),
            state_root=state,
            kernel=kernel,
            network=NetworkMode.PUBLIC,
            require_qemu_sandbox=False,
        )
    )
    try:
        command = session.command()
        rendered = " ".join(command)
        assert "user,id=egress,restrict=on,ipv6=off" in rendered
        assert "guestfwd=tcp:10.0.2.100:3128-cmd:" in rendered
        assert "egress_bridge.py" in rendered
        assert "spawn=allow" in rendered
        assert "elevateprivileges=allow" in rendered
        assert "qemu_launcher.py" in rendered
        assert "virtio-net-pci,netdev=egress" in rendered
        assert "-nic none" not in rendered
        assert session.egress_proxy is not None
        assert session.egress_proxy.token not in rendered
        assert session.egress_proxy.gate_dir == state / ".egress-gate"
    finally:
        session.delete()


def test_egress_gate_caps_connections_across_workers(tmp_path):
    gate = tmp_path / "egress-gate"
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    def worker():
        with _global_connection_slot(gate, 2):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.05)
            with lock:
                state["active"] -= 1

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: worker(), range(6)))

    assert state["peak"] == 2
    assert gate.stat().st_mode & 0o077 == 0


def test_harbor_network_environment_retries_transient_package_downloads():
    try:
        from rootless_vm.harbor_environment import RootlessQemuEnvironment
    except ModuleNotFoundError as exc:
        if exc.name == "harbor":
            pytest.skip("Harbor is installed only in the evaluation environment")
        raise
    environment = RootlessQemuEnvironment.__new__(RootlessQemuEnvironment)
    environment._session = SimpleNamespace(
        egress_proxy=SimpleNamespace(
            proxy_url="http://rootless:token@10.0.2.100:3128"
        )
    )

    variables = environment._network_environment()

    assert variables["UV_HTTP_RETRIES"] == "30"
    assert variables["UV_HTTP_TIMEOUT"] == "120"
    assert variables["PIP_RETRIES"] == "30"
    assert variables["PIP_DEFAULT_TIMEOUT"] == "120"
    assert variables["HTTP_PROXY"] == variables["https_proxy"]
    assert variables["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert variables["no_proxy"] == variables["NO_PROXY"]
    assert variables["GIT_CONFIG_COUNT"] == "1"
    assert variables["GIT_CONFIG_KEY_0"] == "http.proxyAuthMethod"
    assert variables["GIT_CONFIG_VALUE_0"] == "basic"


def test_harbor_container_timeout_owns_the_command_process_group():
    try:
        from rootless_vm.harbor_environment import _container_timed_command
    except ModuleNotFoundError as exc:
        if exc.name == "harbor":
            pytest.skip("Harbor is installed only in the evaluation environment")
        raise

    rendered = _container_timed_command("printf '%s' hello & wait", 12.2)

    assert "exec timeout -s TERM -k 5 13 /bin/bash -lc" in rendered
    assert "printf" in rendered
    assert "else exec /bin/bash" in rendered


def test_runc_config_binds_runtime_owned_localhost_file():
    try:
        from rootless_vm.harbor_environment import _oci_config
    except ModuleNotFoundError as exc:
        if exc.name == "harbor":
            pytest.skip("Harbor is installed only in the evaluation environment")
        raise

    config = _oci_config(
        rootfs="/var/lib/rootless/rootfs",
        env=[],
        cwd="/",
        cpus=2,
        memory_mb=2048,
        private_network_namespace=False,
    )

    hosts = next(
        mount for mount in config["mounts"] if mount["destination"] == "/etc/hosts"
    )
    assert hosts == {
        "destination": "/etc/hosts",
        "type": "bind",
        "source": "/run/rootless-task/hosts",
        "options": ["bind", "ro", "nosuid", "nodev", "noexec"],
    }


def test_guest_network_setup_always_enables_only_expected_interfaces():
    try:
        from rootless_vm.harbor_environment import _guest_network_setup_command
    except ModuleNotFoundError as exc:
        if exc.name == "harbor":
            pytest.skip("Harbor is installed only in the evaluation environment")
        raise

    isolated = _guest_network_setup_command(False)
    public = _guest_network_setup_command(True)

    assert isolated == "ip link set lo up"
    assert public.startswith("ip link set lo up &&")
    assert "10.0.2.15/24" in public
    assert "default via 10.0.2.2" in public


def test_guest_agent_maps_signal_only_status_to_shell_return_code(
    tmp_path, monkeypatch
):
    agent = GuestAgent(tmp_path / "unused.sock")
    responses = iter([{"pid": 42}, {"exited": True, "signal": 15}])
    monkeypatch.setattr(agent, "request", lambda *args, **kwargs: next(responses))

    result = agent.execute("ignored", timeout=1)

    assert result.return_code == 143


def test_qemu_launcher_sets_no_new_privileges():
    probe = subprocess.run(
        [
            sys.executable,
            str(Path(qemu_launcher.__file__).resolve()),
            "--no-jail",
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "print(next(x for x in Path('/proc/self/status').read_text().splitlines() "
                "if x.startswith('NoNewPrivs:')))"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert probe.stdout.split() == ["NoNewPrivs:", "1"]


def test_qemu_launcher_sanitizes_host_environment():
    sanitized = qemu_launcher._sanitized_environment()

    assert sanitized["PATH"] == "/usr/bin:/bin"
    assert "LD_PRELOAD" not in sanitized
    assert "PYTHONPATH" not in sanitized
    assert not any("KEY" in name or "TOKEN" in name for name in sanitized)


def test_parent_death_wrapper_does_not_leave_orphaned_process(tmp_path):
    wrapper = (
        Path(__file__).resolve().parents[1]
        / "rootless_vm"
        / "pdeath_exec.py"
    )
    pid_file = tmp_path / "child.pid"
    parent = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import pathlib,subprocess,sys,time; "
                f"p=subprocess.Popen([sys.executable,{str(wrapper)!r},"
                "'/usr/bin/sleep','30']); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); "
                "time.sleep(0.5)"
            ),
        ],
        check=True,
        timeout=5,
    )
    assert parent.returncode == 0
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    status = Path(f"/proc/{child_pid}/status")
    deadline = time.monotonic() + 3
    while status.exists() and time.monotonic() < deadline:
        if "State:\tZ" in status.read_text(encoding="utf-8", errors="replace"):
            break
        time.sleep(0.05)
    if status.exists():
        assert "State:\tZ" in status.read_text(encoding="utf-8", errors="replace")


def test_confined_command_arms_parent_death_inside_pid_namespace(
    tmp_path, monkeypatch
):
    qemu = _executable(tmp_path / "qemu-system-x86_64")
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(b"kernel")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    runtime = QemuRuntime(qemu=qemu, unshare=Path("/usr/bin/unshare"))
    monkeypatch.setattr(QemuRuntime, "supports_sandbox", lambda self: True)
    monkeypatch.setattr(QemuRuntime, "supports_host_confinement", lambda self: True)

    def prepare_jail(session):
        session._kernel_guest_path = "/inputs/kernel"

    monkeypatch.setattr(SandboxSession, "_prepare_jail", prepare_jail)
    session = SandboxSession(
        SandboxSpec(runtime=runtime, state_root=state, kernel=kernel)
    )
    try:
        command = session.command()
        uts = command.index("--uts")
        assert command[uts + 1] == sys.executable
        assert command[uts + 2].endswith("/rootless_vm/pdeath_exec.py")
        assert command[uts + 3] == sys.executable
        assert command[uts + 4].endswith("/rootless_vm/qemu_launcher.py")
    finally:
        session.delete()


def test_read_only_image_uses_bounded_block_backend(tmp_path):
    qemu = _executable(tmp_path / "qemu-system-x86_64")
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(b"kernel")
    image = tmp_path / "payload,with-comma.iso"
    image.write_bytes(b"iso")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    session = SandboxSession(
        SandboxSpec(
            runtime=QemuRuntime(qemu=qemu),
            state_root=state,
            kernel=kernel,
            read_only_images=(image,),
            require_qemu_sandbox=False,
        )
    )
    try:
        command = session.command()
        blockdevs = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "-blockdev"
        ]
        assert len(blockdevs) == 2
        assert all('"read-only":true' in value for value in blockdevs)
        assert str(image.resolve()) in blockdevs[0]
        assert "ide-cd,drive=readonly-raw-0" in command
    finally:
        session.delete()


def test_overlay_never_shrinks_below_backing_disk(tmp_path):
    qemu = _executable(tmp_path / "qemu-system-x86_64")
    captured = tmp_path / "qemu-img-args"
    qemu_img = _executable(
        tmp_path / "qemu-img",
        body=(
            "#!/bin/sh\n"
            "if [ \"$1\" = info ]; then\n"
            "  printf '%s\\n' '{\"format\":\"qcow2\",\"virtual-size\":21474836480}'\n"
            "  exit 0\n"
            "fi\n"
            f"printf '%s\\n' \"$@\" > {captured}\n"
            "for last do :; done\n"
            ": > \"$last\"\n"
        ),
    )
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"base")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)

    session = SandboxSession(
        SandboxSpec(
            runtime=QemuRuntime(qemu=qemu, qemu_img=qemu_img),
            state_root=state,
            base_disk=base,
            disk_virtual_size_gib=10,
            require_qemu_sandbox=False,
        )
    )
    try:
        assert "10G" not in captured.read_text().splitlines()
    finally:
        session.delete()


def test_confined_backing_chain_is_private_and_jail_local(tmp_path):
    top = tmp_path / "top.qcow2"
    parent = tmp_path / "parent.qcow2"
    top.write_bytes(b"top-layer")
    parent.write_bytes(b"parent-layer")
    calls = tmp_path / "rebase-args"
    qemu_img = _executable(
        tmp_path / "qemu-img",
        body=(
            "#!/bin/sh\n"
            "if [ \"$1\" = info ]; then\n"
            "  printf '%s\\n' '"
            + json.dumps(
                [
                    {"filename": str(top), "format": "qcow2"},
                    {"filename": str(parent), "format": "qcow2"},
                ],
                separators=(",", ":"),
            )
            + "'\n"
            "  exit 0\n"
            "fi\n"
            f"printf '%s\\n' \"$@\" > {calls}\n"
        ),
    )
    session = object.__new__(SandboxSession)
    session.spec = SimpleNamespace(
        base_disk=top,
        runtime=SimpleNamespace(qemu_img=qemu_img),
    )
    inputs = tmp_path / "inputs"
    inputs.mkdir()

    session._prepare_disk_chain(inputs)

    assert (inputs / "base.qcow2").read_bytes() == b"top-layer"
    assert (inputs / "backing-1.img").read_bytes() == b"parent-layer"
    assert (inputs / "base.qcow2").stat().st_mode & 0o777 == 0o444
    assert (inputs / "backing-1.img").stat().st_mode & 0o777 == 0o444
    arguments = calls.read_text().splitlines()
    assert "/inputs/backing-1.img" in arguments
    assert str(parent) not in arguments


def test_prepared_image_cache_is_content_addressed_and_validated(tmp_path):
    qemu = _executable(tmp_path / "qemu-system-x86_64")
    base = tmp_path / "base.qcow2"
    payload = tmp_path / "payload.iso"
    base.write_bytes(b"base-v1")
    payload.write_bytes(b"payload-v1")
    cache_root = tmp_path / "cache"
    cache_root.mkdir(mode=0o700)
    cache = PreparedImageCache(
        PreparedImageSpec(
            runtime=QemuRuntime(qemu=qemu),
            cache_root=cache_root,
            base_disk=base,
            payload_iso=payload,
            task_image="example/task@sha256:abc",
            python_runtime_image="python@sha256:def",
        )
    )
    cache.entry.mkdir(mode=0o700)
    cache.disk.write_bytes(b"prepared-disk")
    cache.disk.chmod(0o444)
    disk_sha = hashlib.sha256(b"prepared-disk").hexdigest()
    cache.marker.write_text(
        json.dumps(cache._marker_payload(disk_sha, {"env": [], "workdir": "/"}))
    )
    cache.marker.chmod(0o600)

    assert cache._cache_hit() is True
    cache.disk.chmod(0o644)
    assert cache._cache_hit() is False

    payload.write_bytes(b"payload-v2")
    changed = PreparedImageCache(
        PreparedImageSpec(
            runtime=QemuRuntime(qemu=qemu),
            cache_root=cache_root,
            base_disk=base,
            payload_iso=payload,
            task_image="example/task@sha256:abc",
            python_runtime_image="python@sha256:def",
        )
    )
    assert changed.recipe.digest != cache.recipe.digest


def test_prepared_image_schema_invalidates_pre_confinement_cache(tmp_path):
    qemu = _executable(tmp_path / "qemu-system-x86_64")
    base = tmp_path / "base.qcow2"
    payload = tmp_path / "payload.iso"
    base.write_bytes(b"base")
    payload.write_bytes(b"payload")
    cache_root = tmp_path / "cache"
    cache_root.mkdir(mode=0o700)

    cache = PreparedImageCache(
        PreparedImageSpec(
            runtime=QemuRuntime(qemu=qemu),
            cache_root=cache_root,
            base_disk=base,
            payload_iso=payload,
            task_image="sha256:" + "a" * 64,
        )
    )

    assert cache.recipe.schema == 4


def test_image_store_resolves_only_digest_pinned_files_below_private_root(tmp_path):
    store = tmp_path / "images"
    store.mkdir(mode=0o700)
    payload_dir = store / "regex-log"
    payload_dir.mkdir()
    payload = payload_dir / "task-image.iso"
    payload.write_bytes(b"immutable-payload")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    index = {
        "schema": 1,
        "images": {
            "example/regex-log:pinned": {
                "iso": "regex-log/task-image.iso",
                "sha256": digest,
                "loaded_image_reference": "sha256:" + "a" * 64,
            }
        },
    }
    (store / "index.json").write_text(json.dumps(index), encoding="utf-8")

    resolved = resolve_image_store(store, "example/regex-log:pinned")

    assert resolved == (payload.resolve(), digest, "sha256:" + "a" * 64)
    index["images"]["example/regex-log:pinned"]["iso"] = "../outside.iso"
    (store / "index.json").write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(ValueError, match="must not contain"):
        resolve_image_store(store, "example/regex-log:pinned")


def test_unimplemented_network_mode_fails_closed(tmp_path):
    qemu = _executable(tmp_path / "qemu-system-x86_64")
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(b"kernel")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    spec = SandboxSpec(
        runtime=QemuRuntime(qemu=qemu),
        state_root=state,
        kernel=kernel,
        network="allowlist",  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="fail-closed"):
        spec.validate()


def test_egress_proxy_rejects_host_and_private_destinations(tmp_path, monkeypatch):
    assert _is_global_address("8.8.8.8") is True
    for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"):
        assert _is_global_address(address) is False

    monkeypatch.setenv("FRIDAY_API_KEY", "must-not-reach-egress-proxy")
    proxy = EgressProxy(
        socket_path=tmp_path / "proxy.sock",
        max_bytes=1024 * 1024,
        gate_dir=tmp_path / "gate",
        global_connections=2,
    )
    try:
        proxy.start()
        assert proxy.process is not None
        assert proxy.gate_dir == tmp_path / "gate"
        child_environment = Path(
            f"/proc/{proxy.process.pid}/environ"
        ).read_bytes()
        assert b"must-not-reach-egress-proxy" not in child_environment

        credentials = base64.b64encode(
            f"rootless:{proxy.token}".encode()
        ).decode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(proxy.socket_reference)
            client.sendall(
                b"CONNECT 127.0.0.1:80 HTTP/1.1\r\n"
                + f"Proxy-Authorization: Basic {credentials}\r\n\r\n".encode()
            )
            response = client.recv(4096)
        assert response.startswith(b"HTTP/1.1 403")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(proxy.socket_reference)
            client.sendall(
                b"CONNECT 8.8.8.8:25 HTTP/1.1\r\n"
                + f"Proxy-Authorization: Basic {credentials}\r\n\r\n".encode()
            )
            response = client.recv(4096)
        assert response.startswith(b"HTTP/1.1 403")
    finally:
        proxy.stop()


def test_session_requires_qemu_seccomp_sandbox_by_default(tmp_path):
    qemu = _executable(tmp_path / "qemu-system-x86_64")
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(b"kernel")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)

    with pytest.raises(QemuUnavailableError, match="without seccomp sandbox support"):
        SandboxSession(
            SandboxSpec(
                runtime=QemuRuntime(qemu=qemu),
                state_root=state,
                kernel=kernel,
            )
        )


def test_state_root_symlink_is_rejected(tmp_path):
    qemu = _executable(tmp_path / "qemu-system-x86_64")
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(b"kernel")
    real_state = tmp_path / "real-state"
    real_state.mkdir(mode=0o700)
    state_link = tmp_path / "state-link"
    state_link.symlink_to(real_state, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        SandboxSession(
            SandboxSpec(
                runtime=QemuRuntime(qemu=qemu),
                state_root=state_link,
                kernel=kernel,
                require_qemu_sandbox=False,
            )
        )


def test_session_delete_requires_exact_marker(tmp_path):
    qemu = _executable(tmp_path / "qemu-system-x86_64")
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(b"kernel")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    session = SandboxSession(
        SandboxSpec(
            runtime=QemuRuntime(qemu=qemu),
            state_root=state,
            kernel=kernel,
            require_qemu_sandbox=False,
        )
    )
    marker = session.marker
    marker.write_text(json.dumps({"schema": 1, "session_id": "wrong"}))
    with pytest.raises(RuntimeError, match="invalid marker"):
        session.delete()
    assert session.session_dir.is_dir()


def test_integrity_snapshot_does_not_follow_symlinks(tmp_path):
    root = tmp_path / "host"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "kept").write_text("same")
    (outside / "secret").write_text("outside-before")
    os.symlink(outside, root / "link")

    before = snapshot_tree(root)
    (outside / "secret").write_text("outside-after")
    after = snapshot_tree(root)

    assert before.digest == after.digest
    (root / "kept").write_text("changed")
    assert snapshot_tree(root).digest != before.digest


def test_harbor_runner_builds_secret_free_single_trial_command(tmp_path):
    harbor = _executable(tmp_path / "harbor")
    task = tmp_path / "task"
    task.mkdir()
    base = tmp_path / "base.qcow2"
    payload = tmp_path / "payload.iso"
    base.write_bytes(b"base")
    payload.write_bytes(b"payload")

    argv = harbor_argv(
        HarborRunSpec(
            harbor=str(harbor),
            task_path=task,
            base_disk=base,
            base_disk_sha256="a" * 64,
            image_iso=payload,
            image_iso_sha256="b" * 64,
            image_reference="example/task@sha256:" + "c" * 64,
            state_root=tmp_path / "state",
            prepared_cache_root=tmp_path / "cache",
            jobs_dir=tmp_path / "jobs",
        )
    )

    rendered = " ".join(argv)
    assert argv[0] == str(harbor.resolve())
    assert "rootless_vm.harbor_tofu_agent:TofuHostAgent" in argv
    assert "deepseek-v4-flash-meituan" in argv
    assert "API_KEY" not in rendered
    assert argv[argv.index("--n-concurrent") + 1] == "1"
    for name in ("state", "cache", "jobs"):
        assert (tmp_path / name).stat().st_mode & 0o077 == 0


def test_bootstrap_conda_lock_is_complete_and_digest_pinned():
    lock = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "rootless_qemu-conda-linux-64.lock"
    )
    entries = [
        line
        for line in lock.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith(("#", "@"))
    ]

    assert len(entries) == 61
    assert len(entries) == len(set(entries))
    for entry in entries:
        url, digest = entry.rsplit("#", 1)
        assert url.startswith("https://conda.anaconda.org/conda-forge/")
        assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_bootstrap_refuses_unsafe_prefixes_before_writing(tmp_path):
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "bootstrap_rootless_qemu.sh"
    )

    relative = subprocess.run(
        [str(script), "--prefix", "relative-prefix"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert relative.returncode == 2
    assert "must be absolute" in relative.stderr
    assert not (tmp_path / "relative-prefix").exists()

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    sentinel = occupied / "sentinel"
    sentinel.write_text("preserve", encoding="utf-8")
    unmarked = subprocess.run(
        [str(script), "--prefix", str(occupied)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert unmarked.returncode == 2
    assert "non-empty prefix" in unmarked.stderr
    assert sentinel.read_text(encoding="utf-8") == "preserve"

    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "prefix-link"
    symlink.symlink_to(target, target_is_directory=True)
    linked = subprocess.run(
        [str(script), "--prefix", str(symlink)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert linked.returncode == 2
    assert "must not be a symbolic link" in linked.stderr
