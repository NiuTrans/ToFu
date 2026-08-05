#!/usr/bin/env python3
"""Guard tests: healthcheck.py --runtime mode + install.sh probe wiring.

Background — the "did my install actually work?" gap (2026-07):
  install.sh ended with `exec python server.py` and NEVER verified the boot.
  healthcheck.py was a dev-time source lint (syntax / imports / vendor files)
  that no installer ever ran and that checked nothing about a live server.
  A fresh user whose server failed to boot (port busy, DB unwritable) or came
  up with no LLM credential got no signal — just raw startup logs.

  The fix: healthcheck.py grows a `--runtime [--port N] [--wait SEC]` mode
  that probes a RUNNING server (/api/health → DB responsive → index page →
  LLM credential → browser engine) and exits 0/1, and install.sh launches it
  backgrounded right before `exec python server.py` so the verdict prints
  over the startup logs.

  Behavioural tests spawn healthcheck.py as a subprocess against a fake
  in-process HTTP server; static guards pin the install.sh wiring so a later
  refactor can't silently drop the probe (NEUTER: delete the probe line or
  the --runtime branch and tests 5/6 go red).
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HC = os.path.join(ROOT, 'healthcheck.py')
INSTALL_SH = os.path.join(ROOT, 'install.sh')

_HEALTH_OK = {
    'ok': True,
    'version': '0.0.0-test',
    'db_engine': 'sqlite',
    'db_responsive': True,
    'bootId': 'abc123def456',
}


def _make_handler(health_payload):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/api/health':
                body = json.dumps(health_payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == '/':
                body = b'<html><head></head><body>tofu</body></html>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    return _Handler


def _serve(health_payload=None, start_delay=0.0):
    """Start a fake tofu server on a free port. Returns (server, port)."""
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _make_handler(health_payload or _HEALTH_OK))

    def _run():
        if start_delay:
            time.sleep(start_delay)
        srv.serve_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _run_runtime(*args, timeout=90):
    return subprocess.run(
        [sys.executable, HC, '--runtime', *args],
        capture_output=True, text=True, timeout=timeout, cwd=ROOT)


def _playwright_chromium_installed() -> bool:
    """True when a Playwright-MANAGED Chromium binary is actually on disk.

    The --runtime probe's final check LAUNCHES the Playwright Chromium build.
    ``import playwright`` alone is not evidence it can (public CI ships the
    Python package but no browser build), and a system-wide google-chrome
    does not count either — the probe launches Playwright's own build. On a
    box without it, the probe CORRECTLY reports the browser engine as
    unavailable and exits 1, so the healthy-server happy path these tests
    assert has no reachable subject.
    """
    try:
        sys.path.insert(0, ROOT)
        import chromium_env
        return bool(chromium_env.chromium_binaries(include_system=False))
    except Exception:
        return False


_requires_chromium_build = pytest.mark.skipif(
    not _playwright_chromium_installed(),
    reason='no Playwright-managed Chromium build on this host — healthcheck '
           '--runtime correctly fails its browser-engine check here, so the '
           'all-green verdict is unreachable (run: python -m playwright '
           'install --only-shell chromium)')


# ── Behavioural (failing-first: pre-change, --runtime ran the dev lint and
#    never printed these lines) ────────────────────────────────────────

@_requires_chromium_build
def test_runtime_healthy_server_passes():
    srv, port = _serve()
    try:
        r = _run_runtime('--port', str(port))
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'server reachable' in r.stdout
    assert 'database responsive' in r.stdout
    assert 'index page serves HTML' in r.stdout


def test_runtime_dead_port_fails_fast():
    port = _free_port()  # nothing listens here
    r = _run_runtime('--port', str(port), '--wait', '0')
    assert r.returncode == 1
    assert 'not answering' in r.stdout


@_requires_chromium_build
def test_runtime_wait_polls_until_server_boots():
    # Server only starts answering 0.5s in; --wait must ride over that.
    srv, port = _serve(start_delay=0.5)
    try:
        r = _run_runtime('--port', str(port), '--wait', '15')
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'server reachable' in r.stdout


def test_runtime_unhealthy_db_fails():
    payload = dict(_HEALTH_OK, db_responsive=False, db_error='locked')
    srv, port = _serve(health_payload=payload)
    try:
        r = _run_runtime('--port', str(port))
    finally:
        srv.shutdown()
    assert r.returncode == 1
    assert 'database NOT responsive' in r.stdout


# ── Static wiring guards (NEUTER: remove the wiring → these go red) ──

def test_install_sh_wires_runtime_probe_before_exec():
    src = open(INSTALL_SH, encoding='utf-8').read()
    probe_at = src.find('healthcheck.py --runtime')
    exec_at = src.find('exec python server.py')
    assert probe_at != -1, 'install.sh lost the post-install runtime probe'
    assert exec_at != -1, 'install.sh no longer execs the server (changed launch?)'
    # exec never returns — a probe placed AFTER it would be dead code.
    assert probe_at < exec_at, 'runtime probe must run BEFORE `exec python server.py`'


def test_healthcheck_runtime_branch_precedes_dev_lint():
    src = open(HC, encoding='utf-8').read()
    branch_at = src.find("if '--runtime' in sys.argv:")
    lint_at = src.find('section("1. Python Syntax Check")')
    assert branch_at != -1, 'healthcheck.py lost the --runtime entry branch'
    assert lint_at != -1
    # The runtime branch sys.exit()s; placed after the dev lint it would
    # never fire (and the lint would slow-fail on a broken fresh install).
    assert branch_at < lint_at, '--runtime branch must precede the dev-lint sections'
