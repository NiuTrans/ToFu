"""Browser bridge poll: only typed client faults answer 400.

A builtin raised while recording the poll is a server fault and reaches the
redacted 500 boundary; a malformed extension-reported quota is the caller's
fault and keeps its 400.
"""

from __future__ import annotations

import pytest

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]

_SECRET = 'registry row is corrupt at /srv/private/state.db'
_CLIENT = 'taxonomy-browser-client'
_POLL = {'clientId': _CLIENT, 'protocolVersion': 2, 'capabilities': [], 'results': []}


def _bridge_headers() -> dict:
    from lib.api_keys import create_key

    _row, token = create_key(
        owner_user_id=41, name='poll-error-taxonomy', scopes=['agents:bridge'])
    return {'X-Bridge-Secret': token}


@pytest.fixture
def forget_client():
    yield
    from lib.browser.queue import _registry
    with _registry._clients_lock:
        _registry._clients.pop(_CLIENT, None)


def test_poll_registry_fault_is_a_redacted_500(flask_client, monkeypatch, forget_client):
    import lib.browser.queue as queue

    def broken(*args, **kwargs):
        raise TypeError(_SECRET)
    monkeypatch.setattr(queue, 'mark_poll', broken)
    response = flask_client.post('/api/browser/poll', json=_POLL, headers=_bridge_headers())
    assert response.status_code == 500
    assert _SECRET not in response.get_data(as_text=True)


def test_poll_malformed_quota_keeps_its_400(flask_client, forget_client):
    response = flask_client.post(
        '/api/browser/poll', json={**_POLL, 'creditHubQuota': {'total': 'many'}},
        headers=_bridge_headers())
    assert response.status_code == 400
    assert 'creditHubQuota counts must be integers' in response.get_data(as_text=True)
