#!/usr/bin/env python3
"""MULTI-PROCESS Redis integration - cross-process N-invariance + SIGKILL crash-reclaim.

Every prior "redis" test ran threads sharing ONE in-memory FakeServer object in
ONE Python process - that proves nothing about cross-process serialization or
about a replica dying mid-hold. This suite runs a REAL Redis over a TCP SOCKET
(fakeredis.TcpFakeServer) and talks to it from SEPARATE OS PROCESSES via
subprocess, so the two guarantees the objective rests on are proven end-to-end:
  1. cross-process N-invariance (a slot held by process A counts against the cap
     as observed by process B, over the wire);
  2. SIGKILL crash-reclaim (a process killed while holding slots has them
     reclaimed by score expiry within ~ttl, as seen by a survivor).

Skips cleanly (with a visible reason) if no socket-capable Redis can be stood up.
"""
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

fakeredis = pytest.importorskip('fakeredis')
pytest.importorskip('redis')

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_redis_mp_worker.py')
PY = sys.executable


def _free_port():
    import socket as _s
    sk = _s.socket()
    sk.bind(('127.0.0.1', 0))
    p = sk.getsockname()[1]
    sk.close()
    return p


@pytest.fixture
def redis_socket():
    """Start a real socket-listening Redis (TcpFakeServer); yield its URL.

    Skips (not fake-passes) if TcpFakeServer is unavailable/cannot bind."""
    if not hasattr(fakeredis, 'TcpFakeServer'):
        pytest.skip('fakeredis has no TcpFakeServer - no socket-Redis available')
    import threading
    port = _free_port()
    try:
        srv = fakeredis.TcpFakeServer(('127.0.0.1', port), server_type='redis')
    except Exception as e:
        pytest.skip('cannot start TcpFakeServer: %s' % e)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    # wait for the socket to accept
    import socket as _s
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            c = _s.create_connection(('127.0.0.1', port), timeout=1)
            c.close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        srv.shutdown()
        pytest.skip('TcpFakeServer did not accept connections')
    url = 'redis://127.0.0.1:%d/0' % port
    yield url
    srv.shutdown()


def _env(url):
    e = dict(os.environ)
    e['TOFU_RUNTIME_STATE_BACKEND'] = 'redis'
    e['TOFU_REDIS_URL'] = url
    return e


def _run(url, *args, timeout=30):
    return subprocess.run([PY, _WORKER, *[str(a) for a in args]],
                          env=_env(url), capture_output=True, text=True, timeout=timeout)


def test_cross_process_N_invariance(redis_socket):
    """Two SEPARATE OS processes acquire slots for the SAME principle against the
    shared socket-Redis; total admitted across BOTH processes must be <= limit."""
    url = redis_socket
    limit = 5
    # Each process tries to grab 5 slots; combined they want 10 but the cap is 5.
    p1 = _run(url, 'acquire', 'userX', limit, 90, 5, 'A')
    p2 = _run(url, 'acquire', 'userX', limit, 90, 5, 'B')
    assert p1.returncode == 0, p1.stderr
    assert p2.returncode == 0, p2.stderr
    def _adm(out):
        for ln in out.splitlines():
            if ln.startswith('ADMITTED '):
                return int(ln.split()[1])
        return -1
    total = _adm(p1.stdout) + _adm(p2.stdout)
    # A third process observes the count over the wire.
    pc = _run(url, 'count', 'userX', limit, 90, 0, 'C')
    observed = int(pc.stdout.split()[1])
    assert total == limit, (
        'cross-process overshoot: admitted %d across 2 procs, cap %d' % (total, limit))
    assert observed == limit, (
        'process C observed count=%d, expected %d (cross-process count wrong)' % (
            observed, limit))


def test_sigkill_crash_reclaim(redis_socket):
    """SIGKILL a process while it HOLDS slots (no release); a survivor must see
    them reclaimed within ~ttl via score expiry - the real kill-window path."""
    import signal
    url = redis_socket
    limit, ttl = 3, 3
    # Process A holds all 3 slots then blocks (never releases).
    holder = subprocess.Popen([PY, _WORKER, 'hold', 'userK', str(limit), str(ttl), str(limit), 'H'],
                              env=_env(url), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # Wait until A reports it has HELD the slots.
        deadline = time.time() + 10
        held = False
        while time.time() < deadline:
            ln = holder.stdout.readline()
            if ln.startswith('HELD '):
                assert int(ln.split()[1]) == limit
                held = True
                break
        assert held, 'holder never reported HELD'
        # A survivor sees the cap FULL while A holds.
        pc = _run(url, 'count', 'userK', limit, ttl, 0, 'C1')
        assert int(pc.stdout.split()[1]) == limit, 'survivor should see cap full while A holds'
        # SIGKILL A - no chance to release.
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=5)
        # Within ~ttl the survivor must see the slots reclaimed by score expiry.
        reclaimed = False
        deadline = time.time() + ttl + 5
        while time.time() < deadline:
            pc = _run(url, 'count', 'userK', limit, ttl, 0, 'C2')
            if int(pc.stdout.split()[1]) == 0:
                reclaimed = True
                break
            time.sleep(0.25)
        assert reclaimed, (
            'SIGKILLed holder slots NEVER reclaimed within ttl - crash-reclaim broken')
        # And a fresh acquire now succeeds (capacity restored).
        pa = _run(url, 'acquire', 'userK', limit, ttl, 1, 'AFTER')
        assert 'ADMIT ' in pa.stdout, 'capacity not restored after reclaim'
    finally:
        if holder.poll() is None:
            holder.kill()


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
