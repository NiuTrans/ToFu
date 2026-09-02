"""Guard: liveness reports an in-memory Sidecar snapshot without storage I/O.

/api/health is the frontend's offline ARBITER (backend_offline_monitor: two
failed probes → red "backend offline" banner). The old implementation ran
``SELECT 1`` inline, so a PG-on-FUSE stall (measured 4–7s Slow queries) pushed
the answer past the frontend's 3–4s probe budget and raised the banner on a
perfectly alive process.

Readiness is a separate endpoint and returns 503 until the production
lifecycle and in-memory Sidecar state are both ready.

Pure-logic: the route function is called under a minimal Quart request
context; storage status is stubbed by monkeypatch. ``unit`` marker.
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def probe_env(monkeypatch):
    """Install the in-memory authority snapshot used by the health route."""
    import quart  # noqa: F401 — installing the same shim the suite relies on
    import lib.storage as storage
    import routes.common as common

    monkeypatch.setattr(storage, 'storage_status', lambda: {
        'ready': True, 'state': 'ready', 'backend': 'postgres',
        'restart_attempts': 0, 'last_error': '',
    })
    return common


def _call_health(common):
    import asyncio
    import quart
    app = quart.Quart('health-test')

    async def _run():
        async with app.test_request_context('/api/health'):
            # health_check() returns (resp, status) since api-contract batch 21
            resp, _status = common.health_check()
            return await resp.get_json()

    return asyncio.run(_run())


def test_request_path_never_touches_db_inline(probe_env, monkeypatch):
    """The liveness path never performs a synchronous storage RPC."""
    common = probe_env

    def _boom(*a, **k):
        raise AssertionError('health request path touched the DB inline')

    import lib.storage as storage
    monkeypatch.setattr(storage, 'get_storage_client', _boom)

    # Warm the ONE-TIME per-process initializations the health route performs
    # on its first call, OUTSIDE the timed region: the cross_dc cluster index
    # and the boot_identity code fingerprint (a `git diff HEAD` subprocess —
    # measured 1.4s on a cold process over FUSE, 0.0s from cache afterwards).
    # The invariant here is "the request path never blocks on storage inline",
    # not "first call in a fresh process is fast"; both warm-ups are
    # config/repo-size-driven, so their one-time cost legitimately differs
    # between deployments.
    import logging
    _warm_log = logging.getLogger(__name__)
    try:
        from lib.cross_dc import get_status as _cdc_status
        _cdc_status()
    except Exception as _w1:  # cross_dc is optional; absence is fine
        _warm_log.debug('cross_dc warm-up skipped: %s', _w1)
    try:
        from lib import boot_identity as _bi
        _bi.code_fingerprint()
    except Exception as _w2:  # fingerprint is best-effort on the route
        _warm_log.debug('code_fingerprint warm-up skipped: %s', _w2)

    t0 = time.monotonic()
    data = _call_health(common)
    elapsed = time.monotonic() - t0
    assert data['ok'] is True
    assert data['storage']['ready'] is True
    assert not any(key.startswith('db_') for key in data)
    assert elapsed < 1.0, f'warm-cache health took {elapsed:.2f}s — blocking on something'


def test_health_contract_has_one_storage_authority(probe_env):
    common = probe_env
    data = _call_health(common)

    assert data['ok'] is True
    assert data['storage']['backend'] == 'postgres'
    assert 'storage_ready' not in data
    assert not any(key.startswith('db_') for key in data)


def test_storage_degradation_is_visible_without_flipping_liveness(
        probe_env, monkeypatch):
    common = probe_env
    import lib.storage as storage
    monkeypatch.setattr(storage, 'storage_status', lambda: {
        'ready': False,
        'state': 'restarting',
        'backend': 'sqlite',
        'restart_attempts': 2,
        'last_error': 'sidecar exited',
    })

    data = _call_health(common)

    assert data['ok'] is True
    assert data['storage']['ready'] is False
    assert data['storage']['state'] == 'restarting'
    assert data['storage']['restart_attempts'] == 2
    assert data['storage']['last_error'] == 'sidecar exited'
    assert not any(key.startswith('db_') for key in data)


def test_ready_requires_completed_lifecycle_and_ready_sidecar(
        probe_env, monkeypatch):
    import asyncio
    import quart
    import lib.storage as storage
    import routes.common as common

    app = quart.Quart('ready-test')
    app.extensions['tofu_production_lifecycle'] = {'status': 'starting'}

    async def call():
        async with app.test_request_context('/api/ready'):
            response, status = common.readiness_check()
            return await response.get_json(), status

    payload, status = asyncio.run(call())
    assert status == 503
    assert payload['ready'] is False

    app.extensions['tofu_production_lifecycle']['status'] = 'ready'
    payload, status = asyncio.run(call())
    assert status == 200
    assert payload['ready'] is True

    monkeypatch.setattr(storage, 'storage_status', lambda: {
        'ready': False, 'state': 'restarting', 'backend': 'postgres',
    })
    payload, status = asyncio.run(call())
    assert status == 503
    assert payload['storage']['state'] == 'restarting'
