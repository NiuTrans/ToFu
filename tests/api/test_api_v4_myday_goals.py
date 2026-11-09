"""/api/v4/myday: the My Day goal journey over the real application and storage.

Create a goal, decompose it, adopt and check off items, reload, regenerate
without duplicates, and map typed failures. The model is the only seam.
"""
from __future__ import annotations

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


@pytest.fixture
def flask_app(chat_sidecar):
    import server
    server.app.config.update(TESTING=True)
    return server.app


@pytest.fixture
def model(monkeypatch):
    answers = []

    def dispatch(messages, owner):
        answers.append(messages[-1]['content'])
        return '{"items": ["Draft the plan", "Review the plan with Ann"]}'
    monkeypatch.setattr('lib.myday.decompose._dispatch', dispatch)
    return answers


def _items(goal):
    return {item['text']: item for item in goal['items']}


def test_goal_journey_survives_reload_and_regeneration(flask_client, model):
    created = flask_client.post('/api/v4/myday/goals', json={'text': 'Plan the offsite'})
    assert created.status_code == 200, created.get_json()
    goal = created.get_json()['goal']
    assert goal['items'] == [] and goal['progress']['percent'] is None
    base = f"/api/v4/myday/goals/{goal['id']}"

    decomposed = flask_client.post(base + '/decompose').get_json()['goal']
    assert [(item['origin'], item['proposal'], item['counted']) for item in decomposed['items']] == [
        ('agent', 'pending', False), ('agent', 'pending', False)]
    assert decomposed['decomposedAt'] and 'Plan the offsite' in model[0]
    owner_item = flask_client.post(base + '/items', json={'text': 'Book the venue'}).get_json()['goal']
    accepted = flask_client.post(base + '/suggestions', json={'decision': 'accept'}).get_json()['goal']
    assert accepted['progress']['counted'] == 3 and accepted['progress']['suggested'] == 0
    venue = _items(owner_item)['Book the venue']['id']
    checked = flask_client.patch(f'{base}/items/{venue}', json={'status': 'completed'})
    assert checked.status_code == 200, checked.get_json()
    assert checked.get_json()['goal']['progress']['percent'] == 33

    reloaded = flask_client.get('/api/v4/myday/goals').get_json()['goals']
    assert [row['id'] for row in reloaded] == [goal['id']]
    shown = _items(reloaded[0])
    assert shown['Book the venue']['status'] == 'completed' and shown['Book the venue']['manual']
    assert reloaded[0]['progress'] == checked.get_json()['goal']['progress']

    again = flask_client.post(base + '/decompose').get_json()['goal']
    assert sorted(item['text'] for item in again['items']) == [
        'Book the venue', 'Draft the plan', 'Review the plan with Ann']
    assert _items(again)['Book the venue'] == shown['Book the venue']
    assert 'Draft the plan (not started)' in model[1], 'the second run sees the adopted items'


def test_typed_failures(flask_client, monkeypatch):
    from lib.myday.decompose import DecompositionError

    assert flask_client.post('/api/v4/myday/goals', json={'text': ''}).status_code == 400
    assert flask_client.post('/api/v4/myday/goals', json={'text': 'x', 'extra': 1}).status_code == 400
    assert flask_client.patch('/api/v4/myday/goals/g-0000000000000000',
                              json={'state': 'archived'}).status_code == 404
    goal = flask_client.post('/api/v4/myday/goals', json={'text': 'Typed'}).get_json()['goal']
    base = f"/api/v4/myday/goals/{goal['id']}"
    assert flask_client.patch(base + '/items/i-0000000000000000', json={'status': 'done'}).status_code == 400
    assert flask_client.patch(base + '/items/i-0000000000000000',
                              json={'status': 'completed'}).status_code == 400

    def failing(messages, owner):
        raise DecompositionError('provider down')
    monkeypatch.setattr('lib.myday.decompose._dispatch', failing)
    failed = flask_client.post(base + '/decompose')
    assert failed.status_code == 502
    listed = {row['id']: row for row in flask_client.get('/api/v4/myday/goals').get_json()['goals']}
    assert listed[goal['id']]['items'] == [] and listed[goal['id']]['decomposedAt'] is None
    archived = flask_client.patch(base, json={'state': 'archived'}).get_json()['goal']
    assert archived['state'] == 'archived'
    requests = flask_client.get('/api/v4/myday/requests')
    assert requests.status_code == 200 and requests.get_json()['requests'] == []
