"""Feishu bridge authority: default-deny admission, owner-only commands and
project-confined file reads. Runs without the lark SDK: replies, the task
runner and canonical persistence are stubbed at their module seams."""
from __future__ import annotations

import json
import os
from unittest import mock

import pytest

import lib.feishu._state as feishu_state
import lib.feishu.commands as commands
import lib.feishu.conversation as conversation
import lib.feishu.events as events
import lib.feishu.pipeline as pipeline
from lib.feishu.authority import FeishuSender, resolve_sender
from lib.feishu.user_state import FeishuUserSessionStore
from lib.tasks_pkg.execution_profile import THIRD_PARTY_TEXT

pytestmark = pytest.mark.unit

OWNER = 'ou_owner'
GUEST = 'ou_guest'
STRANGER = 'ou_stranger'


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    """Owner 7 has one registered project; the guest is only allowlisted."""
    project = tmp_path / 'proj'
    outside = tmp_path / 'outside'
    project.mkdir()
    outside.mkdir()
    (project / 'inside.txt').write_text('inside-content\n')
    (outside / 'secret.txt').write_text('TOP-SECRET-VALUE\n')
    os.symlink(outside, project / 'link')
    monkeypatch.setenv('FEISHU_USER_OWNER_MAP', json.dumps({OWNER: 7}))
    monkeypatch.delenv('FEISHU_DEFAULT_OWNER_USER_ID', raising=False)
    monkeypatch.setattr(feishu_state, 'ALLOWED_USERS', {GUEST})
    monkeypatch.setattr(feishu_state, 'DEFAULT_PROJECT_PATH', str(tmp_path / 'unregistered-default'))
    monkeypatch.setattr(conversation, 'feishu_user_sessions', FeishuUserSessionStore(capacity=16))
    registered = {7: [{'path': str(project)}]}
    monkeypatch.setattr('lib.project_mod.config.get_recent_projects',
                        lambda *, user_id: list(registered.get(user_id, [])))
    events._processed_msgs.clear()
    sent = []
    monkeypatch.setattr(events, 'send_text', lambda message_id, text, **kw: sent.append(text))
    runs = []

    def run_task_sync(config, *, user_id, progress_fn=None, execution_profile=None):
        runs.append({'config': config, 'user_id': user_id,
                     'execution_profile': execution_profile})
        return 'model-reply'
    monkeypatch.setattr('lib.tasks_pkg.sync_run.run_task_sync', run_task_sync)
    persisted = []
    monkeypatch.setattr(pipeline, 'persist_exchange',
                        lambda sender, user, answer: persisted.append((sender, user)) or True)
    return mock.Mock(project=project, outside=outside, sent=sent, runs=runs, persisted=persisted,
                     registered=registered)


def _message(text, open_id, message_id):
    return {'event': {'message': {'message_id': message_id, 'message_type': 'text',
                                  'content': json.dumps({'text': text}), 'chat_id': 'oc_chat'},
                      'sender': {'sender_id': {'open_id': open_id}}}}


def _owner():
    sender = resolve_sender(OWNER)
    assert sender == FeishuSender(OWNER, 7, True)
    return sender


def test_empty_allowlist_admits_only_owner_mapped_senders(bridge, monkeypatch):
    monkeypatch.setattr(feishu_state, 'ALLOWED_USERS', set())
    events.handle_message_event(_message('hello', STRANGER, 'm-stranger'))
    assert bridge.runs == [] and '没有权限' in bridge.sent[-1]
    events.handle_message_event(_message('hello', OWNER, 'm-owner'))
    assert [run['user_id'] for run in bridge.runs] == [7]
    assert bridge.runs[-1]['execution_profile'] is None, 'the owner keeps the full task profile'


def test_default_owner_runs_guest_chat_as_nonhuman_without_commands(bridge, monkeypatch):
    monkeypatch.setenv('FEISHU_DEFAULT_OWNER_USER_ID', '7')
    guest = resolve_sender(GUEST)
    assert guest == FeishuSender(GUEST, 7, False)
    assert resolve_sender(STRANGER) is None, 'the default owner never admits an unlisted sender'
    with mock.patch.object(pipeline, 'exec_project_tool') as tool:
        for text in ('/cat /etc/passwd', '/project /', '/tool', '/help'):
            events.handle_message_event(_message(text, GUEST, 'm-' + text))
        tool.assert_not_called()
    assert len(bridge.sent) == 4 and all('仅限' in text for text in bridge.sent), bridge.sent
    assert conversation.get_mode(GUEST) == 'chat'
    events.handle_message_event(_message('what is new?', GUEST, 'm-chat'))
    assert bridge.runs[-1]['user_id'] == 7
    assert bridge.runs[-1]['execution_profile'] == THIRD_PARTY_TEXT
    assert 'projectPath' not in bridge.runs[-1]['config']
    sender, user_message = bridge.persisted[-1]
    assert sender.is_owner is False and sender.open_id == GUEST


def test_admitted_sender_without_owner_runs_nothing(bridge):
    with mock.patch.object(pipeline, 'exec_project_tool') as tool:
        events.handle_message_event(_message('/ls /', GUEST, 'm-unbound'))
        events.handle_message_event(_message('hello', GUEST, 'm-unbound-chat'))
        tool.assert_not_called()
    assert bridge.runs == [] and all('尚未绑定' in text for text in bridge.sent), bridge.sent


def test_menu_commands_follow_the_same_authority(bridge, monkeypatch):
    monkeypatch.setenv('FEISHU_DEFAULT_OWNER_USER_ID', '7')
    menu = {'event': {'event_key': 'ls', 'chat_id': 'oc_chat',
                      'operator': {'operator_id': {'open_id': GUEST}}}}
    with mock.patch.object(pipeline, 'exec_project_tool') as tool:
        events.handle_menu_event(menu)
        tool.assert_not_called()
    assert '仅限' in bridge.sent[-1]


def test_project_selection_is_limited_to_registered_projects(bridge, tmp_path):
    owner = _owner()
    listing = commands.dispatch_command(owner, '/project')
    assert str(bridge.project) in listing and str(tmp_path) + '\n' not in listing
    for requested in (str(bridge.outside), '/', '/etc', 'outside', str(bridge.project / 'link')):
        reply = commands.dispatch_command(owner, '/project ' + requested)
        assert '已登记' in reply, reply
        assert conversation.get_project(OWNER) != requested
    assert '✅' in commands.dispatch_command(owner, '/project proj')
    assert conversation.get_project(OWNER) == str(bridge.project)


def test_file_commands_require_a_registered_project(bridge):
    owner = _owner()
    with mock.patch('lib.project_mod.tools.execute_tool') as execute:
        reply = commands.dispatch_command(owner, '/cat inside.txt')
    execute.assert_not_called()
    assert '/project' in reply
    commands.dispatch_command(owner, '/project proj')
    bridge.registered[7] = []  # Unregistering revokes access to the selection.
    with mock.patch('lib.project_mod.tools.execute_tool') as execute:
        commands.dispatch_command(owner, '/ls .')
    execute.assert_not_called()


def test_file_commands_stay_inside_the_selected_project(bridge):
    from lib.project_mod.abs_path_guard import is_restricted
    owner = _owner()
    commands.dispatch_command(owner, '/project proj')
    assert 'inside-content' in commands.dispatch_command(owner, '/cat inside.txt')
    assert 'inside.txt' in commands.dispatch_command(owner, '/ls .')
    secret = str(bridge.outside / 'secret.txt')
    for text in ('/cat ../outside/secret.txt', '/cat ' + secret, '/cat link/secret.txt'):
        reply = commands.dispatch_command(owner, text)
        assert 'TOP-SECRET-VALUE' not in reply, (text, reply)
    for text in ('/ls ' + str(bridge.outside), '/ls link', '/ls ..', '/ls ../outside',
                 '/grep TOP-SECRET ' + str(bridge.outside), '/grep TOP-SECRET link',
                 '/grep TOP-SECRET ..'):
        body = commands.dispatch_command(owner, text).split('\n', 1)[1]  # past the echoed header
        assert 'TOP-SECRET-VALUE' not in body and 'secret.txt' not in body, (text, body)
    assert is_restricted() is False, 'the guard is scoped to one command'


def test_tool_mode_binds_the_registered_project_for_the_owner_only(bridge, monkeypatch):
    owner = _owner()
    commands.dispatch_command(owner, '/project proj')
    commands.dispatch_command(owner, '/tool')
    pipeline.run_task_pipeline(owner, 'run the tests', source_message_id='m-tool')
    config = bridge.runs[-1]['config']
    assert config['projectPath'] == str(bridge.project) and 'project_path' not in config
    bridge.registered[7] = []
    pipeline.run_task_pipeline(owner, 'again', source_message_id='m-tool-2')
    assert 'projectPath' not in bridge.runs[-1]['config']
    monkeypatch.setenv('FEISHU_DEFAULT_OWNER_USER_ID', '7')
    conversation.set_mode(GUEST, 'tool')
    conversation.set_project(GUEST, str(bridge.project))
    bridge.registered[7] = [{'path': str(bridge.project)}]
    pipeline.run_task_pipeline(resolve_sender(GUEST), 'read secrets', source_message_id='m-guest')
    assert 'projectPath' not in bridge.runs[-1]['config']
    assert bridge.runs[-1]['execution_profile'] == THIRD_PARTY_TEXT
