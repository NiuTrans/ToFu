"""企业微信 智能机器人 long-connection transport (docs/CHANNELS.md C12).

Responsibility: one WebSocket to ``x-wecom.wsUrl`` per bot, following
智能机器人长连接 (https://developer.work.weixin.qq.com/document/path/101463):
authenticate with ``aibot_subscribe`` {bot_id, secret}; ping every
``heartbeatIntervalS`` (the documented recommendation); turn each
``aibot_msg_callback`` into an ``Inbound`` (private chats and group
@-mentions; images, files and videos arrive only in private chats and are
described, not downloaded; voice arrives transcribed); and send with
``aibot_send_msg`` (markdown to a user ``chat_type`` 1 or a group 2, one
message per send: the outbox chunks a longer text at ``markdownBytes``, the
bot's ``maxTextBytes``), answered on the same connection by ``req_id``. The
platform pushes no message time, so the session mints event IDs at first
sight (``ChannelStore.provider_message_id``).

A refused subscription is ``needs_login``; ``disconnected_event`` (a newer
connection of the same bot took over: one connection per bot) is ``kicked``,
so two hosts never take the bot from each other in turns. A send waits for its
answer or for the connection to end (then its outcome is unknown and the
outbox sends it again): no timeout of its own.

Entry points: ``WeComTransport``, ``open_transport``. Dependencies:
``websockets`` (sync client; proxies from the environment), the vault.
"""
from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Mapping
from typing import Any

from lib.channels.bots.platform import Inbound, SessionEnded, failure
from lib.channels.model import WECOM
from lib.log import get_logger

logger = get_logger(__name__)

CHANNEL = 'wecom'


def _text(body: Mapping[str, Any]) -> str:
    kind = body.get('msgtype')
    if kind == 'text':
        return str((body.get('text') or {}).get('content') or '').strip()
    if kind == 'voice':
        return str((body.get('voice') or {}).get('content') or '').strip() or '[voice]'
    if kind == 'mixed':
        pieces = []
        for item in (body.get('mixed') or {}).get('msg_item') or []:
            if item.get('msgtype') == 'text':
                pieces.append(str((item.get('text') or {}).get('content') or '').strip())
            else:
                pieces.append(f'[{item.get("msgtype") or "item"}]')
        return ' '.join(piece for piece in pieces if piece)
    return f'[{kind or "message"}]'


def normalize(body: Mapping[str, Any]) -> Inbound | None:
    """A user's message to the bot, or None for callbacks that are not one."""
    if body.get('compat_info'):
        return None  # 智能助理 or 频道 calls, not a person writing to the bot
    sender = str((body.get('from') or {}).get('userid') or '')
    message_id = str(body.get('msgid') or '')
    if not sender or not message_id:
        return None
    group = body.get('chattype') == 'group'
    peer = str(body.get('chatid') or '') if group else sender
    if not peer:
        return None
    return Inbound(kind='gid' if group else 'uid', peer=peer, sender=sender, sender_name=sender,
                   message_id=message_id, text=_text(body), title=peer)


class _Answer:
    def __init__(self):
        self.done = threading.Event()
        self.frame: dict[str, Any] | None = None


class WeComTransport:
    def __init__(self, bot_id: str, secret_source, *, ws_url: str = WECOM['wsUrl'],
                 heartbeat_s: float = WECOM['heartbeatIntervalS'], connect=None):
        self._bot_id = bot_id
        self._secret = secret_source
        self._url = ws_url
        self._heartbeat_s = heartbeat_s
        if connect is None:
            from websockets.sync.client import connect
        self._connect = connect
        self._ws = None  # set once the subscription is accepted: sends go only to an authenticated socket
        self._conn = None
        self._lock = threading.Lock()
        self._answers: dict[str, _Answer] = {}

    def _expect(self, request_id: str) -> _Answer:
        answer = _Answer()
        with self._lock:
            self._answers[request_id] = answer
        return answer

    def _settle(self, request_id: str, frame: dict[str, Any] | None) -> None:
        with self._lock:
            answer = self._answers.pop(request_id, None)
        if answer is not None:
            answer.frame = frame
            answer.done.set()

    def run(self, sink) -> None:
        from websockets.exceptions import ConnectionClosed
        subscribe_id = uuid.uuid4().hex
        with self._connect(self._url) as ws:
            beat = threading.Event()
            with self._lock:
                self._conn = ws
            try:
                ws.send(json.dumps({'cmd': 'aibot_subscribe', 'headers': {'req_id': subscribe_id},
                                    'body': {'bot_id': self._bot_id, 'secret': self._secret()}}))
                for raw in ws:
                    if sink.stopped.is_set():
                        return
                    try:
                        frame = json.loads(raw)
                    except ValueError:
                        continue
                    self._handle(frame, sink, ws, subscribe_id, beat)
            except ConnectionClosed:
                return
            finally:
                beat.set()
                with self._lock:
                    self._ws = self._conn = None
                    pending = list(self._answers)
                for request_id in pending:
                    self._settle(request_id, None)

    def _handle(self, frame, sink, ws, subscribe_id: str, beat: threading.Event) -> None:
        cmd = frame.get('cmd')
        request_id = str((frame.get('headers') or {}).get('req_id') or '')
        if not cmd:
            if request_id != subscribe_id:
                self._settle(request_id, frame)
                return
            if frame.get('errcode') != 0:
                raise SessionEnded('needs_login', f'企业微信 refused the BotID or Secret '
                                                  f'({frame.get("errcode")} {frame.get("errmsg") or ""})'.strip())
            with self._lock:
                self._ws = ws
            threading.Thread(target=self._heartbeat, args=(ws, beat), daemon=True,
                             name=f'wecom-heartbeat-{self._bot_id}').start()
            sink.connected({'appId': self._bot_id})
            return
        body = frame.get('body') or {}
        if cmd == 'aibot_msg_callback':
            message = normalize(body)
            if message is not None:
                sink.deliver([message])
        elif cmd == 'aibot_event_callback' and (body.get('event') or {}).get('eventtype') == 'disconnected_event':
            raise SessionEnded('kicked', 'Another connection of this 企业微信 bot took over; '
                                         'enable the account again to take it back')

    def _heartbeat(self, ws, beat: threading.Event) -> None:
        while not beat.wait(self._heartbeat_s):
            try:
                ws.send(json.dumps({'cmd': 'ping', 'headers': {'req_id': uuid.uuid4().hex}}))
            except Exception as exc:
                logger.debug('wecom heartbeat stopped: %s', exc)
                return

    def send(self, kind: str, peer: str, text: str, *, item_id: str,
             reply: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            ws = self._ws
        if ws is None:
            return failure('not_connected', 'The 企业微信 connection is not up')
        request_id = uuid.uuid4().hex
        answer = self._expect(request_id)
        try:
            ws.send(json.dumps({'cmd': 'aibot_send_msg', 'headers': {'req_id': request_id},
                                'body': {'chatid': peer, 'chat_type': 1 if kind == 'uid' else 2,
                                         'msgtype': 'markdown', 'markdown': {'content': text}}},
                               ensure_ascii=False))
        except Exception as error:
            self._settle(request_id, None)
            return failure('unknown', type(error).__name__)
        answer.done.wait()
        frame = answer.frame
        if frame is None:
            return failure('unknown', 'The connection ended before 企业微信 answered')
        code = frame.get('errcode')
        if code != 0:
            return failure('rate_limited' if code in WECOM['rateLimitedErrcodes'] else 'invalid_input',
                           f'{code} {frame.get("errmsg") or ""}'.strip())
        return {'ok': True, 'externalId': ''}

    def close(self) -> None:
        with self._lock:
            ws = self._conn
        if ws is not None:
            try:
                ws.close()
            except Exception as exc:
                logger.debug('wecom transport close failed: %s', exc)


def open_transport(owner: int, account: Mapping[str, Any], *, store=None) -> WeComTransport:
    name = str((account.get('credential') or {}).get('name') or '')

    def secret() -> str:
        from lib.credentials_vault import get_entry
        value = get_entry(name) if name else None
        if not value:
            raise SessionEnded('needs_login', 'The 企业微信 Secret is missing; add it in Settings')
        return value
    return WeComTransport(str(account.get('appId') or ''), secret)


__all__ = ['CHANNEL', 'WeComTransport', 'normalize', 'open_transport']
