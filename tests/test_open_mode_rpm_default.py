#!/usr/bin/env python3
"""Open-mode per-IP throttle: the cap auto-arms only for remote-open installs.

Design decision (owner 2026-08-14): an explicit ``TOFU_OPEN_MODE_RPM`` always
wins, but when UNSET the default is exposure-scoped instead of a flat 120 —
loopback-only installs (the open-source out-of-the-box shape) ship UNCAPPED,
because the only IPs the bucket could ever see are the operator's own tabs
and ambient pollers sharing one bucket. This file pins that resolution rule
and the owner-incident regression (200 loopback requests, zero 429s).

Bare-CI-safe: in-process objects + a Quart test request context only.
"""
import asyncio
import os
import sys

import pytest
from quart import Quart

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

_app = Quart(__name__)


def _in_ctx(path, fn):
    """Run ``fn()`` inside a Quart request context pinned to ``path``."""
    async def _t():
        async with _app.test_request_context(path):
            return fn()
    return asyncio.run(_t())


class _SpyStore:
    """Records record_and_check calls; always allows."""

    def __init__(self):
        self.calls = []

    def record_and_check(self, endpoint, ip, limit, per_seconds):
        self.calls.append((endpoint, ip, limit, per_seconds))
        return True, 1


@pytest.fixture()
def spy(monkeypatch):
    s = _SpyStore()
    import lib.rate_limit_store as store
    monkeypatch.setattr(store, 'get_store', lambda: s)
    return s


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv('TOFU_OPEN_MODE_RPM', raising=False)
    monkeypatch.delenv('TOFU_OPEN_MODE_ALLOW_REMOTE', raising=False)


def test_auto_default_off_for_loopback_only(spy):
    """Unset env + no remote opt-in → cap disarmed; even EXPENSIVE paths
    never consult the shared bucket. This is the out-of-the-box default."""
    import lib.rate_limit_api as mod
    assert mod._open_mode_rpm() == 0
    for path in ('/api/v1/chat/completions', '/api/v1/search',
                 '/api/v1/tasks/abc/stream'):
        allowed = _in_ctx(path, lambda: mod.check_open_mode_request().allowed)
        assert allowed, f'{path} throttled on an uncapped install'
    assert spy.calls == [], 'disarmed cap still consulted the store'


def test_auto_arms_when_remote_peers_admitted(monkeypatch, spy):
    """TOFU_OPEN_MODE_ALLOW_REMOTE=1 (no explicit RPM) → cap auto-arms at
    the remote ceiling; ambient poll reads stay exempt (commit daa2231b)."""
    import lib.rate_limit_api as mod
    monkeypatch.setenv('TOFU_OPEN_MODE_ALLOW_REMOTE', '1')
    assert mod._open_mode_rpm() == mod._OPEN_MODE_REMOTE_RPM == 120
    _in_ctx('/api/v1/chat/completions',
            lambda: mod.check_open_mode_request())
    assert len(spy.calls) == 1, 'expensive path must consume the bucket'
    _in_ctx('/api/v1/tasks/t1/poll', lambda: mod.check_open_mode_request())
    assert len(spy.calls) == 1, 'poll read leaked into the armed bucket'


def test_explicit_rpm_wins_both_directions(monkeypatch, spy):
    """An explicit TOFU_OPEN_MODE_RPM overrides the auto rule in BOTH
    directions: arms without remote peers, disarms with them."""
    import lib.rate_limit_api as mod
    monkeypatch.setenv('TOFU_OPEN_MODE_RPM', '300')
    assert mod._open_mode_rpm() == 300
    monkeypatch.setenv('TOFU_OPEN_MODE_ALLOW_REMOTE', '1')
    monkeypatch.setenv('TOFU_OPEN_MODE_RPM', '0')
    assert mod._open_mode_rpm() == 0


def test_invalid_rpm_falls_back_to_auto(monkeypatch, spy):
    """A garbage value logs-and-falls-through to the exposure-scoped auto
    default instead of silently arming 120 on a loopback-only install."""
    import lib.rate_limit_api as mod
    monkeypatch.setenv('TOFU_OPEN_MODE_RPM', 'not-a-number')
    assert mod._open_mode_rpm() == 0
    monkeypatch.setenv('TOFU_OPEN_MODE_ALLOW_REMOTE', 'true')
    assert mod._open_mode_rpm() == 120


def test_owner_incident_regression_loopback_never_429(spy):
    """2026-08-14 owner incident as a regression: on the default install a
    busy UI (multiple tabs, several running tasks) issuing a mixed stream of
    polls and expensive calls NEVER sees a 429 from its own server."""
    import lib.rate_limit_api as mod
    paths = ['/api/v1/tasks/t1', '/api/v1/tasks/t2',
             '/api/v1/project/brain/summary', '/api/v1/chat/completions',
             '/api/v1/conversations/sync-digest']
    results = []
    for i in range(200):
        p = paths[i % len(paths)]
        results.append(_in_ctx(p, lambda: mod.check_open_mode_request().allowed))
    assert all(results), 'default install throttled its own UI'
    assert spy.calls == [], 'default install consulted the anti-hammer store'


def test_remote_flag_parsing_single_source():
    """lib.auth_mode.open_mode_allows_remote is the one parser for the
    opt-in env (the auth gate and the throttle must agree on its truth)."""
    from lib import auth_mode
    for v in ('1', 'true', 'yes', 'on', ' ON '):
        os.environ['TOFU_OPEN_MODE_ALLOW_REMOTE'] = v
        assert auth_mode.open_mode_allows_remote(), f'{v!r} should opt in'
    for v in ('', '0', 'no', 'off', '2', 'random'):
        os.environ['TOFU_OPEN_MODE_ALLOW_REMOTE'] = v
        assert not auth_mode.open_mode_allows_remote(), f'{v!r} must not opt in'
    del os.environ['TOFU_OPEN_MODE_ALLOW_REMOTE']
    assert not auth_mode.open_mode_allows_remote()


def test_wiring_resolution_uses_the_auth_mode_flag():
    """Source-level pin: _open_mode_rpm must derive the auto default from
    lib.auth_mode.open_mode_allows_remote (single source of truth) rather
    than re-reading the env locally (drift = gate/throttle disagreement)."""
    import ast
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'lib', 'rate_limit_api.py'),
        encoding='utf-8').read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == '_open_mode_rpm')
    body = ast.get_source_segment(src, fn)
    assert 'open_mode_allows_remote' in body, (
        '_open_mode_rpm must consult lib.auth_mode.open_mode_allows_remote')
    assert 'TOFU_OPEN_MODE_ALLOW_REMOTE' not in body.split('open_mode_allows_remote')[1], (
        '_open_mode_rpm must not re-read the remote flag directly')
