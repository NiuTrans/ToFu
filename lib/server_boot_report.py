"""Operator-facing ready report emitted after application startup succeeds."""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any


def _display_url_host(bind_host: str) -> str:
    """Turn a listener address into a browser-copyable URL host."""
    host = str(bind_host or '').strip()
    if host in ('', '0.0.0.0', '::', '[::]'):
        return 'localhost'
    if ':' in host and not host.startswith('['):
        return f'[{host}]'
    return host


def announce_server_ready(
    *,
    host: str,
    port: int,
    tls_enabled: bool,
    configured_cert: bool,
    tls_requested: bool,
    behind_proxy: bool,
    force_no_tls: bool,
    vscode_proxy: str,
    feishu_ok: bool,
    mcp_config: Mapping[str, Any],
    auth_mode: str,
    bootstrap_token: str,
    boot_started_at: float,
    data_root: str,
    boot: Callable[..., Any],
    logger: logging.Logger,
    boot_logger: logging.Logger,
) -> str:
    """Publish readiness only after the native Quart startup hook completed."""
    from lib.version import __version__

    protocol = 'https' if tls_enabled else 'http'
    if tls_enabled:
        h2_status = ('HTTP/2 + HTTP/1.1 (TLS, configured certificate)'
                     if configured_cert else
                     'HTTP/2 + HTTP/1.1 (TLS, development certificate)')
    elif tls_requested:
        h2_status = 'HTTP/1.1 (TLS requested but unavailable)'
    elif behind_proxy:
        h2_status = 'HTTP/1.1 (proxy provides HTTP/2)'
    elif force_no_tls:
        h2_status = 'HTTP/1.1 only (TLS disabled)'
    else:
        h2_status = 'HTTP/1.1 (proxy-safe default; TLS is opt-in)'

    display_url = f'{protocol}://{_display_url_host(host)}:{port}'
    lines = ['=' * 56, f'  🫧 Tofu Server  v{__version__}  [ASYNC]']
    if behind_proxy and vscode_proxy:
        lines.append(f"  {vscode_proxy.replace('{{port}}', str(port))}")
    lines.extend([
        f'  {display_url}',
        f'  Protocol: {h2_status}',
        '  Server: Hypercorn (ASGI)',
    ])
    if tls_enabled and not configured_cert:
        lines.append(
            '  🔐  Development self-signed cert (not device-trusted; use a '
            'trusted ingress for production)')
    if feishu_ok:
        lines.append('  💬  Feishu Bot: ON')
    if mcp_config:
        lines.append(f'  🔌  MCP Apps: {len(mcp_config)} server(s)')
    if auth_mode == 'open':
        lines.append('  🔓  Auth: OPEN — no token required')
        if host not in ('127.0.0.1', 'localhost', '::1'):
            lines.extend([
                f'  ⚠️   Bound to {host}: API is reachable on the LAN WITHOUT '
                'auth unless an outer container/proxy publishes loopback only.',
                '      Keep that outer boundary local, or switch to private '
                'mode / set TOFU_AUTH_MODE=private.',
            ])
    elif auth_mode == 'private':
        lines.append('  🔒  Auth: PRIVATE — Bearer token required')
    elif auth_mode == 'multi-user':
        lines.append('  👥  Auth: MULTI-USER — Bearer token required')

    if bootstrap_token:
        lines.extend([
            '  🔑  Personal admin key minted (first boot)',
            f'      Token: {bootstrap_token}',
            f'      Open: {display_url}/?token={bootstrap_token}',
            '      Saved to data/config/.first_run_token (chmod 0600)',
            '      (auto-cleared when this bootstrap key is revoked)',
        ])

    if host in ('127.0.0.1', 'localhost', '::1') and vscode_proxy.strip():
        lines.extend([
            '  ⚠️   Bound to loopback behind a cloud-IDE proxy: remote desktop '
            'agents can NEVER reach this server',
            '      (the SSO edge rejects cookieless clients). Unset BIND_HOST / '
            'use --host 0.0.0.0 and restart to allow',
            '      direct LAN attach, or rely on the agent-side ssh self-tunnel.',
        ])
    lines.extend([
        '  ⏱  Boot time: %.1fs' % (time.time() - boot_started_at),
        '=' * 56,
    ])
    banner = '\n'.join(lines)
    logger.info('Server starting\n%s', banner)

    try:
        from lib.llm.cache import CACHE_FIX_GEN
        try:
            from lib import boot_identity
            boot_id = boot_identity.BOOT_ID
        except Exception as exc:
            boot_logger.debug('[BootIdentity] boot id unavailable: %s', exc)
            boot_id = '?'
        boot('[CacheFixGen] CACHE_FIX_GEN=%d pid=%d bootId=%s (in-memory)'
             % (CACHE_FIX_GEN, os.getpid(), boot_id))
        try:
            from lib.llm.cache import _mid_placement_mode
            boot('[CacheMidMode] TOFU_CACHE_MID_MODE=%s pid=%d bootId=%s '
                 '(in-memory)'
                 % (_mid_placement_mode(), os.getpid(), boot_id))
        except Exception as exc:
            boot_logger.warning('[CacheMidMode] self-report failed: %s', exc)
    except Exception as exc:
        boot_logger.warning('[CacheFixGen] self-report failed: %s', exc)

    try:
        from lib import boot_identity
        fingerprint = boot_identity.code_fingerprint()
        boot('[CodeFingerprint] head=%s dirty=%s digest=%s'
             % (fingerprint.get('head'), fingerprint.get('dirty'),
                fingerprint.get('digest')))
    except Exception as exc:
        boot_logger.warning('[CodeFingerprint] self-report failed: %s', exc)

    try:
        os.unlink(os.path.join(data_root, '.reexec_in_progress'))
    except FileNotFoundError as exc:
        boot_logger.debug('[Update] no re-exec marker to clear: %s', exc)
    except Exception as exc:
        boot_logger.debug('[Update] re-exec marker clear failed: %s', exc)

    boot('Ready — handing off to Hypercorn.')
    try:
        # stderr is commonly redirected into a durable process-console file.
        # The bootstrap token remains recoverable from its chmod-0600 file;
        # never duplicate the plaintext credential into a general log.
        from lib.log_redaction import redact_text
        sys.stderr.write(f'\n{redact_text(banner)}\n\n')
        sys.stderr.flush()
    except Exception as exc:
        boot_logger.debug('[Server] ready banner console echo failed: %s', exc)
    return banner


__all__ = ['announce_server_ready']
