"""Copyable-address contracts for the operator-facing ready banner."""

import logging
import time

import pytest

from lib.server_boot_report import _display_url_host, announce_server_ready


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(('bind_host', 'display_host'), [
    ('0.0.0.0', 'localhost'),
    ('::', 'localhost'),
    ('::1', '[::1]'),
    ('2001:db8::5', '[2001:db8::5]'),
    ('127.0.0.1', '127.0.0.1'),
    ('tofu.internal', 'tofu.internal'),
])
def test_bind_address_becomes_copyable_url_host(bind_host, display_host):
    assert _display_url_host(bind_host) == display_host


def test_ready_and_first_run_urls_share_display_host_while_warning_uses_bind_host(
        tmp_path, capsys):
    boot_messages = []
    banner = announce_server_ready(
        host='0.0.0.0',
        port=15000,
        tls_enabled=False,
        configured_cert=False,
        tls_requested=False,
        behind_proxy=False,
        force_no_tls=False,
        vscode_proxy='',
        feishu_ok=False,
        mcp_config={},
        auth_mode='open',
        bootstrap_token='first-run-secret',
        boot_started_at=time.time(),
        data_root=str(tmp_path),
        boot=lambda message, *args: boot_messages.append(
            message % args if args else message),
        logger=logging.getLogger('test.server-ready'),
        boot_logger=logging.getLogger('test.server-ready.boot'),
    )

    assert 'http://localhost:15000' in banner
    assert 'Open: http://localhost:15000/?token=first-run-secret' in banner
    assert 'Bound to 0.0.0.0' in banner
    assert 'API is reachable on the LAN WITHOUT auth' in banner
    assert boot_messages[-1] == 'Ready — handing off to Hypercorn.'
    assert 'first-run-secret' not in capsys.readouterr().err
