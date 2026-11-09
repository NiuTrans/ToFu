"""QQ 官方机器人 transport: token, WebSocket gateway and OpenAPI sends (C12).

Responsibility, following the QQ 机器人 documents:

- **Token** (接口调用与鉴权): ``POST x-qq.tokenUrl`` {appId, clientSecret} →
  ``access_token`` valid ``expires_in`` seconds; reused until the last
  ``tokenRefreshLeadS`` seconds, when the endpoint issues a new one. Requests
  carry ``Authorization: QQBot <token>``.
- **Gateway** (WebSocket 方式): ``GET /gateway`` gives the URL; Hello names the
  heartbeat interval; Identify (op 2) subscribes ``GROUP_AND_C2C_EVENT``
  with one shard; heartbeats (op 1) carry the last sequence; READY gives the
  session, which a reconnect (op 7, or a close the document marks
  resumable) Resumes (op 6) so the gateway re-sends what was missed. A
  reconnect happens only after a connection reached READY/RESUMED; otherwise
  the session ends and the supervisor's tick restarts it. Banned or
  offline bots (4914, 4915) and refused tokens are ``needs_login``.
- **Messages**: ``C2C_MESSAGE_CREATE`` (a private chat) and
  ``GROUP_AT_MESSAGE_CREATE`` (an @-mention in a group) become ``Inbound``
  with their time and their ID as the reply context; the same message pushed
  again yields the same event ID.
- **Sends** (发送单聊消息 / 发送群消息): plain text (``msg_type`` 0). Inside the
  documented passive-reply window of the chat's latest message (and its reply
  count) the send quotes it (``msg_id``) with a ``msg_seq`` derived from the
  outbox item, so a resend is answered "消息被去重" (40054005), which proves
  the first one; an expired window falls back to an active message once.
  ``x-qq.sendErrors`` classifies refusals.

Entry points: ``QQTransport``, ``open_transport``, ``probe``. Dependencies:
``lib.channels.bots.http``, ``websockets`` (sync client), the vault.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from lib.channels.bots.http import request_json
from lib.channels.bots.platform import Inbound, SessionEnded, failure
from lib.channels.model import QQ, ChannelError
from lib.log import get_logger

logger = get_logger(__name__)

CHANNEL = 'qq'
_ERRORS = {code: kind for kind, codes in QQ['sendErrors'].items() for code in codes}
_SEND_CODES = {'invalidInput': 'invalid_input', 'forbidden': 'forbidden', 'rateLimited': 'rate_limited'}
# Gateway close codes (WebSocket 错误码): the bot is offline or banned.
_FATAL_CLOSES = frozenset({4914, 4915})


class QQUnavailable(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.code = 'qq_unavailable'


def _seconds(value: Any) -> int | None:
    try:
        return int(datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp())
    except (TypeError, ValueError):
        return None


def _attachments(data: Mapping[str, Any]) -> list[str]:
    pieces = []
    for item in data.get('attachments') or []:
        kind = str(item.get('content_type') or '')
        if kind.startswith('image/'):
            pieces.append('[image]')
        elif kind == 'voice':
            pieces.append(str(item.get('asr_refer_text') or '').strip() or '[voice]')
        elif kind.startswith('video/'):
            pieces.append('[video]')
        else:
            pieces.append(f'[file: {item.get("filename") or "attachment"}]')
    return pieces


def normalize(event: str, data: Mapping[str, Any]) -> Inbound | None:
    author = data.get('author') or {}
    message_id = str(data.get('id') or '')
    sent = _seconds(data.get('timestamp'))
    if not message_id or author.get('bot'):
        return None
    text = ' '.join(piece for piece in [str(data.get('content') or '').strip(), *_attachments(data)] if piece)
    reply = {'msgId': message_id, 'atS': sent} if sent is not None else {}
    if event == 'C2C_MESSAGE_CREATE':
        user = str(author.get('user_openid') or author.get('id') or '')
        if not user:
            return None
        return Inbound(kind='uid', peer=user, sender=user, sender_name=str(author.get('username') or user),
                       message_id=message_id, text=text, sent_at_s=sent, reply=reply)
    if event == 'GROUP_AT_MESSAGE_CREATE':
        group = str(data.get('group_openid') or '')
        member = str(author.get('member_openid') or author.get('id') or '')
        if not group or not member:
            return None
        return Inbound(kind='gid', peer=group, sender=member, sender_name=str(author.get('username') or member),
                       message_id=message_id, text=text, sent_at_s=sent, title=group, reply=reply)
    return None


def _msg_seq(item_id: str) -> int:
    """The outbox item's own reply sequence: a resend repeats it and is deduplicated."""
    return 1 + int(hashlib.sha256(item_id.encode('utf-8')).hexdigest(), 16) % (2 ** 31 - 1)


class QQTransport:
    def __init__(self, app_id: str, secret_source, *, api_base: str = QQ['apiBase'],
                 token_url: str = QQ['tokenUrl'], clock=None, heartbeat_s: float | None = None,
                 connect=None, http=request_json):
        self._app_id = app_id
        self._secret = secret_source
        self._api = api_base.rstrip('/')
        self._token_url = token_url
        self._clock = clock or time.time
        self._heartbeat_override = heartbeat_s
        if connect is None:
            from websockets.sync.client import connect
        self._connect = connect
        self._http = http
        self._token_lock = threading.Lock()
        self._token: str | None = None
        self._token_until = 0.0
        self._session_id: str | None = None
        self._seq: int | None = None
        self._bot: dict[str, str] = {}
        self._conn = None
        self._closing = threading.Event()

    # ── auth ─────────────────────────────────────────────────────────
    def token(self) -> str:
        with self._token_lock:
            now = self._clock()
            if self._token and now < self._token_until - QQ['tokenRefreshLeadS']:
                return self._token
            status, body = self._http('POST', self._token_url,
                                      body={'appId': self._app_id, 'clientSecret': self._secret()})
            if status in (400, 401, 403) or (status == 200 and not body.get('access_token')):
                raise SessionEnded('needs_login', 'QQ refused the AppID or AppSecret')
            if status != 200:
                raise QQUnavailable(f'QQ token endpoint answered HTTP {status}')
            self._token = str(body['access_token'])
            self._token_until = now + int(body.get('expires_in') or 0)
            return self._token

    def _forget_token(self) -> None:
        with self._token_lock:
            self._token = None

    # ── gateway ──────────────────────────────────────────────────────
    def run(self, sink) -> None:
        while not sink.stopped.is_set() and not self._closing.is_set():
            token = self.token()
            status, body = self._http('GET', self._api + '/gateway', headers={'authorization': f'QQBot {token}'})
            if status in (401, 403):
                raise SessionEnded('needs_login', 'QQ refused the bot token')
            if status != 200 or not body.get('url'):
                raise QQUnavailable(f'QQ gateway lookup answered HTTP {status}')
            if not self._connection(str(body['url']), token, sink):
                return

    def _connection(self, url: str, token: str, sink) -> bool:
        """One gateway connection; True when it made progress and asks for a reconnect."""
        from websockets.exceptions import ConnectionClosed
        beat = threading.Event()
        ready = threading.Event()
        try:
            with self._connect(url) as ws:
                self._conn = ws
                hello = json.loads(ws.recv())
                interval = self._heartbeat_override or (hello.get('d') or {}).get('heartbeat_interval', 0) / 1000
                if self._session_id:
                    ws.send(json.dumps({'op': 6, 'd': {'token': f'QQBot {token}', 'session_id': self._session_id,
                                                       'seq': self._seq}}))
                else:
                    ws.send(json.dumps({'op': 2, 'd': {'token': f'QQBot {token}', 'intents': QQ['intents'],
                                                       'shard': [0, 1], 'properties': {}}}))
                if interval:
                    threading.Thread(target=self._heartbeat, args=(ws, beat, interval), daemon=True,
                                     name=f'qq-heartbeat-{self._app_id}').start()
                for raw in ws:
                    if sink.stopped.is_set():
                        return False
                    frame = json.loads(raw)
                    op = frame.get('op')
                    if op == 0:
                        self._dispatch(frame, sink, ready)
                    elif op == 7:
                        return ready.is_set()
                    elif op == 9:
                        self._session_id, self._seq = None, None
                        return ready.is_set()
        except ConnectionClosed as closed:
            code = getattr(getattr(closed, 'rcvd', None), 'code', None)
            if code in _FATAL_CLOSES:
                raise SessionEnded('needs_login', f'QQ closed the gateway ({code}): the bot is offline or banned')
            if code in (4006, 4007):
                self._session_id, self._seq = None, None
            return ready.is_set() and not sink.stopped.is_set()
        finally:
            beat.set()
            self._conn = None
        return ready.is_set() and not sink.stopped.is_set()

    def _dispatch(self, frame: Mapping[str, Any], sink, ready: threading.Event) -> None:
        event, data = frame.get('t'), frame.get('d')
        if event == 'READY':
            self._session_id = str((data or {}).get('session_id') or '') or None
            user = (data or {}).get('user') or {}
            self._bot = {'appId': str(user.get('id') or ''), 'name': str(user.get('username') or '')}
            ready.set()
            sink.connected(self._bot)
        elif event == 'RESUMED':
            ready.set()
            sink.connected(self._bot)
        elif isinstance(data, Mapping):
            message = normalize(str(event), data)
            if message is not None:
                sink.deliver([message])
        if isinstance(frame.get('s'), int):
            self._seq = frame['s']  # after the message was ingested

    def _heartbeat(self, ws, beat: threading.Event, interval: float) -> None:
        while not beat.wait(interval):
            try:
                ws.send(json.dumps({'op': 1, 'd': self._seq}))
            except Exception as exc:
                logger.debug('qq heartbeat stopped: %s', exc)
                return

    # ── sends ────────────────────────────────────────────────────────
    def _passive(self, kind: str, reply: Mapping[str, Any]) -> bool:
        rule = QQ['passiveReply'][kind]
        at = reply.get('atS')
        return (bool(reply.get('msgId')) and isinstance(at, int) and self._clock() - at < rule['windowS']
                and int(reply.get('used') or 0) < rule['replies'])

    def _post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        token = self.token()
        status, answer = self._http('POST', self._api + path, headers={'authorization': f'QQBot {token}'},
                                    body=body)
        if status == 401:
            self._forget_token()
        return status, answer

    def send(self, kind: str, peer: str, text: str, *, item_id: str,
             reply: Mapping[str, Any]) -> dict[str, Any]:
        path = f'/v2/users/{peer}/messages' if kind == 'uid' else f'/v2/groups/{peer}/messages'
        body: dict[str, Any] = {'content': text, 'msg_type': 0}
        passive = self._passive(kind, reply)
        if passive:
            body.update(msg_id=reply['msgId'], msg_seq=_msg_seq(item_id))
        try:
            status, answer = self._post(path, body)
        except SessionEnded as refused:
            return failure('auth_failed', refused.detail)
        kind_of = _ERRORS.get(answer.get('code'))
        if passive and kind_of == 'passiveExpired':
            # The window or count ran out on QQ's side: once more as an active message.
            reply = {**reply, 'used': QQ['passiveReply'][kind]['replies']}
            passive = False
            status, answer = self._post(path, {'content': text, 'msg_type': 0})
            kind_of = _ERRORS.get(answer.get('code'))
        if status in (200, 201, 202) and not answer.get('code'):
            used = {**reply, 'used': int(reply.get('used') or 0) + 1} if passive else (
                dict(reply) if reply.get('used') is not None else None)
            return {'ok': True, 'externalId': str(answer.get('id') or ''), 'reply': used}
        if kind_of == 'duplicate':
            return {'ok': True, 'externalId': ''}
        if status == 401:
            return failure('auth_failed', 'QQ rejected the bot token')
        if kind_of in _SEND_CODES:
            return failure(_SEND_CODES[kind_of], f'{answer.get("code")} {answer.get("message") or ""}'.strip())
        return failure('unknown', f'HTTP {status} {answer.get("code") or ""}'.strip())

    def close(self) -> None:
        self._closing.set()
        conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:
                logger.debug('qq transport close failed: %s', exc)


def probe(app_id: str, secret: str, *, token_url: str = QQ['tokenUrl'], http=request_json) -> None:
    """Verify an AppID and AppSecret with one token request (no other side effect)."""
    try:
        QQTransport(app_id, lambda: secret, token_url=token_url, http=http).token()
    except SessionEnded as refused:
        raise ChannelError('auth_failed', refused.detail) from refused
    except QQUnavailable as error:
        raise ChannelError('qq_unavailable', str(error)) from error


def open_transport(owner: int, account: Mapping[str, Any], *, store=None) -> QQTransport:
    name = str((account.get('credential') or {}).get('name') or '')

    def secret() -> str:
        from lib.credentials_vault import get_entry
        value = get_entry(name) if name else None
        if not value:
            raise SessionEnded('needs_login', 'The QQ AppSecret is missing; add it in Settings')
        return value
    return QQTransport(str(account.get('appId') or ''), secret)


__all__ = ['CHANNEL', 'QQTransport', 'QQUnavailable', 'normalize', 'open_transport', 'probe']
