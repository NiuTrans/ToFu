"""tests/test_chat_routes_scope_gated.py — Scope-gating invariant for chat.

Every UI-facing ``/api/v1/chat/*`` handler MUST carry ``@require_scope('chat')``
(cookie / tunnel / open-mode callers satisfy it transparently via
``AuthContext.has_scope``; only headless ``tofu_live_*`` keys are constrained).

These handlers were migrated from the legacy single-user surface via the
blueprint-alias pattern and originally relied ONLY on the global auth gate,
so a headless key without the ``chat`` scope could drive the full chat
pipeline. 2026-06-01 gated all of them. This test locks that in.

It also asserts the historical duplicate-registration bug stays fixed:
``POST /api/v1/chat/abort/<task_id>`` must resolve to exactly ONE view
(the rich handler in ``routes/chat.py``), not the deleted stub.
"""

import os

import pytest

# Build the real app the way server.py does (Quart shim already applied
# inside server import). The lightweight _AppFixture in
# test_api_v1_integration.py instantiates ``Quart(__name__)`` directly,
# which trips a Quart/Flask version mismatch in some environments; going
# through ``server`` sidesteps that.
os.environ.setdefault('TUNNEL_TOKEN', 'test-tunnel-not-real')

try:
    import server  # noqa: E402
    _APP = server.app
except Exception as exc:  # pragma: no cover - import-time env failure
    _APP = None
    _IMPORT_ERR = exc


pytestmark = pytest.mark.skipif(_APP is None, reason='server app failed to import')


def _chat_rules():
    return [r for r in _APP.url_map.iter_rules()
            if r.rule.startswith('/api/v1/chat/')]


def test_every_v1_chat_handler_requires_chat_scope():
    ungated = []
    for rule in _chat_rules():
        view = _APP.view_functions[rule.endpoint]
        scopes = getattr(view, '_required_scopes', None)
        if not scopes or 'chat' not in scopes:
            ungated.append((rule.rule, rule.endpoint, scopes))
    assert not ungated, (
        'These /api/v1/chat/* handlers are missing @require_scope(\'chat\'): '
        + ', '.join(f'{r} ({ep}, scopes={sc})' for r, ep, sc in ungated))


def test_abort_route_registered_exactly_once():
    abort_rules = [r for r in _chat_rules()
                   if r.rule == '/api/v1/chat/abort/<task_id>']
    assert len(abort_rules) == 1, (
        f'Expected exactly one abort route, found {len(abort_rules)}: '
        f'{[r.endpoint for r in abort_rules]}')
    # The surviving handler must be the rich one in routes/chat.py
    # (subprocess + external-backend kill), not the deleted stub.
    view = _APP.view_functions[abort_rules[0].endpoint]
    assert view.__module__ == 'routes.chat', (
        f'abort handler resolved to {view.__module__}, expected routes.chat')
