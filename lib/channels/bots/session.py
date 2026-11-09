"""One bot platform connection of one owner (docs/CHANNELS.md C12).

Responsibility: run a platform transport (``lib.channels.bots.platform``) and
be its sink. Each inbound batch is, in order: the chat's reply context kept on
its thread record, a pending pairing code consumed (it binds the owner's
platform account and makes that private chat the command chat; never routed),
then every other message normalized to a ``ChannelEvent`` and ingested
through ``lib.channels.ingress`` (the shared C2/C7 boundary: only the bound
owner in the command chat is human input), and only then the transport's
checkpoint (a 微信 cursor) persisted, so a failed route replays as duplicates.
The session also executes its channel's outbox (C5) through the transport;
bot platforms keep no thread history, so an unknown send is sent again. A send
that no network route carried is deferred (``not_connected``), not unknown.

Entry point: ``BotSession`` (``start``, ``stop``, ``alive``, ``tick``,
``deliver``, ``connected``, ``execute_outbox``). Dependencies: ``ChannelStore``,
``ingress``, ``outbox``, ``ids``, the platform transport modules and the
network authority's route-exhaustion evidence (``lib.http_transport``).
"""
from __future__ import annotations

import importlib
import re
import threading
import time
from collections.abc import Mapping
from typing import Any

from lib.channels import ingress, outbox
from lib.channels.bots import ids
from lib.channels.bots.platform import Inbound, SessionEnded, failure
from lib.channels.model import ChannelError, ChannelEvent, ThreadKey
from lib.channels.protocol.contract import adapter_descriptor
from lib.channels.store import ChannelStore, now_ms
from lib.http_transport.connection_setup import NetworkRoutesExhausted
from lib.log import get_logger

logger = get_logger(__name__)

TRANSPORTS = {'wecom': 'lib.channels.wecom.transport', 'qq': 'lib.channels.qq.transport',
              'wechat': 'lib.channels.wechat.transport'}
PAIRED_TEXT = '配对完成：以后你在这个私聊里发的消息会交给 Tofu 总负责人处理，其他人的消息只作为参考信息。'


def account_binding(account: Mapping[str, Any]) -> tuple:
    """The configuration a running session depends on; a change restarts it."""
    return (account.get('appId'), (account.get('credential') or {}).get('name'), account.get('apiBaseUrl'),
            (account.get('login') or {}).get('qrcode'))


def _code_text(text: str) -> str:
    return re.sub(r'\s+', '', str(text or '')).upper()


class _Sender:
    """The outbox sender port (``lib.channels.outbox.OutboxSender``) over the transport."""

    def __init__(self, session: 'BotSession'):
        self._session = session

    def send(self, thread: ThreadKey, text: str, *, marker: str,
             reply_to_external_id: str | None) -> dict[str, Any]:
        transport = self._session.transport()
        record = self._session.store.thread(thread) or {}
        try:
            result = transport.send(thread.kind, ids.peer(thread), text, item_id=marker,
                                    reply=dict(record.get('botReply') or {}))
        except NetworkRoutesExhausted:
            # Every route failed before the request went out (docs/NETWORK.md), so the platform
            # has nothing: a deferral, never an unknown send and its Watchdog fault.
            return failure('not_connected', 'No network route to the platform connected')
        if result.get('reply') is not None:
            self._session.remember_reply(thread, result['reply'])
        return result

    def max_text_bytes(self, thread: ThreadKey) -> int | None:
        """The bot's documented text limit (x-adapters ``maxTextBytes``): the outbox chunks longer text."""
        return adapter_descriptor(self._session.channel)['identities']['bot']['maxTextBytes']

    def history_since(self, thread: ThreadKey, since_ms: int) -> list[ChannelEvent]:
        return []  # bot platforms expose no thread history: an unknown send is sent again

    def send_attachment(self, thread: ThreadKey, path: str, *, kind: str, marker: str) -> dict[str, Any]:
        return failure('attachment_rejected', 'Bot channels take no attachments')

    def history_from(self, thread: ThreadKey, external_id: str) -> list[ChannelEvent]:
        return []


class BotSession:
    def __init__(self, owner: int, channel: str, *, store: ChannelStore | None = None, transport=None):
        if channel not in TRANSPORTS:
            raise ChannelError('invalid_channel', f'{channel} is not a bot channel')
        self.store = store or ChannelStore(owner)
        self.owner = self.store.owner
        self.channel = channel
        self.stopped = threading.Event()
        self._stop_requested = False
        self._wake = threading.Event()
        self._transport = transport
        self._transport_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._outcome: BaseException | None = None
        self.binding = account_binding(self.store.account(channel) or {})

    # ── lifecycle ────────────────────────────────────────────────────
    def transport(self):
        with self._transport_lock:
            if self._transport is None:
                module = importlib.import_module(TRANSPORTS[self.channel])
                self._transport = module.open_transport(self.owner, self.store.account(self.channel) or {},
                                                        store=self.store)
            return self._transport

    def start(self) -> None:
        self.transport()
        outbox.register_executor(self.owner, self.tick, channel=self.channel)
        for target, name in ((self._run, 'run'), (self._outbox_loop, 'outbox')):
            thread = threading.Thread(target=target, name=f'{self.channel}-{name}-{self.owner}', daemon=True)
            self._threads.append(thread)
            thread.start()

    def stop(self, timeout: float = 2.0) -> bool:
        self._stop_requested = True
        self.stopped.set()
        self._wake.set()
        outbox.register_executor(self.owner, None, channel=self.channel)
        transport = self._transport
        if transport is not None:
            try:
                transport.close()
            except Exception:
                logger.warning('[Channels] %s transport close failed owner=%s', self.channel, self.owner,
                               exc_info=True)
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

    def alive(self) -> bool:
        return not self.stopped.is_set() and any(thread.is_alive() for thread in self._threads)

    def tick(self) -> None:
        self._wake.set()

    def outcome(self) -> BaseException | None:
        return self._outcome

    def _status(self, state: str, detail: str = '', **kwargs) -> None:
        # A stopped session never overwrites the owner's newer decision (disable, new secret).
        self.store.set_status(self.channel, state, detail, **kwargs,
                              when=lambda account: bool(account.get('enabled')) and not self._stop_requested
                              and account_binding(account) == self.binding)

    def _run(self) -> None:
        try:
            self.transport().run(self)
            if not self._stop_requested:
                self._status('disconnected', 'The platform closed the connection; reconnecting at the next tick')
        except SessionEnded as ended:
            self._status(ended.state, ended.detail)
        except Exception as error:
            self._outcome = error
            logger.warning('[Channels] %s session failed owner=%s', self.channel, self.owner, exc_info=True)
            self._status('error', f'session failed: {getattr(error, "code", type(error).__name__)}')
        finally:
            self.stopped.set()
            self._wake.set()

    def _outbox_loop(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            if self.stopped.is_set():
                return
            try:
                self.execute_outbox()
            except Exception:
                logger.warning('[Channels] %s outbox pass failed owner=%s', self.channel, self.owner,
                               exc_info=True)

    def execute_outbox(self) -> int:
        return outbox.execute(self.owner, _Sender(self), channel=self.channel, store=self.store)

    # ── sink ─────────────────────────────────────────────────────────
    def connected(self, info: Mapping[str, Any]) -> None:
        self._status('connected', '', connected_at_ms=now_ms())
        self._wake.set()

    def deliver(self, batch: list[Inbound], checkpoint: Mapping[str, Any] | None = None) -> None:
        if self._stop_requested:
            return  # a late answer of a stopped session (a 微信 long poll) writes nothing
        events = []
        for message in batch:
            try:
                thread = ids.thread_key(self.channel, message.kind, message.peer)
            except ChannelError:
                logger.warning('[Channels] %s message with an unusable chat ID skipped owner=%s',
                               self.channel, self.owner)
                continue
            if message.reply:
                self.remember_reply(thread, message.reply)
            if self._pair(message, thread):
                continue
            events.append(self._event(message, thread))
        if events:
            ingress.ingest_batch(self.owner, events, store=self.store)
        if checkpoint:
            self.store.update_account(self.channel, lambda account: {**account, **dict(checkpoint)})
        self._wake.set()

    def remember_reply(self, thread: ThreadKey, reply: Mapping[str, Any]) -> None:
        value = dict(reply)
        self.store.update_thread(thread, lambda record: record if record.get('botReply') == value
                                 else {**record, 'botReply': value})

    def _event(self, message: Inbound, thread: ThreadKey) -> ChannelEvent:
        if message.sent_at_s is None:
            received = int(time.time())
            external = self.store.provider_message_id(
                self.channel, message.message_id, lambda: ids.external_id(received, message.message_id))
        else:
            external = ids.external_id(message.sent_at_s, message.message_id)
        title = message.title or (message.sender_name if thread.kind == 'uid' else '') or message.peer
        return ChannelEvent(thread=thread, external_id=external, sender_id=message.sender,
                            sender_name=message.sender_name or message.sender, text=message.text,
                            sent_at_ms=int(external[:10]) * 1000, thread_title=title)

    def _pair(self, message: Inbound, thread: ThreadKey) -> bool:
        """A pending pairing code sent by its author in a private chat binds that author."""
        if message.kind != 'uid' or message.sender != message.peer or not message.text:
            return False
        code = ((self.store.account(self.channel) or {}).get('pairing') or {}).get('code')
        if not code or _code_text(message.text) != _code_text(code):
            return False
        bound = []

        def change(account):
            if ((account.get('pairing') or {}).get('code')) != code:
                return account
            bound.append(True)
            account.update(identity={'uid': message.sender, 'name': message.sender_name or ''},
                           commandChat=str(thread), pairing=None)
            return account
        self.store.update_account(self.channel, change)
        if not bound:
            return False
        logger.info('[Channels] %s owner paired owner=%s', self.channel, self.owner)
        self.store.update_thread(thread, lambda record: record if record.get('title')
                                 else {**record, 'title': message.sender_name or message.sender})
        try:
            outbox.enqueue(self.owner, str(thread), PAIRED_TEXT, idempotency_key='pairing:' + message.message_id,
                           origin={'kind': 'tool', 'conversationId': '', 'turnId': ''}, store=self.store)
        except ChannelError as error:
            logger.warning('[Channels] %s pairing confirmation not enqueued: %s', self.channel, error.code)
        return True


__all__ = ['BotSession', 'PAIRED_TEXT', 'TRANSPORTS', 'account_binding']
