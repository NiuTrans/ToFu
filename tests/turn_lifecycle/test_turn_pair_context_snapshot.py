"""Every new conversation round records the context its pair was created with.

The browser captures a context snapshot from its live composer. Inputs the
server creates itself (proactive/background-command wakes, peer messages,
Feishu, plan execution) used to persist without one, so their rounds had no
context rail, the background-agent section had no host, and the sticky rail
left the gutter empty beside them (conversation mvk7np4uiwkpv2). The turn
lifecycle derives the snapshot from the accepted config at pair creation.
"""

from __future__ import annotations

import pytest

from lib import turn_lifecycle
from lib.conversation_sync.workspace import _workspace_path
from lib.turn_projection_patch import with_turn_context_snapshot

pytestmark = pytest.mark.unit

_CONFIG = {
    'model': 'claude-opus-5.5',
    'projectPath': '/srv/projects/tofu',
    'searchMode': 'multi',
    'fetchEnabled': True,
    'codeExecEnabled': True,
}
_DERIVED = {
    'blockId': 'turn-context',
    'snapshot': {
        'roots': [{'short': 'projects/tofu', 'path': '/srv/projects/tofu', 'ro': False}],
        'tools': [
            {'label': 'Search ×N', 'tone': 'search'},
            {'label': 'Fetch', 'tone': 'net'},
            {'label': 'Code Exec', 'tone': 'code'},
        ],
        'modes': [],
        'model': 'claude-opus-5.5',
        'depth': '',
    },
}


class _Client:
    def __init__(self):
        self.payloads: list[dict] = []

    def command(self, operation, payload, command_id):
        assert operation == 'turn.create_pair'
        self.payloads.append(payload)
        return {}


def _create(monkeypatch, projection, *, actor='human', config=_CONFIG,
            conversation_id='conv-ctx', input_kind='input'):
    client = _Client()
    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **_: client)
    turn_lifecycle.create_turn_pair(
        conversation_id, command_id='cmd-1', input_projection=projection,
        config=dict(config), user_id=1, input_actor=actor, input_kind=input_kind)
    return client.payloads[0]['input_projection']


def test_server_created_input_records_its_context(monkeypatch):
    stored = _create(monkeypatch, {'content': 'review results',
                                   '_initiator': 'proactive'})
    assert stored['contextSnapshot'] == _DERIVED


def test_browser_snapshot_is_never_replaced(monkeypatch):
    browser = {'blockId': 'turn-context', 'snapshot': {'model': 'browser-model',
                                                       'tools': [], 'roots': []}}
    stored = _create(monkeypatch, {'content': 'hi', 'contextSnapshot': browser})
    assert stored['contextSnapshot'] == browser


@pytest.mark.parametrize('actor,input_kind,initiator', [
    ('human', 'input', None),
    ('virtual_user', 'input', 'brain'),
    ('virtual_user', 'continuation', 'brain'),
    ('virtual_user', 'continuation', 'autopilot'),
    ('virtual_user', 'swarm_autocontinue_input', 'swarm'),
])
def test_agent_started_round_has_human_context_parity(
        monkeypatch, actor, input_kind, initiator):
    human = _create(monkeypatch, {'content': 'continue the request'}, actor='human',
                    input_kind=input_kind)
    projection = {'content': 'continue the request'}
    if initiator:
        projection['_initiator'] = initiator
    stored = _create(monkeypatch, projection, actor=actor, input_kind=input_kind)
    assert stored['contextSnapshot'] == human['contextSnapshot'] == _DERIVED
    if initiator:
        assert stored['origin']['initiator'] == initiator


def test_read_only_workspace_is_recorded_without_changing_its_authority(monkeypatch):
    stored = _create(monkeypatch, {'content': 'inspect', '_initiator': 'brain'},
                     actor='virtual_user',
                     config={**_CONFIG, 'readOnlyPaths': [_CONFIG['projectPath']]})
    assert stored['contextSnapshot']['snapshot']['roots'] == [
        {'short': 'projects/tofu', 'path': '/srv/projects/tofu', 'ro': True}]
    assert stored['origin']['initiator'] == 'brain'


def test_private_managed_workspace_is_not_listed_as_a_root(monkeypatch):
    managed = str(_workspace_path('conv-ctx', 1))
    stored = _create(monkeypatch, {'content': 'wake'},
                     config={**_CONFIG, 'projectPath': managed})
    snapshot = stored['contextSnapshot']['snapshot']
    assert snapshot['roots'] == []
    assert snapshot['model'] == 'claude-opus-5.5'


@pytest.mark.parametrize('projection', [
    {'content': 'x', '_ctx': {'model': 'legacy'}},
    'plain text input',
])
def test_supplied_legacy_context_and_non_mapping_inputs_pass_through(projection):
    assert with_turn_context_snapshot(projection, _CONFIG) is projection


def test_nothing_to_record_adds_no_block():
    projection = {'content': 'x'}
    assert with_turn_context_snapshot(projection, {'searchMode': 'off'}) is projection
