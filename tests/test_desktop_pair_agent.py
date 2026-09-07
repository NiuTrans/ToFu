#!/usr/bin/env python3
"""tests/test_desktop_pair_agent.py — agent-side discovery + resume.

Covers the pure logic of lib/desktop_agent/_pair.py: the LAN broadcast
probe (magic out, verified URLs back), the ssh-config parser, the
self-tunnel lifecycle (kept on success, killed on failure), the ladder's
rung order + short-circuit, and the probe-first resume (attach-bundle
candidates → discovery ladder, token kept).

The pairing-code exchange client (exchange_pair_code) and the first-run
pairing dialog were REMOVED 2026-08-05 (owner decree: zero configuration
burden — the credential rides the per-download attach bundle). Their
coverage left with them; the server-side pair endpoints stay pinned by
tests/test_desktop_pairing.py for shipped-installer compat.
"""

import socket as _socket
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.desktop_agent import _pair  # noqa: E402

pytestmark = pytest.mark.unit


# ── Fakes ────────────────────────────────────────────────────────────────


class _FakeSock:
    """Scripted UDP socket: returns queued (data, addr) then timeouts."""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.sent = []
        self.opts = []
        self.closed = False
        self.timeout = None

    def setsockopt(self, *a):
        self.opts.append(a)

    def settimeout(self, t):
        self.timeout = t

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def recvfrom(self, _n):
        if self.scripted:
            return self.scripted.pop(0)
        raise _socket.timeout()

    def close(self):
        self.closed = True


class _FakePopen:
    def __init__(self, poll_seq):
        self._poll_seq = list(poll_seq)
        self.killed = False
        self.cmd = None

    def poll(self):
        if self._poll_seq:
            return self._poll_seq.pop(0)
        return None

    def kill(self):
        self.killed = True


# ── lan_probe ────────────────────────────────────────────────────────────

def test_lan_probe_broadcasts_magic_and_keeps_verified(monkeypatch):
    url_a = 'http://192.168.1.20:15000'
    url_b = 'http://192.168.1.21:15000'
    scripted = [(b'\x00' * 16 + url_a.encode(), ('192.168.1.20', 15001)),
                (b'\x00' * 16 + url_b.encode(), ('192.168.1.21', 15001)),
                (b'too-short', ('192.168.1.22', 15001))]
    sock = _FakeSock(scripted)
    monkeypatch.setattr(_pair, 'probe_server',
                        lambda url, timeout=0: (url == url_a, ''))
    found = _pair.lan_probe(timeout=0.05,
                            _factory=lambda *a, **k: sock)
    assert found == [url_a]
    assert sock.sent and sock.sent[0][0] == _pair._LAN_MAGIC
    assert sock.sent[0][1][1] == 15001
    assert any(o[1] == _socket.SO_BROADCAST for o in sock.opts)
    assert sock.closed is True


def test_lan_probe_empty_when_nothing_answers(monkeypatch):
    sock = _FakeSock([])
    monkeypatch.setattr(_pair, 'probe_server', lambda url, timeout=0: (True, ''))
    assert _pair.lan_probe(timeout=0.02, _factory=lambda *a, **k: sock) == []


# ── ssh_config_hosts ─────────────────────────────────────────────────────

def test_ssh_config_hosts_skips_wildcards_and_defaults(tmp_path):
    cfg = tmp_path / 'config'
    cfg.write_text(
        '# comment\n'
        'Host *\n'
        '  ServerAliveInterval 30\n'
        'Host codelab\n'
        '  HostName 10.0.0.5\n'
        '  User dev\n'
        'Host dev-*\n'
        'Host office\n'
        'Host codelab\n',  # duplicate must not repeat
        encoding='utf-8')
    assert _pair.ssh_config_hosts(str(cfg)) == ['codelab', 'office']


def test_ssh_config_hosts_missing_file():
    assert _pair.ssh_config_hosts('/nonexistent/path/config') == []


# ── try_ssh_tunnel ───────────────────────────────────────────────────────

def test_tunnel_success_keeps_process_alive():
    proc = _FakePopen([None, None])
    holder = {}

    def fake_popen(cmd, stdout=None, stderr=None):
        holder['cmd'] = cmd
        return proc

    calls = {'n': 0}

    def fake_probe(url, timeout=0):
        calls['n'] += 1
        return True, ''

    url = _pair.try_ssh_tunnel('codelab', local_port=15234, timeout=2,
                               _popen=fake_popen, _probe=fake_probe)
    assert url == 'http://127.0.0.1:15234'
    assert proc.killed is False
    assert proc in _pair._ACTIVE_TUNNELS
    _pair._ACTIVE_TUNNELS.remove(proc)  # keep the registry clean
    assert '-L' in holder['cmd'] and '15234:127.0.0.1:15000' in holder['cmd']
    assert 'BatchMode=yes' in holder['cmd']
    assert holder['cmd'][-1] == 'codelab'


def test_tunnel_probe_failure_kills_process():
    proc = _FakePopen([None])
    url = _pair.try_ssh_tunnel('codelab', timeout=0.3,
                               _popen=lambda *a, **k: proc,
                               _probe=lambda url, timeout=0: (False, 'unreachable'))
    assert url == ''
    assert proc.killed is True
    assert proc not in _pair._ACTIVE_TUNNELS


def test_tunnel_early_death_kills_process():
    spawned = []

    def dying_popen(cmd, stdout=None, stderr=None):
        p = _FakePopen([1])  # exits immediately (auth refused / bind failed)
        spawned.append(p)
        return p

    url = _pair.try_ssh_tunnel('codelab', timeout=2,
                               _popen=dying_popen,
                               _probe=lambda url, timeout=0: (True, ''),
                               _bind=lambda port: None)  # all ports "free"
    assert url == ''
    assert len(spawned) == 3  # one attempt per candidate port
    assert all(p.killed for p in spawned)
    assert not any(p in _pair._ACTIVE_TUNNELS for p in spawned)


def test_tunnel_spawn_failure_is_clean_miss():
    def boom(*a, **k):
        raise FileNotFoundError('ssh not installed')

    assert _pair.try_ssh_tunnel('h', _popen=boom) == ''


# ── discover: rung order + short-circuit ─────────────────────────────────

def test_discover_loopback_short_circuits(monkeypatch):
    called = []
    monkeypatch.setattr(_pair, 'loopback_probe',
                        lambda: called.append('lo') or 'http://127.0.0.1:15000')
    monkeypatch.setattr(_pair, 'lan_probe',
                        lambda: called.append('lan') or [])
    monkeypatch.setattr(_pair, 'try_ssh_tunnel',
                        lambda h, log=None: called.append('ssh') or '')
    assert _pair.discover() == 'http://127.0.0.1:15000'
    assert called == ['lo']


def test_discover_lan_beats_ssh(monkeypatch):
    called = []
    monkeypatch.setattr(_pair, 'loopback_probe',
                        lambda: called.append('lo') or '')
    monkeypatch.setattr(_pair, 'lan_probe',
                        lambda: called.append('lan') or ['http://10.0.0.2:15000'])
    monkeypatch.setattr(_pair, 'try_ssh_tunnel',
                        lambda h, log=None: called.append('ssh') or '')
    assert _pair.discover() == 'http://10.0.0.2:15000'
    assert called == ['lo', 'lan']


def test_discover_ssh_rung_and_candidate_cap(monkeypatch):
    tried = []
    monkeypatch.setattr(_pair, 'loopback_probe', lambda: '')
    monkeypatch.setattr(_pair, 'lan_probe', lambda: [])
    monkeypatch.setattr(_pair, 'ssh_config_hosts',
                        lambda: ['h1', 'h2', 'h3', 'h4', 'h5'])
    monkeypatch.setattr(_pair, 'try_ssh_tunnel',
                        lambda h, log=None: tried.append(h) or
                        ('http://127.0.0.1:15000' if h == 'h2' else ''))
    assert _pair.discover() == 'http://127.0.0.1:15000'
    assert tried == ['h1', 'h2']  # stops at first win


def test_discover_all_miss_returns_empty(monkeypatch):
    monkeypatch.setattr(_pair, 'loopback_probe', lambda: '')
    monkeypatch.setattr(_pair, 'lan_probe', lambda: [])
    monkeypatch.setattr(_pair, 'ssh_config_hosts', lambda: ['h1'])
    monkeypatch.setattr(_pair, 'try_ssh_tunnel', lambda h, log=None: '')
    assert _pair.discover() == ''


def test_discover_ssh_rung_capped(monkeypatch):
    tried = []
    monkeypatch.setattr(_pair, 'loopback_probe', lambda: '')
    monkeypatch.setattr(_pair, 'lan_probe', lambda: [])
    monkeypatch.setattr(_pair, 'ssh_config_hosts',
                        lambda: ['h%d' % i for i in range(10)])
    monkeypatch.setattr(_pair, 'try_ssh_tunnel',
                        lambda h, log=None: tried.append(h) or '')
    assert _pair.discover() == ''
    assert len(tried) == _pair._MAX_SSH_CANDIDATES


# ── resume_attachment: probe-first resume (owner review landmine) ────────

def test_resume_alive_is_zero_action(monkeypatch):
    monkeypatch.setattr(_pair, 'probe_server',
                        lambda url, timeout=0: (True, ''))
    monkeypatch.setattr(
        _pair, 'discover',
        lambda log=None: pytest.fail('live attachment must not re-run the ladder'))
    # save_remote_server is imported lazily INSIDE resume_attachment, so
    # patch it at its home module.
    import lib.desktop_agent.config as cfg
    monkeypatch.setattr(cfg, 'save_remote_server',
                        lambda u, s: pytest.fail('live attachment must not be re-written'))
    assert _pair.resume_attachment('http://srv:15000', 'tok') == (
        'http://srv:15000', 'tok')


def test_resume_dead_repoints_and_keeps_token(monkeypatch):
    monkeypatch.setattr(_pair, 'probe_server',
                        lambda url, timeout=0: (False, 'unreachable'))
    monkeypatch.setattr(_pair, 'discover',
                        lambda log=None: 'http://10.0.0.9:15000')
    import lib.desktop_agent.config as cfg
    saved = []
    monkeypatch.setattr(cfg, 'save_remote_server',
                        lambda u, s: saved.append((u, s)) or {'url': u})
    assert _pair.resume_attachment('http://127.0.0.1:15000', 'tok-keep') == (
        'http://10.0.0.9:15000', 'tok-keep')
    assert saved == [('http://10.0.0.9:15000', 'tok-keep')]


def test_resume_dead_same_address_is_not_rewritten(monkeypatch):
    monkeypatch.setattr(_pair, 'probe_server',
                        lambda url, timeout=0: (False, 'unreachable'))
    monkeypatch.setattr(_pair, 'discover',
                        lambda log=None: 'http://127.0.0.1:15000')
    import lib.desktop_agent.config as cfg
    monkeypatch.setattr(cfg, 'save_remote_server',
                        lambda u, s: pytest.fail('same address must not be re-written'))
    assert _pair.resume_attachment('http://127.0.0.1:15000', 'tok') == (
        'http://127.0.0.1:15000', 'tok')


def test_resume_nothing_found_keeps_attachment(monkeypatch):
    """A dead saved address with an empty ladder means the server is likely
    OFF — the attachment (and its token) must survive, the poll loop keeps
    retrying, and the user is NEVER bounced into a first-run dialog."""
    monkeypatch.setattr(_pair, 'probe_server',
                        lambda url, timeout=0: (False, 'unreachable'))
    monkeypatch.setattr(_pair, 'discover', lambda log=None: '')
    import lib.desktop_agent.config as cfg
    monkeypatch.setattr(cfg, 'save_remote_server',
                        lambda u, s: pytest.fail('no rewrite on a failed ladder'))
    assert _pair.resume_attachment('http://127.0.0.1:15000', 'tok') == (
        'http://127.0.0.1:15000', 'tok')


def test_resume_triggers_tunnel_rebuild(monkeypatch):
    """The landmine itself: saved loopback is dead after reboot, the ladder
    re-runs, and the tunnel that comes back up is registered to stay alive."""
    monkeypatch.setattr(_pair, 'probe_server',
                        lambda url, timeout=0: (False, 'unreachable'))
    proc = _FakePopen([None])

    def fake_discover(log=None):
        _pair._ACTIVE_TUNNELS.append(proc)  # what try_ssh_tunnel does on a win
        return 'http://127.0.0.1:15000'

    monkeypatch.setattr(_pair, 'discover', fake_discover)
    url, secret = _pair.resume_attachment('http://127.0.0.1:15000', 'tok')
    assert (url, secret) == ('http://127.0.0.1:15000', 'tok')
    assert proc in _pair._ACTIVE_TUNNELS and proc.killed is False
    _pair._ACTIVE_TUNNELS.remove(proc)


# ── tunnel local-port candidates (owner hardening ③) ─────────────────────

def test_tunnel_skips_busy_local_port():
    proc = _FakePopen([None, None])
    holder = {}

    def fake_popen(cmd, stdout=None, stderr=None):
        holder['cmd'] = cmd
        return proc

    def fake_bind(port):
        if port == 15000:
            raise OSError('address in use')

    url = _pair.try_ssh_tunnel('codelab', timeout=2,
                               _popen=fake_popen,
                               _probe=lambda url, timeout=0: (True, ''),
                               _bind=fake_bind)
    assert url == 'http://127.0.0.1:15100'
    assert '15100:127.0.0.1:15000' in holder['cmd']
    _pair._ACTIVE_TUNNELS.remove(proc)


def test_tunnel_all_ports_busy_is_clean_miss():
    def busy_bind(port):
        raise OSError('address in use')

    called = []
    url = _pair.try_ssh_tunnel('codelab', timeout=0.2,
                               _popen=lambda *a, **k: called.append(1),
                               _probe=lambda url, timeout=0: (True, ''),
                               _bind=busy_bind)
    assert url == ''
    assert called == []  # ssh never spawned against a port it cannot bind


# ── hidden tunnel spawns (owner report 2026-08-04: black console windows) ──

def test_quiet_spawn_kwargs_empty_off_windows():
    """POSIX has no console-allocation concept — no kwargs, no hints."""
    if sys.platform.startswith('win'):
        pytest.skip('posix branch only')
    assert _pair._quiet_spawn_kwargs() == {}


def test_quiet_spawn_kwargs_windows_hides_console(monkeypatch):
    """The agent exe is windowed (tofu-agent.spec console=False), so every
    ssh.exe child gets a VISIBLE console from Windows unless the spawn
    carries CREATE_NO_WINDOW (+ a hidden STARTUPINFO for grandchildren).
    The Windows-only subprocess attrs are getattr-guarded, so the test
    supplies them to simulate the win32 surface from Linux."""
    class _SI:
        def __init__(self):
            self.dwFlags = 0
            self.wShowWindow = None

    monkeypatch.setattr(_pair.sys, 'platform', 'win32')
    monkeypatch.setattr(_pair.subprocess, 'CREATE_NO_WINDOW', 0x08000000,
                        raising=False)
    monkeypatch.setattr(_pair.subprocess, 'STARTUPINFO', _SI, raising=False)
    monkeypatch.setattr(_pair.subprocess, 'STARTF_USESHOWWINDOW', 1,
                        raising=False)
    kw = _pair._quiet_spawn_kwargs()
    assert kw['creationflags'] == 0x08000000
    assert isinstance(kw['startupinfo'], _SI)
    assert kw['startupinfo'].dwFlags & 1  # STARTF_USESHOWWINDOW honored
    assert kw['startupinfo'].wShowWindow == 0  # SW_HIDE


def test_spawn_tunnel_routes_quiet_kwargs(monkeypatch):
    """_spawn_tunnel is the ONE real-spawn seam: the quiet kwargs land on
    the actual Popen call, output still DEVNULL'd."""
    monkeypatch.setattr(_pair, '_quiet_spawn_kwargs',
                        lambda: {'creationflags': 99})
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen['cmd'] = cmd
        seen['kwargs'] = kwargs
        return _FakePopen([None])

    monkeypatch.setattr(_pair.subprocess, 'Popen', fake_popen)
    _pair._spawn_tunnel(['ssh', '-N'])
    assert seen['cmd'] == ['ssh', '-N']
    assert seen['kwargs']['creationflags'] == 99
    assert seen['kwargs']['stdout'] is _pair.subprocess.DEVNULL
    assert seen['kwargs']['stderr'] is _pair.subprocess.DEVNULL


def test_tunnel_without_injected_popen_uses_quiet_spawn(monkeypatch):
    """Wiring: the production path (no _popen injection) must go through
    _spawn_tunnel — a regression back to a bare subprocess.Popen call is
    exactly the black-window bug returning. The Popen patch below keeps a
    broken wiring from spawning a REAL ssh during the test."""
    spawned = []

    def quiet_spawn(cmd):
        spawned.append(cmd)
        return _FakePopen([None])

    monkeypatch.setattr(_pair, '_spawn_tunnel', quiet_spawn)
    monkeypatch.setattr(_pair.subprocess, 'Popen',
                        lambda *a, **k: _FakePopen([1]))  # dead on arrival
    url = _pair.try_ssh_tunnel('codelab', local_port=15234, timeout=2,
                               _probe=lambda url, timeout=0: (True, ''))
    assert url == 'http://127.0.0.1:15234'
    assert len(spawned) == 1 and spawned[0][-1] == 'codelab'
