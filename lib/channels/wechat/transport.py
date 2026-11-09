"""微信 ClawBot iLink transport: long-poll ingress and replies (docs/CHANNELS.md C12).

Responsibility: ``POST /ilink/bot/getupdates`` with the stored cursor
(``get_updates_buf``), held by the server up to its ``longpolling_timeout_ms``
(``x-wechat.longPollMs`` until it names one; a read outliving it is an empty
answer, as in Tencent's client); user messages (``message_type`` 1) become
``Inbound`` with their 微信 context token as the chat's reply context, and the
bot's own messages (2) are its sends, never input. The new cursor is the
batch checkpoint: the session stores it only after the batch was ingested,
so a failed route polls the same messages again. ``ret``/``errcode`` -14
(session expired) is ``needs_login``: the owner scans a new code.

``send`` is ``POST /ilink/bot/sendmessage`` with the chat's context token
(微信 offers no reply without one, so a chat that never wrote is refused as
``unsupported_target``) and the outbox item as ``client_id``. A pending QR
login is completed first (``lib.channels.wechat.login``).

Entry points: ``WeChatTransport``, ``LoginTransport``, ``open_transport``.
Dependencies: ``lib.channels.bots.http``, ``protocol``, ``login``, the vault.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.channels.bots.http import is_timeout, request_json
from lib.channels.bots.platform import Inbound, SessionEnded, failure
from lib.channels.model import WECHAT
from lib.channels.wechat import protocol
from lib.log import get_logger

logger = get_logger(__name__)

CHANNEL = 'wechat'


class WeChatUnavailable(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.code = 'wechat_unavailable'


def normalize(message: Mapping[str, Any]) -> Inbound | None:
    if message.get('message_type') != 1:
        return None
    sender = str(message.get('from_user_id') or '')
    message_id = message.get('message_id') or message.get('seq')
    if not sender or message_id in (None, ''):
        return None
    group = str(message.get('group_id') or '')
    created = message.get('create_time_ms')
    context = str(message.get('context_token') or '')
    return Inbound(kind='gid' if group else 'uid', peer=group or sender, sender=sender, sender_name=sender,
                   message_id=str(message_id), text=protocol.message_text(message.get('item_list')),
                   sent_at_s=int(created) // 1000 if isinstance(created, int) else None,
                   reply={'contextToken': context} if context else {})


class WeChatTransport:
    def __init__(self, token_source, *, api_base: str = WECHAT['apiBase'], cursor: str = '', http=request_json):
        self._token = token_source
        self._api = api_base.rstrip('/')
        self._cursor = cursor
        self._http = http
        self._closing = False

    def run(self, sink) -> None:
        hold_ms = WECHAT['longPollMs']
        connected = False
        while not sink.stopped.is_set() and not self._closing:
            try:
                status, body = self._http('POST', self._api + '/ilink/bot/getupdates',
                                          headers=protocol.bot_headers(self._token()),
                                          body={'get_updates_buf': self._cursor, 'base_info': protocol.base_info()},
                                          timeout=hold_ms / 1000)
            except Exception as error:
                if is_timeout(error):
                    continue
                raise
            if sink.stopped.is_set() or self._closing:
                return
            if status in (401, 403):
                raise SessionEnded('needs_login', '微信 refused the bot token; scan a new login code in Settings')
            if status != 200:
                raise WeChatUnavailable(f'iLink getupdates answered HTTP {status}')
            code = body.get('ret') or body.get('errcode') or 0
            if code == WECHAT['sessionExpiredCode']:
                raise SessionEnded('needs_login', '微信 ended the bot session; scan a new login code in Settings')
            if code:
                raise WeChatUnavailable(f'iLink getupdates returned {code}')
            if not connected:
                connected = True
                sink.connected({})
            if isinstance(body.get('longpolling_timeout_ms'), int) and body['longpolling_timeout_ms'] > 0:
                hold_ms = body['longpolling_timeout_ms']
            messages = body.get('msgs') or []
            batch = [message for message in (normalize(item) for item in messages) if message]
            if messages:  # counts only: IDs, context tokens and text are never logged
                logger.info('[Channels] wechat getupdates returned %d message(s), %d from users',
                            len(messages), len(batch))
            cursor = str(body.get('get_updates_buf') or '') or self._cursor
            if batch or cursor != self._cursor:
                sink.deliver(batch, {'cursor': cursor})
                self._cursor = cursor

    def send(self, kind: str, peer: str, text: str, *, item_id: str,
             reply: Mapping[str, Any]) -> dict[str, Any]:
        context = str(reply.get('contextToken') or '')
        if kind != 'uid' or not context:
            return failure('unsupported_target', '微信 replies only to a chat that wrote to the bot')
        status, answer = self._http('POST', self._api + '/ilink/bot/sendmessage',
                                    headers=protocol.bot_headers(self._token()),
                                    body={'msg': {'from_user_id': '', 'to_user_id': peer, 'client_id': item_id,
                                                  'message_type': 2, 'message_state': 2, 'context_token': context,
                                                  'item_list': [{'type': 1, 'text_item': {'text': text}}]},
                                          'base_info': protocol.base_info()})
        if status in (401, 403):
            return failure('auth_failed', '微信 refused the bot token')
        if status != 200:
            return failure('unknown', f'HTTP {status}')
        code = answer.get('ret') or answer.get('errcode') or 0
        if code == WECHAT['sessionExpiredCode']:
            return failure('auth_failed', '微信 ended the bot session')
        if code:
            return failure('invalid_input', f'iLink sendmessage returned {code}')
        return {'ok': True, 'externalId': ''}

    def close(self) -> None:
        self._closing = True  # a long poll in flight returns within its hold; its answer is dropped


class LoginTransport:
    """Complete the pending QR login, then poll with the token it issued."""

    def __init__(self, owner: int, store, *, qrcode: str, api_base: str = WECHAT['apiBase']):
        self._owner, self._store, self._api = owner, store, api_base
        self._qrcode = qrcode
        self._inner: WeChatTransport | None = None
        self._closing = False

    def run(self, sink) -> None:
        from lib.channels.wechat.login import complete
        if not complete(self._owner, store=self._store, api_base=self._api, stop=sink.stopped,
                        expected_qrcode=self._qrcode) or self._closing:
            return
        from lib.channels.bots.session import account_binding
        account = self._store.account(CHANNEL) or {}
        if account.get('login') or not account.get('enabled'):
            return
        sink.binding = account_binding(account)
        self._inner = _polling(account)
        self._inner.run(sink)

    def send(self, kind: str, peer: str, text: str, *, item_id: str, reply: Mapping[str, Any]) -> dict[str, Any]:
        if self._inner is None:
            return failure('not_connected', 'The 微信 login is not complete')
        return self._inner.send(kind, peer, text, item_id=item_id, reply=reply)

    def close(self) -> None:
        self._closing = True
        if self._inner is not None:
            self._inner.close()


def _polling(account: Mapping[str, Any]) -> WeChatTransport:
    name = str((account.get('credential') or {}).get('name') or '')

    def token() -> str:
        from lib.credentials_vault import get_entry
        value = get_entry(name) if name else None
        if not value:
            raise SessionEnded('needs_login', 'The 微信 bot token is missing; scan a new login code in Settings')
        return value
    return WeChatTransport(token, api_base=account.get('apiBaseUrl') or WECHAT['apiBase'],
                           cursor=str(account.get('cursor') or ''))


def open_transport(owner: int, account: Mapping[str, Any], *, store=None):
    if (account.get('login') or {}).get('state') in ('waiting', 'scanned'):
        return LoginTransport(owner, store, qrcode=str(account['login'].get('qrcode') or ''),
                              api_base=account.get('apiBaseUrl') or WECHAT['apiBase'])
    return _polling(account)


__all__ = ['CHANNEL', 'LoginTransport', 'WeChatTransport', 'normalize', 'open_transport']
