"""The desktop status poll never blocks the serving event loop.

LoopWatch caught ``GET /api/v4/desktop/status`` stalling the whole server
for 5.2 s and 5.8 s (2026-10-27 15:20:46 and 17:11:58): the async handler
walked the installer store (``makedirs``/``isfile`` on the network
filesystem) and queried bridge credentials on the loop thread itself.
"""

from __future__ import annotations

pytest_plugins = ('tests._credential_sidecar',)

import asyncio

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('private')]

OWNER_USER_ID = 17


def _bearer():
    from lib.api_keys import create_key
    _row, token = create_key(
        owner_user_id=OWNER_USER_ID, name='test-status-loop', scopes=['chat'])
    return {'Authorization': f'Bearer {token}'}


def _on_event_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def test_status_reads_store_and_credentials_off_the_serving_loop(
        flask_client, monkeypatch):
    import lib.api_keys as api_keys
    import routes.api_v1.desktop as desktop

    headers = _bearer()
    calls = []
    original_list_keys = api_keys.list_keys

    def find_for_platform(*_args, **_kwargs):
        calls.append(('find_for_platform', _on_event_loop_thread()))
        return []

    def list_keys(**kwargs):
        calls.append(('list_keys', _on_event_loop_thread()))
        return original_list_keys(**kwargs)

    monkeypatch.setattr(desktop._dist_store, 'find_for_platform',
                        find_for_platform)
    monkeypatch.setattr(desktop._dist_mirror, 'ensure_fresh', lambda: False)
    monkeypatch.setattr(api_keys, 'list_keys', list_keys)

    response = flask_client.get(
        '/api/v4/desktop/status',
        headers={**headers,
                 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})

    assert response.status_code == 200
    assert {name for name, _ in calls} == {'find_for_platform', 'list_keys'}
    assert [name for name, on_loop in calls if on_loop] == []
