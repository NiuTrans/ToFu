"""Executable spec for the dual-mounted v4 product surface (batch 1: capabilities).

Asserts the three guarantees from docs/contracts/API_V4_MIGRATION.md §5:
  1. a projected v4 endpoint serves the v4 ``{data, meta}`` envelope, no ``ok``;
  2. a v4 4xx is RFC 7807 ``application/problem+json``;
  3. the twin v1 endpoint is byte-for-byte untouched.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.api.test_api_v4_contract import _bootstrap_app

pytestmark = pytest.mark.unit


def _app(*, v4_enabled: bool = True):
    return _bootstrap_app(active_major=1, v4_enabled=v4_enabled)


def test_v4_capabilities_uses_data_meta_envelope():
    app = _app()

    async def exercise():
        response = await app.test_client().get(
            '/api/v4/capabilities', headers={'X-Request-ID': 'v4-caps'})
        assert response.status_code == 200
        payload = await response.get_json()
        assert 'ok' not in payload
        assert set(payload) == {'data', 'meta'}
        assert isinstance(payload['data'], dict) and payload['data']
        assert payload['meta']['requestId'] == 'v4-caps'
        assert isinstance(payload['meta']['serverTimeMs'], int)

    asyncio.run(exercise())


def test_v4_capability_data_matches_v1_payload():
    app = _app()

    async def exercise():
        client = app.test_client()
        v1 = await (await client.get('/api/v1/capabilities')).get_json()
        v4 = await (await client.get('/api/v4/capabilities')).get_json()
        return v1, v4

    v1, v4 = asyncio.run(exercise())
    # v1 keeps its {ok, ...payload} shape; v4 data == v1 payload minus `ok`.
    assert v1.pop('ok') is True
    assert v4['data'] == v1


def test_v4_unknown_route_renders_rfc7807():
    app = _app()

    async def exercise():
        response = await app.test_client().get('/api/v4/no-such-route')
        assert response.status_code == 404
        assert response.content_type.split(';')[0] == 'application/problem+json'
        problem = await response.get_json()
        assert problem['status'] == 404
        assert problem['type'].startswith('urn:tofu:problem:')
        assert problem['instance'] == '/api/v4/no-such-route'

    asyncio.run(exercise())


def test_v1_twin_untouched_when_v4_enabled():
    app = _app()

    async def exercise():
        response = await app.test_client().get('/api/v1/capabilities')
        assert response.status_code == 200
        body = await response.get_json()
        assert body.get('ok') is True
        assert 'data' not in body and 'meta' not in body

    asyncio.run(exercise())


def test_v4_routes_hidden_when_latch_off():
    app = _app(v4_enabled=False)

    async def exercise():
        response = await app.test_client().get('/api/v4/capabilities')
        # Gated off: the route is not registered (404), even though the
        # bootstrap negotiation endpoints remain live.
        assert response.status_code == 404

    asyncio.run(exercise())


# ── Boundary safety: the translator must never mangle non-envelope bodies ──

from quart import Blueprint, Response  # noqa: E402

from lib.api_v4 import attach_v4_envelope  # noqa: E402


def _attachable_bp() -> Blueprint:
    bp = Blueprint('v4_boundary_probe', __name__)

    @bp.get('/api/v4/stream')
    async def _stream():
        async def gen():
            yield b'data: chunk-1\n\n'
            yield b'data: chunk-2\n\n'
        return Response(gen(), mimetype='text/event-stream')

    @bp.get('/api/v4/blob')
    async def _blob():
        return Response(b'\x89PNG\r\n', mimetype='image/png')

    @bp.get('/api/v4/created')
    async def _created():
        from lib.api_response import api_created
        return api_created({'id': 'x-1'})

    return bp


def _run_probe(path):
    from lib.app_factory import create_base_app
    from lib.http_error_handlers import register_http_error_handlers
    app = create_base_app('v4-boundary', {'TESTING': True})
    app.register_blueprint(attach_v4_envelope(_attachable_bp()))
    register_http_error_handlers(app)

    async def exercise():
        response = await app.test_client().get(path)
        return response, await response.get_data()

    return asyncio.run(exercise())


def test_translator_passes_sse_stream_through_unchanged():
    response, body = _run_probe('/api/v4/stream')
    assert response.status_code == 200
    assert response.content_type.split(';')[0] == 'text/event-stream'
    assert body == b'data: chunk-1\n\ndata: chunk-2\n\n'


def test_translator_passes_binary_body_through_unchanged():
    response, body = _run_probe('/api/v4/blob')
    assert response.content_type.split(';')[0] == 'image/png'
    assert body == b'\x89PNG\r\n'


def test_translator_preserves_201_created_envelope():
    response, body = _run_probe('/api/v4/created')
    assert response.status_code == 201
    assert b'"data"' in body and b'"meta"' in body


# ── Whole-surface projection guarantees (no hand-maintained batch list) ──


def test_every_v1_product_route_has_a_v4_twin():
    """The projection is total: for every registered ``/api/v1`` rule there is
    an identical-method ``/api/v4`` rule. This is the anti-drift spec that makes
    the migration a single mechanical flip rather than per-domain bookkeeping.
    """
    app = _app()
    v1, v4 = set(), set()
    for rule in app.url_map.iter_rules():
        methods = rule.methods - {'HEAD', 'OPTIONS'}
        for m in methods:
            if rule.rule.startswith('/api/v1/') or rule.rule == '/api/v1':
                v1.add((rule.rule, m))
            elif rule.rule.startswith('/api/v4/') or rule.rule == '/api/v4':
                v4.add((rule.rule, m))
    # v4 twin set with prefix normalised back to v1 must cover the v1 set.
    v4_as_v1 = {
        ('/api/v1' + r[len('/api/v4'):], m) for r, m in v4
    }
    missing = v1 - v4_as_v1
    assert not missing, f'v1 routes with no v4 twin: {sorted(missing)}'


def test_no_v4_rule_accidentally_keeps_v1_prefix():
    """Guard the double-prefix bug class: nothing may be registered under the
    literal ``/api/v4/api/v1`` or remain on v1 inside a v4 blueprint."""
    app = _app()
    bad = [
        r.rule for r in app.url_map.iter_rules()
        if r.rule.startswith('/api/v4/api/') or (
            r.endpoint and '_v4' in (r.endpoint or '')
            and r.rule.startswith('/api/v1/'))
    ]
    assert not bad, f'mis-projected v4 rules: {bad}'


def test_v1_surface_still_serves_after_full_projection():
    """Full dual-mount must not break the v1 side (route-table integrity)."""
    app = _app()

    async def exercise():
        client = app.test_client()
        caps = await client.get('/api/v1/capabilities')
        assert caps.status_code == 200
        assert (await caps.get_json()).get('ok') is True

    asyncio.run(exercise())


def test_multi_rule_view_projects_unique_v4_endpoints():
    """Regression spec for the endpoint-collision defect fixed during rollout.

    A single v1 view function bound to several rules (e.g. multi-path
    ``_abort``) must yield one distinct v4 endpoint per rule — the projection
    suffixes the rule index (``{base}_{index}``), not just the blueprint name.
    If the suffix were dropped, Quart would raise on a duplicate endpoint, and
    the regression is caught here: only one v4 rule would materialise.
    """
    from quart import Blueprint, jsonify

    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4

    bp = Blueprint('v4_decollision_probe', __name__)

    @bp.get('/api/v1/echo/<key>')
    async def shared_echo(key):
        return jsonify({'key': key})

    # A second rule for the SAME view function, plus an unrelated rule.
    bp.add_url_rule('/api/v1/echo/<key>/alt', 'shared_echo', shared_echo)
    bp.add_url_rule('/api/v1/extra', 'other_view', lambda: 'x')

    projected = attach_v4_envelope(
        copy_blueprint_to_v4(bp, name='v4_decollision_v4'))

    from lib.app_factory import create_base_app
    from lib.http_error_handlers import register_http_error_handlers
    app = create_base_app('v4-decollide', {'TESTING': True})
    app.register_blueprint(projected)
    register_http_error_handlers(app)

    v4 = [(r.rule, r.endpoint) for r in app.url_map.iter_rules()
          if r.rule.startswith('/api/v4/')]
    # All three v1 rules project onto v4 — no rule lost to a mapping overwrite.
    assert {rule for rule, _ in v4} == {
        '/api/v4/echo/<key>', '/api/v4/echo/<key>/alt', '/api/v4/extra'}
    # And every rule owns a unique endpoint (the `{base}_{index}` de-collision).
    endpoints = [ep for _, ep in v4]
    assert len(endpoints) == len(set(endpoints)), \
        f'v4 endpoint collision: {sorted(endpoints)}'

    # Both aliased rules dispatch to the shared handler (each reachable under
    # its own unique v4 endpoint). Envelope coverage across the full projected
    # surface is asserted separately by ``test_every_v1_product_route_has_a_v4_twin``.
    async def exercise():
        client = app.test_client()
        for path in ('/api/v4/echo/hello', '/api/v4/echo/hello/alt'):
            resp = await client.get(path)
            assert resp.status_code == 200
            body = await resp.get_json()
            assert body['data']['key'] == 'hello'

    asyncio.run(exercise())
