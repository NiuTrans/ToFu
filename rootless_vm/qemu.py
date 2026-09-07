"""QEMU TCG discovery and a real, fail-closed runtime preflight."""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


_VERSION_RE = re.compile(r"QEMU emulator version\s+(\d+)\.(\d+)(?:\.(\d+))?")


def _isolated_subprocess_env() -> dict[str, str]:
    """Return a deterministic environment with no inherited credentials.

    QEMU and qemu-img need no provider configuration, home directory, proxy,
    or shell state. The boot path uses absolute executables and a binary with
    a pinned runtime search path, so a minimal POSIX path and locale suffice.
    """

    return {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"}


class QemuUnavailableError(RuntimeError):
    """Raised when a verified TCG-capable QEMU runtime is unavailable."""


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    qemu_path: str
    version: tuple[int, int, int]
    accelerators: tuple[str, ...]
    qmp_version: tuple[int, int, int]
    uid: int
    effective_capabilities: str
    seccomp_mode: int | None
    no_new_privileges: bool
    host_confinement: bool
    qemu_sandbox: bool
    user_network: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _resolve_executable(
    value: str | os.PathLike[str] | None,
    env_name: str,
    default_name: str,
) -> Path:
    candidate = str(value or os.environ.get(env_name) or default_name).strip()
    resolved = shutil.which(candidate) if candidate else None
    if resolved is None and candidate:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            resolved = str(path.resolve())
    if resolved is None:
        raise QemuUnavailableError(
            f"executable not found; pass it explicitly or set {env_name}"
        )
    path = Path(resolved).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise QemuUnavailableError(f"not an executable file: {path}")
    return path


def _read_process_security(pid: int) -> tuple[str, int | None, bool]:
    caps = ""
    seccomp = None
    no_new_privileges = False
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("CapEff:"):
                caps = line.split(":", 1)[1].strip()
            elif line.startswith("Seccomp:"):
                seccomp = int(line.split(":", 1)[1].strip())
            elif line.startswith("NoNewPrivs:"):
                no_new_privileges = line.split(":", 1)[1].strip() == "1"
    except (OSError, ValueError):
        pass
    return caps, seccomp, no_new_privileges


def _qmp_request(stream, command: str, request_id: str) -> dict:
    payload = {"execute": command, "id": request_id}
    stream.write((json.dumps(payload) + "\n").encode())
    stream.flush()
    while True:
        line = stream.readline()
        if not line:
            raise QemuUnavailableError(f"QMP closed while waiting for {command}")
        response = json.loads(line)
        if response.get("id") == request_id:
            if "error" in response:
                raise QemuUnavailableError(f"QMP {command} failed: {response['error']}")
            return response


@dataclass(frozen=True)
class QemuRuntime:
    """Paths to QEMU system emulation tools.

    The runtime never falls back to KVM. A caller asking for this package gets
    TCG or a loud failure, making it usable on hosts with no ``/dev/kvm`` and
    no root privileges.
    """

    qemu: Path
    qemu_img: Path | None = None
    unshare: Path | None = None
    egress_bridge: Path | None = None

    @classmethod
    def discover(
        cls,
        qemu: str | os.PathLike[str] | None = None,
        qemu_img: str | os.PathLike[str] | None = None,
    ) -> "QemuRuntime":
        qemu_path = _resolve_executable(qemu, "ROOTLESS_VM_QEMU", "qemu-system-x86_64")
        try:
            image_path = _resolve_executable(qemu_img, "ROOTLESS_VM_QEMU_IMG", "qemu-img")
        except QemuUnavailableError:
            image_path = None
        unshare_value = os.environ.get("ROOTLESS_VM_UNSHARE") or shutil.which("unshare")
        unshare_path = (
            Path(unshare_value).expanduser().resolve()
            if unshare_value and Path(unshare_value).expanduser().is_file()
            else None
        )
        bridge_value = os.environ.get("ROOTLESS_VM_EGRESS_BRIDGE")
        bridge_candidate = (
            Path(bridge_value).expanduser()
            if bridge_value
            else qemu_path.with_name("rootless-egress-bridge")
        )
        bridge_path = (
            bridge_candidate.resolve()
            if bridge_candidate.is_file() and os.access(bridge_candidate, os.X_OK)
            else None
        )
        return cls(
            qemu=qemu_path,
            qemu_img=image_path,
            unshare=unshare_path,
            egress_bridge=bridge_path,
        )

    def version(self) -> tuple[int, int, int]:
        result = subprocess.run(
            [str(self.qemu), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=_isolated_subprocess_env(),
        )
        rendered = (result.stdout or result.stderr).strip()
        match = _VERSION_RE.search(rendered)
        if result.returncode or not match:
            raise QemuUnavailableError(f"cannot parse QEMU version: {rendered}")
        major, minor, micro = match.groups()
        return int(major), int(minor), int(micro or 0)

    def accelerators(self) -> tuple[str, ...]:
        result = subprocess.run(
            [str(self.qemu), "-accel", "help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=_isolated_subprocess_env(),
        )
        if result.returncode:
            raise QemuUnavailableError(
                f"cannot query QEMU accelerators: {(result.stderr or result.stdout).strip()}"
            )
        lines = (result.stdout or result.stderr).splitlines()
        accelerators = tuple(
            line.strip()
            for line in lines
            if line.strip() and not line.lower().startswith("accelerators supported")
        )
        if "tcg" not in accelerators:
            raise QemuUnavailableError(
                f"QEMU lacks the required TCG accelerator: {accelerators}"
            )
        return accelerators

    @functools.lru_cache(maxsize=16)
    def supports_sandbox(self) -> bool:
        """Whether this QEMU build exposes its seccomp-backed sandbox option."""

        result = subprocess.run(
            [
                str(self.qemu),
                "-sandbox",
                "on",
                "-machine",
                "none",
                "-nodefaults",
                "-display",
                "none",
                "-monitor",
                "none",
                "-serial",
                "none",
                "-S",
                "-qmp",
                "stdio",
            ],
            input=(
                '{"execute":"qmp_capabilities","id":"capabilities"}\n'
                '{"execute":"quit","id":"quit"}\n'
            ),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_isolated_subprocess_env(),
        )
        if result.returncode != 0:
            return False
        try:
            responses = [
                json.loads(line)
                for line in result.stdout.splitlines()
                if line.strip()
            ]
        except json.JSONDecodeError:
            return False
        return any("QMP" in response for response in responses) and any(
            response.get("id") == "quit" and "return" in response
            for response in responses
        )

    @functools.lru_cache(maxsize=16)
    def supports_user_network(self) -> bool:
        """Probe the compiled libslirp backend without creating a guest NIC."""

        result = subprocess.run(
            [
                str(self.qemu),
                "-machine",
                "none",
                "-nodefaults",
                "-display",
                "none",
                "-monitor",
                "none",
                "-serial",
                "none",
                "-netdev",
                "user,id=probe,restrict=on,ipv6=off",
                "-S",
                "-qmp",
                "stdio",
            ],
            input=(
                '{"execute":"qmp_capabilities","id":"capabilities"}\n'
                '{"execute":"quit","id":"quit"}\n'
            ),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_isolated_subprocess_env(),
        )
        if result.returncode:
            return False
        return '"id":"quit"' in result.stdout.replace(" ", "")

    @functools.lru_cache(maxsize=16)
    def supports_host_confinement(self) -> bool:
        """Probe unprivileged user/net/PID namespaces plus chroot capability."""

        if self.unshare is None:
            return False
        root = Path(tempfile.mkdtemp(prefix="rootless-vm-jail-probe-"))
        root.chmod(0o700)
        launcher = Path(__file__).with_name("qemu_launcher.py").resolve()
        try:
            result = subprocess.run(
                [
                    str(self.unshare),
                    "--propagation",
                    "unchanged",
                    "--user",
                    "--map-root-user",
                    "--net",
                    "--pid",
                    "--fork",
                    "--ipc",
                    "--uts",
                    sys.executable,
                    str(launcher),
                    "--probe-chroot",
                    str(root),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=_isolated_subprocess_env(),
            )
            return result.returncode == 0 and result.stdout.strip() == "confined"
        except (OSError, subprocess.TimeoutExpired):
            return False
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def preflight(self, *, timeout: float = 8.0) -> PreflightReport:
        """Launch a paused machine and complete a QMP round trip.

        Version/help output alone is insufficient: seccomp may allow ``exec``
        but block memory mappings or threads when QEMU actually initializes.
        This probe therefore launches the real TCG engine with no disk, NIC,
        firmware, host mount, or secret.
        """

        version = self.version()
        accelerators = self.accelerators()
        qemu_sandbox = self.supports_sandbox()
        user_network = self.supports_user_network()
        host_confinement = self.supports_host_confinement()
        if not qemu_sandbox:
            raise QemuUnavailableError(
                "QEMU cannot launch with its seccomp sandbox; refusing unsafe runtime"
            )
        if not host_confinement:
            raise QemuUnavailableError(
                "unprivileged user/net/PID namespace confinement is unavailable"
            )
        probe_dir = Path(tempfile.mkdtemp(prefix="rootless-vm-preflight-"))
        probe_dir.chmod(0o700)
        qmp_path = probe_dir / "qmp.sock"
        launcher = Path(__file__).with_name("qemu_launcher.py").resolve()
        command = [
            sys.executable,
            str(launcher),
            "--no-jail",
            str(self.qemu),
            "-machine",
            "none",
            "-accel",
            "tcg,thread=multi",
            "-nodefaults",
            "-display",
            "none",
            "-nic",
            "none",
            "-S",
            "-qmp",
            f"unix:{qmp_path},server=on,wait=off",
        ]
        command.extend(
            [
                "-sandbox",
                "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
            ]
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=_isolated_subprocess_env(),
        )
        deadline = time.monotonic() + timeout
        caps, seccomp, no_new_privileges = "", None, False
        try:
            while not qmp_path.is_socket():
                if process.poll() is not None:
                    stderr = process.stderr.read() if process.stderr else ""
                    raise QemuUnavailableError(
                        f"QEMU TCG exited during preflight: {stderr.strip()}"
                    )
                if time.monotonic() >= deadline:
                    raise QemuUnavailableError("timed out waiting for QEMU QMP socket")
                time.sleep(0.025)

            # Read after QMP becomes available so QEMU has completed sandbox
            # installation rather than racing the immediate post-fork state.
            caps, seccomp, no_new_privileges = _read_process_security(process.pid)
            if seccomp != 2:
                raise QemuUnavailableError(
                    f"QEMU process lacks seccomp filter mode: {seccomp!r}"
                )
            if not no_new_privileges:
                raise QemuUnavailableError("QEMU process lacks no_new_privs")

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(max(0.1, deadline - time.monotonic()))
                client.connect(str(qmp_path))
                stream = client.makefile("rwb", buffering=0)
                greeting = json.loads(stream.readline())
                raw_version = greeting["QMP"]["version"]["qemu"]
                qmp_version = (
                    int(raw_version["major"]),
                    int(raw_version["minor"]),
                    int(raw_version["micro"]),
                )
                _qmp_request(stream, "qmp_capabilities", "capabilities")
                status = _qmp_request(stream, "query-status", "status")["return"]
                if status.get("running") or status.get("status") != "prelaunch":
                    raise QemuUnavailableError(f"unexpected QEMU state: {status}")
                _qmp_request(stream, "quit", "quit")
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
            if process.returncode:
                stderr = process.stderr.read() if process.stderr else ""
                raise QemuUnavailableError(
                    f"QEMU preflight exited {process.returncode}: {stderr.strip()}"
                )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if process.stderr:
                process.stderr.close()
            shutil.rmtree(probe_dir, ignore_errors=True)

        return PreflightReport(
            ok=True,
            qemu_path=str(self.qemu),
            version=version,
            accelerators=accelerators,
            qmp_version=qmp_version,
            uid=os.geteuid(),
            effective_capabilities=caps,
            seccomp_mode=seccomp,
            no_new_privileges=no_new_privileges,
            host_confinement=host_confinement,
            qemu_sandbox=qemu_sandbox,
            user_network=user_network,
        )
