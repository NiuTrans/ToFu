"""Listener address, reverse-proxy and TLS policy for the server entrypoint."""

from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Mapping
from typing import Any

from lib.log import get_logger
from tofu_dotenv import parse_env_boolean


_MODULE_LOG = get_logger(__name__)


def detect_reverse_proxy(
    environ: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    """Detect an HTTPS-terminating IDE/notebook proxy from launch signals."""
    env = os.environ if environ is None else environ
    if env.get('VSCODE_PROXY_URI'):
        return True, 'VS Code'
    if env.get('CODESPACES'):
        return True, 'Codespaces'
    if env.get('GITPOD_WORKSPACE_URL'):
        return True, 'Gitpod'
    if (env.get('JUPYTERHUB_USER')
            or env.get('JUPYTERHUB_SERVICE_PREFIX')
            or env.get('JUPYTERHUB_API_URL')):
        return True, 'JupyterHub'
    if env.get('CODELAB_API_URL'):
        return True, 'Codelab'
    return False, ''


def resolve_tls_policy(
    *,
    no_tls: bool = False,
    tls_value: Any = '',
    certfile: str = '',
    keyfile: str = '',
    behind_proxy: bool = False,
) -> tuple[bool, str, str]:
    """Resolve explicit listener TLS policy without guessing upstream TLS."""
    raw = str(tls_value or '').strip()
    preference = parse_env_boolean(raw)
    if no_tls:
        return False, 'command-line-disabled', ''
    if preference is False:
        return False, 'explicitly-disabled', ''
    if preference is True:
        return True, 'explicitly-enabled', ''
    if certfile and keyfile:
        return True, 'configured-certificate', ''
    if behind_proxy:
        return False, 'reverse-proxy', raw
    return False, 'proxy-safe-default', raw


def listener_configuration_error(
    *,
    port: object,
    no_tls: bool = False,
    tls_value: Any = '',
    certfile: str = '',
    keyfile: str = '',
) -> str:
    """Return a human-facing error for an unsafe/ambiguous listener config."""
    try:
        numeric_port = int(port)
    except (TypeError, ValueError):
        numeric_port = 0
    if not 1 <= numeric_port <= 65535:
        return f'port must be an integer from 1 to 65535 (got {port!r})'
    if bool(certfile) != bool(keyfile):
        return '--certfile/TLS_CERTFILE and --keyfile/TLS_KEYFILE must be configured together'
    raw_tls = str(tls_value or '').strip()
    if raw_tls and parse_env_boolean(raw_tls) is None \
            and not no_tls and not (certfile and keyfile):
        return (
            f'unsupported TOFU_TLS={raw_tls!r}; expected 0/1, false/true, '
            'no/yes, off/on, or disabled/enabled')
    return ''


def find_free_port(start: int = 15000, end: int = 15100) -> int:
    """Return the first connection-free TCP port in ``[start, end)``."""
    for port in range(start, end):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                if probe.connect_ex(('localhost', port)) != 0:
                    return port
        except Exception as exc:
            _MODULE_LOG.debug('[Port] probe for localhost:%d failed: %s',
                              port, exc)
            return port
    return start


def wait_port_free(
    host: str,
    port: int,
    timeout: float = 10.0,
    *,
    logger: logging.Logger | None = None,
) -> bool:
    """Wait until ``host:port`` is bindable, bounded by ``timeout`` seconds."""
    bind_host = '' if host in ('', '0.0.0.0', '::') else host
    deadline = time.time() + timeout
    log = logger or logging.getLogger('server')
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((bind_host, port))
                return True
            except OSError as exc:
                if time.time() >= deadline:
                    log.debug(
                        '[Port] %s:%d still busy after %.1fs wait: %s',
                        bind_host or '*', port, timeout, exc,
                    )
                    return False
        time.sleep(0.25)


__all__ = [
    'detect_reverse_proxy',
    'find_free_port',
    'listener_configuration_error',
    'resolve_tls_policy',
    'wait_port_free',
]
