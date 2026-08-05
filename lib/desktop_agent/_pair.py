#!/usr/bin/env python3
"""lib/desktop_agent/_pair.py — agent-side pairing-code exchange + the
first-run server-discovery ladder (docs/DESKTOP_AGENT_DIST_DESIGN.md §11.2.1).

Two pieces:

* ``exchange_pair_code(url, code)`` — the wire client for
  ``POST /api/desktop/pair``. The 6-digit code IS the credential (no
  bearer); a successful exchange returns a fresh ``agents:bridge`` token
  the caller saves as its remote attachment.

* ``discover()`` — the zero-question ladder: loopback → LAN broadcast →
  ``~/.ssh/config`` self-tunnels. Each rung is cheap and silent; the first
  reachable Tofu wins and is returned as a base URL for the caller to
  pre-fill into the pairing dialog. Only when EVERY rung misses does the
  user get asked — and then exactly once.

The SSH rung spawns REAL ``ssh -N -L`` processes. A tunnel that wins is
kept alive in ``_ACTIVE_TUNNELS`` for the process lifetime (the agent
polls through it) and reaped at exit; a losing candidate is killed
immediately.
"""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
import time

from lib.desktop_agent._probe import probe_server
from lib.log import get_logger

logger = get_logger(__name__)

# Mirror of lib/desktop/pairing.py's wire constants — the agent half of
# the same protocol. Kept as local copies: the agent bundle must not
# import the server-side module (it drags the store + responder in).
_LAN_MAGIC = b'TOFU-DESKTOP-DISC\x01'
_LAN_PORT = 15001
_DEFAULT_PORT = 15000

# Tunnels this process opened and must keep alive (the agent polls
# through them). Entries: subprocess.Popen. Reaped via atexit.
_ACTIVE_TUNNELS: list = []

# Bound the SSH rung: each candidate costs up to its probe timeout, so an
# unbounded ~/.ssh/config could stall first run for minutes.
_MAX_SSH_CANDIDATES = 3


def _noop_log(_msg: str) -> None:
    pass


# ═══════════════════════════════════════════════════════════════════
#  Pairing-code exchange (POST /api/desktop/pair)
# ═══════════════════════════════════════════════════════════════════

def exchange_pair_code(url: str, code: str, name: str = '',
                       platform: str = '', timeout: float = 10.0):
    """Exchange a 6-digit pairing code for a bridge token.

    Returns ``(True, token)`` on success, else ``(False, reason)`` where
    reason is one of:

      * ``invalid_code`` — wrong / expired / already-used code (server 409);
      * ``rate_limited`` — too many failed attempts from this address
        (server 429; wait a few minutes and mint a fresh code);
      * ``unreachable`` / ``timeout`` / ``error`` — transport failures;
      * ``http_<n>`` — any other HTTP status (a proxy's refusal lands
        here, e.g. an SSO edge 401 — the ADDRESS is wrong, not the code);
      * ``bad_response`` — a 2xx that does not carry a token.
    """
    import socket as _socket

    import requests
    base = (url or '').strip().rstrip('/')
    code = (code or '').strip()
    if not base.startswith(('http://', 'https://')):
        return False, 'unreachable'
    if not code:
        return False, 'invalid_code'
    if not name:
        try:
            name = _socket.gethostname()
        except Exception as e:
            logger.debug('[Agent] hostname unavailable: %s', e)
            name = ''
    payload = {'code': code,
               'name': name or 'paired-agent',
               'platform': platform or sys.platform}
    try:
        resp = requests.post(base + '/api/desktop/pair', json=payload,
                             timeout=timeout,
                             proxies={'no_proxy': '*'})
    except requests.exceptions.ConnectTimeout as e:
        logger.debug('[Agent] pair exchange timed out: %s', e)
        return False, 'timeout'
    except requests.exceptions.ConnectionError as e:
        logger.debug('[Agent] pair exchange unreachable: %s', e)
        return False, 'unreachable'
    except requests.RequestException as e:
        logger.debug('[Agent] pair exchange failed: %s', e)
        return False, 'error'
    if resp.status_code == 409:
        return False, 'invalid_code'
    if resp.status_code == 429:
        return False, 'rate_limited'
    if resp.status_code not in (200, 201):
        return False, 'http_%d' % resp.status_code
    try:
        body = resp.json()
    except ValueError as e:
        logger.debug('[Agent] pair response not JSON: %s', e)
        return False, 'bad_response'
    token = (body.get('token') or '') if isinstance(body, dict) else ''
    if not token:
        return False, 'bad_response'
    return True, token


# ═══════════════════════════════════════════════════════════════════
#  Discovery ladder: loopback → LAN broadcast → ~/.ssh/config tunnels
# ═══════════════════════════════════════════════════════════════════

def loopback_probe(port: int = _DEFAULT_PORT, timeout: float = 1.5) -> str:
    """Rung A: Tofu on THIS machine. Returns its URL or ''."""
    ok, _ = probe_server('http://127.0.0.1:%d' % port, timeout=timeout)
    return ('http://127.0.0.1:%d' % port) if ok else ''


def lan_probe(timeout: float = 1.5, _factory=None) -> list:
    """Rung B: broadcast the discovery magic on UDP 15001, keep the
    responder URLs that pass ``probe_server``.

    The responder's HMAC is unverifiable without a shared secret (the
    server-side responder itself documents this), so trust-but-verify:
    any URL offered is probed against ``/api/health`` before use — a
    hostile LAN responder can at worst cost us one dropped probe.
    ``_factory`` injects a fake socket for tests.
    """
    import socket as _socket
    factory = _factory or _socket.socket
    urls: list[str] = []
    try:
        s = factory(_socket.AF_INET, _socket.SOCK_DGRAM)
    except OSError as e:
        logger.debug('[Agent] LAN discovery socket failed: %s', e)
        return urls
    try:
        s.setsockopt(_socket.SOL_SOCKET, _socket.SO_BROADCAST, 1)
        s.settimeout(0.4)
        try:
            s.sendto(_LAN_MAGIC, ('255.255.255.255', _LAN_PORT))
        except OSError as e:
            logger.debug('[Agent] LAN discovery broadcast failed: %s', e)
            return urls
        deadline = time.time() + timeout
        seen = set()
        while time.time() < deadline:
            try:
                data, _addr = s.recvfrom(1024)
            except _socket.timeout as e:
                logger.debug('[Agent] LAN discovery recv timeout: %s', e)
                continue
            except OSError as e:
                logger.debug('[Agent] LAN discovery recv failed: %s', e)
                break
            if len(data) <= 16:
                continue
            try:
                url = data[16:].decode('utf-8', 'replace').strip()
            except Exception as e:
                logger.debug('[Agent] LAN discovery response undecodable: %s', e)
                continue
            if not url.startswith(('http://', 'https://')) or url in seen:
                continue
            seen.add(url)
            ok, _ = probe_server(url, timeout=1.5)
            if ok:
                urls.append(url.rstrip('/'))
    finally:
        try:
            s.close()
        except Exception as e:
            logger.debug('[Agent] LAN discovery socket close failed: %s', e)
    return urls


def ssh_config_hosts(path: str = '') -> list:
    """Rung C input: the Host aliases from ``~/.ssh/config`` (VS Code
    Remote-SSH writes here too, so a codelab/dev-box the user already
    SSHes into is a free candidate). Wildcards and the global ``Host *``
    defaults are skipped; order preserved, duplicates dropped."""
    path = path or os.path.expanduser('~/.ssh/config')
    hosts: list[str] = []
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if not parts or parts[0].lower() != 'host':
                    continue
                for alias in parts[1:]:
                    if '*' in alias or '?' in alias or '!' in alias:
                        continue
                    if alias not in hosts:
                        hosts.append(alias)
    except OSError as e:
        logger.debug('[Agent] ssh config unreadable: %s', e)
        return []
    return hosts


def _reap_tunnels() -> None:
    for proc in list(_ACTIVE_TUNNELS):
        try:
            proc.kill()
        except Exception as e:
            logger.debug('[Agent] tunnel reap failed: %s', e)
    _ACTIVE_TUNNELS.clear()


atexit.register(_reap_tunnels)


def _quiet_spawn_kwargs() -> dict:
    """Popen kwargs that keep a console-subsystem child INVISIBLE.

    The agent ships as a windowed exe (tofu-agent.spec: console=False),
    so every ``ssh.exe`` it spawns without this gets a fresh console
    window from Windows — a black shell popping up per tunnel attempt,
    up to 3 hosts x 3 ports per ladder walk, re-run at every boot-time
    resume (owner report 2026-08-04). CREATE_NO_WINDOW allocates no
    console at all; the hidden STARTUPINFO covers children that create
    their own window anyway. POSIX: no such concept, no kwargs.
    All Windows-only subprocess attrs are getattr-guarded so the module
    stays importable and testable on Linux.
    """
    if not sys.platform.startswith('win'):
        return {}
    kwargs = {}
    no_window = getattr(subprocess, 'CREATE_NO_WINDOW', None)
    if no_window is not None:
        kwargs['creationflags'] = no_window
    startupinfo_cls = getattr(subprocess, 'STARTUPINFO', None)
    if startupinfo_cls is not None:
        si = startupinfo_cls()
        si.dwFlags |= getattr(subprocess, 'STARTF_USESHOWWINDOW', 1)
        si.wShowWindow = 0  # SW_HIDE
        kwargs['startupinfo'] = si
    return kwargs


def _spawn_tunnel(cmd: list):
    """The REAL ssh spawn — quiet-kwargs applied exactly here, so the
    injected ``_popen`` fakes in tests keep their narrow signature."""
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            **_quiet_spawn_kwargs())


def _local_port_busy(port: int, _bind=None) -> bool:
    """Whether 127.0.0.1:<port> is already taken. A bind probe costs
    nothing, so an occupied candidate is skipped INSTANTLY instead of
    burning the ssh attempt's whole timeout on ExitOnForwardFailure."""
    if _bind is not None:
        try:
            _bind(port)
            return False
        except OSError as e:
            logger.debug('[Agent] port %d busy: %s', port, e)
            return True
    import socket as _socket
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', port))
        return False
    except OSError as e:
        logger.debug('[Agent] port %d busy: %s', port, e)
        return True
    finally:
        s.close()


def _tunnel_once(host: str, local_port: int, remote_port: int,
                 timeout: float, log, _popen, _probe) -> str:
    """One ssh -N -L attempt on one local port. Win → process kept, URL
    returned; any failure → process killed, ''."""
    probe = _probe or probe_server
    url = 'http://127.0.0.1:%d' % local_port
    cmd = ['ssh', '-N', '-T',
           '-o', 'BatchMode=yes',
           '-o', 'ConnectTimeout=6',
           '-o', 'ExitOnForwardFailure=yes',
           '-o', 'ServerAliveInterval=15',
           '-L', '%d:127.0.0.1:%d' % (local_port, remote_port),
           host]
    try:
        if _popen is not None:
            proc = _popen(cmd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
        else:
            proc = _spawn_tunnel(cmd)
    except (OSError, FileNotFoundError) as e:
        log('SSH tunnel to %s could not start: %s' % (host, e))
        return ''
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            break  # died (auth refused, bind failed, host unreachable)
        ok, _ = probe(url, timeout=1.5)
        if ok:
            _ACTIVE_TUNNELS.append(proc)
            log('SSH self-tunnel up: %s -> %s' % (host, url))
            return url
        time.sleep(0.4)
    try:
        proc.kill()
    except Exception as e:
        logger.debug('[Agent] losing tunnel kill failed: %s', e)
    return ''


def try_ssh_tunnel(host: str, local_port: int = _DEFAULT_PORT,
                   remote_port: int = _DEFAULT_PORT, timeout: float = 8.0,
                   log=_noop_log, _popen=None, _probe=None, _bind=None) -> str:
    """Rung C: ``ssh -N -L <local>:127.0.0.1:<remote> <host>`` in BatchMode
    (never prompts — a password-gated host simply fails fast) and probe the
    loopback end for Tofu.

    The preferred local port is tried first (stable URL across reboots);
    when it is busy, two alternates are tried — a machine with something
    already on 15000 must not lose the whole rung (owner review
    2026-08-03). Busy candidates are skipped by a free bind probe, so only
    a genuinely attemptable port ever pays the ssh spawn + probe budget."""
    for port in (local_port, local_port + 100, local_port + 200):
        if _local_port_busy(port, _bind=_bind):
            log('local port %d busy for the tunnel — trying next' % port)
            continue
        url = _tunnel_once(host, port, remote_port, timeout, log,
                           _popen, _probe)
        if url:
            return url
    return ''


def resume_attachment(url: str, secret: str, log=_noop_log):
    """Probe-first resume for a SAVED attachment. Returns ``(url, secret)``.

    The landmine this defuses (owner review 2026-08-03): a tunnel that won
    during first-run discovery was a child of the agent process, and
    ``_reap_tunnels`` (atexit) kills it — so a saved loopback URL is a dead
    port on EVERY subsequent boot, while the launcher used to skip the
    ladder whenever any URL was saved. Autostart-on-install meant the very
    machine that paired fine on day one came up dead on day two.

    Contract:
      * saved address answers → return it unchanged (zero action);
      * saved address dead → re-run the ladder; when it finds the server,
        KEEP THE TOKEN (a bridge token is a bearer credential — it does not
        care which address reaches the server) and persist the re-pointed
        address only when it actually changed;
      * ladder finds nothing → return the saved pair UNCHANGED. The server
        may simply be off right now; the poll loop keeps retrying and the
        tray link line says 'unreachable' honestly. Never bounce the user
        into a first-run dialog over a transient outage.
    """
    ok, _ = probe_server(url, timeout=3.0)
    if ok:
        return url, secret
    log('Saved attachment %s is not answering — re-running discovery' % url)
    found = discover(log=log)
    if not found:
        return url, secret
    if found != url:
        try:
            from lib.desktop_agent.config import save_remote_server
            save_remote_server(found, secret)
            log('Attachment re-pointed: %s -> %s (token kept)' % (url, found))
        except Exception as e:
            log('Could not persist re-pointed attachment: %s' % e)
            logger.warning('[Agent] could not persist re-pointed attachment: %s', e)
    return found, secret


def discover(log=_noop_log, lan: bool = True, ssh: bool = True) -> str:
    """Walk the ladder, cheapest rung first. Returns the first reachable
    Tofu base URL, or '' when every rung missed (the caller then asks the
    user — exactly once, with an editable address field)."""
    url = loopback_probe()
    if url:
        log('Discovered Tofu on loopback: %s' % url)
        return url
    if lan:
        found = lan_probe()
        if found:
            log('Discovered Tofu on the LAN: %s' % found[0])
            return found[0]
    if ssh:
        for host in ssh_config_hosts()[:_MAX_SSH_CANDIDATES]:
            url = try_ssh_tunnel(host, log=log)
            if url:
                return url
    return ''


__all__ = ['exchange_pair_code', 'loopback_probe', 'lan_probe',
           'ssh_config_hosts', 'try_ssh_tunnel', 'discover',
           'resume_attachment', '_ACTIVE_TUNNELS']
