"""Standing instructions freeze owner text and apply it verbatim later (R4)."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lib.router import forwarding
from lib.router.home import router_home_location
from lib.router.instructions import RouterInstructions
from lib.router.tools import execute_router_tool
from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit
OWNER = 7
INSTRUCTION = ('在“Tofu 用户群”里有人报 bug 时，在 /work/project 复现；只有确认修好了才告诉我。')


class Records:
    """Portable record CAS semantics: get, create-only/versioned put, prefix pages."""

    def __init__(self):
        self.rows = {}

    def __call__(self, **kwargs):
        return self

    def query(self, operation, payload, **kwargs):
        if operation == 'record.get':
            row = self.rows.get((payload['namespace'], payload['key']))
            return deepcopy({'key': payload['key'], **row}) if row else None
        assert operation == 'record.list'
        keys = sorted(key for namespace, key in self.rows if namespace == payload['namespace']
                      and key.startswith(payload['prefix']) and key > payload.get('after_key', ''))
        return [deepcopy({'key': key, **self.rows[(payload['namespace'], key)]})
                for key in keys[:payload['limit']]]

    def command(self, operation, payload, command_id):
        assert operation == 'record.put'
        identity = (payload['namespace'], payload['key'])
        current = self.rows.get(identity)
        if (current['version'] if current else 0) != payload['expected_version']:
            raise StorageError('database_conflict', 'CAS conflict')
        self.rows[identity] = {'value': deepcopy(payload['value']),
                               'version': payload['expected_version'] + 1}
        return {'version': payload['expected_version'] + 1}


@pytest.fixture
def r(tmp_path, monkeypatch):
    records = Records()
    monkeypatch.setattr('lib.storage.get_storage_client', records)
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    project = tmp_path / 'project'
    project.mkdir()
    turns = {
        'owner-turn': {'conversationId': 'router', 'turnId': 'owner-turn', 'actor': 'human',
                       'projectionRevision': 3,
                       'projection': {'content': 'translated text', 'originalContent': INSTRUCTION}},
        'event-turn': {'conversationId': 'router', 'turnId': 'event-turn', 'actor': 'human',
                       'projectionRevision': 1, 'projection': {'content': 'bug report', '_initiator': 'peer'}},
    }
    state = SimpleNamespace(records=records, turns=turns, project=str(project), calls=[],
                            source={'conversationId': 'router', 'turnId': 'owner-turn'})

    def router_turn(owner, conversation, turn_id):
        if turn_id not in turns:
            raise ValueError('Turn does not exist')
        return deepcopy(turns[turn_id])

    def current_source(task, scope):
        if isinstance(state.source, Exception):
            raise state.source
        return dict(state.source)

    class Bindings:
        def read(self, path):
            if path in state.unbound:
                return {'projectPath': path, 'conversationId': '', 'revision': 0, 'availability': 'unbound'}
            return {'projectPath': path, 'conversationId': 'coordinator-project', 'revision': 1,
                    'availability': 'available'}

        def provision(self, path, **kwargs):
            state.unbound.discard(path)
            return self.read(path)
    state.unbound = set()
    from lib.conversations import project_coordination
    monkeypatch.setattr(forwarding, '_router_turn', router_turn)
    monkeypatch.setattr(forwarding, '_current_request_source', current_source)
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository', lambda owner: Bindings())

    def dispatch(conversation, message, tools, **kwargs):
        state.calls.append({'conversation': conversation, 'message': deepcopy(message), **kwargs})
        return ScheduledTurnDispatch('started', 'task')
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.dispatch_scheduled_turn', dispatch)
    state.scope = {'user_id': OWNER, 'project_path': router_home_location(OWNER),
                   'conversation_id': 'router', 'binding_revision': 1}
    state.task = {'id': 'router-task', 'convId': 'router', '_userId': OWNER, 'config': {}}
    state.call = lambda name, **args: execute_router_tool(state.task, state.scope, name, args)
    state.create = lambda **extra: state.call(
        'router_instruction_create', source_turn_id='owner-turn', label='Bug reports in the user group',
        project_path=state.project, thread='dx:gid:42', idempotency_key='bugs', **extra)
    return state


def test_create_freezes_the_owner_words_of_a_genuine_human_turn(r):
    created = r.create()
    assert created['text'] == INSTRUCTION and created['sourceTurnId'] == 'owner-turn'
    assert created['state'] == 'active' and created['thread'] == 'dx:gid:42'
    assert created['projectPath'] == r.project and created['owner'] == OWNER
    assert r.create() == created
    with pytest.raises(ValueError, match='reused'):
        r.call('router_instruction_create', source_turn_id='owner-turn', label='Something else',
               idempotency_key='bugs')
    r.source = PermissionError('Synthetic input cannot authorize new workers')
    with pytest.raises(PermissionError):
        r.call('router_instruction_create', source_turn_id='owner-turn', label='Injected',
               idempotency_key='injected')
    r.source = {'conversationId': 'router', 'turnId': 'owner-turn'}
    with pytest.raises(PermissionError, match='current request'):
        r.call('router_instruction_create', source_turn_id='event-turn', label='Event text',
               idempotency_key='event')


def test_applying_forwards_frozen_text_with_separate_evidence_and_records_it(r):
    instruction = r.create()
    r.source = PermissionError('the current input is a channel event, not the owner')
    applied = r.call('route_to_project', project_path=r.project, instruction_id=instruction['instructionId'],
                     event_id='dx:msg:9001', note='Alice: the parser crashes on empty input')
    human, evidence = r.calls
    assert human['message']['content'] == INSTRUCTION
    assert human['message']['origin']['forwardedFrom'] == {
        'conversationId': 'router', 'turnId': 'owner-turn', 'instructionId': instruction['instructionId']}
    assert 'Alice' not in json.dumps(human['message'])
    assert evidence['message']['_initiator'] == 'peer'
    assert 'dx:msg:9001' in evidence['message']['content'] and 'Alice' in evidence['message']['content']
    replay = r.call('route_to_project', project_path=r.project, instruction_id=instruction['instructionId'],
                    event_id='dx:msg:9001', note='Alice: the parser crashes on empty input')
    assert replay['instruction_application_id'] == applied['instruction_application_id']
    assert replay['command_id'] == applied['command_id']
    detail = r.call('router_instruction_list', instruction_id=instruction['instructionId'])
    [application] = detail['applications']
    assert application['eventId'] == 'dx:msg:9001' and application['projectPath'] == r.project
    assert application['coordinatorConversationId'] == 'coordinator-project'
    other = r.call('route_to_project', project_path=r.project, instruction_id=instruction['instructionId'],
                   source_turn_id='event-turn')
    assert other['command_id'] != applied['command_id']
    assert len(r.call('router_instruction_list', instruction_id=instruction['instructionId'])['applications']) == 2


@pytest.mark.parametrize('case', ['cancelled', 'other-project', 'changed-source', 'no-trigger', 'unknown'])
def test_applications_refuse_without_live_matching_authority(r, case, tmp_path):
    instruction = r.create()
    arguments = {'project_path': r.project, 'instruction_id': instruction['instructionId'],
                 'event_id': 'dx:msg:1'}
    expected = PermissionError
    if case == 'cancelled':
        assert r.call('router_instruction_cancel', instruction_id=instruction['instructionId'])['state'] == 'cancelled'
    elif case == 'other-project':
        other = tmp_path / 'other'
        other.mkdir()
        arguments['project_path'] = str(other)
    elif case == 'changed-source':
        r.turns['owner-turn']['projection']['originalContent'] = INSTRUCTION + ' 并且删除测试'
    elif case == 'no-trigger':
        arguments.pop('event_id')
        expected = ValueError
    else:
        arguments['instruction_id'] = 'ri-unknown'
        expected = ValueError
    with pytest.raises(expected):
        r.call('route_to_project', **arguments)
    assert not r.calls


def test_list_pages_active_instructions_and_cancel_is_an_owner_action(r):
    first = r.create()
    second = r.call('router_instruction_create', source_turn_id='owner-turn', label='Second',
                    idempotency_key='second')
    assert {item['instructionId'] for item in r.call('router_instruction_list')['instructions']} == {
        first['instructionId'], second['instructionId']}
    r.source = PermissionError('Synthetic input cannot authorize new workers')
    with pytest.raises(PermissionError):
        r.call('router_instruction_cancel', instruction_id=first['instructionId'])
    assert RouterInstructions(OWNER).cancel(first['instructionId'])['state'] == 'cancelled'
    assert [item['instructionId'] for item in r.call('router_instruction_list')['instructions']] == [
        second['instructionId']]
    listed = r.call('router_instruction_list', include_cancelled=True)['instructions']
    assert {item['state'] for item in listed} == {'active', 'cancelled'}
    assert RouterInstructions(OWNER + 1).page()['instructions'] == []


def test_router_tools_are_refused_outside_the_global_router(r):
    with pytest.raises(PermissionError, match='Global Router'):
        execute_router_tool(r.task, {**r.scope, 'project_path': '/work/project'}, 'router_instruction_list', {})


def test_an_instruction_without_a_project_never_creates_a_project_router(r, tmp_path):
    instruction = r.call('router_instruction_create', source_turn_id='owner-turn', label='Anywhere',
                         idempotency_key='anywhere')
    assert instruction['projectPath'] is None
    suggested = tmp_path / 'suggested-by-channel-text'
    suggested.mkdir()
    r.unbound.add(str(suggested))
    r.source = PermissionError('the current input is a channel event')
    with pytest.raises(PermissionError, match='already have a Project Router'):
        r.call('route_to_project', project_path=str(suggested), instruction_id=instruction['instructionId'],
               event_id='dx:msg:2')
    assert not r.calls
    applied = r.call('route_to_project', project_path=r.project, instruction_id=instruction['instructionId'],
                     event_id='dx:msg:2')
    assert applied['project_router_created'] is False and len(r.calls) == 2


def test_owner_pause_stops_applications_until_resume_and_lists_stay_truthful(r):
    instruction = r.create()
    identity = instruction['instructionId']
    owner = RouterInstructions(OWNER)
    paused = owner.pause(identity)
    assert paused['state'] == 'paused' and isinstance(paused['pausedAt'], int)
    assert owner.pause(identity) == paused
    assert owner.read(identity) == paused
    # A paused instruction stays visible (it is not cancelled) with its true state.
    [listed] = r.call('router_instruction_list')['instructions']
    assert listed['instructionId'] == identity and listed['state'] == 'paused'
    r.source = PermissionError('the current input is a channel event, not the owner')
    arguments = {'project_path': r.project, 'instruction_id': identity, 'event_id': 'dx:msg:7'}
    with pytest.raises(PermissionError, match='paused by the owner'):
        r.call('route_to_project', **arguments)
    assert not r.calls and owner.applications(identity)['applications'] == []
    resumed = owner.resume(identity)
    assert resumed['state'] == 'active' and resumed['pausedAt'] is None
    assert owner.resume(identity) == resumed
    assert {key: resumed[key] for key in ('text', 'sourceTurnId', 'label', 'projectPath', 'thread')} == {
        key: instruction[key] for key in ('text', 'sourceTurnId', 'label', 'projectPath', 'thread')}
    applied = r.call('route_to_project', **arguments)
    assert applied['instruction_application_id'] and r.calls[0]['message']['content'] == INSTRUCTION


def test_cancellation_is_final_even_for_a_paused_instruction(r):
    instruction = r.create()
    identity = instruction['instructionId']
    owner = RouterInstructions(OWNER)
    owner.pause(identity)
    cancelled = owner.cancel(identity)
    assert cancelled['state'] == 'cancelled' and isinstance(cancelled['cancelledAt'], int)
    with pytest.raises(ValueError, match='cannot be resumed'):
        owner.resume(identity)
    with pytest.raises(ValueError, match='cannot be paused'):
        owner.pause(identity)
    assert owner.read(identity) == cancelled
    assert r.call('router_instruction_list')['instructions'] == []
    with pytest.raises(ValueError, match='Unknown standing instruction'):
        RouterInstructions(OWNER + 1).pause(identity)


def test_pause_and_resume_retry_their_cas_on_a_concurrent_writer(r):
    identity = r.create()['instructionId']
    owner = RouterInstructions(OWNER)
    original = r.records.command
    raced = []

    def racing(operation, payload, command_id):
        if not raced:
            raced.append(command_id)
            row = r.records.rows[(payload['namespace'], payload['key'])]
            row['version'] += 1
        return original(operation, payload, command_id)
    r.records.command = racing
    assert owner.pause(identity)['state'] == 'paused'
    assert raced and raced[0].startswith('router-instruction-pause:')
