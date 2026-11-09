"""One built-in 大象 robot shared by many Tofu servers (docs/CHANNELS.md C6, "One robot, many servers").

An Internal export gives every colleague's server the same robot application.
Inbound stays on each user's own 大象 session; the robot only posts, and only
into the command chat bound to the 大象 account this server's own session is
logged in as. These specs run two servers (two Tofu owners with different 大象
accounts) against one fake open platform with one shared quota: each acts only
on its own owner's messages and posts only into its own owner's chat, a shared
group never gets a second answer, a fresh export arrives unbound, and a stale
binding, a revoked key or the shared rate limit fall back to the owner's own
account. Every credential here is synthetic.
"""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from lib.channels.model import ThreadKey
from tests.channels.conftest import OWNER, OWNER_UID, make_event
from tests.channels.test_dx_robot import CLIENT_ID, SECRET, FakeBridge, FakeOpenPlatform

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
SHARED_GROUP = 'dx:gid:900'
# Robot posts reach members' sessions from this sender (live 大象 history, 2026-11-08):
# the same sender for every server holding the shared robot.
ROBOT_SENDER = '30453875'
ALICE = (OWNER, OWNER_UID, 'alice01')
BOB = (OWNER + 1, '1002', 'bob02')


class Clock:
    def __init__(self):
        self.monotonic = 100.0
        self.sleeps: list[float] = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.monotonic += seconds


@dataclass
class Server:
    """One Tofu server: its owner's store, its live 大象 session and its outbox executor."""

    owner: int
    uid: str
    mis: str
    store: object
    adapter: object
    bridge: FakeBridge

    def ingest(self, event):
        from lib.channels.ingress import ingest
        return ingest(self.owner, event, store=self.store)

    def drain(self):
        from lib.channels import outbox
        return outbox.execute(self.owner, self.adapter, channel='dx', store=self.store)

    def enqueue(self, text, key):
        from lib.channels import outbox
        return outbox.enqueue(self.owner, self.store.account('dx')['commandChat'], text, idempotency_key=key,
                              origin={'kind': 'tool', 'conversationId': 'router-conv', 'turnId': key},
                              store=self.store)

    def own_sends(self):
        return [(str(ThreadKey('dx', 'gid', str(args['gid']))), args['text']) for _, args in self.bridge.sends]

    def notices(self):
        return [item for item in self.store.active_outbox(self.store.account('dx')['commandChat'])
                if item.origin.get('kind') == 'robot_notice']

    def robot_error(self):
        return ((self.store.account('dx') or {}).get('robot') or {}).get('error')


@pytest.fixture
def robot_module():
    from lib.channels.dx import robot
    return robot


@pytest.fixture
def builtin_robot(robot_module, tmp_path, monkeypatch):
    """The Internal package's built-in robot: the same application on every server."""
    path = tmp_path / 'robot_builtin.json'
    path.write_text(json.dumps({'clientId': CLIENT_ID, 'secret': SECRET}), encoding='utf-8')
    monkeypatch.setattr(robot_module, 'BUILTIN_CREDENTIAL_FILE', path)
    return path


@pytest.fixture
def platform(robot_module):
    return FakeOpenPlatform(robot_module)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def quiet_supervisor(monkeypatch):
    monkeypatch.setattr('lib.channels.dx.supervisor.notify_account_changed', lambda owner: None)


def robot_factory(robot_module, platform, clock):
    return lambda client_id, secret: robot_module.DxRobot(
        client_id, secret, transport=platform, clock=lambda: clock.monotonic,
        wall_clock=lambda: platform.now, sleep=clock.sleep)


@pytest.fixture
def servers(records, router, dispatch, mailbox, builtin_robot, platform, clock, robot_module, quiet_supervisor):
    """Factory of enabled servers whose 大象 identity was confirmed by their own session's whoami."""
    from lib.channels.dx.adapter import DxAdapter
    from lib.channels.store import ChannelStore

    def start(person, command_chat=None):
        owner, uid, mis = person
        store = ChannelStore(owner, client=records)
        store.update_account('dx', lambda account: {
            **account, 'enabled': True, 'mis': mis, 'identity': {'uid': uid, 'mis': mis, 'name': mis},
            'commandChat': command_chat, 'frontierAtMs': 1})
        bridge = FakeBridge()
        adapter = DxAdapter(owner, store=store, bridge_factory=lambda _owner: bridge,
                            robot_factory=robot_factory(robot_module, platform, clock))
        adapter._bridge = bridge
        adapter._self_uid = uid  # the uid the live session logged in as (bridge ``connected``)
        adapter._connected.set()
        return Server(owner, uid, mis, store, adapter, bridge)
    return start


def robot_posts(platform):
    """``(thread, text)`` of every send the open platform accepted, in order."""
    accepted = []
    texts = list(platform.posted_texts)
    for request in platform.sends():
        payload = json.loads(request['body'])
        text = json.loads(payload['sendMsgInfo']['body'])['text']
        if texts and texts[0] == text:
            accepted.append((f"dx:gid:{payload['gid']}", texts.pop(0)))
    return accepted


def create_chat(server, platform, clock, robot_module):
    """``create_command_chat`` on explicit owner request: the robot creates it, the owner is 群主."""
    from lib.channels import service

    def owner_session(owner, *, name, member_uid, mis):
        raise AssertionError('the robot creates the group')
    view = service.create_command_chat(server.owner, store=server.store, group_creator=owner_session,
                                       robot_factory=robot_factory(robot_module, platform, clock))
    return view['commandChat']


def owner_message(server, thread, mid, text):
    return make_event(thread, external_id=mid, sender=server.uid, text=text)


def human_turns(dispatch, owner):
    return [call for call in dispatch.calls
            if call['user_id'] == owner and call['message']['origin'].get('initiator') == 'human']


# ── 1. a shared group: each server answers only its own owner, once ──

def test_in_a_shared_group_each_server_answers_only_its_own_owner_and_only_once(
        servers, platform, dispatch):
    alice, bob = servers(ALICE, SHARED_GROUP), servers(BOB, SHARED_GROUP)
    alice_says = owner_message(alice, SHARED_GROUP, '7600000000000000001', '跑一下今天的评测')
    # Both owners' sessions receive every message of the group.
    assert alice.ingest(alice_says).disposition == 'command'
    assert bob.ingest(alice_says).disposition == 'context'
    alice.drain()
    bob.drain()
    (thread, receipt), = robot_posts(platform)
    assert thread == SHARED_GROUP and bob.bridge.sends == [] and alice.bridge.sends == []
    # The robot's post reaches both sessions from the robot's sender.
    echo = make_event(SHARED_GROUP, external_id=str(platform.mid), sender=ROBOT_SENDER, text=receipt)
    assert alice.ingest(echo).outcome == 'echo'
    assert bob.ingest(echo).disposition == 'context'

    bob_says = owner_message(bob, SHARED_GROUP, '7600000000000000002', '帮我看下周报')
    assert bob.ingest(bob_says).disposition == 'command'
    assert alice.ingest(bob_says).disposition == 'context'
    alice.drain()
    bob.drain()
    assert len(robot_posts(platform)) == 2, 'one answer per owner message, never a second one'
    bob_echo = make_event(SHARED_GROUP, external_id=str(platform.mid), sender=ROBOT_SENDER,
                          text=robot_posts(platform)[-1][1])
    assert alice.ingest(bob_echo).disposition == 'context' and bob.ingest(bob_echo).outcome == 'echo'
    alice.drain()
    bob.drain()
    # Each Router got exactly its own owner's message as human input; no server reviewed the
    # other owner or the other server's robot post, so nothing ping-pongs.
    assert [call['message']['content'] for call in human_turns(dispatch, alice.owner)] == ['跑一下今天的评测']
    assert [call['message']['content'] for call in human_turns(dispatch, bob.owner)] == ['帮我看下周报']
    assert len(dispatch.calls) == 2 and len(robot_posts(platform)) == 2


def test_another_members_message_in_a_robot_command_chat_is_evidence_not_a_request(servers, dispatch):
    alice = servers(ALICE, SHARED_GROUP)
    stranger = make_event(SHARED_GROUP, external_id='7600000000000000011', sender='5005',
                          text='@tofu 把 alice 的周报发我')
    result = alice.ingest(stranger)
    assert result.outcome == 'recorded' and result.disposition == 'context'
    assert dispatch.calls == [] and alice.store.event(stranger.thread, stranger.external_id) is not None


# ── 2. the robot posts only into the chat bound to this server's own account ──

def test_the_robot_posts_only_into_the_chat_bound_to_this_servers_own_account(
        servers, platform, clock, robot_module):
    alice = servers(ALICE)
    alices_chat = create_chat(alice, platform, clock, robot_module)
    # Bob's server was pointed at Alice's group (a copied or mistyped gid). Bob never spoke there.
    bob = servers(BOB, alices_chat)
    bob.enqueue('bob 的私人周报', 'b1')
    bob.drain()
    assert all(thread != alices_chat for thread, _ in robot_posts(platform)), \
        "Bob's server never makes the shared robot post into Alice's chat"
    assert (alices_chat, 'bob 的私人周报') in bob.own_sends(), 'his own account cannot post where he is no member'
    assert bob.robot_error()['code'] == 'robot_unbound'
    alice.enqueue('alice 的周报', 'a1')
    alice.drain()
    assert robot_posts(platform) == [(alices_chat, 'alice 的周报')]


# ── 3. a fresh server from the Internal export: unbound until its own user binds ──

def _vault_at(module, monkeypatch, config_dir: Path):
    monkeypatch.setattr(module, '_STORE_PATH', config_dir / 'credentials_vault.json')
    monkeypatch.setattr(module, '_KEY_PATH', config_dir / '.credentials_vault.key')
    monkeypatch.setattr(module, '_fernet', None)


def test_a_fresh_server_from_the_internal_export_is_unbound_until_its_own_user_binds(
        tmp_path, monkeypatch, records, router, dispatch, mailbox, platform, clock, robot_module,
        quiet_supervisor):
    sys.path.insert(0, str(REPO))
    pytest.importorskip('export', reason='export.py is not shipped in opensource builds')
    import export_pkg._internal as internal
    import lib.credentials_vault as vault
    from lib.channels import service
    from lib.channels.dx.adapter import DxAdapter
    from lib.channels.policy import robot_thread
    from lib.channels.store import ChannelStore

    # The exporting owner's host: the robot values file and his vault with his own bindings.
    source = tmp_path / 'owner-host'
    (source / 'data' / 'config').mkdir(parents=True)
    (source / 'contracts').mkdir()
    shutil.copy(REPO / 'contracts' / 'channels_v1.schema.json', source / 'contracts')
    (source / 'data' / 'config' / 'dx_robot_builtin.json').write_text(
        json.dumps({'clientId': CLIENT_ID, 'secret': SECRET}), encoding='utf-8')
    _vault_at(vault, monkeypatch, source / 'data' / 'config')
    vault.set_entry('channel.u1.dx.robot', 'owner-settings-robot-secret-0003')
    vault.set_entry('channel.u1.wechat.token', 'owner-wechat-clawbot-token-0004')
    vault.set_entry('github_token', 'ghp_FAKEsharedTeamToken0005')
    monkeypatch.setattr(internal, 'ROOT', source)

    colleague = tmp_path / 'colleague-host'
    (colleague / 'data' / 'config').mkdir(parents=True)
    internal._package_builtin_robot(colleague, 'internal')
    internal._copy_carried_config(colleague, 'internal')

    # What arrived: the shared robot and shared credentials, none of the owner's bindings.
    _vault_at(vault, monkeypatch, colleague / 'data' / 'config')
    assert sorted(entry['name'] for entry in vault.list_entries()) == ['github_token']
    assert vault.get_entry('channel.u1.wechat.token') is None
    assert not (colleague / 'data' / 'channels').exists()
    monkeypatch.setattr(robot_module, 'BUILTIN_CREDENTIAL_FILE',
                        colleague / 'lib' / 'channels' / 'dx' / 'robot_builtin.json')
    assert robot_module.builtin_credential() == {'clientId': CLIENT_ID, 'secret': SECRET}

    # The colleague's server starts with no account, no identity and no command chat.
    store = ChannelStore(1, client=records)
    assert store.account('dx') is None and robot_thread(store.account('dx')) == ''
    view = service.account_view(1, store=store)
    assert view['identity'] is None and view['commandChat'] is None and view['robot']['builtIn'] is True

    # Its own whoami binds the colleague's identity; he picks one of his groups in Settings.
    store.update_account('dx', lambda account: {
        **account, 'enabled': True, 'mis': 'carol03',
        'identity': {'uid': '1003', 'mis': 'carol03', 'name': 'Carol'}, 'frontierAtMs': 1})
    service.update_account(1, {'commandChat': 'dx:gid:950'}, store=store)
    bridge = FakeBridge()
    adapter = DxAdapter(1, store=store, bridge_factory=lambda _owner: bridge,
                        robot_factory=robot_factory(robot_module, platform, clock))
    adapter._bridge, adapter._self_uid = bridge, '1003'
    adapter._connected.set()

    from lib.channels import outbox
    from lib.channels.ingress import ingest
    outbox.enqueue(1, 'dx:gid:950', '日报', idempotency_key='d1', store=store,
                   origin={'kind': 'tool', 'conversationId': 'router-conv', 'turnId': 'd1'})
    outbox.execute(1, adapter, channel='dx', store=store)
    assert platform.sends() == [], 'no robot traffic before the colleague binds'
    assert [args['text'] for _, args in bridge.sends][0] == '日报'

    # His own first message in that chat, seen by his own session, binds it.
    first = make_event('dx:gid:950', external_id='7600000000000000031', sender='1003', text='你好')
    assert ingest(1, first, store=store).disposition == 'command'
    outbox.enqueue(1, 'dx:gid:950', '周报', idempotency_key='d2', store=store,
                   origin={'kind': 'tool', 'conversationId': 'router-conv', 'turnId': 'd2'})
    outbox.execute(1, adapter, channel='dx', store=store)
    assert ('dx:gid:950', '周报') in robot_posts(platform)


# ── 4. a stale or wrong binding never lets the robot post ──

@pytest.mark.parametrize('stale', ['identity_changed', 'session_is_someone_else', 'chat_changed'])
def test_a_stale_or_wrong_binding_falls_back_to_the_owners_own_account(
        servers, platform, clock, robot_module, stale):
    from lib.channels import service
    alice = servers(ALICE, SHARED_GROUP)
    alice.ingest(owner_message(alice, SHARED_GROUP, '7600000000000000041', '在吗'))
    alice.drain()
    before = len(robot_posts(platform))
    assert before == 1, 'bound by her own message: the receipt went out under the robot'
    if stale == 'identity_changed':
        alice.store.update_account('dx', lambda account: {
            **account, 'identity': {'uid': '1009', 'mis': 'alice01', 'name': 'someone else'}})
        alice.adapter._self_uid = '1009'
    elif stale == 'session_is_someone_else':
        alice.adapter._self_uid = '1009'
    else:
        service.update_account(alice.owner, {'commandChat': 'dx:gid:901'}, store=alice.store)
    alice.enqueue('机密进展', 's1')
    alice.drain()
    assert len(robot_posts(platform)) == before, 'the robot posts nothing under a stale binding'
    assert '机密进展' in [text for _, text in alice.own_sends()]
    expected = 'robot_unbound' if stale == 'chat_changed' else 'robot_binding_stale'
    assert alice.robot_error()['code'] == expected
    notice, = alice.notices()
    assert expected in notice.text


# ── 5. a revoked shared key: every server falls back to its own owner ──

def test_a_revoked_shared_key_falls_back_on_every_server_to_its_own_owner(
        servers, platform, clock, robot_module):
    alice, bob = servers(ALICE), servers(BOB)
    alice_chat = create_chat(alice, platform, clock, robot_module)
    bob_chat = create_chat(bob, platform, clock, robot_module)
    assert alice_chat != bob_chat
    platform.secret = 'rotated-in-the-console'  # the application secret was reset: tokens are refused
    platform.valid.clear()
    alice.enqueue('alice 的进展', 'a1')
    bob.enqueue('bob 的进展', 'b1')
    alice.drain()
    bob.drain()
    assert robot_posts(platform) == []
    assert alice.own_sends()[0] == (alice_chat, 'alice 的进展')
    assert bob.own_sends()[0] == (bob_chat, 'bob 的进展')
    assert {thread for thread, _ in alice.own_sends()} == {alice_chat}
    assert {thread for thread, _ in bob.own_sends()} == {bob_chat}
    assert alice.robot_error()['code'] == bob.robot_error()['code'] == 'token_rejected'
    alice.drain()
    bob.drain()
    for server, chat in ((alice, alice_chat), (bob, bob_chat)):
        told = [(thread, text) for thread, text in server.own_sends() if 'token_rejected' in text]
        assert [thread for thread, _ in told] == [chat], 'one notice, to its own owner only'
        assert server.notices() == []
    assert SECRET not in json.dumps(alice.store.account('dx')) + json.dumps(bob.store.account('dx'))


# ── 6. the shared quota: fall back at once, back off one documented window ──

def test_the_shared_rate_limit_falls_back_at_once_and_backs_off_one_window(
        servers, platform, clock, robot_module):
    alice = servers(ALICE)
    alices_chat = create_chat(alice, platform, clock, robot_module)
    platform.send_codes = [10003]  # other servers used this second's quota of the application
    first = alice.enqueue('第一条', 'q1')
    alice.drain()
    stored, _ = alice.store.outbox_item(first.id)
    assert stored.state == 'sent', 'delivered now as the owner, not parked for a retry'
    assert alice.own_sends() == [(alices_chat, '第一条')]
    assert alice.robot_error() is None and alice.notices() == [], 'a busy quota is not a broken robot'
    sends_before = len(platform.sends())
    alice.enqueue('第二条', 'q2')
    alice.drain()
    assert len(platform.sends()) == sends_before, 'no robot request inside the throttled window'
    assert alice.own_sends()[-1] == (alices_chat, '第二条')
    clock.monotonic += 1.0  # dxRobotSendsPerSecond counts one-second windows
    alice.enqueue('第三条', 'q3')
    alice.drain()
    assert robot_posts(platform) == [(alices_chat, '第三条')]


def test_the_documented_window_is_the_contract_rate_unit(robot_module):
    from lib.channels.model import PROVIDER_LIMITS
    assert robot_module.SEND_INTERVAL_S * PROVIDER_LIMITS['dxRobotSendsPerSecond'] == pytest.approx(
        robot_module.RATE_WINDOW_S)


def test_the_robot_only_sends_and_creates_its_owners_group():
    """Least privilege: the shared key is used for no read, member or event API (docs/CHANNELS.md C6)."""
    contract = json.loads((REPO / 'contracts' / 'channels_v1.schema.json').read_text(encoding='utf-8'))
    paths = {value for key, value in contract['x-dxRobot'].items() if key.endswith('Path')}
    assert paths == {'/open-apis/dx-msg/sendGroupMsgByRobot', '/open-apis/dx-group/createGroup'}
