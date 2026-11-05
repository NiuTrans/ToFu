"""/api/v4/desktop/device-waits: owners list and cancel their own device waits.

Waits are registered by conversations with await_device
(docs/modules/remote_execution.md); deleting a conversation cancels its waits.
"""
from __future__ import annotations

import uuid

import pytest

from tests._seed import seed_conversation

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open'),
              pytest.mark.usefixtures('chat_sidecar')]
ENDPOINT = '/api/v4/desktop/device-waits'


def _register(owner, conversation, selector='Laptop'):
    from lib.desktop.device_waits import DeviceWaits, device_key, wake_capture
    wait, created = DeviceWaits(owner).register(
        conversation_id=conversation,
        device={'selector': selector, 'agentId': None, 'name': None,
                'key': device_key('sel', selector.casefold())},
        reason='Run the nightly backup on the laptop.',
        wake=wake_capture({'desktopEnabled': True}), source={'taskId': 'task', 'turnId': None})
    assert created
    return wait


def test_owner_lists_and_cancels_only_their_own_waits(flask_client):
    from lib.desktop.device_waits import DeviceWaits
    empty = flask_client.get(ENDPOINT).get_json()
    assert empty['waits'] == [] and empty['nextCursor'] is None
    mine = _register(1, 'conv-' + uuid.uuid4().hex)
    theirs = _register(2, 'conv-' + uuid.uuid4().hex)
    listed = flask_client.get(ENDPOINT)
    assert listed.status_code == 200, listed.get_json()
    [wait] = listed.get_json()['waits']
    assert wait['waitId'] == mine['waitId'] and wait['state'] == 'pending'
    assert wait['device'] == {'selector': 'Laptop', 'agentId': None, 'name': None}
    assert not {'wake', 'source', 'owner'} & set(wait)
    assert flask_client.post(f"{ENDPOINT}/{theirs['waitId']}/cancel").status_code == 404
    cancelled = flask_client.post(f"{ENDPOINT}/{mine['waitId']}/cancel")
    assert cancelled.status_code == 200, cancelled.get_json()
    assert cancelled.get_json()['state'] == 'cancelled'
    assert flask_client.post(f"{ENDPOINT}/{mine['waitId']}/cancel").get_json() == cancelled.get_json()
    assert flask_client.get(ENDPOINT).get_json()['waits'] == []
    closed = flask_client.get(ENDPOINT, query_string={'includeClosed': 'true'}).get_json()['waits']
    assert [(item['waitId'], item['closeReason']) for item in closed] == [
        (mine['waitId'], 'owner_cancelled')]
    assert flask_client.get(ENDPOINT, query_string={'owner': '2'}).status_code == 400
    assert flask_client.get(ENDPOINT, query_string={'includeClosed': 'yes'}).status_code == 400
    assert DeviceWaits(2).read(theirs['waitId'])['state'] == 'pending'


def test_deleting_a_conversation_cancels_its_pending_waits(flask_client):
    from lib.desktop.device_waits import DeviceWaits
    conversation = 'device-wait-' + uuid.uuid4().hex
    seed_conversation(conversation, user_id=1, title='Needs the laptop')
    doomed = _register(1, conversation)
    survivor = _register(1, 'conv-' + uuid.uuid4().hex)
    response = flask_client.delete(f'/api/v4/conversations/{conversation}')
    assert response.status_code == 200, response.get_json()
    cancelled = DeviceWaits(1).read(doomed['waitId'])
    assert (cancelled['state'], cancelled['closeReason']) == ('cancelled', 'conversation_deleted')
    assert DeviceWaits(1).read(survivor['waitId'])['state'] == 'pending'
