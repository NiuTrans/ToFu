"""Bounded host-loopback relay for explicit QEMU control-plane services.

The QEMU process runs in a private network namespace, so it cannot connect to
the host loopback interface directly.  A parent-owned subprocess keeps the
host network namespace, accepts only same-owner Unix-socket clients, and
relays each connection to one predeclared ``127.0.0.1`` port.  The guest sees
that Unix socket only through QEMU's ``restrict=on`` ``guestfwd`` rule.

This module never accepts a hostname, non-loopback address, or task-provided
destination.  It is a narrow control-plane seam, not a second egress proxy.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


_CONNECT_TIMEOUT_SECONDS = 5.0
_IDLE_TIMEOUT_SECONDS = 360.0
_WRITE_TIMEOUT_SECONDS = 120.0
_MAX_CONFIG_BYTES = 16 * 1024
_MAX_CONNECTIONS = 32


@dataclass
class _ByteBudget:
    remaining: int
    lock: threading.Lock

    def consume(self, amount: int) -> None:
        with self.lock:
            if amount > self.remaining:
                raise PermissionError("loopback service byte budget exhausted")
            self.remaining -= amount


def _write_with_timeout(target: socket.socket, payload: bytes) -> None:
    target.setblocking(True)
    target.settimeout(_WRITE_TIMEOUT_SECONDS)
    try:
        target.sendall(payload)
    finally:
        target.settimeout(None)
        target.setblocking(False)


def _relay(left: socket.socket, right: socket.socket, budget: _ByteBudget) -> None:
    selector = selectors.DefaultSelector()
    for endpoint in (left, right):
        endpoint.setblocking(False)
        selector.register(endpoint, selectors.EVENT_READ)
    last_activity = time.monotonic()
    try:
        while selector.get_map():
            ready = selector.select(timeout=1.0)
            if not ready:
                if time.monotonic() - last_activity >= _IDLE_TIMEOUT_SECONDS:
                    return
                continue
            for key, _ in ready:
                source = key.fileobj
                target = right if source is left else left
                try:
                    payload = source.recv(64 * 1024)
                except BlockingIOError:
                    continue
                except ConnectionResetError:
                    payload = b""
                if not payload:
                    selector.unregister(source)
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                budget.consume(len(payload))
                _write_with_timeout(target, payload)
                last_activity = time.monotonic()
    finally:
        selector.close()


def _peer_uid(connection: socket.socket) -> int:
    try:
        _pid, uid, _gid = struct.unpack(
            "3i",
            connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
        )
    except (OSError, struct.error):
        return -1
    return uid


def _serve_connection(
    connection: socket.socket,
    *,
    host_port: int,
    budget: _ByteBudget,
) -> None:
    upstream: socket.socket | None = None
    try:
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.settimeout(_CONNECT_TIMEOUT_SECONDS)
        upstream.connect(("127.0.0.1", host_port))
        upstream.settimeout(None)
        _relay(connection, upstream, budget)
    except (OSError, PermissionError):
        # The raw guest protocol owns its own error representation.  Closing the
        # connection is fail-closed and cannot echo host exception details.
        return
    finally:
        if upstream is not None:
            upstream.close()
        connection.close()


def _child_main(listen_fd: int) -> int:
    config_line = sys.stdin.buffer.readline(_MAX_CONFIG_BYTES)
    config = json.loads(config_line)
    host_port = int(config["host_port"])
    maximum_bytes = int(config["maximum_bytes"])
    maximum_connections = int(config["maximum_connections"])
    if not 1 <= host_port <= 65535:
        raise ValueError("loopback service host port is invalid")
    if maximum_bytes < 1024 * 1024:
        raise ValueError("loopback service byte budget is too small")
    if not 1 <= maximum_connections <= _MAX_CONNECTIONS:
        raise ValueError("loopback service connection limit is invalid")

    listener = socket.socket(fileno=listen_fd)
    listener.settimeout(0.5)
    budget = _ByteBudget(maximum_bytes, threading.Lock())
    slots = threading.BoundedSemaphore(maximum_connections)
    stopping = threading.Event()

    def stop_on_parent_close() -> None:
        sys.stdin.buffer.read()
        stopping.set()

    threading.Thread(target=stop_on_parent_close, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    workers: set[threading.Thread] = set()
    try:
        while not stopping.is_set():
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if _peer_uid(connection) != os.getuid():
                connection.close()
                continue
            if not slots.acquire(timeout=5.0):
                connection.close()
                continue

            def worker(client: socket.socket) -> None:
                try:
                    _serve_connection(
                        client,
                        host_port=host_port,
                        budget=budget,
                    )
                finally:
                    slots.release()

            thread = threading.Thread(target=worker, args=(connection,), daemon=True)
            workers.add(thread)
            thread.start()
            workers = {item for item in workers if item.is_alive()}
    finally:
        listener.close()
        for worker in workers:
            worker.join(timeout=2.0)
    return 0


class LoopbackServiceRelay:
    """Lifecycle owner for one fixed host-loopback destination."""

    def __init__(
        self,
        *,
        socket_path: Path,
        host_port: int,
        maximum_bytes: int,
        maximum_connections: int,
    ) -> None:
        self.host_port = int(host_port)
        self.maximum_bytes = int(maximum_bytes)
        self.maximum_connections = int(maximum_connections)
        if not 1 <= self.host_port <= 65535:
            raise ValueError("loopback service host port must be between 1 and 65535")
        if self.maximum_bytes < 1024 * 1024:
            raise ValueError("loopback service maximum_bytes must be at least 1 MiB")
        if not 1 <= self.maximum_connections <= _MAX_CONNECTIONS:
            raise ValueError(
                f"loopback service maximum_connections must be between 1 and {_MAX_CONNECTIONS}"
            )

        parent = socket_path.expanduser().parent.resolve(strict=True)
        parent_info = parent.stat()
        if parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o077:
            raise PermissionError("loopback service socket parent must be private and owned")
        self.socket_path = parent / socket_path.name
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise FileExistsError(
                f"loopback service socket path already exists: {self.socket_path}"
            )
        self._parent_fd: int | None = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        socket_reference = (
            f"/proc/self/fd/{self._parent_fd}/{self.socket_path.name}"
        )
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._listener.bind(socket_reference)
        except Exception:
            self._listener.close()
            os.close(self._parent_fd)
            self._parent_fd = None
            raise
        self.socket_path.chmod(0o600)
        self._listener.listen(self.maximum_connections)
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("loopback service relay is already started")
        command: list[str] = []
        prlimit = Path("/usr/bin/prlimit")
        if prlimit.is_file():
            command.extend(
                [
                    str(prlimit),
                    "--core=0:0",
                    "--nofile=128:128",
                    f"--as={256 * 1024**2}:{256 * 1024**2}",
                    "--fsize=0:0",
                    "--",
                ]
            )
        command.extend(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--listen-fd",
                str(self._listener.fileno()),
            ]
        )
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=(self._listener.fileno(),),
            start_new_session=True,
            env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
        )
        assert self.process.stdin is not None
        self.process.stdin.write(
            (
                json.dumps(
                    {
                        "host_port": self.host_port,
                        "maximum_bytes": self.maximum_bytes,
                        "maximum_connections": self.maximum_connections,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )
        self.process.stdin.flush()
        self._listener.close()
        time.sleep(0.05)
        if self.process.poll() is not None:
            error = (
                self.process.stderr.read().decode(errors="replace")
                if self.process.stderr
                else ""
            )
            raise RuntimeError(
                f"loopback service relay failed to start: {error.strip()}"
            )

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        if self.process is None:
            self._listener.close()
            self.socket_path.unlink(missing_ok=True)
            self._close_parent_fd()
            return
        process, self.process = self.process, None
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        if process.stderr:
            process.stderr.close()
        self.socket_path.unlink(missing_ok=True)
        self._close_parent_fd()

    def _close_parent_fd(self) -> None:
        if self._parent_fd is not None:
            os.close(self._parent_fd)
            self._parent_fd = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-fd", type=int, required=True)
    args = parser.parse_args(argv)
    return _child_main(args.listen_fd)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["LoopbackServiceRelay"]
