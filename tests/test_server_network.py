"""Listener network policy remains deterministic outside server assembly."""

from __future__ import annotations

import socket

import pytest

from lib import server_network


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ('environment', 'expected'),
    [
        ({'VSCODE_PROXY_URI': 'https://example/{{port}}'}, (True, 'VS Code')),
        ({'CODESPACES': 'true'}, (True, 'Codespaces')),
        ({'GITPOD_WORKSPACE_URL': 'https://example'}, (True, 'Gitpod')),
        ({'JUPYTERHUB_USER': 'u'}, (True, 'JupyterHub')),
        ({'CODELAB_API_URL': 'https://example'}, (True, 'Codelab')),
        ({}, (False, '')),
    ],
)
def test_reverse_proxy_detection_uses_explicit_environment(environment, expected):
    assert server_network.detect_reverse_proxy(environment) == expected


def test_server_keeps_compatibility_exports():
    import server

    assert server._detect_reverse_proxy is server_network.detect_reverse_proxy
    assert server._resolve_tls_policy is server_network.resolve_tls_policy
    assert server._find_free_port is server_network.find_free_port
    assert server._wait_port_free is server_network.wait_port_free


def test_wait_port_free_observes_a_real_listener():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert server_network.wait_port_free(
            '127.0.0.1', port, timeout=0.01,
        ) is False

    assert server_network.wait_port_free(
        '127.0.0.1', port, timeout=0.05,
    ) is True
