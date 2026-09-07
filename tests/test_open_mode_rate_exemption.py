#!/usr/bin/env python3
"""Open-mode per-IP throttle: ambient poll/status reads are exempt.

Owner incident 2026-08-14 (JOURNAL「429 另立票据」): behind the VS Code
proxy every UI client shares ONE IP, and the UI's own ambient polling —
``/api/v1/browser/status`` + ``/api/v1/desktop/status`` every 3s, the
TaskRuntime poll seam at 1.2–2.5s — consumed the default 120/min budget,
so two normal filter clicks in the knowledge panel already 429'd.

The cap exists to stop a remote IP hammering EXPENSIVE surfaces
(chat/agent/search/generate). Cheap status/poll reads are therefore
exempt from the open-mode bucket; everything else stays capped, and the
exemption is path-scoped (a sibling mutation path under the same prefix
is still counted — the NC test bites that).

Bare-CI-safe: in-process objects + a Quart test request context only.
"""
import asyncio
import importlib
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
    """Counts record_and_check calls; always allows (limit enforced by
    the real store — here we only assert whether the bucket was CONSULTED)."""

    def __init__(self):
        self.calls = []

    def record_and_check(self, endpoint, ip, limit, per_seconds):
        self.calls.append((endpoint, ip, limit, per_seconds))
        return True, 1


@pytest.fixture()
def rl():
    os.environ['TOFU_RATE_LIMIT_BACKEND'] = 'memory'
    os.environ['TOFU_OPEN_MODE_RPM'] = '3'
    import lib.rate_limit_api as mod
    mod = importlib.reload(mod)
    yield mod
    os.environ.pop('TOFU_OPEN_MODE_RPM', None)
    os.environ.pop('TOFU_RATE_LIMIT_BACKEND', None)
    os.environ.pop('TOFU_OPEN_MODE_EXEMPT_PATHS', None)
    import lib.rate_limit_store as store
    store.reset_for_test()
    importlib.reload(mod)


def _with_spy(mod):
    spy = _SpyStore()
    import lib.rate_limit_store as store
    orig = store.get_store
    store.get_store = lambda: spy
    return spy, orig, store


def test_ambient_status_polls_never_touch_the_bucket(rl):
    """browser/desktop status probes (3s cadence) must not consume budget."""
    spy, orig, store = _with_spy(rl)
    try:
        for path in ('/api/v1/browser/status', '/api/v1/desktop/status',
                     '/api/v1/dispatch/model-health'):
            allowed = [_in_ctx(path, lambda: rl.check_open_mode_request().allowed)
                       for _ in range(10)]
            assert all(allowed), f'{path} was throttled/bucketed'
        assert spy.calls == [], 'ambient status probes consumed open-mode budget'
    finally:
        store.get_store = orig


def test_task_poll_seam_never_touches_the_bucket(rl):
    """The canonical poll shapes (GET <prefix>/poll/<id>, flat …/poll) are
    cheap cursor reads the UI drives at 1.2–2.5s while a job runs."""
    spy, orig, store = _with_spy(rl)
    try:
        for path in ('/api/v1/tasks/t123/poll',
                     '/api/v1/paper/qa/poll',
                     '/api/v1/paper/report/poll/t9',
                     '/api/paper/podcast/poll'):
            allowed = [_in_ctx(path, lambda: rl.check_open_mode_request().allowed)
                       for _ in range(10)]
            assert all(allowed), f'{path} was throttled/bucketed'
        assert spy.calls == [], 'task poll reads consumed open-mode budget'
    finally:
        store.get_store = orig


def test_expensive_api_paths_still_counted(rl):
    """The cap itself must survive the exemption: real API surface (chat)
    keeps consuming the bucket and still throttles at the configured rpm."""
    import lib.rate_limit_store as store
    store.reset_for_test()
    allowed = [_in_ctx('/api/v1/chat/completions',
                       lambda: rl.check_open_mode_request().allowed)
               for _ in range(5)]
    assert allowed[:3] == [True, True, True]
    assert allowed[3] is False and allowed[4] is False, (
        'expensive path escaped the open-mode cap — exemption over-broad')


def test_NC_exemption_is_path_scoped_not_prefix_broad(rl):
    """NEUTER-grade guard: only the status/poll READ paths are exempt. A
    sibling path under the same prefix (browser command surface) must stay
    counted — an over-broad prefix match would silently un-cap it."""
    spy, orig, store = _with_spy(rl)
    try:
        _in_ctx('/api/v1/browser/commands', lambda: rl.check_open_mode_request())
        _in_ctx('/api/v1/desktop/restart', lambda: rl.check_open_mode_request())
        assert len(spy.calls) == 2, (
            'sibling mutation paths leaked into the exemption')
    finally:
        store.get_store = orig


def test_env_extends_the_exemption(rl):
    """TOFU_OPEN_MODE_EXEMPT_PATHS (comma-separated substrings) lets an
    operator exempt site-specific panel polls without a redeploy."""
    os.environ['TOFU_OPEN_MODE_EXEMPT_PATHS'] = (
        '/api/v1/optimizer/status, /api/v1/custompanel/feed')
    mod = importlib.reload(rl)
    spy, orig, store = _with_spy(mod)
    try:
        a = _in_ctx('/api/v1/optimizer/status',
                    lambda: mod.check_open_mode_request().allowed)
        b = _in_ctx('/api/v1/custompanel/feed',
                    lambda: mod.check_open_mode_request().allowed)
        assert a and b
        assert spy.calls == [], 'env-listed paths were not exempted'
    finally:
        store.get_store = orig


def test_no_request_context_still_counts(rl):
    """Contract guard for the existing Epic-A backpressure suite: called
    WITHOUT a request context (explicit client_ip), nothing is exempt and
    the shared store is consulted — the pre-exemption behavior."""
    spy, orig, store = _with_spy(rl)
    try:
        assert rl.check_open_mode_request(client_ip='203.0.113.9').allowed
        assert len(spy.calls) == 1
    finally:
        store.get_store = orig


def test_wiring_exemption_runs_before_the_store_and_keeps_the_seam():
    """Source-level pin (mirrors test_wiring_check_request_routes_open_mode_
    to_ip_throttle): the exemption consult must live INSIDE
    check_open_mode_request, BEFORE the shared-store call, and the function
    must keep delegating to lib.rate_limit_store (replica correctness)."""
    import ast
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'lib', 'rate_limit_api.py'),
        encoding='utf-8').read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == 'check_open_mode_request')
    body = ast.get_source_segment(src, fn)
    assert 'record_and_check' in body, 'shared-store seam must stay'
    assert '_open_mode_path_exempt(' in body, (
        'exemption must be consulted inside check_open_mode_request')
    assert body.index('_open_mode_path_exempt(') < body.index('record_and_check'), (
        'exempt reads must short-circuit BEFORE the shared-store call')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
