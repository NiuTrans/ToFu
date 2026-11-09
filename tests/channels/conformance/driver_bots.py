"""Bot platform conformance drivers: 企业微信, QQ and 微信 sessions behind the shim (C12).

Each driver runs today's ``BotSession`` with its real transport against the
platform fake in ``tests/channels/bot_fakes.py`` (real local WebSocket/HTTP
servers), extended here with the behaviour the cases need: deliveries recorded
per chat, a lost answer after delivery, the platforms' documented rate-limit
refusals, QQ's documented deduplication of a passive reply by ``msg_seq``
(40054005), 企业微信's ``disconnected_event``, iLink's session expiry (-14).
The session runs its transport thread only; its outbox runs through the shim,
and restarts go through the real bot supervisor (``reconcile_owner``) as at a
scheduler tick. ``settle`` pushes a message into an ignored group and waits
until the session handed it to ingress: everything pushed before it is done.
"""
from __future__ import annotations

import itertools
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

from lib.channels.bots import ids
from lib.channels.bots.session import BotSession, _Sender
from lib.channels.model import ThreadKey
from lib.channels.store import now_ms
from tests.channels.bot_fakes import (
    OTHER_ID, OWNER_ID, QQ_APP_ID, QQ_SECRET, WECHAT_TOKEN, WECOM_BOT_ID, WECOM_SECRET, FakeILink, FakeQQ, FakeWeCom,
    ilink_message, qq_c2c, qq_group, wecom_text,
)
from tests.channels.conftest import OWNER
from tests.channels.conformance.harness import Driver, Msg, NotApplicable, Shim, advance_core_clock, eventually

SYNC_GROUP = 'conformance-sync'


class RunOnlySession(BotSession):
    """A bot session whose outbox runs only through the conformance shim (no executor thread)."""

    def start(self) -> None:
        self.transport()
        thread = threading.Thread(target=self._run, name=f'{self.channel}-conformance-run', daemon=True)
        self._threads.append(thread)
        thread.start()


class BotDriver(Driver):
    secret = ''
    credential = ''
    edits_reason = 'the platform delivers no edits'

    def setup(self) -> None:
        from lib.channels.bots import supervisor
        monkeypatch = self.env.monkeypatch
        monkeypatch.setattr(supervisor, '_SESSIONS', {})
        monkeypatch.setattr(supervisor, '_HOSTING', threading.Event())
        self.supervisor = supervisor
        self.platform = self.make_platform()
        self.credential = f'channel.u{OWNER}.{self.channel}.' + ('token' if self.channel == 'wechat' else 'secret')
        self.env.vault.set_entry(self.credential, self.secret)
        self.command = ids.thread_key(self.channel, 'uid', OWNER_ID)
        self.other = ids.thread_key(self.channel, 'uid', OTHER_ID)
        self.store.update_account(self.channel, lambda account: {
            **account, 'enabled': True, 'appId': self.app_id(), 'frontierAtMs': 1,
            'credential': {'name': self.credential, 'managed': True},
            'identity': {'uid': OWNER_ID, 'name': 'Owner'}, 'commandChat': str(self.command),
            **self.account_extra()})
        self.seen: set[str] = set()
        self.starts: list = []
        self._ids = itertools.count(1)
        self.shim = Shim(self.env, self.channel, sender_source=lambda: _Sender(self.session))
        self.host = self.shim.host
        self.session = self._session()
        self.session.start()
        supervisor._SESSIONS[(OWNER, self.channel)] = self.session

    # platform specifics ---------------------------------------------------
    def make_platform(self):
        raise NotImplementedError

    def make_transport(self):
        raise NotImplementedError

    def app_id(self) -> str:
        return ''

    def account_extra(self) -> dict:
        return {}

    def push(self, message) -> None:
        raise NotImplementedError

    def message(self, peer: str, text: str, *, sender: str, seconds_ago: int = 0, group: str = ''):
        """A platform frame for one user message (not yet pushed) and its message ID."""
        raise NotImplementedError

    # session lifecycle ----------------------------------------------------
    def _session(self) -> RunOnlySession:
        session = RunOnlySession(OWNER, self.channel, store=self.store, transport=self.make_transport())
        session.connected_event = threading.Event()
        deliver, connected = session.deliver, session.connected

        def tracking_deliver(batch, checkpoint=None):
            deliver(batch, checkpoint)
            self.seen.update(item.message_id for item in batch)

        def tracking_connected(info):
            connected(info)
            session.connected_event.set()
        session.deliver, session.connected = tracking_deliver, tracking_connected
        return session

    def _factory(self, owner, channel, *, store):
        self.starts.append(channel)
        self.session = self._session()
        return self.session

    def _tick(self) -> str:
        return self.supervisor.reconcile_owner(OWNER, self.channel, store=self.store, session_factory=self._factory,
                                               stop_timeout=1.0)

    def _ended(self) -> None:
        eventually(lambda: not self.session.alive(), what=f'the {self.channel} session ending')

    def teardown(self) -> None:
        for session in [self.session, *self.supervisor._SESSIONS.values()]:
            session.stop(1.0)
        self.platform.close()

    def connect(self) -> None:
        eventually(lambda: self.session.connected_event.is_set(), what=f'the {self.channel} session connecting')

    def settle(self) -> None:
        if not self.session.alive():
            return
        frame, message_id = self.message(SYNC_GROUP, 'sync', sender=OTHER_ID, group=SYNC_GROUP)
        self.push(frame)
        eventually(lambda: message_id in self.seen or not self.session.alive(), what='the session delivering')

    def alive(self) -> bool:
        return self.session.alive()

    def command_thread(self) -> ThreadKey:
        return self.command

    def watched_thread(self) -> ThreadKey:
        return self.other

    def prepare_send(self, thread: ThreadKey) -> None:
        """The owner writes first (the chat's reply context); its receipt goes out before the case's own item."""
        self.post(thread, 'hi ' + uuid.uuid4().hex[:6], sender=OWNER_ID)
        self.settle()
        self.execute()

    def execute(self) -> None:
        if not self.session.alive():
            self._tick()
            self.connect()
        super().execute()

    # inbound -----------------------------------------------------------
    def post(self, thread: ThreadKey, text: str, *, sender: str = OTHER_ID) -> Msg:
        frame, message_id = self.message(ids.peer(thread), text, sender=sender)
        self.push(frame)
        return Msg(thread, text, message_id, {'frame': frame})

    def redeliver(self, msg: Msg) -> None:
        self.push(msg.extra['frame'])

    def pair(self, thread: ThreadKey) -> tuple[Msg, Msg]:
        pairs = []
        for label, ago in (('older', 30), ('newer', 0)):
            text = f'{label} {uuid.uuid4().hex[:6]}'
            frame, message_id = self.message(ids.peer(thread), text, sender=OTHER_ID, seconds_ago=ago)
            pairs.append(Msg(thread, text, message_id, {'frame': frame}))
        return pairs[0], pairs[1]

    def deliver(self, msg: Msg) -> None:
        self.push(msg.extra['frame'])

    def edit(self, msg: Msg, text: str) -> None:
        raise NotApplicable(self.edits_reason)

    def recall(self, msg: Msg) -> None:
        raise NotApplicable(self.edits_reason)

    # outbound ----------------------------------------------------------
    def deliver_without_receipt(self, item) -> None:
        self.claim(item)
        _Sender(self.session).send(ThreadKey.parse(item.thread), item.text, marker=item.id, reply_to_external_id=None)

    def restart(self) -> None:
        self.session.stop(1.0)
        self.session = self._session()
        self.session.start()
        self.supervisor._SESSIONS[(OWNER, self.channel)] = self.session
        self.connect()

    def assert_no_restart(self, *, boot: bool = False) -> None:
        if boot:
            self.env.monkeypatch.setattr(self.supervisor, '_SESSIONS', {})
        before = len(self.starts)
        outcome = self._tick()
        assert len(self.starts) == before, f'the supervisor started a session ({outcome})'


# ── 企业微信 ───────────────────────────────────────────────────────────────

class WeComPlatform(FakeWeCom):
    """FakeWeCom that records deliveries and can lose an answer or refuse at its rate limit."""

    def __init__(self):
        self.delivered: list[dict] = []
        self.attempted: list[dict] = []
        self.lose_next = False
        self.rate_limited = False
        super().__init__()

    def _handle(self, ws):
        self.connections.append(ws)
        for raw in ws:
            frame = json.loads(raw)
            self.frames.append(frame)
            cmd, req = frame.get('cmd'), frame.get('headers', {}).get('req_id')
            if cmd == 'aibot_subscribe':
                code = self.subscribe_errcode
                ws.send(json.dumps({'headers': {'req_id': req}, 'errcode': code,
                                    'errmsg': 'ok' if code == 0 else 'invalid secret'}))
            elif cmd == 'ping':
                ws.send(json.dumps({'headers': {'req_id': req}, 'errcode': 0, 'errmsg': 'ok'}))
            elif cmd == 'aibot_send_msg':
                body = frame['body']
                send = {'chatid': body['chatid'], 'content': body['markdown']['content']}
                self.attempted.append(send)
                if self.rate_limited:
                    ws.send(json.dumps({'headers': {'req_id': req}, 'errcode': 45009,
                                        'errmsg': 'api freq out of limit'}))
                    continue
                self.delivered.append(send)
                if self.lose_next:
                    self.lose_next = False
                    ws.close()
                    return
                ws.send(json.dumps({'headers': {'req_id': req}, 'errcode': 0, 'errmsg': 'ok'}))


class WeComDriver(BotDriver):
    name = channel = 'wecom'
    secret = WECOM_SECRET
    edits_reason = '企业微信 智能机器人 callbacks carry no edits or recalls'

    def make_platform(self):
        return WeComPlatform()

    def make_transport(self):
        from lib.channels.wecom.transport import WeComTransport
        return WeComTransport(WECOM_BOT_ID, lambda: WECOM_SECRET, ws_url=self.platform.url)

    def app_id(self) -> str:
        return WECOM_BOT_ID

    def message(self, peer, text, *, sender, seconds_ago=0, group=''):
        message_id = f'wecom-{next(self._ids)}-{uuid.uuid4().hex[:6]}'
        return wecom_text(message_id, text, sender=sender, chat=group), message_id

    def push(self, frame) -> None:
        self.platform.push(frame)

    def received(self, thread: ThreadKey) -> list[str]:
        return [send['content'] for send in self.platform.delivered if send['chatid'] == ids.peer(thread)]

    def attempts(self, item) -> int:
        return sum(1 for send in self.platform.attempted if send['content'] == item.text)

    def lose_next_answer(self) -> None:
        self.platform.lose_next = True

    def rate_limit_sends(self) -> None:
        self.platform.rate_limited = True

    def end_rate_limit(self) -> None:
        """Every documented per-chat window (x-wecom: per minute and per hour) has passed."""
        self.platform.rate_limited = False
        advance_core_clock(self.env, 3_600_000 + 1)

    def _close_connection(self) -> None:
        self.platform.connections[-1].close()
        self._ended()

    def expire_auth(self) -> None:
        self.platform.subscribe_errcode = 40001
        self._close_connection()
        self._tick()
        self._ended()

    def displace(self) -> None:
        """A newer connection of the same bot took over (智能机器人长连接 disconnected_event)."""
        self.platform.push({'msgid': 'evt-1', 'event': {'eventtype': 'disconnected_event'}}, cmd='aibot_event_callback')
        self._ended()

    def network_fault(self) -> None:
        for connection in self.platform.connections:
            connection.close()
        self.platform.close()
        self._ended()
        self._tick()
        self._ended()


# ── QQ ────────────────────────────────────────────────────────────────────

def _iso(seconds_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


class QQPlatform(FakeQQ):
    """FakeQQ that records deliveries, deduplicates a passive reply by msg_seq (40054005),
    can lose an answer after delivery and refuse at its rate limit (40034100)."""

    def __init__(self):
        self.delivered: list[dict] = []
        self.attempted: list[dict] = []
        self.passive: set[tuple] = set()
        self.lose_next = False
        self.rate_limited = False
        self.gateway_status = 200
        super().__init__()

    def _route(self, method, path, query, headers, body):
        if path == '/gateway' and self.gateway_status != 200:
            return self.gateway_status, {'message': 'token invalid'}
        if not path.startswith('/v2/'):
            return super()._route(method, path, query, headers, body)
        payload = json.loads(body or b'{}')
        send = {'peer': path.split('/')[3], 'content': payload.get('content', ''), 'msg_id': payload.get('msg_id'),
                'msg_seq': payload.get('msg_seq')}
        self.attempted.append(send)
        if self.rate_limited:
            return 200, {'code': 40034100, 'message': 'rate limited'}
        if send['msg_id']:
            key = (send['msg_id'], send['msg_seq'])
            if key in self.passive:
                return 400, {'code': 40054005, 'message': '消息被去重，请检查请求msgseq'}
            self.passive.add(key)
        self.delivered.append(send)
        if self.lose_next:
            self.lose_next = False
            return 500, {}
        return 200, {'id': f'ROBOT1.0_{len(self.delivered)}', 'timestamp': _iso()}


class QQDriver(BotDriver):
    name = channel = 'qq'
    secret = QQ_SECRET
    edits_reason = 'QQ group and C2C events carry no edits or recalls'

    def make_platform(self):
        return QQPlatform()

    def make_transport(self):
        from lib.channels.qq.transport import QQTransport
        return QQTransport(QQ_APP_ID, lambda: QQ_SECRET, api_base=self.platform.base,
                           token_url=self.platform.token_url)

    def app_id(self) -> str:
        return QQ_APP_ID

    def message(self, peer, text, *, sender, seconds_ago=0, group=''):
        message_id = f'qq-{next(self._ids)}-{uuid.uuid4().hex[:6]}'
        if group:
            return ('GROUP_AT_MESSAGE_CREATE', qq_group(message_id, text, group=group, sender=sender,
                                                        timestamp=_iso(seconds_ago))), message_id
        return ('C2C_MESSAGE_CREATE', qq_c2c(message_id, text, sender=sender, timestamp=_iso(seconds_ago))), message_id

    def push(self, frame) -> None:
        eventually(lambda: self.platform.ops(2) or self.platform.ops(6), what='the QQ gateway session')
        self.platform.dispatch(*frame)

    def received(self, thread: ThreadKey) -> list[str]:
        return [send['content'] for send in self.platform.delivered if send['peer'] == ids.peer(thread)]

    def attempts(self, item) -> int:
        return sum(1 for send in self.platform.attempted if send['content'] == item.text)

    def lose_next_answer(self) -> None:
        self.platform.lose_next = True

    def rate_limit_sends(self) -> None:
        self.platform.rate_limited = True

    def end_rate_limit(self) -> None:
        self.platform.rate_limited = False

    def bot_post(self, text: str) -> Msg:
        group = 'GROUPOPENID01'
        message_id = f'qq-bot-{next(self._ids)}'
        data = qq_group(message_id, text, group=group, sender='OTHERBOT01', timestamp=_iso())
        data['author']['bot'] = True
        self.platform.dispatch('GROUP_AT_MESSAGE_CREATE', data)
        return Msg(ids.thread_key('qq', 'gid', group), text, message_id)

    def expire_auth(self) -> None:
        self.platform.gateway_status = 401
        self.platform.connections[-1].close()
        self._ended()

    def network_fault(self) -> None:
        for connection in self.platform.connections:
            connection.close()
        self.platform.close()
        self._ended()
        self._tick()
        self._ended()


# ── 微信 ──────────────────────────────────────────────────────────────────

class WeChatPlatform(FakeILink):
    """FakeILink that records deliveries and can lose an answer after delivery."""

    def __init__(self):
        self.lose_next = False
        super().__init__()

    def _route(self, method, path, query, headers, body):
        if path == '/ilink/bot/sendmessage' and self.lose_next:
            self.lose_next = False
            super()._route(method, path, query, headers, body)
            return 500, {}
        return super()._route(method, path, query, headers, body)


class WeChatDriver(BotDriver):
    name = channel = 'wechat'
    secret = WECHAT_TOKEN
    edits_reason = 'iLink documents no edit or recall flow (update_time_ms/delete_time_ms are type surface only)'

    def make_platform(self):
        return WeChatPlatform()

    def account_extra(self) -> dict:
        return {'apiBaseUrl': self.platform.base}

    def make_transport(self):
        from lib.channels.wechat.transport import WeChatTransport
        return WeChatTransport(lambda: WECHAT_TOKEN, api_base=self.platform.base,
                               cursor=str((self.store.account('wechat') or {}).get('cursor') or ''))

    def message(self, peer, text, *, sender, seconds_ago=0, group='', message_type=1):
        number = next(self._ids) + 1000
        message = ilink_message(number, text, sender=sender, context_token=f'ctx-{sender}',
                                created_ms=now_ms() - seconds_ago * 1000, message_type=message_type)
        if group:
            message['group_id'] = group
        return message, str(number)

    def push(self, message) -> None:
        self.platform.updates.append({'ret': 0, 'msgs': [message], 'get_updates_buf': f'cursor-{uuid.uuid4().hex}'})

    def received(self, thread: ThreadKey) -> list[str]:
        peer = ids.peer(thread)
        return [sent['body']['msg']['item_list'][0]['text_item']['text'] for sent in self.platform.sent
                if sent['body']['msg']['to_user_id'] == peer]

    def attempts(self, item) -> int:
        return self.received(ThreadKey.parse(item.thread)).count(item.text)

    def lose_next_answer(self) -> None:
        self.platform.lose_next = True

    def echo(self, item) -> None:
        """iLink returns this bot's own messages (message_type 2) in getupdates."""
        message, _ = self.message(ids.peer(ThreadKey.parse(item.thread)), item.text, sender='bot@im.bot',
                                  message_type=2)
        self.push(message)

    def self_send_outside_outbox(self, text: str) -> Msg:
        """Any send of the bot's account comes back flagged as the bot's (message_type 2)."""
        message, message_id = self.message(OWNER_ID, text, sender='bot@im.bot', message_type=2)
        self.push(message)
        return Msg(self.command, text, message_id)

    def expire_auth(self) -> None:
        self.platform.updates.append({'ret': -14, 'errmsg': 'session expired'})
        self._ended()

    def network_fault(self) -> None:
        self.platform.close()
        self._ended()
