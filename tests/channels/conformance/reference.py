"""The protocol-native reference adapter, its fake platform and its drivers.

``ReferencePlatform`` is a chat platform in memory with every capability the
protocol names (push, history, threads, edits, recalls, sender kinds, echoes,
markers, idempotency lookup) and fault controls. ``ReferenceAdapter`` speaks
the protocol over it exactly as docs/CHANNEL_PROTOCOL.md says, so every case it
fails on today's core is a core gap, not an adapter gap. It runs in-process
(``ReferenceDriver``) and in a child process over the stdio binding
(``ReferenceStdioDriver``, entry point ``python -m tests.channels.conformance.reference``).

The core knows a channel only through the channels contract, so the drivers
add the test-only channel ``ref`` to its vocabulary for the test's duration
(``install_reference_channel``): exactly the edits a new chat app needs in
contracts/channels_v1.schema.json today (docs/CHANNEL_PROTOCOL.md, Adding a chat app).
"""
from __future__ import annotations

import itertools
import queue
import re
import sys
import threading
import time
from typing import Any

from lib.channels.protocol.adapter import ProtocolAdapter
from lib.channels.protocol.contract import ERRORS, ProtocolError

CHANNEL = 'ref'
OWNER_ID = 'ref-owner'
OTHER_ID = 'ref-alice'
BOT_ID = 'ref-alertbot'
GROUP_ID = 'ref-team'
# The time a rate-limited send's receipt names (Receipt.retryAfterMs).
RATE_LIMIT_RETRY_AFTER_MS = 60_000


class ReferencePlatform:
    """Messages per thread, this account's sends, and fault controls."""

    def __init__(self):
        self.lock = threading.RLock()
        self.clock_ms = 0
        self.threads: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.attempts: dict[str, int] = {}
        self._ids = itertools.count(1)
        self.lose_next = False
        self.rate_limited = False
        self.history_failures = 0
        self.network_down = False

    def add(self, kind: str, thread_id: str, *, sender: str, text: str, sender_kind: str = 'user',
            marker: str = '', time_ms: int | None = None) -> dict[str, Any]:
        with self.lock:
            number = next(self._ids)
            at = self.now() if time_ms is None else int(time_ms)
            self.clock_ms = max(self.clock_ms, at) + 1
            message = {'id': f'm{number}', 'seq': f'{at:013d}{number:06d}', 'timeMs': at, 'kind': kind,
                       'threadId': thread_id, 'sender': sender, 'senderKind': sender_kind, 'text': text,
                       'marker': marker, 'revisions': [], 'recalled': False}
            self.threads.setdefault((kind, thread_id), []).append(message)
            return message

    def now(self) -> int:
        """Platform time: this host's clock, never repeating a message time (the core's catch-up
        windows compare it with its own clock, as with a real platform)."""
        with self.lock:
            return max(int(time.time() * 1000), self.clock_ms)

    def find(self, message_id: str) -> dict[str, Any]:
        with self.lock:
            return next(message for messages in self.threads.values() for message in messages
                        if message['id'] == message_id)

    def by_marker(self, kind: str, thread_id: str, marker: str) -> dict[str, Any] | None:
        with self.lock:
            return next((message for message in self.threads.get((kind, thread_id), ())
                         if message['marker'] == marker), None)


def envelope(message: dict[str, Any], *, source: str = 'live') -> dict[str, Any]:
    event: dict[str, Any] = {
        'type': 'message', 'eventId': f'{message["kind"]}:{message["threadId"]}/{message["id"]}',
        'thread': {'kind': message['kind'], 'id': message['threadId']},
        'message': {'id': message['id'], 'seq': message['seq'], 'timeMs': message['timeMs']},
        'sender': {'id': message['sender'], 'kind': message['senderKind']},
        'parts': [{'type': 'text', 'text': message['text']}], 'source': source}
    if message['marker']:
        event['echo'] = {'marker': message['marker']}
    return event


class ReferenceAdapter(ProtocolAdapter):
    """The protocol, implemented as specified, over ``ReferencePlatform``."""

    name = 'reference'
    version = '1'

    def __init__(self, platform: ReferencePlatform):
        super().__init__()
        self.platform = platform
        self._outgoing: queue.Queue = queue.Queue()
        self._busy = 0
        self._busy_lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def capabilities(self) -> dict[str, Any]:
        identity = {'notifiesOwner': False, 'formats': ['markdown'], 'maxTextBytes': None, 'quoteReply': True,
                    'markers': True, 'attachments': {}, 'rateLimits': []}
        return {'channel': CHANNEL, 'threadKinds': ['uid', 'gid'], 'commandChat': True,
                'inbound': {'mode': 'push', 'senderKinds': ['user', 'bot', 'system'], 'mentions': True,
                            'quotes': True, 'edits': True, 'recalls': True, 'attachments': [], 'echoes': True},
                'history': {'read': True, 'threads': True, 'byMessageId': True},
                'outbound': {'lookup': True, 'idempotentResend': 'none', 'requiresInboundFirst': False,
                             'replyWindows': None},
                'identities': {'self': identity}}

    # session -----------------------------------------------------------
    def start(self, params: dict[str, Any]) -> dict[str, Any]:
        self._worker = threading.Thread(target=self._deliver, name='reference-adapter', daemon=True)
        self._worker.start()
        self.core.health({'state': 'connected', 'code': 'ok', 'atMs': self.platform.now()})
        return {}

    def shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        self._outgoing.put(None)
        return {}

    def _deliver(self) -> None:
        while True:
            batch = self._outgoing.get()
            if batch is None:
                return
            try:
                self.core.events({'events': batch})
            except Exception as error:  # at least once: the batch comes again with the next delivery
                print(f'reference adapter: delivery deferred: {getattr(error, "code", type(error).__name__)}',
                      file=sys.stderr)
            finally:
                with self._busy_lock:
                    self._busy -= 1

    def push(self, events: list[dict[str, Any]]) -> None:
        with self._busy_lock:
            self._busy += 1
        self._outgoing.put(events)

    def idle(self) -> bool:
        with self._busy_lock:
            return self._busy == 0

    def identity(self, params: dict[str, Any]) -> dict[str, Any]:
        return {'id': OWNER_ID}

    # outbound ----------------------------------------------------------
    def send(self, intent: dict[str, Any]) -> dict[str, Any]:
        platform, key = self.platform, intent['idempotencyKey']
        receipt: dict[str, Any] = {'intentId': intent['intentId'], 'identity': intent['identity']}
        with platform.lock:
            platform.attempts[key] = platform.attempts.get(key, 0) + 1
            if platform.network_down:
                return {**receipt, 'status': 'deferred', 'code': 'not_connected'}
            if platform.rate_limited:
                return {**receipt, 'status': 'deferred', 'code': 'rate_limited',
                        'retryAfterMs': RATE_LIMIT_RETRY_AFTER_MS}
            part = intent['part']
            if part['type'] != 'text':
                return {**receipt, 'status': 'failed', 'code': 'attachment_rejected'}
            thread = intent['thread']
            message = platform.add(thread['kind'], thread['id'], sender=OWNER_ID, text=part['text'], marker=key)
            lost, platform.lose_next = platform.lose_next, False
        if lost:
            return {**receipt, 'status': 'unknown', 'code': 'answer_lost'}
        return {**receipt, 'status': 'sent', 'platformMessageId': message['id']}

    def lookup(self, query: dict[str, Any]) -> dict[str, Any]:
        thread = query['thread']
        message = self.platform.by_marker(thread['kind'], thread['id'], query['idempotencyKey'])
        return {'found': message is not None, 'platformMessageId': message['id'] if message else None}

    def history(self, query: dict[str, Any]) -> dict[str, Any]:
        platform, thread = self.platform, query['thread']
        with platform.lock:
            if platform.history_failures:
                platform.history_failures -= 1
                raise ProtocolError(ERRORS['platformFailure'], 'The platform failed the history read')
            messages = list(platform.threads.get((thread['kind'], thread['id']), ()))
        if 'afterSeq' in query:
            messages = [m for m in messages if int(m['seq']) > int(query['afterSeq'])]
        if 'fromSeq' in query:
            messages = [m for m in messages if int(m['seq']) >= int(query['fromSeq'])]
        if 'sinceMs' in query:
            messages = [m for m in messages if m['timeMs'] >= int(query['sinceMs'])]
        return {'events': [envelope(m, source='history') for m in messages if not m['recalled']]}

    def threads(self, query: dict[str, Any]) -> dict[str, Any]:
        """Threads with any activity in [sinceMs, untilMs), also those active again since."""
        with self.platform.lock:
            found = [{'thread': {'kind': kind, 'id': thread_id}, 'updatedMs': messages[-1]['timeMs']}
                     for (kind, thread_id), messages in self.platform.threads.items()
                     if any(query['sinceMs'] <= m['timeMs'] < query['untilMs'] for m in messages)]
        return {'threads': found}

    # test controls (the platform's own behaviour, not protocol) ----------
    def control(self, name: str, params: dict[str, Any]) -> Any:
        platform = self.platform
        if name == 'post':
            message = platform.add(params['kind'], params['threadId'], sender=params['sender'],
                                   sender_kind=params.get('senderKind', 'user'), text=params['text'],
                                   marker=params.get('marker', ''), time_ms=params.get('timeMs'))
            if params.get('deliver', True):
                self.push([envelope(message)])
            return {'id': message['id'], 'seq': message['seq']}
        if name == 'deliver':
            self.push([envelope(platform.find(params['id']))])
            return {}
        if name == 'edit':
            message = platform.find(params['id'])
            message['revisions'].append(params['text'])
            revision = {**envelope(message), 'type': 'edit',
                        'eventId': f'{message["kind"]}:{message["threadId"]}/{message["id"]}#'
                                   f'{len(message["revisions"])}',
                        'target': {'messageId': message['id'], 'seq': message['seq']},
                        'parts': [{'type': 'text', 'text': params['text']}]}
            revision['message'] = {'id': f'{message["id"]}#{len(message["revisions"])}', 'timeMs': platform.now()}
            self.push([revision])
            return {}
        if name == 'recall':
            message = platform.find(params['id'])
            message['recalled'] = True
            self.push([{**envelope(message), 'type': 'recall', 'eventId': f'{message["kind"]}:'
                        f'{message["threadId"]}/{message["id"]}#recall', 'parts': [],
                        'target': {'messageId': message['id'], 'seq': message['seq']},
                        'message': {'id': f'{message["id"]}#recall', 'timeMs': platform.now()}}])
            return {}
        if name == 'echo':
            thread = params['thread']
            message = platform.by_marker(thread['kind'], thread['id'], params['marker'])
            self.push([envelope(message)])
            return {}
        if name == 'deliver_without_receipt':
            thread = params['thread']
            platform.add(thread['kind'], thread['id'], sender=OWNER_ID, text=params['text'], marker=params['marker'])
            return {}
        if name == 'received':
            with platform.lock:
                return {'texts': [m['text'] for m in platform.threads.get((params['kind'], params['threadId']), ())
                                  if m['marker']]}
        if name == 'attempts':
            return {'count': platform.attempts.get(params['key'], 0)}
        if name == 'set':
            for key in ('lose_next', 'rate_limited', 'history_failures', 'network_down'):
                if key in params:
                    setattr(platform, key, params[key])
            return {}
        if name == 'health':
            self.core.health(params['health'])
            return {}
        if name == 'idle':
            return {'idle': self.idle()}
        raise ProtocolError(ERRORS['methodNotFound'], f'no control {name!r}')


def install_reference_channel(monkeypatch) -> None:
    """Add the test-only channel ``ref`` (base32 thread keys) to the channels vocabulary."""
    import lib.channels.model as model
    monkeypatch.setattr(model, 'CHANNELS', (*model.CHANNELS, CHANNEL))
    monkeypatch.setattr(model, 'BOT_CHANNELS', (*model.BOT_CHANNELS, CHANNEL))
    monkeypatch.setitem(model._KINDS_BY_CHANNEL, CHANNEL, frozenset({'uid', 'gid'}))
    monkeypatch.setitem(model.CHANNEL_TRAITS, CHANNEL,
                        {'label': 'Reference', 'commandChat': True, 'quoteReply': True, 'attachments': False})
    for name in ('_THREAD_KEY_RE', '_EVENT_ID_RE'):
        pattern = getattr(model, name).pattern.replace('(wecom|qq|wechat)', f'(wecom|qq|wechat|{CHANNEL})')
        monkeypatch.setattr(model, name, re.compile(pattern))


def main() -> int:
    """Run the reference adapter over the stdio binding (its platform lives in this process)."""
    from lib.channels.protocol.stdio import serve
    adapter = ReferenceAdapter(ReferencePlatform())
    controls = {f'test/{name}': (lambda params, name=name: adapter.control(name, params))
                for name in ('post', 'deliver', 'edit', 'recall', 'echo', 'deliver_without_receipt', 'received',
                             'attempts', 'set', 'health', 'idle')}
    return serve(adapter, extra_methods=controls)


if __name__ == '__main__':
    raise SystemExit(main())
