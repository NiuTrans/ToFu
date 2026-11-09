"""route_to_project forwards the owner's exact words through the ordinary turn command (R2)."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from lib.router import forwarding
from lib.router.forwarding import forward_command_id, route_to_project
from lib.router.home import router_home_location
from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch

pytestmark = pytest.mark.unit
OWNER = 7
TEXT = '把 parser 的空指针修掉，\n并补一个回归测试。  '


class Bindings:
    def __init__(self):
        self.bound = None
        self.provisioned = []

    def read(self, path):
        if self.bound is None:
            return {'projectPath': path, 'conversationId': '', 'revision': 0, 'availability': 'unbound'}
        return dict(self.bound)

    def provision(self, path, *, title, settings, command_id):
        self.provisioned.append({'path': path, 'title': title, 'settings': settings,
                                 'command_id': command_id})
        self.bound = {'projectPath': path, 'conversationId': 'coordinator-project', 'revision': 1,
                      'availability': 'available'}
        return dict(self.bound)


@pytest.fixture
def r(tmp_path, monkeypatch):
    from lib.conversations import project_coordination
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    project = tmp_path / 'project'
    project.mkdir()
    turns = {'human-1': {'conversationId': 'router', 'turnId': 'human-1', 'actor': 'human',
                         'projectionRevision': 2, 'projection': {'content': TEXT}}}

    def router_turn(owner, conversation, turn_id):
        assert owner == OWNER and conversation == 'router'
        if turn_id not in turns:
            raise ValueError('Turn does not exist')
        return deepcopy(turns[turn_id])

    state = type('State', (), {})()
    state.source = {'conversationId': 'router', 'turnId': 'human-1', 'projectionRevision': 2}

    def current_source(task, scope):
        if isinstance(state.source, Exception):
            raise state.source
        return dict(state.source)
    monkeypatch.setattr(forwarding, '_router_turn', router_turn)
    monkeypatch.setattr(forwarding, '_current_request_source', current_source)
    bindings = Bindings()
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository', lambda owner: bindings)
    calls, dispositions = [], []

    def dispatch(conversation, message, tools, **kwargs):
        calls.append({'conversation': conversation, 'message': deepcopy(message), 'tools': tools, **kwargs})
        return ScheduledTurnDispatch(dispositions.pop(0) if dispositions else 'started', 'task-1')
    state.project, state.turns, state.bindings = str(project), turns, bindings
    state.calls, state.dispositions = calls, dispositions
    state.scope = {'user_id': OWNER, 'project_path': router_home_location(OWNER),
                   'conversation_id': 'router', 'binding_revision': 1}
    state.task = {'id': 'router-task', 'convId': 'router', '_userId': OWNER,
                  'config': {'codeExecEnabled': True, 'searchMode': 'single', 'model': 'router-model'}}
    state.route = lambda **args: route_to_project(state.task, state.scope, args, dispatch=dispatch)
    return state


def test_forwards_the_owner_text_byte_identical_with_its_provenance(r):
    result = r.route(project_path=r.project, source_turn_id='human-1')
    [call] = r.calls
    assert call['conversation'] == 'coordinator-project'
    assert call['message'] == {'role': 'user', 'content': TEXT, 'origin': {
        'initiator': 'human', 'forwardedFrom': {'conversationId': 'router', 'turnId': 'human-1'}}}
    assert call['input_actor'] == 'human' and call['inject_mode'] == 'steer' and call['user_id'] == OWNER
    assert call['command_id'] == forward_command_id('router', {'turnId': 'human-1'}, r.project)
    assert result['disposition'] == 'started' and result['project_router_created'] is True
    assert result['context_disposition'] is None
    [provisioned] = r.bindings.provisioned
    assert provisioned['settings'] == {'codeExecEnabled': True, 'searchMode': 'single'}


def test_replay_reuses_the_command_and_the_existing_project_router(r):
    first = r.route(project_path=r.project + '/', source_turn_id='human-1')
    second = r.route(project_path=r.project, source_turn_id='human-1')
    assert first['command_id'] == second['command_id'] == r.calls[0]['command_id'] == r.calls[1]['command_id']
    assert len(r.bindings.provisioned) == 1 and second['project_router_created'] is False
    other = r.route(project_path=r.project, source_turn_id='human-1', note='context')
    assert other['command_id'] == first['command_id']


@pytest.mark.parametrize('forward', ['started', 'steered', 'queued'])
def test_note_is_nonhuman_context_of_the_same_request_never_the_owner_text(r, forward):
    """The note is evidence inside the one authorized request, not a second input.

    A second input became its own synthetic Coordinator request when the steer
    window was closed; the note therefore rides the forwarded input, while the
    owner's verbatim words (``originalContent``) stay the compared authority.
    """
    r.dispositions[:] = [forward]
    result = r.route(project_path=r.project, source_turn_id='human-1', note='The owner prefers small PRs.')
    [call] = r.calls
    assert call['message']['originalContent'] == TEXT
    assert call['message']['content'].startswith(TEXT)
    assert 'nonhuman' in call['message']['content'] and 'small PRs' in call['message']['content']
    assert '_initiator' not in call['message']
    assert call['message']['origin'] == {'initiator': 'human', 'forwardedFrom': {
        'conversationId': 'router', 'turnId': 'human-1'}}
    assert result['disposition'] == forward and result['context_disposition'] is None


@pytest.mark.parametrize('case', ['other-turn', 'synthetic-request'])
def test_only_the_owner_message_of_the_current_request_can_be_forwarded(r, case):
    if case == 'synthetic-request':
        r.source = PermissionError('Synthetic input cannot authorize new workers')
        with pytest.raises(PermissionError, match='Synthetic'):
            r.route(project_path=r.project, source_turn_id='human-1')
    else:
        r.turns['human-0'] = {**r.turns['human-1'], 'turnId': 'human-0'}
        with pytest.raises(PermissionError, match='current request'):
            r.route(project_path=r.project, source_turn_id='human-0')
    assert not r.calls and not r.bindings.provisioned


def test_target_must_be_an_existing_project_other_than_the_home(r, tmp_path):
    for path in (str(tmp_path / 'missing'), 'relative/project', r.scope['project_path']):
        with pytest.raises(ValueError):
            r.route(project_path=path, source_turn_id='human-1')
    with pytest.raises(ValueError, match='source_turn_id or source_steer_id is required'):
        r.route(project_path=r.project)
    with pytest.raises(ValueError, match='standing instruction'):
        r.route(project_path=r.project, source_turn_id='human-1', event_id='dx:1')
    assert not r.calls


@pytest.mark.parametrize('disposition, started', [('start_failed', None), ('target_missing', False)])
def test_an_unaccepted_forward_is_reported_not_hidden(r, disposition, started):
    r.dispositions[:] = [disposition]
    with pytest.raises(ValueError) as caught:
        r.route(project_path=r.project, source_turn_id='human-1')
    assert caught.value.execution_started is started


class _Outcome:
    def __init__(self, value):
        self.value = value


@pytest.mark.parametrize('lane, expected', [('busy', 'steered'), ('idle', 'started')])
def test_ordinary_turn_command_steers_a_busy_target_and_starts_an_idle_one(r, monkeypatch, lane, expected):
    from lib.scheduler import conversation_dispatch
    payloads = []

    class Commands:
        def create_turn(self, conversation, owner, payload, *, request_started_at):
            payloads.append((conversation, owner, deepcopy(payload)))
            return _Outcome({'steered': True, 'blockId': 'block'} if lane == 'busy'
                            else {'attempt': {'taskId': 'project-task'}})

    class Storage:
        def query(self, operation, payload):
            assert operation == 'conversation.get' and payload['user_id'] == OWNER
            return {'metadata': {'settings': {'projectPath': r.project}}}
    monkeypatch.setattr(conversation_dispatch, 'conversation_turn_commands', Commands())
    monkeypatch.setattr(conversation_dispatch, 'get_storage_client', lambda: Storage())
    result = route_to_project(r.task, r.scope, {'project_path': r.project, 'source_turn_id': 'human-1'})
    [(conversation, owner, payload)] = payloads
    assert (conversation, owner) == ('coordinator-project', OWNER)
    assert payload['injectMode'] == 'steer' and payload['inputActor'] == 'human'
    assert payload['commandId'] == forward_command_id('router', {'turnId': 'human-1'}, r.project)
    assert payload['inputTurn']['content'] == TEXT and '_initiator' not in payload['inputTurn']
    assert payload['inputTurn']['origin']['forwardedFrom'] == {'conversationId': 'router',
                                                              'turnId': 'human-1'}
    assert result['disposition'] == expected
