"""Conformance harness: the compatibility shim for today's adapters and the driver base.

The shim puts today's adapters behind the protocol without changing them,
through three seams inside the test process:

1. inbound: ``lib.channels.ingress.ingest``/``ingest_batch``, where every
   adapter delivers ``ChannelEvent`` facts, become envelopes
   (``codec.envelope_from_event``) that an ``AdapterHost`` validates and ingests
   through the original ingress;
2. outbound: the driver runs the outbox with ``host.sender()`` (one intent and
   receipt per platform message) over ``LegacyProtocolAdapter``, which calls
   the adapter's own sender port;
3. health: each account status the adapter writes is recorded as typed health
   through the contract's ``x-legacy`` map.

A driver is one adapter against its platform fake. It exposes the platform
verbs the cases use; a verb its platform cannot express raises
``NotApplicable`` with the reason the baseline records.
"""
from __future__ import annotations

import hashlib
import itertools
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from lib.channels.ingress import IngestResult
from lib.channels.model import ChannelMedia, ThreadKey
from lib.channels.protocol import codec
from lib.channels.protocol.adapter import ProtocolAdapter
from lib.channels.protocol.contract import adapter_descriptor
from lib.channels.protocol.host import AdapterHost, InProcessLink
from lib.channels.store import now_ms
from tests.channels.conftest import OWNER, ROUTER


class NotApplicable(Exception):
    """The platform offers nothing this case could exercise (the baseline names why)."""


def eventually(check, timeout: float = 10.0, *, what: str = 'condition'):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f'{what} not reached in time')
        time.sleep(0.01)


def unique(label: str) -> str:
    return f'{label} {uuid.uuid4().hex[:10]}'


@dataclass(frozen=True)
class Msg:
    """One platform message a case created, as the core should record it."""

    thread: ThreadKey
    text: str
    platform_id: str = ''
    extra: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass
class Env:
    """The per-test fakes every driver uses (tests/channels/conftest.py)."""

    store: Any
    records: Any
    dispatch: Any
    monkeypatch: Any
    tmp_path: Any
    vault: Any = None


class Core:
    """What the core recorded and routed: the observations cases assert on."""

    def __init__(self, store, dispatch):
        self.store, self.dispatch = store, dispatch

    def events(self, thread: ThreadKey) -> list[dict[str, Any]]:
        rows, before = [], ''
        while True:
            page, before = self.store.event_page(thread, before=before)
            rows.extend(page)
            if not before:
                return rows

    def with_text(self, thread: ThreadKey, text: str) -> list[dict[str, Any]]:
        return [row for row in self.events(thread) if text in (row.get('text') or '')]

    def routed(self, record: dict[str, Any]) -> int:
        """How many turn dispatches carried this event (any target)."""
        return sum(1 for call in self.dispatch.calls
                   if record['eventId'] in (((call['message'].get('origin') or {}).get('channel') or {})
                                            .get('eventIds') or ()))

    def item(self, item_id: str):
        return self.store.outbox_item(item_id)[0]

    def thread(self, thread: ThreadKey) -> dict[str, Any]:
        return self.store.thread(thread) or {}


class LegacyProtocolAdapter(ProtocolAdapter):
    """Today's adapter as a protocol adapter: its own sender and media ports, the contract's descriptor."""

    name = 'legacy-compatibility-shim'
    version = '1'

    def __init__(self, channel: str, *, sender_source):
        super().__init__()
        self.channel = channel
        self._sender_source = sender_source
        self._media: dict[str, tuple[Any, ChannelMedia]] = {}
        self._handles = itertools.count(1)

    def capabilities(self) -> dict[str, Any]:
        return adapter_descriptor(self.channel)

    def start(self, params: dict[str, Any]) -> dict[str, Any]:
        return {}  # today's adapter runs its own session

    def register_media(self, port, item: ChannelMedia) -> str:
        handle = f'media-{next(self._handles)}'
        self._media[handle] = (port, item)
        return handle

    def send(self, intent: dict[str, Any]) -> dict[str, Any]:
        sender = self._sender_source()
        thread = codec.thread_key(self.channel, intent['thread'])
        part = intent['part']
        try:
            if part['type'] == 'attachment':
                result = sender.send_attachment(thread, part['path'], kind=part['kind'], marker=intent['intentId'])
            else:
                result = sender.send(thread, part['text'], marker=intent['intentId'],
                                     reply_to_external_id=(intent.get('replyTo') or {}).get('seq'))
        except Exception as error:  # today's outbox counts a raised send as unknown
            return {'intentId': intent['intentId'], 'identity': intent['identity'], 'status': 'unknown',
                    'code': 'adapter_failed', 'detail': type(error).__name__}
        return codec.receipt_from_legacy(intent, result)

    def history(self, query: dict[str, Any]) -> dict[str, Any]:
        sender = self._sender_source()
        thread = codec.thread_key(self.channel, query['thread'])
        if 'fromSeq' in query:
            events = sender.history_from(thread, query['fromSeq'])
        else:
            events = sender.history_since(thread, int(query.get('sinceMs') or 0))
        return {'events': [codec.envelope_from_event(event, source='history',
                                                     locator=lambda item: self.register_media(None, item))
                           for event in events]}

    def download(self, request: dict[str, Any]) -> dict[str, Any]:
        port, item = self._media.get(request['locator'], (None, None))
        if port is None:
            return {'ok': False, 'detail': 'this original is no longer reachable'}
        result = port.download_media(item, request['path'])
        answer = {'ok': bool(result.get('ok'))}
        for key in ('contentType', 'detail'):
            if result.get(key):
                answer[key] = str(result[key])
        return answer


class Shim:
    """Today's adapter of ``channel`` behind an ``AdapterHost`` (the three seams above)."""

    def __init__(self, env: Env, channel: str, *, sender_source):
        from lib.channels import ingress
        self.env, self.channel, self.store = env, channel, env.store
        self.adapter = LegacyProtocolAdapter(channel, sender_source=sender_source)
        self._original_ingest = ingress.ingest_batch
        self.host = AdapterHost(OWNER, channel, InProcessLink(self.adapter), store=self.store,
                                record_status=False, ingest=self._original_ingest)
        self.host.open()
        self.envelopes: list[dict[str, Any]] = []
        mode = self.host.descriptor['inbound']['mode']
        self._source = 'poll' if mode == 'poll' else 'live'
        env.monkeypatch.setattr(ingress, 'ingest_batch', self._ingest_batch)
        env.monkeypatch.setattr(ingress, 'ingest', self._ingest)
        original_status = self.store.set_status

        def set_status(channel_name, state, detail='', **kwargs):
            before = (self.store.account(channel_name) or {}).get('status') or {}
            account = original_status(channel_name, state, detail, **kwargs)
            after = (account or {}).get('status') or {}
            if channel_name == self.channel and (after.get('state'), after.get('detail')) != (
                    before.get('state'), before.get('detail')):
                health = codec.health_from_legacy(after.get('state') or 'disabled', after.get('detail') or '',
                                                  at_ms=now_ms())
                if health is not None:
                    self.host.record_health(health)
            return account
        env.monkeypatch.setattr(self.store, 'set_status', set_status)

    def _ingest_batch(self, owner, events, *, store=None, media=None, advance_watermark=True):
        events = list(events)
        if int(owner) != OWNER or any(event.channel != self.channel for event in events):
            return self._original_ingest(owner, events, store=store, media=media,
                                         advance_watermark=advance_watermark)
        if not events:
            return []
        envelopes = [codec.envelope_from_event(event, source=self._source,
                                               locator=lambda item: self.adapter.register_media(media, item))
                     for event in events]
        self.envelopes.extend(envelopes)
        ack = self.host.events({'events': envelopes}, advance_watermark=advance_watermark)
        return [IngestResult(result['eventId'], result['outcome']) for result in ack['results']]

    def _ingest(self, owner, event, *, store=None, media=None, advance_watermark=True):
        return self._ingest_batch(owner, [event], store=store, media=media, advance_watermark=advance_watermark)[0]

    def execute(self) -> int:
        from lib.channels import outbox
        return outbox.execute(OWNER, self.host.sender(), channel=self.channel, store=self.store)


class Driver:
    """One adapter against its platform fake; the platform verbs the cases use."""

    name = ''
    channel = ''

    def __init__(self, env: Env):
        self.env = env
        self.store = env.store
        self.core = Core(env.store, env.dispatch)
        self.shim: Shim | None = None
        self.host: AdapterHost | None = None

    # lifecycle ---------------------------------------------------------
    def setup(self) -> None:
        raise NotImplementedError

    def teardown(self) -> None:
        pass

    @property
    def descriptor(self) -> dict[str, Any]:
        return self.host.descriptor

    def health(self) -> dict[str, Any] | None:
        return self.host.last_health

    def connect(self) -> None:
        raise NotImplementedError

    def settle(self) -> None:
        """Wait until the adapter has handed everything the platform pushed to the core."""

    def execute(self) -> None:
        """One outbox pass as the adapter's own executor would run it now."""
        try:
            self.shim.execute()
        except Exception as error:  # every adapter's executor logs a failed pass and keeps running
            self.last_execute_error = error

    def alive(self) -> bool:
        raise NotImplementedError

    # threads -----------------------------------------------------------
    def command_thread(self) -> ThreadKey:
        raise NotApplicable('this platform has no command chat')

    def watched_thread(self) -> ThreadKey:
        raise NotImplementedError

    def send_thread(self) -> ThreadKey:
        """Where Tofu's own sends go in the outbound cases: the command chat where there is one."""
        return self.command_thread()

    def prepare_send(self, thread: ThreadKey) -> None:
        """Whatever the platform needs before Tofu can write there (a reply context)."""

    # inbound verbs -----------------------------------------------------
    def post(self, thread: ThreadKey, text: str) -> Msg:
        raise NotImplementedError

    def redeliver(self, msg: Msg) -> None:
        raise NotImplementedError

    def pair(self, thread: ThreadKey) -> tuple[Msg, Msg]:
        """An older and a newer message, neither delivered yet."""
        raise NotImplementedError

    def deliver(self, msg: Msg) -> None:
        raise NotImplementedError

    def edit(self, msg: Msg, text: str) -> None:
        raise NotApplicable('the platform delivers no edits')

    def recall(self, msg: Msg) -> None:
        raise NotApplicable('the platform delivers no recalls')

    def bot_post(self, text: str) -> Msg:
        """A message of another bot, in the thread where this platform delivers one."""
        raise NotApplicable('the platform delivers no messages of other bots')

    # history -----------------------------------------------------------
    def post_unseen(self, thread: ThreadKey, text: str) -> Msg:
        """A message only the platform's history holds (sent while the live stream was down)."""
        raise NotApplicable('the platform keeps no history for this adapter')

    def fail_next_history(self) -> None:
        raise NotApplicable('the platform keeps no history for this adapter')

    def reconnect(self) -> None:
        raise NotApplicable('the platform keeps no history for this adapter')

    def tick(self) -> None:
        """The supervisors' scheduler tick."""

    # outbound verbs ----------------------------------------------------
    def enqueue(self, thread: ThreadKey, text: str, **extra):
        from lib.channels import outbox
        return outbox.enqueue(OWNER, str(thread), text, idempotency_key=unique('key'),
                              origin={'kind': 'tool', 'conversationId': ROUTER, 'turnId': 'turn-1'},
                              store=self.store, **extra)

    def received(self, thread: ThreadKey) -> list[str]:
        """Texts the platform received in ``thread`` from Tofu."""
        raise NotImplementedError

    def lose_next_answer(self) -> None:
        raise NotImplementedError

    def deliver_without_receipt(self, item) -> None:
        """The platform got ``item``'s message, then the process died before the receipt."""
        raise NotImplementedError

    def send_threads(self) -> tuple[ThreadKey, ThreadKey]:
        """Two threads where the owner's own request is sent without a draft (the send thread and one more)."""
        raise NotApplicable('the platform offers Tofu one thread to send to')

    def fail_reconciliation_reads(self, thread: ThreadKey) -> None:
        """Every read that could prove a send in ``thread`` missing fails until ``heal_reconciliation_reads``."""
        raise NotApplicable('the platform keeps no history for this adapter')

    def heal_reconciliation_reads(self, thread: ThreadKey) -> None:
        raise NotImplementedError

    def accept_files_unseen(self, thread: ThreadKey) -> None:
        """The platform accepts file messages in ``thread`` and answers with their IDs, but neither echoes them nor
        shows them in the thread until ``show_files``. This is 大象's documented false success (C10)."""
        raise NotApplicable('the platform carries no files natively')

    def show_files(self, thread: ThreadKey) -> None:
        raise NotImplementedError

    def file_sends(self, thread: ThreadKey) -> int:
        """How many file messages the platform received in ``thread`` from Tofu."""
        raise NotImplementedError

    def bot_thread(self) -> ThreadKey:
        """A thread where Tofu's sends go out under the bot identity first, the owner's next (P7)."""
        raise NotApplicable('the platform has one outbound identity')

    def lose_next_bot_answer(self) -> None:
        """The bot identity's next send reaches the platform, but its answer is lost."""
        raise NotImplementedError

    def received_as(self, thread: ThreadKey) -> list[tuple[str, str]]:
        """``(identity, text)`` of every message Tofu's sends put into ``thread``."""
        raise NotImplementedError

    def claim(self, item):
        """Mark ``item`` sending the way an executor does before its network call."""
        current, version = self.store.outbox_item(item.id)
        current.state, current.attempt_at_ms = 'sending', now_ms()
        self.store.save_outbox_item(current, version)
        return current

    def restart(self) -> None:
        raise NotImplementedError

    def rate_limit_sends(self) -> None:
        raise NotApplicable('the platform documents no rate-limit refusal for this identity')

    def end_rate_limit(self) -> None:
        raise NotImplementedError

    def attempts(self, item) -> int:
        """How many times the platform saw a send attempt of ``item`` (accepted or refused)."""
        raise NotImplementedError

    def echo(self, item) -> None:
        raise NotApplicable('the platform does not deliver this account\'s own sends back')

    def self_send_outside_outbox(self, text: str) -> Msg:
        """This account's message sent by another Tofu path (stamped with Tofu's marker, no outbox item),
        delivered the way the platform brings it back (history backfill where there is one)."""
        raise NotApplicable('the platform does not deliver this account\'s own sends back')

    # faults: each returns once the adapter and its supervisor reported the outcome ----
    def expire_auth(self) -> None:
        raise NotImplementedError

    def displace(self) -> None:
        raise NotApplicable('the platform documents no event for another login taking the session')

    def assert_no_restart(self, *, boot: bool = False) -> None:
        """The supervisor's next tick (``boot``: a restarted server's first) leaves an owner-only state alone."""

    def runtime_fault(self) -> None:
        raise NotApplicable('this adapter has no runtime of its own beside the server')

    def network_fault(self) -> None:
        raise NotImplementedError


def advance_core_clock(env: Env, ms: int) -> None:
    """Time passes for the core: the outbox's clock moves ``ms`` ahead of real time (a platform's
    rate-limit window ending), for the rest of the test."""
    from lib.channels import outbox, store
    offset = getattr(env, '_core_clock_offset_ms', 0) + int(ms)
    env._core_clock_offset_ms = offset
    env.monkeypatch.setattr(outbox, 'now_ms', lambda: store.now_ms() + offset)


def record_outbox_faults(env: Env) -> list[tuple[str, str, str | None]]:
    """The ``(item, state, error)`` of every Router Watchdog outbox fault the core raises from now on (R7)."""
    faults: list[tuple[str, str, str | None]] = []
    env.monkeypatch.setattr('lib.router.watchdog.signals.report_outbox_fault',
                            lambda owner, item: faults.append((item.id, item.state, item.error)))
    return faults


def attachment_record(tmp_path, name: str = 'report.pdf') -> dict[str, Any]:
    """An admitted attachment record (lib/channels/attachments.py shape) for a file in ``tmp_path``."""
    data = b'%PDF-1.4 conformance\n'
    path = tmp_path / name
    path.write_bytes(data)
    return {'name': name, 'type': 'application/pdf', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'kind': 'file', 'renditionOf': None, 'source': {'path': str(path), 'roots': [str(tmp_path)]},
            'state': 'pending', 'error': None}


__all__ = ['Core', 'Driver', 'Env', 'LegacyProtocolAdapter', 'Msg', 'NotApplicable', 'Shim', 'attachment_record',
           'eventually', 'unique']
