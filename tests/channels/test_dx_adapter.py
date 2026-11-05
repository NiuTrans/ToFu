"""大象 adapter: normalization, catch-up replay, live ingest, sender port (C1, C6)."""
import json
import queue
import threading

import pytest

from lib.channels.dx.adapter import (
    DxAdapter, event_from_history, event_from_live, svr_time_from_mid, thread_from_session,
)
from lib.channels.model import OUTBOX_MARKER_FIELD, PROVIDER_LIMITS, ThreadKey
from tests.channels.conftest import OWNER, OWNER_UID, ROUTER

pytestmark = pytest.mark.unit

EPOCH = PROVIDER_LIMITS['dxMidEpochMs']
GROUP = ThreadKey.parse('dx:gid:500')
PRIVATE = ThreadKey.parse('dx:uid:2002')


def mid_at(ms: int, seq: int = 0) -> str:
    return str(((ms - EPOCH) << PROVIDER_LIMITS['dxMidTimestampShift']) + seq)


def history_row(mid, *, sender='2002', text='t', extension=None, uuid='', quoted=None):
    raw = {'mid': mid, 'uuid': uuid, 'from': sender, 'fromName': f'user{sender}', 'cts': 1, 'type': 1,
           'message': {'text': text}, 'extension': json.dumps(extension or {})}
    if quoted:
        raw['type'] = 20
        raw['quotedMessage'] = [{'message': quoted}]
    return {'id': mid, 'text': text, 'raw': raw}


def test_snowflake_server_time():
    assert svr_time_from_mid(mid_at(1_761_000_000_123, 5)) == 1_761_000_000_123
    assert svr_time_from_mid('12345') is None


def test_live_group_private_and_public_messages():
    group = event_from_live({'mid': mid_at(1_761_000_000_000), 'uuid': 'u1', 'from': '2002',
                             'fromName': 'Ann', 'gid': '500', 'groupName': 'Team', 'type': 1,
                             'message': {'text': 'hi [@Owner|mtdaxiang://www.internal.example.com/profile?uid=1001&isAt=true]'},
                             'extension': json.dumps({'at': ['3003']})},
                            source='im', self_uid=OWNER_UID)
    assert str(group.thread) == 'dx:gid:500' and group.thread_title == 'Team'
    assert set(group.mentioned_ids) == {'1001', '3003'} and group.sent_at_ms == 1_761_000_000_000
    mine = event_from_live({'mid': mid_at(1_761_000_000_001), 'from': OWNER_UID, 'to': '2002',
                            'message': {'text': 'yo'},
                            'extension': json.dumps({OUTBOX_MARKER_FIELD: 'ob1'})},
                           source='im', self_uid=OWNER_UID, own_connection=True)
    assert str(mine.thread) == 'dx:uid:2002' and mine.outbox_marker == 'ob1' and mine.own_connection
    theirs = event_from_live({'mid': mid_at(1_761_000_000_002), 'from': '2002', 'to': OWNER_UID,
                              'fromName': 'Ann', 'message': {'text': 'q'}}, source='im', self_uid=OWNER_UID)
    assert str(theirs.thread) == 'dx:uid:2002' and theirs.thread_title == 'Ann'
    pub = event_from_live({'mid': mid_at(1_761_000_000_003), 'from': '9009', 'pubUid': '9009',
                           'message': {'text': 'notice'}}, source='pub', self_uid=OWNER_UID)
    assert str(pub.thread) == 'dx:pid:9009'
    quote = event_from_live({'mid': mid_at(1_761_000_000_004), 'from': '2002', 'gid': '500', 'type': 20,
                             'message': {'searchText': 'agree'}}, source='im', self_uid=OWNER_UID,
                            quotes=[{'mid': '7000000000000000001', 'from': OWNER_UID}])
    assert quote.reply_to_sender_id == OWNER_UID and quote.text == 'agree'
    assert event_from_live({'mid': 'x', 'from': '1'}, source='im', self_uid=OWNER_UID) is None
    assert event_from_live({'mid': mid_at(1_761_000_000_005), 'from': '2', 'gid': '500', 'isCancel': True},
                           source='im', self_uid=OWNER_UID) is None


def test_history_rows_keep_sender_markers_and_quote_facts():
    row = history_row(mid_at(1_761_000_000_000), sender=OWNER_UID, uuid='uu',
                      extension={OUTBOX_MARKER_FIELD: 'ob9', 'atAll': True},
                      quoted={'mid': '7000000000000000002', 'fromUid': '2002'})
    event = event_from_history(row, GROUP, title='Team')
    assert (event.sender_id, event.client_id, event.outbox_marker) == (OWNER_UID, 'uu', 'ob9')
    assert event.mentions_all and event.reply_to_sender_id == '2002' and event.thread_title == 'Team'
    assert thread_from_session({'type': 'chat', 'id': '2002'}) == PRIVATE
    assert thread_from_session({'type': 'group', 'id': 'abc'}) is None


def provider_history(history, args):
    """The 大象 range query as observed live: ``cursor`` is an exclusive upper
    bound, the newest ``limit`` rows below it are returned oldest-first for
    groups/private chats and newest-first for public accounts, and
    ``nextCursor`` is the returned page's last row."""
    kind = next(k for k in ('gid', 'uid', 'pid') if k in args)
    rows = sorted(history.get(f'dx:{kind}:{args[kind]}', []), key=lambda row: int(row['id']))
    bound = int(args['cursor']) if args.get('cursor') else None
    below = [row for row in rows if bound is None or int(row['id']) < bound]
    page = below[-args['limit']:]
    if kind == 'pid':
        page = list(reversed(page))
    return {'ok': True, 'result': {'messages': page, 'nextCursor': page[-1]['id'] if page else ''}}


class FakeBridge:
    """Scripted bridge: ordered event queue plus request handlers."""

    def __init__(self, sessions, history):
        self.events = queue.Queue()
        self.sessions, self.history = sessions, history
        self.requests = []
        self.returncode = None
        self.sends = []

    def request(self, op, **args):
        self.requests.append((op, args))
        if op == 'sessions':
            return {'ok': True, 'result': {'sessions': self.sessions}}
        if op == 'history':
            return provider_history(self.history, args)
        if op in ('send_markdown', 'reply_message'):
            self.sends.append((op, args))
            return {'ok': True, 'result': {'serverMsgId': '7400000000000000001', 'msgUuid': 'uu-1'}}
        raise AssertionError(op)

    def alive(self):
        return self.returncode is None

    def terminate(self):
        self.returncode = -15
        self.events.put(None)

    def close(self, timeout):
        self.returncode = 0
        return True


@pytest.fixture
def channel(store, enabled, router, dispatch, mailbox):
    enabled(command_chat='dx:gid:900')
    store.update_account('dx', lambda account: {**account, 'frontierAtMs': 1_761_000_000_000})
    store.update_thread(GROUP, lambda value: {**value, 'mode': 'watch',
                                              'watermark': mid_at(1_761_000_001_000)})
    return store


def _run(adapter, bridge, *items):
    for item in items:
        bridge.events.put(item)
    bridge.events.put(None)
    adapter._threads[0].join(5)
    assert not adapter._threads[0].is_alive()


def test_reconnect_catch_up_replays_after_watermarks_and_frontier(channel, dispatch, mailbox, monkeypatch):
    w = [mid_at(1_761_000_000_500), mid_at(1_761_000_001_000), mid_at(1_761_000_002_000),
         mid_at(1_761_000_003_000), mid_at(1_761_000_004_000)]
    sessions = [{'type': 'group', 'id': '500', 'name': 'Team', 'lastMessageServerTime': 1_761_000_004_000},
                {'type': 'chat', 'id': '2002', 'name': 'Ann', 'lastMessageServerTime': 1_761_000_002_500},
                {'type': 'group', 'id': '777', 'name': 'Noisy', 'lastMessageServerTime': 1_761_000_003_500}]
    history = {
        'dx:gid:500': [history_row(w[4], text='w4'), history_row(w[3], text='w3'),
                       history_row(w[2], text='w2'), history_row(w[1], text='w1'), history_row(w[0])],
        'dx:uid:2002': [history_row(mid_at(1_761_000_002_500), text='late dm'),
                        history_row(mid_at(1_760_999_999_000), text='before frontier')],
    }
    monkeypatch.setattr('lib.channels.dx.adapter.HISTORY_PAGE_LIMIT', 2)  # several pages
    bridge = FakeBridge(sessions, history)
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: bridge)
    adapter.start()
    live_new = {'type': 'message', 'source': 'im', 'message': {
        'mid': mid_at(1_761_000_005_000), 'from': '2002', 'gid': '500', 'message': {'text': 'live'}}}
    live_dup = {'type': 'message', 'source': 'im', 'message': {
        'mid': w[4], 'from': '2002', 'gid': '500', 'message': {'text': 'w4'}}}
    _run(adapter, bridge, {'type': 'status', 'status': 'connected', 'selfUid': OWNER_UID}, live_dup, live_new)
    adapter.stop(1)
    contents = [call['message']['content'] for call in dispatch.to(ROUTER)]
    replay, live = contents
    assert all(text in replay for text in ('w2', 'w3', 'w4', 'late dm'))
    assert 'w1' not in replay and 'before frontier' not in replay
    assert 'live' in live and len(contents) == 2  # the live duplicate of w4 was absorbed
    # The ignored group kept metadata only; history was never fetched for it.
    assert channel.thread(ThreadKey.parse('dx:gid:777'))['title'] == 'Noisy'
    assert not any(op == 'history' and args.get('gid') == '777' for op, args in bridge.requests)
    # Paging stopped at the watermark page instead of walking the whole history.
    assert len([1 for op, args in bridge.requests if op == 'history' and args.get('gid') == '500']) == 2
    assert channel.thread(GROUP)['watermark'] == mid_at(1_761_000_005_000)
    assert channel.account('dx')['frontierAtMs'] >= 1_761_000_005_000
    assert channel.account('dx')['status']['state'] == 'connected'


def test_first_connection_starts_at_the_frontier_without_history(store, enabled, router, dispatch):
    enabled()
    store.update_account('dx', lambda account: {**account, 'frontierAtMs': None})
    bridge = FakeBridge([], {})
    adapter = DxAdapter(OWNER, store=store, bridge_factory=lambda owner: bridge)
    adapter.start()
    _run(adapter, bridge, {'type': 'status', 'status': 'connected', 'selfUid': OWNER_UID})
    adapter.stop(1)
    assert bridge.requests == [] and store.account('dx')['frontierAtMs']


def test_ingest_failure_halts_the_session_before_the_watermark_moves(channel, dispatch):
    dispatch.outcomes[ROUTER] = 'start_failed'
    bridge = FakeBridge([], {})
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: bridge)
    adapter.start()
    channel.update_account('dx', lambda account: {**account, 'frontierAtMs': None})
    event = {'type': 'message', 'source': 'im', 'message': {
        'mid': mid_at(1_761_000_009_000), 'from': '2002', 'gid': '500', 'message': {'text': 'x'}}}
    _run(adapter, bridge, {'type': 'status', 'status': 'connected', 'selfUid': OWNER_UID}, event)
    assert bridge.returncode == -15  # terminated: the supervisor restarts it at the next tick
    assert adapter.outcome()[0] == 'error'
    assert channel.thread(GROUP)['watermark'] == mid_at(1_761_000_001_000)
    assert channel.account('dx')['status']['state'] == 'error'


@pytest.mark.parametrize('status, state', [('kicked', 'kicked'), ('disconnected', 'disconnected')])
def test_bridge_status_lines_are_surfaced(channel, status, state):
    bridge = FakeBridge([], {})
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: bridge)
    adapter.start()
    _run(adapter, bridge, {'type': 'status', 'status': status, 'detail': 'x'})
    bridge.returncode = 43 if status == 'kicked' else 3
    assert adapter.outcome()[0] == state
    assert channel.account('dx')['status']['state'] == state


def test_sender_port_sends_markdown_with_the_outbox_marker(channel):
    bridge = FakeBridge([], {'dx:gid:900': [history_row(mid_at(1_761_000_000_000), sender=OWNER_UID,
                                                       extension={OUTBOX_MARKER_FIELD: 'ob1'})]})
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: bridge)
    adapter._bridge = bridge
    adapter._connected.set()
    result = adapter.send(ThreadKey.parse('dx:gid:900'), 'hello', marker='ob1', reply_to_external_id=None)
    assert result == {'ok': True, 'externalId': '7400000000000000001', 'clientId': 'uu-1', 'error': None}
    op, args = bridge.sends[0]
    assert op == 'send_markdown' and json.loads(args['extension'])[OUTBOX_MARKER_FIELD] == 'ob1'
    adapter.send(ThreadKey.parse('dx:gid:900'), 'q', marker='ob2', reply_to_external_id='7000000000000000001')
    assert bridge.sends[1][0] == 'reply_message' and bridge.sends[1][1]['replyTo'] == {'mid': '7000000000000000001'}
    found = adapter.history_since(ThreadKey.parse('dx:gid:900'), 1_760_000_000_000)
    assert [event.outbox_marker for event in found] == ['ob1']


def test_outbox_executor_wakes_on_kick_and_sends_once(channel, router, dispatch):
    from lib.channels import outbox
    bridge = FakeBridge([], {})
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: bridge)
    adapter.start()
    bridge.events.put({'type': 'status', 'status': 'connected', 'selfUid': OWNER_UID})
    deadline = threading.Event()
    item = outbox.enqueue(OWNER, 'dx:gid:900', 'answer', idempotency_key='turn-1',
                          origin={'kind': 'router_reply', 'conversationId': ROUTER, 'turnId': 'turn-1'},
                          store=channel)
    for _ in range(100):
        if channel.outbox_item(item.id)[0].state == 'sent':
            break
        deadline.wait(0.05)
    adapter.stop(1)
    assert channel.outbox_item(item.id)[0].state == 'sent'
    assert [op for op, _ in bridge.sends] == ['send_markdown']


@pytest.mark.parametrize('kind', ['gid', 'uid', 'pid'])
def test_history_pages_walk_newest_to_oldest_whatever_the_provider_row_order(kind, monkeypatch):
    from lib.channels.dx.adapter import history_pages
    monkeypatch.setattr('lib.channels.dx.adapter.HISTORY_PAGE_LIMIT', 3)
    thread = ThreadKey('dx', kind, '42')
    mids = [mid_at(1_761_000_000_000 + step * 1000) for step in range(8)]
    bridge = FakeBridge([], {str(thread): [history_row(mid) for mid in mids]})
    pages = [[row['id'] for row in page] for page in history_pages(bridge, thread)]
    assert [sorted(page, key=int) for page in pages] == [mids[5:], mids[2:5], mids[:2]]
    assert sum(len(page) for page in pages) == len(set(mids))  # every row exactly once
    assert len([op for op, _ in bridge.requests if op == 'history']) == 4  # last call ends the walk


def test_history_pages_stop_at_the_provider_end_marker():
    from lib.channels.dx.adapter import history_pages

    class EndMarker(FakeBridge):
        def request(self, op, **args):
            self.requests.append((op, args))
            return {'ok': True, 'result': {'messages': [history_row(mid_at(1_761_000_000_000))],
                                           'nextCursor': '-1'}}
    bridge = EndMarker([], {})
    assert len(list(history_pages(bridge, GROUP))) == 1 and len(bridge.requests) == 1


def test_live_pushes_never_advance_the_catch_up_frontier(channel, dispatch, monkeypatch):
    """Pushes of different threads can arrive out of server-time order: only a
    completed catch-up moves the frontier, to its own start time."""
    monkeypatch.setattr('lib.channels.dx.adapter.now_ms', lambda: 1_761_000_100_000)
    bridge = FakeBridge([], {})
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: bridge)
    adapter.start()
    future_push = {'type': 'message', 'source': 'im', 'message': {
        'mid': mid_at(1_761_000_900_000), 'from': '2002', 'gid': '500', 'message': {'text': 'x'}}}
    _run(adapter, bridge, {'type': 'status', 'status': 'connected', 'selfUid': OWNER_UID}, future_push)
    adapter.tick()
    adapter.stop(1)
    assert channel.account('dx')['frontierAtMs'] == 1_761_000_100_000
