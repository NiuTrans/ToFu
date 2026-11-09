"""大象 adapter: normalization, catch-up replay, live ingest, sender port (C1, C6)."""
import json
import os
import queue
import sys
import threading

import pytest

from lib.channels.dx.adapter import (
    DxAdapter, DxBridge, event_from_history, event_from_live, svr_time_from_mid, thread_from_session,
)
from lib.channels.model import OUTBOX_MARKER_FIELD, PROVIDER_LIMITS, ThreadKey
from lib.storage.errors import StorageError
from tests.channels.conftest import OWNER, OWNER_UID, ROUTER

pytestmark = pytest.mark.unit

EPOCH = PROVIDER_LIMITS['dxMidEpochMs']
GROUP = ThreadKey.parse('dx:gid:500')
PRIVATE = ThreadKey.parse('dx:uid:2002')
CONNECTED = {'type': 'status', 'status': 'connected', 'selfUid': OWNER_UID}
# The engine's failure shapes as the bridge relays them (11/07-11/08 logs): every engine HTTP
# request is aborted after 15 s (dx-engine http-client.js), and the abort surfaces as
# session_list_failed (api/sessions.js) or browserless_runtime_error (handler.js).
ABORTED = 'This operation was aborted'
SESSIONS_ABORTED = {'ok': False, 'error': {'code': 'session_list_failed', 'message': ABORTED}}
HISTORY_ABORTED = {'ok': False, 'error': {'code': 'browserless_runtime_error', 'message': ABORTED}}
# The refusal once 大象 rejects the bridge's launch-time SSO ticket while minting an IM token for an HTTP
# read (production 2026-11-09 from 06:14; dx-engine credential-provider.js generateImToken).
TICKET_EXPIRED = {'ok': False, 'error': {'code': 'browserless_runtime_error',
                                         'message': 'DX IM token generation failed: rescode=92002'}}
TICKET_EXPIRED_DETAIL = ('大象 SSO ticket expired (browserless_runtime_error: DX IM token generation failed: '
                         'rescode=92002); the next tick relaunches the bridge for a fresh exchange')


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
        self.refusal = None
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


class FlakyBridge(FakeBridge):
    """Answers the first request of each op in ``failures`` with that recorded engine failure."""

    def __init__(self, sessions, history, failures):
        super().__init__(sessions, history)
        self.failures = dict(failures)

    def request(self, op, **args):
        if op in self.failures:
            self.requests.append((op, args))
            return self.failures.pop(op)
        return super().request(op, **args)


def push(mid, text, gid='500'):
    return {'type': 'message', 'source': 'im', 'message': {
        'mid': mid, 'from': '2002', 'gid': gid, 'message': {'text': text}}}


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


def _drain(adapter, bridge, *items):
    """Process more queued items (and whatever ``tick`` queued) after ``_run`` ended the ingest thread."""
    for item in items:
        bridge.events.put(item)
    bridge.events.put(None)
    adapter._process()


def _routed(dispatch):
    return [call['message']['content'] for call in dispatch.to(ROUTER)]


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


def test_same_bridge_reconnect_replays_gap_without_duplicate_delivery(channel, dispatch):
    first = mid_at(1_761_000_002_000)
    gap = mid_at(1_761_000_003_000)
    bridge = FakeBridge([{'type': 'group', 'id': '500', 'name': 'Team'}],
                        {'dx:gid:500': [history_row(first, text='before drop')]})
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: bridge)
    adapter._bridge = bridge
    connected = {'type': 'status', 'status': 'connected', 'selfUid': OWNER_UID}
    adapter._on_status(connected)
    adapter._on_status({'type': 'status', 'status': 'disconnected', 'detail': 'status 8 onClose'})
    assert not adapter._connected.is_set()
    bridge.history['dx:gid:500'].append(history_row(gap, text='during gap'))
    adapter._on_status(connected)
    adapter._on_status(connected)  # a repeated ready notification cannot redeliver
    contents = [call['message']['content'] for call in dispatch.to(ROUTER)]
    assert len(contents) == 2
    assert 'before drop' in contents[0] and 'during gap' in contents[1]
    assert 'before drop' not in contents[1]
    assert channel.thread(GROUP)['watermark'] == gap
    assert adapter._connected.is_set() and bridge.alive()


def test_owner_messages_this_hosts_engine_sent_are_echoes_never_commands(store, enabled, router, dispatch):
    """A dx-pura-api send from another process of this server (the bridge marks rows the
    engine's own sent-message cache holds as ``hostSent``) is Tofu's own send: in the command
    chat it is neither human input nor an approval, replayed or live; the owner's own message
    between them still is (2026-11-08: 19 CLI sends replayed as owner steers)."""
    command_chat = ThreadKey.parse('dx:gid:900')
    enabled(command_chat=str(command_chat))
    store.update_account('dx', lambda account: {**account, 'frontierAtMs': 1_761_000_000_000})
    store.update_thread(command_chat, lambda value: {**value, 'watermark': mid_at(1_761_000_000_500)})
    cli_intro = history_row(mid_at(1_761_000_001_000), sender=OWNER_UID, uuid='n3nyn3a2r',
                            text='HIFL report as requested, files follow')
    cli_intro['hostSent'] = True
    owner_question = history_row(mid_at(1_761_000_002_000), sender=OWNER_UID, uuid='phone-uuid',
                                 text='why was my account used for the files?')
    bridge = FakeBridge([{'type': 'group', 'id': '900', 'name': 'Tofu Router',
                          'lastMessageServerTime': 1_761_000_002_000}],
                        {str(command_chat): [owner_question, cli_intro]})
    adapter = DxAdapter(OWNER, store=store, bridge_factory=lambda owner: bridge)
    adapter.start()
    cli_image = {'type': 'message', 'source': 'im', 'hostSent': True, 'message': {
        'mid': mid_at(1_761_000_003_000), 'uuid': 'n3uEiunpc', 'from': OWNER_UID, 'gid': '900',
        'message': {'text': 'preview 01'}}}
    _run(adapter, bridge, CONNECTED, cli_image)
    adapter.stop(1)
    routed = ' '.join(_routed(dispatch))
    assert 'why was my account used for the files?' in routed
    assert 'HIFL report as requested' not in routed and 'preview 01' not in routed


def test_first_connection_starts_at_the_frontier_without_history(store, enabled, router, dispatch):
    enabled()
    store.update_account('dx', lambda account: {**account, 'frontierAtMs': None})
    bridge = FakeBridge([], {})
    adapter = DxAdapter(OWNER, store=store, bridge_factory=lambda owner: bridge)
    adapter.start()
    _run(adapter, bridge, {'type': 'status', 'status': 'connected', 'selfUid': OWNER_UID})
    adapter.stop(1)
    assert bridge.requests == [] and store.account('dx')['frontierAtMs']


@pytest.mark.parametrize('failures, code', [({'sessions': SESSIONS_ABORTED}, 'sessions_failed'),
                                            ({'history': HISTORY_ABORTED}, 'history_failed')])
def test_a_failed_catch_up_keeps_the_live_session_and_the_next_tick_replays_the_gap(
        channel, dispatch, failures, code):
    gap, live = mid_at(1_761_000_003_000), mid_at(1_761_000_004_000)
    bridge = FlakyBridge([{'type': 'group', 'id': '500', 'name': 'Team'}],
                         {'dx:gid:500': [history_row(gap, text='sent while down')]}, failures)
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: bridge)
    adapter.start()
    try:
        _run(adapter, bridge, CONNECTED, push(live, 'live push'))
        # The engine failure neither ended the connection nor held the live push back.
        assert bridge.alive()
        assert len(_routed(dispatch)) == 1 and 'live push' in _routed(dispatch)[0]
        status = channel.account('dx')['status']
        assert status['state'] == 'connected'
        assert code in status['detail'] and ABORTED in status['detail']
        # The watermark stays below the unreplayed gap, so the replay cannot skip it.
        assert channel.thread(GROUP)['watermark'] == mid_at(1_761_000_001_000)
        before = channel.account('dx')['frontierAtMs']
        bridge.history['dx:gid:500'].append(history_row(live, text='live push'))
        requests = len(bridge.requests)
        adapter.tick()
        adapter.tick()  # one retry is queued per tick at most
        _drain(adapter, bridge)
        assert [op for op, _ in bridge.requests[requests:]].count('sessions') == 1
        contents = _routed(dispatch)
        assert len(contents) == 2 and 'sent while down' in contents[1] and 'live push' not in contents[1]
        assert channel.thread(GROUP)['watermark'] == live
        assert channel.account('dx')['frontierAtMs'] > before
        assert channel.account('dx')['status']['state'] == 'connected'
        assert channel.account('dx')['status']['detail'] == ''
        # Nothing is owed any more: a later tick asks the engine for nothing.
        requests = len(bridge.requests)
        adapter.tick()
        _drain(adapter, bridge)
        assert len(bridge.requests) == requests
    finally:
        adapter.stop(1)


def engine_sessions(args, im_sessions, pub_sessions, im_failed):
    """dx-engine get_session_list (api/sessions.js): a rejected chatlist source fails the call
    only when the request selected its types; otherwise the other source's rows come back as
    a success that carries the failure only among its warnings."""
    if im_failed and set(args.get('types') or ()) & {'chat', 'group'}:
        return SESSIONS_ABORTED
    rows = pub_sessions + ([] if im_failed else im_sessions)
    if im_failed and not rows:
        return SESSIONS_ABORTED
    return {'ok': True, 'result': {'sessions': rows, 'warnings': [ABORTED] if im_failed else None}}


def test_a_session_list_missing_a_failed_chatlist_does_not_complete_the_catch_up(channel, dispatch):
    # The IM chatlist (chats and groups) timed out while public accounts had activity: the
    # engine answers with the public sessions only, which must not advance the frontier.
    group, notices = {'type': 'group', 'id': '500', 'name': 'Team'}, {'type': 'pub', 'id': '600', 'name': 'News'}
    im_failed = [True]

    class Engine(FakeBridge):
        def request(self, op, **args):
            if op == 'sessions':
                self.requests.append((op, args))
                return engine_sessions(args, [group], [notices], im_failed[0])
            return super().request(op, **args)

    bridge = Engine([], {'dx:gid:500': [history_row(mid_at(1_761_000_003_000), text='sent while down')]})
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: bridge)
    adapter.start()
    try:
        before = channel.account('dx')['frontierAtMs']
        _run(adapter, bridge, CONNECTED)
        assert channel.account('dx')['frontierAtMs'] == before and not _routed(dispatch)
        im_failed[0] = False
        adapter.tick()
        _drain(adapter, bridge)
        assert 'sent while down' in _routed(dispatch)[0]
        assert channel.account('dx')['frontierAtMs'] > before
    finally:
        adapter.stop(1)


def test_a_storage_stall_on_connect_keeps_the_live_session(channel, dispatch, monkeypatch):
    # 11/08 07:15:34: the connected status write timed out while the storage sidecar was down.
    bridge = FakeBridge([], {})
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: bridge)
    write = channel.set_status
    stalls = [StorageError('database_timeout', 'storage.v2 connection budget elapsed')]

    def set_status(*args, **kwargs):
        if stalls:
            raise stalls.pop()
        return write(*args, **kwargs)
    monkeypatch.setattr(channel, 'set_status', set_status)
    adapter.start()
    try:
        _run(adapter, bridge, CONNECTED, push(mid_at(1_761_000_004_000), 'live push'))
        assert bridge.alive() and not stalls
        assert 'live push' in _routed(dispatch)[0]
        assert channel.account('dx')['status']['state'] == 'connected'  # written once storage answered
        assert channel.thread(GROUP)['watermark'] == mid_at(1_761_000_004_000)
    finally:
        adapter.stop(1)


def test_a_failed_live_ingest_keeps_the_session_and_the_catch_up_routes_it_again(channel, dispatch):
    failed, later = mid_at(1_761_000_009_000), mid_at(1_761_000_010_000)
    bridge = FakeBridge([{'type': 'group', 'id': '500', 'name': 'Team'}], {'dx:gid:500': []})
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: bridge)
    adapter.start()
    try:
        dispatch.outcomes[ROUTER] = 'start_failed'
        _run(adapter, bridge, CONNECTED, push(failed, 'first'))
        del dispatch.outcomes[ROUTER]
        _drain(adapter, bridge, push(later, 'second'))
        assert bridge.alive()
        assert 'second' in _routed(dispatch)[-1]
        # Routed at once, but the watermark waits below the event whose route failed.
        assert channel.thread(GROUP)['watermark'] == mid_at(1_761_000_001_000)
        assert 'delivery_failed' in channel.account('dx')['status']['detail']
        bridge.history['dx:gid:500'] += [history_row(failed, text='first'), history_row(later, text='second')]
        adapter.tick()
        _drain(adapter, bridge)
        contents = _routed(dispatch)
        assert 'first' in contents[-1] and 'second' not in contents[-1] and len(contents) == 3
        assert channel.thread(GROUP)['watermark'] == later
    finally:
        adapter.stop(1)


@pytest.mark.parametrize('status, state', [('kicked', 'kicked'), ('disconnected', 'disconnected')])
def test_bridge_status_lines_are_surfaced(channel, status, state):
    bridge = FakeBridge([], {})
    adapter = DxAdapter(OWNER, store=channel, bridge_factory=lambda owner: bridge)
    adapter.start()
    _run(adapter, bridge, {'type': 'status', 'status': status, 'detail': 'x'})
    bridge.returncode = 43 if status == 'kicked' else 3
    assert adapter.outcome()[0] == state
    assert channel.account('dx')['status']['state'] == state


# The SSO adapter's public envelope (tofu_mtsso_auth OfficialAuthError.envelope) and the exit
# code its ``exec`` gives it: 42 when the user must act, otherwise 1.
@pytest.mark.parametrize('code, error_code, recovery, state', [
    (1, 'SSO_EXCHANGE_FAILED', 'check_sso_service', 'disconnected'),
    (1, 'SSO_NODE_UNAVAILABLE', 'repair_agent_configuration', 'runtime_unavailable'),
    (42, 'SSO_SESSION_REQUIRED', 'approve_daxiang_login', 'needs_login'),
])
def test_session_outcome_names_the_launcher_refusal(store, tmp_path, code, error_code, recovery, state):
    # The launcher refuses before the bridge runs: its public envelope on stderr,
    # then (exit 42) dx-tofu.sh's own hint line. A refusal of the SSO step's own
    # runtime is runtime_unavailable: logging in again cannot fix it.
    envelope = {'ok': False, 'error_code': error_code, 'error': 'refused before login',
                'recovery_action': recovery}
    script = ('import json, sys\nprint(json.dumps(%r), file=sys.stderr)\n'
              'print("dx-tofu: hint", file=sys.stderr)\nsys.exit(%d)\n') % (envelope, code)
    bridge = DxBridge([sys.executable, '-c', script], env=dict(os.environ), cwd=str(tmp_path))
    adapter = DxAdapter(OWNER, store=store, bridge_factory=lambda owner: bridge)
    adapter.start()
    assert adapter.stop(5)
    outcome, detail = adapter.outcome()
    assert outcome == state and f'{code}' in detail
    assert detail.endswith(f'{error_code}: refused before login')


@pytest.mark.parametrize('read', ['history', 'catch_up', 'download'])
def test_an_expired_launch_ticket_ends_the_session_for_the_supervisors_relaunch(store, tmp_path, read):
    """2026-11-09: 大象 refused the bridge's launch-time SSO ticket when it minted an IM token (rescode 92002).
    The WebSocket kept sending, but every history read failed for hours, silently. Only the launcher's
    exchange renews that ticket, so the refusal ends the session, and the supervisor's next tick
    relaunches the bridge (C1)."""
    from lib.channels.dx.adapter import FILE_URL_PREFIXES
    from lib.channels.model import ChannelError, ChannelMedia
    bridge = FlakyBridge([], {}, {'sessions' if read == 'catch_up' else read: dict(TICKET_EXPIRED)})
    adapter = DxAdapter(OWNER, store=store, bridge_factory=lambda owner: bridge)
    adapter._bridge = bridge
    adapter._connected.set()
    assert adapter.alive()
    if read == 'history':
        with pytest.raises(ChannelError):
            adapter.history_from(GROUP, mid_at(1_761_000_000_000))
    elif read == 'catch_up':
        store.update_account('dx', lambda account: {**account, 'frontierAtMs': 1})
        with pytest.raises(ChannelError):
            adapter.catch_up()
    else:
        media = ChannelMedia('file', 'report.pdf', source=FILE_URL_PREFIXES[0] + 'report.pdf')
        assert adapter.download_media(media, str(tmp_path / 'report.pdf'))['ok'] is False
    assert not adapter.alive()
    assert adapter.outcome() == ('disconnected', TICKET_EXPIRED_DETAIL)


def test_a_read_that_fails_for_another_reason_keeps_the_session(store):
    """C1: an aborted or refused history read never ends the session. Only the expired launch ticket does."""
    from lib.channels.model import ChannelError
    bridge = FlakyBridge([], {}, {'history': dict(HISTORY_ABORTED)})
    adapter = DxAdapter(OWNER, store=store, bridge_factory=lambda owner: bridge)
    adapter._bridge = bridge
    adapter._connected.set()
    with pytest.raises(ChannelError):
        adapter.history_from(GROUP, mid_at(1_761_000_000_000))
    assert adapter.alive()


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


def test_whoami_binds_the_account_the_launcher_logged_in_as(monkeypatch):
    from lib.channels.dx import adapter as module
    from lib.channels.dx.login import LoginSession
    from lib.channels.model import ChannelError
    answers = iter([{'uid': '3003', 'mis': 'Alice', 'name': 'Alice'}, {'uid': '3003', 'name': 'Alice'},
                    {'uid': '4004', 'mis': 'bob'}])
    calls = []
    monkeypatch.setattr(module, '_one_shot', lambda owner, session, *extra: calls.append((session.mis, extra))
                        or next(answers))
    session = LoginSession('alice', {})
    assert module.resolve_identity(OWNER, session) == {'uid': '3003', 'mis': 'alice', 'name': 'Alice'}
    assert module.resolve_identity(OWNER, session)['mis'] == 'alice'
    with pytest.raises(ChannelError) as raised:
        module.resolve_identity(OWNER, session)
    assert raised.value.code == 'identity_mismatch'
    assert calls == [('alice', ('--command', 'whoami'))] * 3


@pytest.mark.parametrize('exit_code, error_code, recovery, raised_code', [
    (42, 'SSO_LOGIN_NOT_APPROVED', 'approve_daxiang_login', 'needs_login'),
    (1, 'SSO_NODE_RUNTIME_INCOMPATIBLE', 'repair_agent_configuration', 'runtime_unavailable'),
])
def test_a_refused_launch_reports_the_sso_adapter_envelope(monkeypatch, tmp_path, exit_code, error_code,
                                                            recovery, raised_code):
    import subprocess
    from lib.channels.dx import adapter as module
    from lib.channels.dx.login import LoginSession
    from lib.channels.model import ChannelError
    spec = module.LaunchSpec(skill_dir=str(tmp_path), launcher=str(tmp_path / 'dx-tofu.sh'),
                             engine_dir=str(tmp_path / 'engine'))
    monkeypatch.setattr(module, 'launch_spec', lambda owner: spec)
    monkeypatch.setattr(module.LaunchSpec, 'installed', property(lambda self: True))
    envelope = json.dumps({'ok': False, 'error_code': error_code, 'error': '用户未批准登录',
                           'recovery_action': recovery}, ensure_ascii=False)
    seen = {}

    def run(argv, *, env, **options):
        seen['env'] = env
        return subprocess.CompletedProcess(argv, exit_code, b'', f'node noise\n{envelope}\n'.encode())
    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(ChannelError) as raised:
        module.resolve_identity(OWNER, LoginSession('alice', {'MTSSO_MIS': 'alice'}))
    assert raised.value.code == raised_code
    assert str(raised.value) == f'{error_code}: 用户未批准登录'
    assert seen['env'] == {'MTSSO_MIS': 'alice'}
