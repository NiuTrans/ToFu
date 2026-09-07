"""Listener network policy remains deterministic outside server assembly."""

from __future__ import annotations

import socket
from pathlib import Path

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
    assert server._listener_configuration_error is \
        server_network.listener_configuration_error
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


@pytest.mark.parametrize(
    ('kwargs', 'expected'),
    [
        ({'port': 'bad'}, 'port must be an integer'),
        ({'port': 0}, 'port must be an integer'),
        ({'port': 15000, 'tls_value': 'sometimes'}, 'unsupported TOFU_TLS'),
        ({'port': 15000, 'certfile': 'cert.pem'}, 'must be configured together'),
    ],
)
def test_listener_configuration_rejects_ambiguous_values(kwargs, expected):
    assert expected in server_network.listener_configuration_error(**kwargs)


def test_explicit_listener_overrides_make_tls_intent_unambiguous():
    assert server_network.listener_configuration_error(
        port=15000, no_tls=True, tls_value='sometimes') == ''
    assert server_network.listener_configuration_error(
        port=15000, tls_value='sometimes',
        certfile='cert.pem', keyfile='key.pem') == ''


def test_managed_and_reexec_workers_never_shift_configured_endpoints():
    source = (Path(__file__).resolve().parents[1] / 'server.py').read_text(
        encoding='utf-8')
    port_policy = source.split(
        "if _reexec_port_env:", 1)[1].split(
        "# Record the port we actually bound", 1)[0]
    reexec_branch, managed_and_unmanaged = port_policy.split(
        "    else:\n        if os.environ.get('TOFU_SERVER_WORKER')", 1)
    managed_branch = managed_and_unmanaged.split(
        "        else:\n            port = _find_free_port", 1)[0]

    assert 'refusing to shift endpoints' in reexec_branch
    assert 'workers never shift ports' in managed_branch
    assert reexec_branch.count('raise SystemExit(1)') == 1
    assert managed_branch.count('raise SystemExit(1)') == 1
    assert '_find_free_port' not in reexec_branch
    assert '_find_free_port' not in managed_branch
