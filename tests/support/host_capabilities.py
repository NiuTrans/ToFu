"""Host capability probes shared by suites that exercise pathname-IPC.

The rootless runtime anchors its loopback/egress channels in unix sockets
under a private scratch root. DolphinFS FUSE returns ENOSYS on bind(), which
the product preflight surfaces as a hard failure by design (no silent /tmp
fallback). Suites covering that contract skip on incapable hosts; the
contract itself stays validated on capable hosts and CI runners.
"""
from __future__ import annotations

import socket

import pytest

_UNIX_SOCKET_SUPPORT: bool | None = None


def require_unix_socket_host(tmp_path) -> None:
    """Skip the calling test when this host cannot host a unix socket."""
    global _UNIX_SOCKET_SUPPORT
    if _UNIX_SOCKET_SUPPORT is None:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.bind(str(tmp_path / '.unix-capability-probe'))
        except OSError:
            _UNIX_SOCKET_SUPPORT = False
        else:
            _UNIX_SOCKET_SUPPORT = True
        finally:
            probe.close()
    if not _UNIX_SOCKET_SUPPORT:
        pytest.skip(
            'host filesystem cannot host unix sockets (DolphinFS FUSE ENOSYS)')
