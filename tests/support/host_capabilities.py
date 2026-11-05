"""Host capability probes shared by suites that need a kernel or filesystem feature.

The rootless runtime anchors its loopback/egress channels in unix sockets
under a private scratch root. DolphinFS FUSE returns ENOSYS on bind(), which
the product preflight surfaces as a hard failure by design (no silent /tmp
fallback). Identity-bound process control uses pidfds, which Linux before
5.3 lacks (ENOSYS). Suites covering those contracts skip on incapable hosts;
the contracts themselves stay validated on capable hosts and CI runners.
"""
from __future__ import annotations

import errno
import os
import socket

import pytest

_UNIX_SOCKET_SUPPORT: bool | None = None
_PIDFD_SUPPORT: bool | None = None


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


def require_pidfd_host() -> None:
    """Skip the calling test when this kernel has no pidfd_open."""
    global _PIDFD_SUPPORT
    if _PIDFD_SUPPORT is None:
        try:
            descriptor = os.pidfd_open(os.getpid())
        except AttributeError:
            _PIDFD_SUPPORT = False
        except OSError as error:
            if error.errno != errno.ENOSYS:
                raise
            _PIDFD_SUPPORT = False
        else:
            os.close(descriptor)
            _PIDFD_SUPPORT = True
    if not _PIDFD_SUPPORT:
        pytest.skip('kernel has no pidfd_open (Linux < 5.3; ENOSYS)')
