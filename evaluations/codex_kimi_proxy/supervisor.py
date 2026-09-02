"""Lifecycle owner for the loopback-only Codex↔Kimi benchmark proxy.

The launcher keeps the upstream credential in this process and exposes only a
predeclared loopback TCP port to the rootless-QEMU relay.  The proxy is never
registered with Tofu's production routes and is stopped before a run is
settled or resumed.
"""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path
from types import TracebackType
from typing import Self

from .server import CodexKimiProxy, ProxyConfig


def reserve_loopback_port() -> int:
    """Return a currently free loopback port for a non-running dry-run config."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class CodexKimiProxySupervisor:
    """Start and stop exactly one isolated proxy server in a bounded thread."""

    def __init__(self, config: ProxyConfig, *, port: int = 0) -> None:
        if isinstance(port, bool) or not isinstance(port, int) \
                or not 0 <= port <= 65535:
            raise ValueError("proxy port must be an integer between 0 and 65535")
        self._config = config
        self._requested_port = port
        self._server: CodexKimiProxy | None = None
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("benchmark proxy is not started")
        return int(self._server.server_address[1])

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _serve(self) -> None:
        assert self._server is not None
        try:
            self._server.serve_forever(poll_interval=0.1)
        except BaseException as exc:  # surfaced synchronously by assert_healthy
            self._failure = exc

    def start(self) -> "CodexKimiProxySupervisor":
        if self._server is not None or self._thread is not None:
            raise RuntimeError("benchmark proxy supervisor cannot be started twice")
        server = CodexKimiProxy(
            ("127.0.0.1", self._requested_port), self._config
        )
        thread = threading.Thread(
            target=self._serve,
            name="tofu-codex-kimi-proxy",
            daemon=False,
        )
        self._server = server
        self._thread = thread
        thread.start()
        self.assert_healthy()
        return self

    def assert_healthy(self) -> None:
        if self._failure is not None:
            raise RuntimeError("benchmark proxy server failed") from self._failure
        if not self.is_alive:
            raise RuntimeError("benchmark proxy server is not running")

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        if server is None:
            return
        try:
            if thread is not None and thread.is_alive():
                server.shutdown()
        finally:
            server.server_close()
            if thread is not None:
                thread.join(timeout=5.0)
                if thread.is_alive():
                    raise RuntimeError("benchmark proxy thread did not stop")
            self._server = None
            self._thread = None
        if self._failure is not None:
            raise RuntimeError("benchmark proxy server failed") from self._failure

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()


def private_metrics_directory(path: Path) -> Path:
    """Create or validate an owner-only, non-symlink metrics directory."""

    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ValueError("proxy metrics directory must not be a symlink")
    candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
    resolved = candidate.resolve(strict=True)
    info = resolved.stat()
    if not resolved.is_dir() or info.st_uid != os.getuid() \
            or info.st_mode & 0o077:
        raise PermissionError(
            "proxy metrics directory must be private and owner-scoped"
        )
    return resolved


__all__ = [
    "CodexKimiProxySupervisor",
    "private_metrics_directory",
    "reserve_loopback_port",
]
