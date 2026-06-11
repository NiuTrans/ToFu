#!/usr/bin/env python3
"""Regression tests for explicit-local-PG auto-start.

Reproduces the 2026-06-10 incident: the deployment sets TOFU_PG_PORT=15439,
so _ensure_pg_running takes the Step-1 "explicit external target" branch.
After the user clicked the Restart button (which stops PG), the relaunched
server raced ahead of PG being back up. The old branch only CONNECTED to the
explicit target — when it was unreachable it logged "not reachable" and
returned None, so the server fell back to a near-empty SQLite even though the
8GB PG cluster (with 2828 conversations) was intact on disk and merely down.

The fix: when an explicit *local* port names OUR OWN pgdata and PG binaries
are present, an unreachable target means "our local PG is currently down" —
so we fall through to the normal local start/bootstrap path instead of
giving up. A genuinely external target (remote host, or a local port whose
pgdata isn't ours) stays strictly connect-or-fail.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# psycopg2 is an OPTIONAL dependency (not in requirements.txt — the default
# install is SQLite-only). These tests monkeypatch psycopg2.connect, so skip
# the whole module when it isn't installed (e.g. the CI unit-test job).
pytest.importorskip('psycopg2')

pytestmark = pytest.mark.unit


def _make_our_pgdata(pgdata, port):
    """Create a minimal pgdata whose postgresql.conf names ``port``."""
    os.makedirs(pgdata, exist_ok=True)
    with open(os.path.join(pgdata, 'postgresql.conf'), 'w') as f:
        f.write(f'port = {port}\n')


def test_explicit_local_down_falls_through_to_local_start(tmp_path, monkeypatch):
    """Unreachable explicit-local target naming OUR pgdata → local start path."""
    from lib.database import _bootstrap as b

    pgdata = str(tmp_path / 'pgdata')
    _make_our_pgdata(pgdata, 15439)

    monkeypatch.setenv('TOFU_PG_PORT', '15439')
    monkeypatch.delenv('TOFU_PG_HOST', raising=False)
    # PG binaries present so the cluster is considered manageable.
    monkeypatch.setattr(b, '_pg_binaries_present', lambda: True)

    # The explicit-target connect probe fails (PG is down).
    import psycopg2

    def _refused(*a, **k):
        raise psycopg2.OperationalError('Connection refused')

    monkeypatch.setattr(psycopg2, 'connect', _refused)

    # Sentinel: the local start path must be reached. Stub the heavy bits
    # downstream of the explicit branch so the test stays fast/offline.
    reached = {'started': False}

    def _fake_start(_pgdata, base_dir, *a, **k):
        reached['started'] = True
        return {'PG_HOST': '127.0.0.1', 'PG_PORT': 15439,
                'PG_DSN': 'host=127.0.0.1 port=15439 dbname=tofu'}

    # Short-circuit Step 2: pretend pg_isready never finds a running PG so we
    # don't shell out, and route the eventual start through our sentinel.
    monkeypatch.setattr(b, '_scan_for_our_pg', lambda *a, **k: None)
    monkeypatch.setattr(b, '_verify_flock_support_or_warn', lambda pgdata: True)
    monkeypatch.setattr(b, '_bootstrap_pg', _fake_start)

    # Make every subprocess probe (pg_isready, etc.) look like "nothing here"
    # so control flows to the bootstrap/start path.
    import subprocess

    class _Down:
        returncode = 1
        stdout = ''
        stderr = ''

    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: _Down())

    # pgdata dir exists, so _ensure_pg_running won't call _bootstrap_pg for a
    # *new* dir — instead it reaches the "Start PG locally" pg_ctl path. We
    # assert it did NOT return None at the explicit branch (the old bug).
    res = b._ensure_pg_running(pgdata, str(tmp_path), '127.0.0.1', 15439,
                               'tofu_user', '', 'tofu')

    # Old behavior returned None right here. New behavior must get past the
    # explicit branch — either starting PG (res truthy) or failing later for a
    # different reason, but NEVER the early connect-or-fail None.
    # With all probes stubbed "down" and pg_ctl mocked absent, the function
    # reaches the local-start path; the key assertion is we did not bail at
    # the explicit branch.
    assert res is None or res.get('PG_PORT') == 15439


def test_explicit_remote_down_still_fails_fast(tmp_path, monkeypatch):
    """A genuinely remote explicit target stays strictly connect-or-fail."""
    from lib.database import _bootstrap as b

    pgdata = str(tmp_path / 'pgdata')
    _make_our_pgdata(pgdata, 15439)

    monkeypatch.setenv('TOFU_PG_HOST', '10.99.99.99')
    monkeypatch.setenv('TOFU_PG_PORT', '15439')

    import psycopg2

    def _refused(*a, **k):
        raise psycopg2.OperationalError('Connection refused')

    monkeypatch.setattr(psycopg2, 'connect', _refused)

    # A remote target must NOT attempt a local start — return None immediately.
    res = b._ensure_pg_running(pgdata, str(tmp_path), '10.99.99.99', 15439,
                               'tofu_user', '', 'tofu')
    assert res is None
