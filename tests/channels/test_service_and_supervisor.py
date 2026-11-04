"""Service commands, auto-reply and supervisor restart policy (C5, C6)."""
import threading
from types import SimpleNamespace

import pytest

from lib.channels import autoreply, service
from lib.channels.dx.login import LoginSession
from lib.channels.model import ChannelError, ThreadKey
from tests.channels.conftest import OWNER, OWNER_UID, ROUTER

pytestmark = pytest.mark.unit

COMMAND = 'dx:gid:900'
IDENTITY = {'uid': OWNER_UID, 'mis': 'owner', 'name': 'Owner'}
ALICE = {'uid': '3003', 'mis': 'alice', 'name': 'Alice'}


@pytest.fixture(autouse=True)
def quiet_supervisor(monkeypatch):
    calls = []
    monkeypatch.setattr('lib.channels.dx.supervisor.notify_account_changed', calls.append)
    return calls


@pytest.fixture
def installed(monkeypatch, tmp_path):
    from lib.channels.dx.adapter import LaunchSpec
    (tmp_path / 'engine').mkdir()
    (tmp_path / 'engine' / 'handler.js').write_text('')
    (tmp_path / 'dx-tofu.sh').write_text('')
    spec = LaunchSpec(skill_dir=str(tmp_path), launcher=str(tmp_path / 'dx-tofu.sh'),
                      engine_dir=str(tmp_path / 'engine'))
    assert spec.installed
    monkeypatch.setattr('lib.channels.dx.adapter.launch_spec', lambda owner: spec)
    monkeypatch.setattr('lib.channels.dx.supervisor.launch_spec', lambda owner: spec)
    return spec


def test_enable_returns_at_once_and_starts_ingress_now(store, router, installed, quiet_supervisor):
    view = service.update_account(OWNER, {'enabled': True}, store=store)
    # No launcher command runs in the request: the supervisor confirms the identity.
    assert view['enabled'] and view['identity'] is None and view['loginMis'] is None
    assert view['status']['state'] == 'starting' and view['routerConversationId'] == ROUTER
    frontier = store.account('dx')['frontierAtMs']
    assert frontier and quiet_supervisor == [OWNER]
    # Re-enabling keeps the frontier: catch-up resumes, never restarts history.
    service.update_account(OWNER, {'enabled': False}, store=store)
    service.update_account(OWNER, {'enabled': True}, store=store)
    assert store.account('dx')['frontierAtMs'] == frontier


def test_login_mis_is_validated_and_normalized(store, router, installed):
    view = service.update_account(OWNER, {'mis': '  Alice.W-2 '}, store=store)
    assert view['loginMis'] == 'alice.w-2' and not view['enabled']
    for bad in ('', 'has space', '-lead', 'x' * 65, 7, None, '大象'):
        with pytest.raises(ChannelError) as raised:
            service.update_account(OWNER, {'mis': bad}, store=store)
        assert raised.value.code == 'invalid_update'
    assert store.account('dx')['mis'] == 'alice.w-2'


def test_switching_the_login_mis_drops_the_old_account(store, router, installed, enabled, quiet_supervisor):
    enabled(command_chat=COMMAND)
    store.set_status('dx', 'connected', '', connected_at_ms=5)
    # Entering the bound account's own MIS changes nothing it depends on.
    view = service.update_account(OWNER, {'mis': 'Owner'}, store=store)
    assert view['identity'] == IDENTITY and view['commandChat'] == COMMAND
    assert view['status']['state'] == 'connected' and view['loginMis'] == 'owner'
    view = service.update_account(OWNER, {'mis': 'alice'}, store=store)
    assert view['loginMis'] == 'alice' and view['identity'] is None and view['commandChat'] is None
    assert view['status']['state'] == 'starting' and view['enabled']
    # Ingress of the new account starts now instead of replaying from the old frontier.
    assert store.account('dx')['frontierAtMs'] > 1


def test_switching_a_disabled_account_and_enabling_it_in_one_command(store, router, installed, enabled):
    enabled(command_chat=COMMAND)
    service.update_account(OWNER, {'enabled': False}, store=store)
    view = service.update_account(OWNER, {'mis': 'alice', 'enabled': True}, store=store)
    assert view['enabled'] and view['loginMis'] == 'alice' and view['identity'] is None
    assert view['status']['state'] == 'starting' and store.account('dx')['frontierAtMs'] > 1


def test_command_chat_selection_and_validation(store, router, installed):
    view = service.update_account(OWNER, {'commandChat': COMMAND}, store=store)
    assert view['commandChat'] == COMMAND
    assert store.thread(ThreadKey.parse(COMMAND)) is not None
    with pytest.raises(ChannelError):
        service.update_account(OWNER, {'commandChat': 'dx:gid:abc'}, store=store)
    with pytest.raises(ChannelError):
        service.update_account(OWNER, {'unknown': 1}, store=store)


def test_create_command_chat_is_single_member_and_idempotent(store, router, installed, enabled):
    enabled()
    created = []

    def creator(owner, *, name, member_uid, mis):
        created.append((name, member_uid, mis))
        return '4242'
    view = service.create_command_chat(OWNER, store=store, group_creator=creator)
    assert view['commandChat'] == 'dx:gid:4242' and created == [('Tofu Router', OWNER_UID, 'owner')]
    assert store.thread(ThreadKey.parse('dx:gid:4242'))['title'] == 'Tofu Router'
    service.create_command_chat(OWNER, store=store, group_creator=creator)
    assert len(created) == 1


def test_failed_group_creation_releases_its_claim(store, router, installed, enabled):
    enabled()

    def failing(owner, *, name, member_uid, mis):
        raise ChannelError('group_create_failed', 'no gid')
    with pytest.raises(ChannelError):
        service.create_command_chat(OWNER, store=store, group_creator=failing)
    assert store.account('dx').get('commandChatCreation') is None


def test_enabling_a_thread_does_not_replay_its_ignored_past(store, router, enabled):
    enabled()
    group = ThreadKey.parse('dx:gid:500')
    store.update_thread(group, lambda value: {**value, 'watermark': '7000000000000000001'})
    view = service.configure_thread(OWNER, str(group), mode='watch', store=store)
    assert view['mode'] == 'watch' and view['watermark'] is None
    assert store.thread(group)['sinceMs']
    assert service.configure_thread(OWNER, str(group), mode=None, store=store)['mode'] == 'ignore'
    with pytest.raises(ChannelError):
        service.configure_thread(OWNER, str(group), mode='command', store=store)


# ── auto-reply ────────────────────────────────────────────────────────

@pytest.fixture
def router_turns(monkeypatch):
    rows = {}
    monkeypatch.setattr('lib.turn_lifecycle.get_turn',
                        lambda conv, turn, *, user_id, include_projection=True: rows.get((conv, turn)))

    def human(origin):
        rows[(ROUTER, 'in-1')] = {'conversationId': ROUTER, 'turnId': 'in-1', 'actor': 'human',
                                  'projection': {'content': 'q', 'origin': origin}}
        rows[(ROUTER, 'out-1')] = {'conversationId': ROUTER, 'turnId': 'out-1',
                                  'actor': 'assistant', 'parentTurnId': 'in-1'}
    return human


@pytest.fixture
def command_origin(router_turns):
    return {'blockId': 'origin', 'initiator': 'human',
            'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': [f'{COMMAND}/1']}}


def test_task_event_forwards_only_canonical_intermediate_text_once(
        store, router, enabled, router_turns, command_origin, monkeypatch):
    enabled(command_chat=COMMAND)
    router_turns(command_origin)
    task = {'id': 'task-1', '_userId': OWNER, 'convId': ROUTER, '_turnId': 'out-1',
            'segments': [
                {'type': 'thinking', 'blockId': 'thinking:1', 'deliverable': False,
                 'text': 'private reasoning'},
                {'type': 'text', 'blockId': 'text:round-1', 'deliverable': False,
                 'text': 'Checking the project now.'},
                {'type': 'text', 'blockId': 'text:discarded', 'deliverable': False,
                 '_providerAttemptDiscarded': True, 'text': 'Discarded provider attempt'},
                {'type': 'text', 'blockId': 'text:terminal', 'deliverable': True,
                 'text': 'Final answer'},
            ]}
    monkeypatch.setattr('lib.tasks_pkg.manager.runtime.chat_task_runtime.get_owned', lambda *a, **k: task)
    payload = {'type': 'round_committed', '_ownerUserId': str(OWNER), 'conversationId': ROUTER,
               'turnId': 'out-1'}
    autoreply.on_task_event('chat', 'task-1', payload)
    autoreply.on_task_event('chat', 'task-1', payload)
    [item] = store.active_outbox()
    assert item.text == 'Checking the project now.'
    assert item.origin['kind'] == 'router_content'
    assert 'private reasoning' not in item.text and 'Final answer' not in item.text


def test_task_event_forwards_a_progress_board_summary_once(
        store, router, enabled, router_turns, command_origin, monkeypatch):
    enabled(command_chat=COMMAND)
    router_turns(command_origin)
    task = {'id': 'task-1', '_userId': OWNER, 'convId': ROUTER, '_turnId': 'out-1', 'segments': []}
    monkeypatch.setattr('lib.tasks_pkg.manager.runtime.chat_task_runtime.get_owned', lambda *a, **k: task)

    class Board:
        def __init__(self, owner, project):
            assert project == '/srv/router-home'
        def read(self):
            return {'board': {'revision': 3, 'summary': 'Two fixes are verified.'}}
    monkeypatch.setattr('lib.conversations.coordinator_progress.CoordinatorTaskProgress', Board)
    payload = {'type': 'tool_complete', '_ownerUserId': str(OWNER), 'conversationId': ROUTER,
               'turnId': 'out-1', 'toolName': 'update_task_progress',
               'toolContent': '{"revision": 3, "changed": true}'}
    autoreply.on_task_event('chat', 'task-1', payload)
    autoreply.on_task_event('chat', 'task-1', payload)
    [item] = store.active_outbox()
    assert item.text == '进度：Two fixes are verified.'
    assert item.origin['kind'] == 'router_progress'


def test_installed_push_observer_receives_a_real_chat_frame(
        store, router, enabled, router_turns, command_origin, monkeypatch):
    enabled(command_chat=COMMAND)
    router_turns(command_origin)
    task = {'id': 'task-1', '_userId': OWNER, 'convId': ROUTER, '_turnId': 'out-1',
            '_attemptId': 'attempt-1', 'segments': [{
                'type': 'text', 'blockId': 'text:round-1', 'deliverable': False,
                'text': 'I am checking it.'}]}
    monkeypatch.setattr('lib.tasks_pkg.manager.runtime.chat_task_runtime.get_owned', lambda *a, **k: task)
    monkeypatch.setattr('lib.turn_lifecycle.get_attempt',
                        lambda attempt_id, *, user_id: {'commandId': 'channel-owner-command'})
    autoreply.acknowledge_command(OWNER, ROUTER, SimpleNamespace(
        channel='dx', thread=COMMAND, event_id=f'{COMMAND}/1'), 'started')
    monkeypatch.setattr(autoreply, '_INSTALLED', False)
    monkeypatch.setattr('lib.tasks_pkg.manager._terminal.register_terminal_listener', lambda listener: True)
    from lib.agent_core.push import PushHub
    hub = PushHub()
    monkeypatch.setattr('lib.agent_core.push.hub', hub)
    assert autoreply.install()
    hub.push_event('chat', 'task-1', {'type': 'tool_start', 'conversationId': ROUTER,
                                     'turnId': 'out-1'}, user_id=OWNER)
    items = store.active_outbox()
    assert [item.text for item in items] == ['收到，总负责人已开始处理。', 'I am checking it.']
    hub.stop()


def test_task_event_ignores_another_conversation(
        store, router, enabled, router_turns, command_origin):
    enabled(command_chat=COMMAND)
    router_turns(command_origin)
    autoreply.on_task_event('chat', 'task-1', {
        'type': 'round_committed', '_ownerUserId': str(OWNER),
        'conversationId': 'another-conversation', 'turnId': 'out-1'})
    assert store.active_outbox() == []


def _done(conversation=ROUTER, **extra):
    return {'id': 't', 'status': 'done', '_userId': OWNER, 'convId': conversation,
            '_turnId': 'out-1', 'content': 'The answer', **extra}


def test_router_answer_to_a_command_chat_message_returns_to_that_thread(store, router, enabled, router_turns):
    enabled(command_chat=COMMAND)
    router_turns({'blockId': 'origin', 'initiator': 'human',
                  'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': [f'{COMMAND}/1']}})
    autoreply.on_task_terminal(_done())
    autoreply.on_task_terminal(_done())  # observer replay stays exactly once
    items = store.active_outbox()
    assert [item.text for item in items] == ['The answer', '总负责人处理完成。']
    assert [item.idempotency_key for item in items] == ['out-1', 'out-1:terminal:done']
    assert [item.origin['kind'] for item in items] == ['router_reply', 'router_status']


@pytest.mark.parametrize('origin, task', [
    ({'blockId': 'origin', 'initiator': 'channel',
      'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': []}}, _done()),
    ({'blockId': 'origin', 'initiator': 'human'}, _done()),
    ({'blockId': 'origin', 'initiator': 'human',
      'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': []}}, _done(conversation='worker')),
])
def test_no_auto_reply_for_nonhuman_input_other_conversations_or_failures(
        store, router, enabled, router_turns, origin, task):
    enabled(command_chat=COMMAND)
    router_turns(origin)
    autoreply.on_task_terminal(task)
    assert store.active_outbox() == []


def test_human_steer_into_a_notice_returns_the_final_answer_to_its_command_thread(
        store, router, enabled, monkeypatch):
    enabled(command_chat=COMMAND)
    from lib.turn_initiation import steer_author
    author = steer_author({'content': 'Answer in 大象', 'origin': {
        'initiator': 'human', 'channel': {
            'channel': 'dx', 'thread': COMMAND, 'eventIds': [f'{COMMAND}/42']}}},
        actor='human')
    rows = {
        (ROUTER, 'in-1'): {
            'conversationId': ROUTER, 'turnId': 'in-1', 'actor': 'human',
            'projection': {'content': 'project notice', 'origin': {'initiator': 'peer'}},
        },
        (ROUTER, 'out-1'): {
            'conversationId': ROUTER, 'turnId': 'out-1', 'actor': 'assistant',
            'parentTurnId': 'in-1', 'projection': {'_userSteerInjects': [{
                'blockId': 'injection:user-steer:channel-owner-42',
                'commandId': 'channel-owner-42', 'deliveryState': 'delivered',
                'author': author}]},
        },
    }
    monkeypatch.setattr('lib.turn_lifecycle.get_turn',
                        lambda conv, turn, *, user_id, include_projection=True: rows[(conv, turn)])
    autoreply.on_task_terminal(_done(content='Here is the answer.'))
    items = store.active_outbox()
    assert [item.thread for item in items] == [COMMAND, COMMAND]
    assert [item.text for item in items] == ['Here is the answer.', '总负责人处理完成。']
    autoreply.on_task_terminal(_done(content='', status='error', finishReason='error'))
    [item] = store.active_outbox()[2:]
    assert item.thread == COMMAND and item.origin['kind'] == 'router_status'


@pytest.mark.parametrize('origin, outcome, status', [
    ({'blockId': 'notice', 'initiator': 'channel', 'channel': {
        'channel': 'dx', 'thread': COMMAND, 'eventIds': [f'{COMMAND}/99']}}, 'error', 'error'),
    ({'blockId': 'report', 'initiator': 'peer'}, 'interrupted', 'interrupted'),
])
def test_failed_notice_review_warns_the_command_chat_even_without_a_reply_origin(
        store, router, enabled, router_turns, origin, outcome, status):
    enabled(command_chat=COMMAND)
    router_turns(origin)
    autoreply.on_task_terminal(_done(content='', _silentTurn=True,
                                    status=status, finishReason=outcome))
    autoreply.on_task_terminal(_done(content='', _silentTurn=True,
                                    status=status, finishReason=outcome))
    autoreply.on_task_terminal(_done(content='', _silentTurn=True,
                                    status=status, finishReason=outcome))
    [item] = store.active_outbox()
    assert item.thread == COMMAND
    assert '尚未完成审阅' in item.text
    assert item.idempotency_key == f'out-1:notice-review:{outcome}'
    assert item.reply_to == (f'{COMMAND}/99' if outcome == 'error' else None)


def test_deliberate_silence_leaves_no_empty_reply_or_false_activity_completion(
        store, router, enabled, router_turns, command_origin):
    enabled(command_chat=COMMAND)
    router_turns(command_origin)
    autoreply.on_task_terminal(_done(content='', _silentTurn=True,
                                    _turnId='notice-out'))
    assert store.active_outbox() == []


def test_empty_success_still_reports_terminal_without_an_empty_reply(
        store, router, enabled, router_turns, command_origin):
    enabled(command_chat=COMMAND)
    router_turns(command_origin)
    autoreply.on_task_terminal(_done(content=''))
    [item] = store.active_outbox()
    assert item.text == '总负责人处理完成。'


@pytest.mark.parametrize('status, aborted, finish_reason, expected', [
    ('error', False, 'error', '总负责人处理失败，请重新发送或到 Tofu 查看详情。'),
    ('aborted', True, 'aborted', '总负责人已中止处理。'),
])
def test_failed_or_aborted_router_turn_reports_its_terminal_status(
        store, router, enabled, router_turns, status, aborted, finish_reason, expected):
    enabled(command_chat=COMMAND)
    router_turns({'blockId': 'origin', 'initiator': 'human',
                  'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': [f'{COMMAND}/1']}})
    autoreply.on_task_terminal(_done(status=status, aborted=aborted, finishReason=finish_reason,
                                     content='partial answer'))
    [item] = store.active_outbox()
    assert item.text == expected
    assert item.origin['kind'] == 'router_status'
    assert item.idempotency_key == f'out-1:terminal:{status}'


# ── supervisor ────────────────────────────────────────────────────────

class FakeAdapter:
    instances = []

    def __init__(self, owner, *, store=None, login_mis=None):
        self.owner, self.running, self.exit = owner, False, ('disconnected', 'bridge exited (1)')
        self.login_mis = login_mis
        self.ticks = 0
        FakeAdapter.instances.append(self)

    def start(self):
        self.running = True

    def alive(self):
        return self.running

    def tick(self):
        self.ticks += 1

    def stop(self, timeout):
        self.running = False
        return True

    def outcome(self):
        return self.exit


@pytest.fixture
def supervisor(monkeypatch):
    from lib.channels.dx import supervisor as module
    FakeAdapter.instances = []
    monkeypatch.setattr(module, '_ADAPTERS', {})
    monkeypatch.setattr(module, '_CONFIRMING', set())
    return module


class Login:
    """Launcher seams of identity confirmation: a session per MIS, whether its login
    waits for a 大象 approval (MIS not in ``reusable``) and a scripted ``whoami``."""

    def __init__(self, *, shared='owner', reusable=('owner',), identities=(IDENTITY, ALICE)):
        self.shared, self.reusable = shared, set(reusable)
        self.identities = {identity['mis']: identity for identity in identities}
        self.whoami: list[str | None] = []
        # Per login MIS: a gate its whoami waits on (a pending 大象 approval) and its failure.
        self.gates: dict[str, threading.Event] = {}
        self.failures: dict[str, ChannelError] = {}

    def session(self, owner, mis):
        return LoginSession(mis or self.shared, {})

    def approval_required(self, owner, session):
        return session.mis not in self.reusable

    def resolve(self, owner, session):
        self.whoami.append(session.mis)
        gate = self.gates.get(session.mis)
        if gate is not None:
            assert gate.wait(5)
        if session.mis in self.failures:
            raise self.failures[session.mis]
        return dict(self.identities[session.mis])

    def reconcile(self, supervisor, store):
        return supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter,
                                          identity_resolver=self.resolve, session_factory=self.session,
                                          approval_check=self.approval_required)


def settle_confirmations():
    for thread in threading.enumerate():
        if thread.name.startswith('dx-identity-'):
            thread.join(5)
            assert not thread.is_alive()


def test_supervisor_starts_restarts_and_stops_from_configuration(store, router, installed, enabled, supervisor):
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter) == 'idle'
    enabled()
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter) == 'started'
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter) == 'running'
    assert FakeAdapter.instances[0].ticks == 1
    FakeAdapter.instances[0].running = False  # bridge exited (disconnect)
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter) == 'started'
    assert len(FakeAdapter.instances) == 2
    store.update_account('dx', lambda account: {**account, 'enabled': False})
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter) == 'stopped'
    assert not FakeAdapter.instances[1].running


@pytest.mark.parametrize('state', ['needs_login', 'kicked'])
def test_session_required_and_kickoff_stop_restarts_until_reenabled(
        store, router, installed, enabled, supervisor, state):
    enabled()
    supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter)
    FakeAdapter.instances[0].running = False
    FakeAdapter.instances[0].exit = (state, 'surfaced')
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter) == state
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter) == state
    assert len(FakeAdapter.instances) == 1 and store.account('dx')['status']['state'] == state
    service.update_account(OWNER, {'enabled': True}, store=store)
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter) == 'started'


def test_enabling_a_live_account_again_keeps_the_status_its_session_reported(
        store, router, installed, enabled, supervisor):
    enabled()
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter) == 'started'
    store.set_status('dx', 'connected', '', connected_at_ms=5)
    view = service.update_account(OWNER, {'enabled': True}, store=store)
    # The running session reports a transition only once; a reset to 'starting' would stick.
    assert view['status']['state'] == 'connected' and view['status']['connectedAtMs'] == 5
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter) == 'running'
    assert store.account('dx')['status']['state'] == 'connected' and len(FakeAdapter.instances) == 1


def test_supervisor_degrades_clearly_without_router_or_skill(store, enabled, supervisor, monkeypatch, installed):
    import sys
    enabled()
    monkeypatch.setitem(sys.modules, 'lib.router.binding', None)
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter) == 'error'
    assert 'Global Router' in store.account('dx')['status']['detail']
    assert FakeAdapter.instances == []


def test_supervisor_confirms_the_identity_off_the_tick_then_starts_the_session(
        store, router, installed, supervisor):
    login = Login()
    service.update_account(OWNER, {'enabled': True}, store=store)
    assert login.reconcile(supervisor, store) == 'confirming'
    settle_confirmations()
    account = store.account('dx')
    assert account['identity'] == IDENTITY and login.whoami == ['owner']
    # The confirmation thread started the session of the confirmed login.
    [adapter] = FakeAdapter.instances
    assert adapter.running and adapter.login_mis == 'owner'
    assert account['status']['state'] == 'starting'
    assert login.reconcile(supervisor, store) == 'running' and login.whoami == ['owner']


def test_a_mis_without_a_reusable_session_waits_in_authorizing(store, router, installed, supervisor):
    login = Login(reusable=())
    approval = login.gates['alice'] = threading.Event()
    service.update_account(OWNER, {'mis': 'alice', 'enabled': True}, store=store)
    assert login.reconcile(supervisor, store) == 'confirming'
    try:
        for _ in range(500):
            if store.account('dx')['status']['state'] == 'authorizing':
                break
            threading.Event().wait(0.01)
        view = service.account_view(OWNER, store=store)
        assert view['status']['state'] == 'authorizing' and view['loginMis'] == 'alice'
        # Ticks and repeated enables neither start a second confirmation nor reset the state.
        assert login.reconcile(supervisor, store) == 'confirming'
        assert service.update_account(OWNER, {'enabled': True}, store=store)['status']['state'] == 'authorizing'
    finally:
        approval.set()
    settle_confirmations()
    assert login.whoami == ['alice'] and store.account('dx')['identity'] == ALICE
    assert [adapter.login_mis for adapter in FakeAdapter.instances] == ['alice']


def test_switching_the_account_replaces_the_running_session(store, router, installed, enabled, supervisor):
    login = Login()
    enabled(command_chat=COMMAND)
    assert login.reconcile(supervisor, store) == 'started'
    service.update_account(OWNER, {'mis': 'alice'}, store=store)
    assert login.reconcile(supervisor, store) == 'confirming'
    old = FakeAdapter.instances[0]
    assert not old.running
    settle_confirmations()
    assert [adapter.login_mis for adapter in FakeAdapter.instances] == ['owner', 'alice']
    assert FakeAdapter.instances[1].running and store.account('dx')['identity'] == ALICE
    # The replaced session's exit is not reported as the new account's state.
    assert store.account('dx')['status']['state'] == 'starting'


def test_a_newer_choice_does_not_wait_for_an_older_pending_approval(store, router, installed, supervisor):
    bob = {'uid': '4004', 'mis': 'bob', 'name': 'Bob'}
    login = Login(reusable=(), identities=(IDENTITY, ALICE, bob))
    alice_approval = login.gates['alice'] = threading.Event()
    service.update_account(OWNER, {'mis': 'alice', 'enabled': True}, store=store)
    assert login.reconcile(supervisor, store) == 'confirming'
    # The owner picks another account while alice's approval is still pending:
    # bob's confirmation starts at once and his session runs.
    service.update_account(OWNER, {'mis': 'bob'}, store=store)
    assert login.reconcile(supervisor, store) == 'confirming'
    for _ in range(500):
        if FakeAdapter.instances:
            break
        threading.Event().wait(0.01)
    assert store.account('dx')['identity'] == bob
    assert [adapter.login_mis for adapter in FakeAdapter.instances] == ['bob']
    # alice's late answer, success or failure, lands nowhere.
    login.failures['alice'] = ChannelError('needs_login', 'not approved in time')
    alice_approval.set()
    settle_confirmations()
    account = store.account('dx')
    assert account['identity'] == bob and account['status']['state'] == 'starting'
    assert sorted(login.whoami) == ['alice', 'bob'] and len(FakeAdapter.instances) == 1
    # Re-choosing a MIS whose confirmation is still running does not start a second one.
    approval = login.gates['carol'] = threading.Event()
    login.identities['carol'] = {'uid': '5005', 'mis': 'carol', 'name': 'Carol'}
    service.update_account(OWNER, {'mis': 'carol'}, store=store)
    assert login.reconcile(supervisor, store) == 'confirming'
    assert login.reconcile(supervisor, store) == 'confirming'
    approval.set()
    settle_confirmations()
    assert login.whoami.count('carol') == 1 and store.account('dx')['identity']['mis'] == 'carol'


def test_confirmation_failures_surface_as_states(store, router, installed, supervisor):
    login = Login(shared=None)
    service.update_account(OWNER, {'enabled': True}, store=store)
    assert login.reconcile(supervisor, store) == 'confirming'
    settle_confirmations()
    status = store.account('dx')['status']
    assert status['state'] == 'needs_login' and 'MIS' in status['detail'] and login.whoami == []
    # needs_login stops further attempts until the owner acts.
    assert login.reconcile(supervisor, store) == 'needs_login'
    login.failures['alice'] = ChannelError('needs_login', 'SSO_LOGIN_NOT_APPROVED: not approved')
    service.update_account(OWNER, {'mis': 'alice'}, store=store)
    assert login.reconcile(supervisor, store) == 'confirming'
    settle_confirmations()
    status = store.account('dx')['status']
    assert (status['state'], status['detail']) == ('needs_login', 'SSO_LOGIN_NOT_APPROVED: not approved')
    login.failures['alice'] = ChannelError('identity_mismatch', '大象 logged in as bob, not alice')
    service.update_account(OWNER, {'enabled': True}, store=store)
    assert login.reconcile(supervisor, store) == 'confirming'
    settle_confirmations()
    assert store.account('dx')['status']['state'] == 'error' and FakeAdapter.instances == []
