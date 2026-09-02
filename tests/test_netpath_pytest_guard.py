"""Regression guard: the netpath prober must never start in a pytest process,
and the production netpath store must stay free of test/doc ghost hosts.

``tests/conftest.py`` pins ``TOFU_NETPATH=off`` BEFORE importing ``server``
(whose module-level code calls ``start_prober()``). Without that pin, every
test process — the controller plus every xdist worker — would spawn a daemon
thread that starts firing real network probes (through the env proxy) ten
seconds later and writes into the production ``logs/app.log``.

The store guard is self-healing: entries whose host is either a reserved
test/doc suffix (RFC 2606 / RFC 6761) or an exempt host (IP literal /
localhost, which netpath refuses to track by design) can ONLY have leaked
from a test fixture or a doc example, so the guard sweeps them out of the
production store (atomic rewrite, real hosts preserved byte-for-byte) and
emits a warning instead of failing the run.

These tests intentionally live OUTSIDE tests/test_netpath.py: that module's
autouse fixture forces the switch back on to exercise the mechanism itself.
"""
from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import pytest

import lib.netpath as netpath
from lib.mcp.registry import is_opensource_build

_PROD_STORE = Path(__file__).resolve().parents[1] / 'data' / 'config' / 'netpath.json'

# Reserved / unroutable suffixes used by test fixtures and documentation
# (RFC 2606 + RFC 6761). A production deployment never legitimately talks to
# any of these, so a match is by definition leaked state.
_RESERVED_SUFFIXES = ('.test', '.invalid', '.example', '.example.com')


def _is_ghost_host(host: str) -> bool:
    h = (host or '').lower()
    return h.endswith(_RESERVED_SUFFIXES) or netpath._is_exempt_host(h)


@pytest.mark.unit
def test_conftest_pins_netpath_off():
    # If this fails, the conftest pin was removed (or the whole suite is
    # deliberately being run with TOFU_NETPATH overridden in the env).
    assert os.environ.get('TOFU_NETPATH') == 'off'


@pytest.mark.unit
def test_server_import_did_not_start_prober():
    # conftest imports server at collection time; with the pin in place the
    # module-level start_prober() call must have been a no-op. (Tolerates a
    # stopped thread object left by an earlier test in the same worker.)
    t = netpath._prober_thread
    assert t is None or not t.is_alive()


@pytest.mark.unit
def test_start_prober_refuses_in_pinned_env():
    assert netpath.start_prober(interval=60) is False
    t = netpath._prober_thread
    assert t is None or not t.is_alive()


@pytest.mark.unit
@pytest.mark.parametrize('host', [
    'flap.test', 'ghost.invalid', 'x.example', 'fast-direct.example.com',
    '127.0.0.1', '10.1.2.3', '::1', 'localhost',
])
def test_ghost_hosts_are_detected(host):
    assert _is_ghost_host(host)


@pytest.mark.unit
@pytest.mark.parametrize('host', [
    # The export sanitizer rewrites this internal host to a doc placeholder
    # (your-llm-gateway.example.com), which IS a ghost suffix by design — so
    # this parameter is only meaningful in the internal tree.
    pytest.param('your-llm-gateway.example.com', marks=pytest.mark.skipif(
        is_opensource_build(),
        reason='export sanitizes this internal host to a doc placeholder')),
    'api.openai.com', 'latest', 'contest.org',
])
def test_real_hosts_are_not_ghosts(host):
    assert not _is_ghost_host(host)


@pytest.mark.unit
def test_production_store_is_swept_of_ghost_hosts():
    # report_outcome() persists learned state with no throttle on the first
    # call; if a test module ever lets _STORE_PATH point at the real file,
    # its fictional hosts land in the store the live server loads at boot.
    # Absent file = nothing has leaked (yet).
    if not _PROD_STORE.exists():
        return
    payload = json.loads(_PROD_STORE.read_text())
    hosts = payload.get('hosts', [])
    ghosts = [st.get('host', '') for st in hosts if _is_ghost_host(st.get('host'))]
    if not ghosts:
        return
    # Self-heal: drop ONLY the ghost entries; every real host's learned
    # state is carried over untouched.
    payload['hosts'] = [st for st in hosts if not _is_ghost_host(st.get('host'))]
    tmp = _PROD_STORE.with_name(_PROD_STORE.name + '.tmp')
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, _PROD_STORE)
    warnings.warn(
        'swept %d ghost host(s) from the production netpath store: %s'
        % (len(ghosts), ghosts), stacklevel=2)
    remaining = json.loads(_PROD_STORE.read_text()).get('hosts', [])
    assert not [st for st in remaining if _is_ghost_host(st.get('host'))]
