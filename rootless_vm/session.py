"""Ephemeral QEMU session construction with conservative host exposure."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .egress_proxy import EgressProxy
from .guest_agent import GuestAgent, GuestAgentError
from .qemu import (
    QemuRuntime,
    QemuUnavailableError,
    _isolated_subprocess_env,
    _qmp_request,
)


_MARKER = ".rootless-vm-session.json"
_FICLONE = 0x40049409


def _private_clone(source: Path, target: Path, mode: int) -> None:
    """Create a distinct reflink/copy so a jailed process cannot alter source."""

    source = source.resolve(strict=True)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        try:
            fcntl.ioctl(outgoing.fileno(), _FICLONE, incoming.fileno())
        except OSError:
            incoming.seek(0)
            outgoing.seek(0)
            outgoing.truncate()
            shutil.copyfileobj(incoming, outgoing, length=4 * 1024 * 1024)
    target.chmod(mode)


class NetworkMode(str, Enum):
    NONE = "none"
    PUBLIC = "public"


def _existing_file(value: Path | None, label: str) -> Path | None:
    if value is None:
        return None
    path = value.expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"{label} must be a regular file: {path}")
    return path


def _private_state_root(path: Path) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError(f"state root must not be a symbolic link: {path}")
    path = path.resolve()
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"state root must be a real directory: {path}")
        if path.stat().st_mode & 0o077:
            raise PermissionError(f"state root must not be group/world accessible: {path}")
    else:
        path.mkdir(parents=True, mode=0o700)
    return path


@dataclass(frozen=True)
class SandboxSpec:
    runtime: QemuRuntime
    state_root: Path
    base_disk: Path | None = None
    kernel: Path | None = None
    initrd: Path | None = None
    kernel_append: str = "console=ttyS0"
    memory_mib: int = 1024
    cpus: int = 2
    disk_virtual_size_gib: int | None = None
    read_only_images: tuple[Path, ...] = ()
    network: NetworkMode = NetworkMode.NONE
    egress_max_bytes: int = 4 * 1024**3
    egress_global_concurrency: int = 16
    require_qemu_sandbox: bool = True
    virtual_time_shift: int | None = None

    def validate(self) -> None:
        if not isinstance(self.network, NetworkMode):
            raise ValueError(
                "only fail-closed network=none and proxied public egress are implemented"
            )
        if self.egress_max_bytes < 1024 * 1024:
            raise ValueError("egress_max_bytes must be at least 1 MiB")
        if not 1 <= self.egress_global_concurrency <= 128:
            raise ValueError("egress_global_concurrency must be between 1 and 128")
        if self.network is NetworkMode.PUBLIC and not self.runtime.supports_user_network():
            raise QemuUnavailableError(
                "QEMU was built without rootless user-network support"
            )
        if self.require_qemu_sandbox and not self.runtime.supports_sandbox():
            raise QemuUnavailableError(
                "QEMU was built without seccomp sandbox support; refusing unsafe session"
            )
        if self.require_qemu_sandbox and not self.runtime.supports_host_confinement():
            raise QemuUnavailableError(
                "unprivileged user/net/PID namespace confinement is unavailable"
            )
        if (
            self.require_qemu_sandbox
            and self.network is NetworkMode.PUBLIC
            and self.runtime.egress_bridge is None
        ):
            raise QemuUnavailableError(
                "the native rootless egress bridge is required for public networking"
            )
        if not 128 <= self.memory_mib <= 1024 * 1024:
            raise ValueError("memory_mib must be between 128 and 1048576")
        if not 1 <= self.cpus <= 256:
            raise ValueError("cpus must be between 1 and 256")
        if self.virtual_time_shift is not None and not 0 <= self.virtual_time_shift <= 10:
            raise ValueError("virtual_time_shift must be between 0 and 10")
        base_disk = _existing_file(self.base_disk, "base_disk")
        kernel = _existing_file(self.kernel, "kernel")
        initrd = _existing_file(self.initrd, "initrd")
        for image in self.read_only_images:
            _existing_file(image, "read_only_image")
        if base_disk is None and kernel is None:
            raise ValueError("a base_disk or kernel is required")
        if initrd is not None and kernel is None:
            raise ValueError("initrd requires kernel")
        if base_disk is not None and self.runtime.qemu_img is None:
            raise QemuUnavailableError("qemu-img is required for copy-on-write disks")
        if self.disk_virtual_size_gib is not None and self.disk_virtual_size_gib < 1:
            raise ValueError("disk_virtual_size_gib must be positive")


class SandboxSession:
    """One disposable VM whose only writable disk is a qcow2 overlay."""

    def __init__(self, spec: SandboxSpec):
        spec.validate()
        self.spec = spec
        self.state_root = _private_state_root(spec.state_root)
        self.session_id = uuid.uuid4().hex
        self.session_dir = self.state_root / self.session_id
        self.session_dir.mkdir(mode=0o700)
        self.marker = self.session_dir / _MARKER
        self.marker.write_text(
            json.dumps({"schema": 1, "session_id": self.session_id}) + "\n",
            encoding="utf-8",
        )
        self.marker.chmod(0o600)
        self.confined = spec.require_qemu_sandbox
        self.jail_root = self.session_dir / "jail" if self.confined else None
        self.run_dir = (
            self.jail_root / "run" if self.jail_root is not None else self.session_dir
        )
        self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.qmp_path = self.run_dir / "qmp.sock"
        self.guest_agent_path = self.run_dir / "guest-agent.sock"
        self.guest_agent = GuestAgent(self.guest_agent_path)
        self.serial_log = self.run_dir / "serial.log"
        self._qmp_guest_path = "/run/qmp.sock" if self.confined else str(self.qmp_path)
        self._qga_guest_path = (
            "/run/guest-agent.sock" if self.confined else str(self.guest_agent_path)
        )
        self._serial_guest_path = (
            "/run/serial.log" if self.confined else str(self.serial_log)
        )
        self._overlay_guest_path: str | None = None
        self._kernel_guest_path: str | None = None
        self._initrd_guest_path: str | None = None
        self._read_only_guest_paths: list[str] = []
        self._bios_guest_path: str | None = None
        self.overlay: Path | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.egress_proxy: EgressProxy | None = None
        try:
            if self.confined:
                self._prepare_jail()
            else:
                self._kernel_guest_path = (
                    str(spec.kernel.resolve()) if spec.kernel is not None else None
                )
                self._initrd_guest_path = (
                    str(spec.initrd.resolve()) if spec.initrd is not None else None
                )
                self._read_only_guest_paths = [
                    str(image.resolve()) for image in spec.read_only_images
                ]
            if spec.network is NetworkMode.PUBLIC:
                self.egress_proxy = EgressProxy(
                    socket_path=self.run_dir / "egress.sock",
                    max_bytes=spec.egress_max_bytes,
                    gate_dir=self.state_root / ".egress-gate",
                    global_connections=spec.egress_global_concurrency,
                )
            if spec.base_disk is not None:
                self.overlay = self.run_dir / "root.qcow2"
                self._overlay_guest_path = (
                    "/run/root.qcow2" if self.confined else str(self.overlay)
                )
                self._create_overlay()
        except Exception:
            self.delete()
            raise

    def _jail_path(self, guest_path: str) -> Path:
        assert self.jail_root is not None
        if not guest_path.startswith("/") or ".." in Path(guest_path).parts:
            raise ValueError(f"invalid jail path: {guest_path}")
        return self.jail_root / guest_path.lstrip("/")

    def _linked_libraries(self, executable: Path) -> set[Path]:
        result = subprocess.run(
            ["ldd", str(executable)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_isolated_subprocess_env(),
        )
        if result.returncode:
            raise RuntimeError(f"cannot inspect trusted executable dependencies: {executable}")
        libraries: set[Path] = set()
        for raw_line in result.stdout.splitlines():
            fields = raw_line.strip().split()
            candidate = ""
            if len(fields) >= 3 and fields[1] == "=>" and fields[2].startswith("/"):
                candidate = fields[2]
            elif fields and fields[0].startswith("/"):
                candidate = fields[0]
            if candidate:
                path = Path(candidate)
                resolved = path.resolve(strict=True)
                if not resolved.is_file():
                    raise RuntimeError(f"dependency is not a regular file: {resolved}")
                libraries.add(path)
        return libraries

    def _clone_absolute(self, source: Path, *, executable: bool = False) -> str:
        guest_path = str(source.absolute())
        source = source.resolve(strict=True)
        target = self._jail_path(guest_path)
        if not target.exists():
            _private_clone(source, target, 0o555 if executable else 0o444)
        return guest_path

    def _prepare_jail(self) -> None:
        assert self.jail_root is not None
        qemu = self.spec.runtime.qemu.resolve(strict=True)
        self._clone_absolute(qemu, executable=True)
        executables = [qemu]
        if self.spec.network is NetworkMode.PUBLIC:
            bridge = self.spec.runtime.egress_bridge
            assert bridge is not None
            self._clone_absolute(bridge, executable=True)
            executables.append(bridge)
        for executable in executables:
            for library in self._linked_libraries(executable):
                self._clone_absolute(
                    library,
                    executable=library.name.startswith("ld-linux"),
                )

        bios = qemu.parent.parent / "share" / "qemu" / "bios-256k.bin"
        self._bios_guest_path = self._clone_absolute(bios)
        self._clone_absolute(qemu.parent.parent / "share" / "qemu" / "kvmvapic.bin")
        dev = self._jail_path("/dev")
        dev.mkdir(parents=True, exist_ok=True, mode=0o755)
        null = dev / "null"
        null.touch(mode=0o666, exist_ok=False)
        null.chmod(0o666)

        inputs = self._jail_path("/inputs")
        inputs.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.spec.base_disk is not None:
            self._prepare_disk_chain(inputs)
        if self.spec.kernel is not None:
            _private_clone(self.spec.kernel, inputs / "kernel", 0o444)
            self._kernel_guest_path = "/inputs/kernel"
        if self.spec.initrd is not None:
            _private_clone(self.spec.initrd, inputs / "initrd", 0o444)
            self._initrd_guest_path = "/inputs/initrd"
        for index, image in enumerate(self.spec.read_only_images):
            guest = f"/inputs/readonly-{index}.img"
            _private_clone(image, self._jail_path(guest), 0o444)
            self._read_only_guest_paths.append(guest)

    def _prepare_disk_chain(self, inputs: Path) -> None:
        """Clone and normalize every qcow backing layer inside the jail."""

        assert self.spec.base_disk is not None
        assert self.spec.runtime.qemu_img is not None
        result = subprocess.run(
            [
                str(self.spec.runtime.qemu_img),
                "info",
                "--backing-chain",
                "--output=json",
                str(self.spec.base_disk),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_isolated_subprocess_env(),
        )
        if result.returncode:
            raise RuntimeError(
                f"qemu-img cannot inspect base disk chain: {result.stderr.strip()}"
            )
        chain = json.loads(result.stdout)
        if not isinstance(chain, list) or not chain or len(chain) > 16:
            raise RuntimeError("base disk must have between 1 and 16 backing layers")
        layers: list[tuple[Path, str, Path, str]] = []
        for index, item in enumerate(chain):
            if not isinstance(item, dict):
                raise RuntimeError("qemu-img returned an invalid backing chain")
            filename = item.get("filename")
            image_format = item.get("format")
            if not isinstance(filename, str) or not isinstance(image_format, str):
                raise RuntimeError("qemu-img omitted a backing layer filename or format")
            source = Path(filename).resolve(strict=True)
            if not source.is_file():
                raise RuntimeError(f"backing layer is not a regular file: {source}")
            guest = "/inputs/base.qcow2" if index == 0 else f"/inputs/backing-{index}.img"
            target = inputs / Path(guest).name
            _private_clone(source, target, 0o600)
            layers.append((target, guest, source, image_format))

        for index, (target, _, _, image_format) in enumerate(layers[:-1]):
            _, next_guest, _, next_format = layers[index + 1]
            rewrite = subprocess.run(
                [
                    str(self.spec.runtime.qemu_img),
                    "rebase",
                    "-u",
                    "-f",
                    image_format,
                    "-F",
                    next_format,
                    "-b",
                    next_guest,
                    str(target),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=_isolated_subprocess_env(),
            )
            if rewrite.returncode:
                raise RuntimeError(
                    f"failed to normalize private backing chain: {rewrite.stderr.strip()}"
                )
        for target, _, _, _ in layers:
            target.chmod(0o444)

    def _create_overlay(self) -> None:
        assert self.overlay is not None
        assert self.spec.base_disk is not None
        assert self.spec.runtime.qemu_img is not None
        info = subprocess.run(
            [str(self.spec.runtime.qemu_img), "info", "--output=json", str(self.spec.base_disk)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_isolated_subprocess_env(),
        )
        if info.returncode:
            raise RuntimeError(f"qemu-img cannot inspect base disk: {info.stderr.strip()}")
        image_info = json.loads(info.stdout)
        base_format = image_info.get("format")
        if not isinstance(base_format, str) or not base_format:
            raise RuntimeError("qemu-img did not report a base disk format")
        command = [
            str(self.spec.runtime.qemu_img),
            "create",
            "-f",
            "qcow2",
            "-B",
            base_format,
            "-b",
            "../inputs/base.qcow2" if self.confined else str(self.spec.base_disk.resolve()),
        ]
        base_virtual_size = image_info.get("virtual-size")
        requested_bytes = (
            self.spec.disk_virtual_size_gib * 1024**3
            if self.spec.disk_virtual_size_gib is not None
            else None
        )
        if self.confined:
            if not isinstance(base_virtual_size, int) or base_virtual_size <= 0:
                raise RuntimeError("qemu-img did not report a valid base virtual size")
            # The normalized backing paths only exist after chroot, so create
            # the top overlay without opening the backing chain host-side.
            command.append("-u")
        command.append(self.overlay.name if self.confined else str(self.overlay))
        if self.confined:
            command.append(str(max(base_virtual_size, requested_bytes or 0)))
        elif requested_bytes is not None:
            # qemu-img permits a new overlay smaller than its backing image,
            # but a filesystem spanning the original disk then cannot mount.
            # Inherit the backing size unless the requested size is a genuine
            # expansion; never turn a Harbor storage hint into disk shrinkage.
            if not isinstance(base_virtual_size, int) or requested_bytes > base_virtual_size:
                command.append(f"{self.spec.disk_virtual_size_gib}G")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=_isolated_subprocess_env(),
            cwd=self.overlay.parent if self.confined else None,
        )
        if result.returncode:
            raise RuntimeError(f"failed to create qcow2 overlay: {result.stderr.strip()}")

    def command(self, *, paused: bool = False) -> list[str]:
        prlimit = shutil.which("prlimit")
        if prlimit is None:
            raise QemuUnavailableError(
                "prlimit is required to bound QEMU host resource consumption"
            )
        launcher = Path(__file__).with_name("qemu_launcher.py").resolve()
        parent_death_wrapper = Path(__file__).with_name("pdeath_exec.py").resolve()
        address_space_mib = self.spec.memory_mib * 2 + 2048
        disk_limit_gib = (self.spec.disk_virtual_size_gib or 22) + 2
        address_space_bytes = address_space_mib * 1024 * 1024
        file_size_bytes = disk_limit_gib * 1024 * 1024 * 1024
        command = [
            str(Path(prlimit).resolve()),
            "--core=0:0",
            "--nofile=256:256",
            f"--as={address_space_bytes}:{address_space_bytes}",
            f"--fsize={file_size_bytes}:{file_size_bytes}",
            "--",
        ]
        if self.confined:
            assert self.spec.runtime.unshare is not None
            assert self.jail_root is not None
            command.extend(
                [
                    str(self.spec.runtime.unshare),
                    "--propagation",
                    "unchanged",
                    "--user",
                    "--map-root-user",
                    "--net",
                    "--pid",
                    "--fork",
                    "--ipc",
                    "--uts",
                    # unshare --pid --fork becomes QEMU's direct host-visible
                    # parent. Arm PDEATHSIG again on the namespace child so an
                    # interrupted Harbor worker cannot leave QEMU reparented
                    # to PID 1 after the outer wrapper has exited.
                    sys.executable,
                    str(parent_death_wrapper),
                    sys.executable,
                    str(launcher),
                    "--chroot",
                    str(self.jail_root),
                ]
            )
        else:
            command.extend([sys.executable, str(launcher), "--no-jail"])
        accelerator = "tcg,thread=multi,tb-size=512"
        if self.spec.virtual_time_shift is not None:
            # Instruction-counted virtual time keeps sub-second guest timers
            # tied to guest work rather than slow host-side TCG translation.
            # QEMU documents icount as incompatible with multi-threaded TCG.
            accelerator = "tcg,thread=single,tb-size=512"
        command.extend(
            [
                str(self.spec.runtime.qemu),
            "-accel",
            accelerator,
            "-machine",
            "q35,vmport=off",
            "-cpu",
            # Expose the broad TCG CPU while hiding hardware-virtualization
            # flags that only make the guest load unusable KVM modules.
            "max,-svm,-vmx",
            "-m",
            str(self.spec.memory_mib),
            "-smp",
            str(self.spec.cpus),
            "-nodefaults",
            "-no-reboot",
            "-display",
            "none",
            "-monitor",
            "none",
            "-serial",
            f"file:{self._serial_guest_path}",
            "-chardev",
            f"socket,path={self._qga_guest_path},server=on,wait=off,id=qga0",
            "-device",
            "virtio-serial-pci,id=virtio-serial0",
            "-device",
            "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
            "-qmp",
            f"unix:{self._qmp_guest_path},server=on,wait=off",
            ]
        )
        if self.spec.virtual_time_shift is not None:
            command.extend(
                [
                    "-icount",
                    f"shift={self.spec.virtual_time_shift},sleep=off",
                    "-rtc",
                    "clock=vm",
                ]
            )
        if self._bios_guest_path is not None:
            command.extend(["-bios", self._bios_guest_path])
        if self.egress_proxy is None:
            command.extend(["-nic", "none"])
        else:
            if self.confined:
                assert self.spec.runtime.egress_bridge is not None
                bridge_parts = [
                    str(self.spec.runtime.egress_bridge),
                    "--socket",
                    "/run/egress.sock",
                ]
            else:
                bridge = Path(__file__).with_name("egress_bridge.py").resolve()
                bridge_parts = [
                    sys.executable,
                    str(bridge),
                    "--socket",
                    str(self.egress_proxy.socket_path),
                ]
            # All fields are harness-owned absolute paths or integers. libslirp
            # invokes this fixed command once per guest proxy connection; no
            # task-controlled value is interpolated into its shell command.
            bridge_command = shlex.join(
                bridge_parts
            )
            command.extend(
                [
                    "-netdev",
                    (
                        "user,id=egress,restrict=on,ipv6=off,"
                        f"guestfwd=tcp:{self.egress_proxy.guest_host}:"
                        f"{self.egress_proxy.guest_port}-cmd:{bridge_command}"
                    ),
                    "-device",
                    "virtio-net-pci,netdev=egress,romfile=",
                ]
            )
        if self.spec.runtime.supports_sandbox():
            public_network = self.spec.network is NetworkMode.PUBLIC
            spawn_policy = "allow" if public_network else "deny"
            # libslirp's child setup uses session/process syscalls that QEMU's
            # elevateprivileges=deny group also blocks. QEMU is already an
            # unprivileged, capability-free process and the launcher imposed
            # no_new_privs before exec, so allowing that syscall group cannot
            # create privilege. Offline mode keeps the stricter deny policy.
            privilege_policy = "allow" if public_network else "deny"
            command.extend(
                [
                    "-sandbox",
                    f"on,obsolete=deny,elevateprivileges={privilege_policy},"
                    f"spawn={spawn_policy},resourcecontrol=deny",
                ]
            )
        if paused:
            command.append("-S")
        if self.overlay is not None:
            assert self._overlay_guest_path is not None
            command.extend(
                [
                    "-drive",
                    f"file={self._overlay_guest_path},format=qcow2,if=virtio,cache=writeback",
                ]
            )
        if self._kernel_guest_path is not None:
            command.extend(["-kernel", self._kernel_guest_path])
        if self._initrd_guest_path is not None:
            command.extend(["-initrd", self._initrd_guest_path])
        if self.spec.kernel is not None and self.spec.kernel_append:
            command.extend(["-append", self.spec.kernel_append])
        for index, image in enumerate(self._read_only_guest_paths):
            file_node = f"readonly-file-{index}"
            raw_node = f"readonly-raw-{index}"
            command.extend(
                [
                    "-blockdev",
                    json.dumps(
                        {
                            "driver": "file",
                            "filename": image,
                            "node-name": file_node,
                            "read-only": True,
                        },
                        separators=(",", ":"),
                    ),
                    "-blockdev",
                    json.dumps(
                        {
                            "driver": "raw",
                            "file": file_node,
                            "node-name": raw_node,
                            "read-only": True,
                        },
                        separators=(",", ":"),
                    ),
                    "-device",
                    f"ide-cd,drive={raw_node}",
                ]
            )
        return command

    def start(self, *, paused: bool = False, timeout: float = 10.0) -> None:
        if self.process is not None:
            raise RuntimeError("session already started")
        if self.egress_proxy is not None:
            self.egress_proxy.start()
        try:
            parent_death_wrapper = Path(__file__).with_name("pdeath_exec.py")
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    str(parent_death_wrapper),
                    *self.command(paused=paused),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=_isolated_subprocess_env(),
            )
        except Exception:
            if self.egress_proxy is not None:
                self.egress_proxy.stop()
            raise
        deadline = time.monotonic() + timeout
        while not self.qmp_path.is_socket():
            if self.process.poll() is not None:
                stderr = (
                    self.process.stderr.read().decode(errors="replace")
                    if self.process.stderr
                    else ""
                )
                if self.egress_proxy is not None:
                    self.egress_proxy.stop()
                raise RuntimeError(f"QEMU exited during startup: {stderr.strip()}")
            if time.monotonic() >= deadline:
                self.stop()
                raise TimeoutError("timed out waiting for QMP")
            time.sleep(0.025)

    def _qmp(self, command: str, *, timeout: float) -> object:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(self.qmp_path))
            stream = client.makefile("rwb", buffering=0)
            stream.readline()
            _qmp_request(stream, "qmp_capabilities", "capabilities")
            return _qmp_request(stream, command, command)["return"]

    def wait_guest_agent(self, *, timeout: float = 120.0) -> None:
        """Wait until the guest has opened the virtio-serial endpoint.

        Sending QGA sync frames before its frontend is open is racy: the
        transport may drop them. QMP exposes the actual frontend state, so no
        distro-specific serial-log parsing or arbitrary boot sleep is needed.
        """

        process = self.process
        if process is None:
            raise RuntimeError("session is not started")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = (
                    process.stderr.read().decode(errors="replace")
                    if process.stderr
                    else ""
                )
                raise RuntimeError(f"QEMU exited before guest-agent was ready: {stderr.strip()}")
            remaining = deadline - time.monotonic()
            try:
                chardevs = self._qmp("query-chardev", timeout=min(2.0, remaining))
            except (OSError, TimeoutError, QemuUnavailableError):
                chardevs = []
            if isinstance(chardevs, list) and any(
                isinstance(item, dict)
                and item.get("label") == "qga0"
                and item.get("frontend-open") is True
                for item in chardevs
            ):
                self.guest_agent.wait_ready(timeout=max(0.1, remaining))
                return
            time.sleep(0.1)
        raise TimeoutError("guest-agent frontend did not become ready")

    def stop(self, *, timeout: float = 5.0) -> None:
        process = self.process
        if process is None:
            if self.egress_proxy is not None:
                self.egress_proxy.stop()
            return
        if process.poll() is None and self.qmp_path.is_socket():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(timeout)
                    client.connect(str(self.qmp_path))
                    stream = client.makefile("rwb", buffering=0)
                    stream.readline()
                    _qmp_request(stream, "qmp_capabilities", "capabilities")
                    _qmp_request(stream, "quit", "quit")
            except (OSError, ValueError, QemuUnavailableError):
                self._signal_process_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._signal_process_group(process, signal.SIGKILL)
            process.wait(timeout=timeout)
        if process.stderr:
            process.stderr.close()
        self.process = None
        if self.egress_proxy is not None:
            self.egress_proxy.stop()

    @staticmethod
    def _signal_process_group(process: subprocess.Popen[bytes], signum: int) -> None:
        """Stop the unshare wrapper and every descendant in its private session."""

        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    def shutdown_guest(self, *, timeout: float = 60.0) -> None:
        """Request a filesystem-clean guest shutdown, then bound the wait.

        QGA's ``guest-shutdown`` command intentionally sends no success
        response: the channel normally disappears while the request is in
        flight. A closed socket is therefore expected, not evidence that the
        shutdown failed. Cache builders use this before publishing a qcow2
        overlay as a new immutable backing image.
        """

        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                self.guest_agent.request(
                    "guest-shutdown",
                    {"mode": "powerdown"},
                    timeout=min(5.0, timeout),
                )
            except (OSError, TimeoutError, GuestAgentError):
                pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop(timeout=5.0)
            raise TimeoutError(f"guest did not power down within {timeout}s")
        if process.stderr:
            process.stderr.close()
        self.process = None
        if self.egress_proxy is not None:
            self.egress_proxy.stop()

    def delete(self) -> None:
        self.stop()
        expected_parent = self.state_root.resolve()
        actual = self.session_dir.resolve()
        if actual.parent != expected_parent or actual.name != self.session_id:
            raise RuntimeError(f"refusing to delete unexpected session path: {actual}")
        marker = actual / _MARKER
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"refusing to delete unmarked session: {actual}") from exc
        if payload != {"schema": 1, "session_id": self.session_id}:
            raise RuntimeError(f"refusing to delete session with invalid marker: {actual}")
        shutil.rmtree(actual)

    def __enter__(self) -> "SandboxSession":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.delete()
