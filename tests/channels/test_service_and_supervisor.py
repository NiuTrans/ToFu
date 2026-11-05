"""Service commands, auto-reply and supervisor restart policy (C5, C6)."""
import pytest

from lib.channels import autoreply, service
from lib.channels.model import ChannelError, ThreadKey
from tests.channels.conftest import OWNER, OWNER_UID, ROUTER

pytestmark = pytest.mark.unit

COMMAND = 'dx:gid:900'
IDENTITY = {'uid': OWNER_UID, 'mis': 'owner', 'name': 'Owner'}


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


def test_enable_resolves_identity_and_starts_ingress_now(store, router, installed, quiet_supervisor):
    view = service.update_account(OWNER, {'enabled': True}, store=store,
                                  identity_resolver=lambda owner: dict(IDENTITY))
    assert view['enabled'] and view['identity'] == IDENTITY
    assert view['status']['state'] == 'starting' and view['routerConversationId'] == ROUTER
    frontier = store.account('dx')['frontierAtMs']
    assert frontier and quiet_supervisor == [OWNER]
    # Re-enabling keeps the frontier: catch-up resumes, never restarts history.
    service.update_account(OWNER, {'enabled': False}, store=store)
    service.update_account(OWNER, {'enabled': True}, store=store,
                           identity_resolver=lambda owner: dict(IDENTITY))
    assert store.account('dx')['frontierAtMs'] == frontier


def test_enable_without_sso_session_surfaces_needs_login(store, router, installed):
    def resolver(owner):
        raise ChannelError('needs_login', '大象 SSO session needs the owner to log in again')
    with pytest.raises(ChannelError):
        service.update_account(OWNER, {'enabled': True}, store=store, identity_resolver=resolver)
    assert store.account('dx')['status']['state'] == 'needs_login'
    assert not store.account('dx').get('enabled')


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

    def creator(owner, *, name, member_uid):
        created.append((name, member_uid))
        return '4242'
    view = service.create_command_chat(OWNER, store=store, group_creator=creator)
    assert view['commandChat'] == 'dx:gid:4242' and created == [('Tofu Router', OWNER_UID)]
    assert store.thread(ThreadKey.parse('dx:gid:4242'))['title'] == 'Tofu Router'
    service.create_command_chat(OWNER, store=store, group_creator=creator)
    assert len(created) == 1


def test_failed_group_creation_releases_its_claim(store, router, installed, enabled):
    enabled()

    def failing(owner, *, name, member_uid):
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
        rows[(ROUTER, 'out-1')] = {'conversationId': ROUTER, 'turnId': 'out-1', 'parentTurnId': 'in-1'}
    return human


def _done(conversation=ROUTER, **extra):
    return {'id': 't', 'status': 'done', '_userId': OWNER, 'convId': conversation,
            '_turnId': 'out-1', 'content': 'The answer', **extra}


def test_router_answer_to_a_command_chat_message_returns_to_that_thread(store, router, enabled, router_turns):
    enabled(command_chat=COMMAND)
    router_turns({'blockId': 'origin', 'initiator': 'human',
                  'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': [f'{COMMAND}/1']}})
    autoreply.on_task_terminal(_done())
    autoreply.on_task_terminal(_done())  # observer replay stays exactly once
    [item] = store.active_outbox()
    assert (item.thread, item.text, item.state, item.idempotency_key) == (COMMAND, 'The answer', 'queued', 'out-1')
    assert item.origin == {'kind': 'router_reply', 'conversationId': ROUTER, 'turnId': 'out-1'}


@pytest.mark.parametrize('origin, task', [
    ({'blockId': 'origin', 'initiator': 'channel',
      'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': []}}, _done()),
    ({'blockId': 'origin', 'initiator': 'human'}, _done()),
    ({'blockId': 'origin', 'initiator': 'human',
      'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': []}}, _done(conversation='worker')),
    ({'blockId': 'origin', 'initiator': 'human',
      'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': []}}, _done(status='error')),
])
def test_no_auto_reply_for_nonhuman_input_other_conversations_or_failures(
        store, router, enabled, router_turns, origin, task):
    enabled(command_chat=COMMAND)
    router_turns(origin)
    autoreply.on_task_terminal(task)
    assert store.active_outbox() == []


# ── supervisor ────────────────────────────────────────────────────────

class FakeAdapter:
    instances = []

    def __init__(self, owner, *, store=None):
        self.owner, self.running, self.exit = owner, False, ('disconnected', 'bridge exited (1)')
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
    return module


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
    service.update_account(OWNER, {'enabled': True}, store=store,
                           identity_resolver=lambda owner: dict(IDENTITY))
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter) == 'started'


def test_enabling_a_live_account_again_keeps_the_status_its_session_reported(
        store, router, installed, supervisor):
    service.update_account(OWNER, {'enabled': True}, store=store,
                           identity_resolver=lambda owner: dict(IDENTITY))
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=FakeAdapter) == 'started'
    store.set_status('dx', 'connected', '', connected_at_ms=5)
    view = service.update_account(OWNER, {'enabled': True}, store=store,
                                  identity_resolver=lambda owner: dict(IDENTITY))
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
