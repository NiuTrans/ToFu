"""Core endpoint of one channel adapter session, and the in-process binding.

Responsibility: what the core does at the protocol boundary for one
(owner, channel) adapter session, adding no policy of its own:

- ``open``: initialize (version negotiation among the core's N and N-1,
  descriptor validation) and ``start`` from the checkpoint the core persisted;
- ``events``: validate an ``EventBatch``, turn its message envelopes into
  ``ChannelEvent`` (``codec``) and ingest them through the existing ingress
  (echo, policy, CAS record, media, route, watermark: docs/CHANNELS.md
  C1/C2/C7/C9), record its edits and recalls on the messages they change
  (``ingress.apply_change``), in delivery order, then persist the batch's
  checkpoint; a failed ingest raises to the adapter before the checkpoint
  moves;
- ``health``: validate a transition and record it typed (``health_log``) and,
  unless a compatibility shim's legacy adapter already wrote it, as today's
  account status; every ``connected`` the adapter reports owes a catch-up,
  which the core runs (``catchup.CatchUp``: ``threads`` and ``history``, retried
  at ``tick`` while owed, live events meanwhile leave watermarks alone);
- ``sender()``: present the adapter to the existing outbox as its sender port,
  one ``OutboundIntent`` per platform message, ``Receipt`` back into the
  outbox's vocabulary (a deferral keeps the platform's time: its
  ``retryAfterMs``, else the descriptor's documented window; while the
  adapter reports ``rate_limited`` until ``resumeAtMs`` nothing is attempted);
  ``download_media``: the media port for ingress.

Every object crossing the boundary is validated against
``contracts/channel_protocol_v1.schema.json`` on the core side, whatever the
binding. ``InProcessLink`` is the in-process binding (the adapter object is
called directly); ``lib.channels.protocol.stdio`` is the out-of-process one.

Entry points: ``AdapterHost`` (``open``, ``events``, ``health``, ``tick``,
``sender``, ``close``), ``InProcessLink``, ``AdapterLink``.
Dependencies: ``contract``, ``codec``, ``catchup``, ``lib.channels.ingress``
(resolved at call time unless injected), ``ChannelStore``, ``policy`` (robot thread).
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from lib.channels.model import ChannelEvent, ChannelMedia, ThreadKey
from lib.channels.protocol.catchup import CatchUp
from lib.channels.protocol.codec import (
    change_from_envelope, event_from_envelope, legacy_from_health, legacy_from_receipt, thread_ref,
)
from lib.channels.protocol.contract import (
    CORE_FEATURES, ERRORS, HEALTH_CODES, METHODS, SUPPORTED_VERSIONS, ProtocolError, validate,
)
from lib.channels.store import ChannelStore
from lib.log import get_logger

logger = get_logger(__name__)

# Optional methods and the descriptor capability that admits each (x-methods).
_CAPABILITY_PATHS = {name: tuple(spec['capability'].split('.')) for name, spec in METHODS.items()
                     if spec.get('capability')}


class AdapterLink(Protocol):
    """A binding: carries core-to-adapter requests and delivers adapter-to-core calls to ``host``."""

    def bind(self, host: 'AdapterHost') -> None: ...

    def request(self, method: str, params: dict[str, Any]) -> Any: ...

    def close(self, timeout: float) -> bool: ...


class InProcessLink:
    """The in-process binding: the adapter object is called with the same validated objects."""

    def __init__(self, adapter):
        self.adapter = adapter

    def bind(self, host: 'AdapterHost') -> None:
        self.adapter.attach(host)

    def request(self, method: str, params: dict[str, Any]) -> Any:
        return getattr(self.adapter, method)(params)

    def close(self, timeout: float) -> bool:
        return True


def _valid_health(health: dict[str, Any]) -> dict[str, Any]:
    validate('Health', health)
    if HEALTH_CODES[health['code']]['state'] != health['state']:
        raise ProtocolError(ERRORS['invalidParams'],
                            f'Health code {health["code"]} belongs to {HEALTH_CODES[health["code"]]["state"]}')
    return health


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


class AdapterHost:
    """The core side of one adapter session (``CoreLink`` for the adapter)."""

    def __init__(self, owner: int, channel: str, link: AdapterLink, *, store: ChannelStore | None = None,
                 record_status: bool = True, ingest: Callable[..., Any] | None = None,
                 clock: Callable[[], float] = time.time):
        self.store = store or ChannelStore(owner)
        self.owner = self.store.owner
        self.channel = channel
        self.link = link
        # False for a compatibility shim whose legacy adapter writes the account status itself.
        self.record_status = record_status
        self._ingest = ingest
        self._clock = clock
        self.descriptor: dict[str, Any] | None = None
        self.version: int | None = None
        self.adapter_info: dict[str, Any] = {}
        self.health_log: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.catch_up = CatchUp(self)
        link.bind(self)

    # ── lifecycle ────────────────────────────────────────────────────
    def open(self, account: dict[str, Any] | None = None) -> dict[str, Any]:
        """initialize, then start from the persisted checkpoint; returns the adapter's descriptor."""
        params = validate('InitializeParams', {
            'protocolVersions': list(SUPPORTED_VERSIONS), 'channel': self.channel, 'account': dict(account or {}),
            'coreFeatures': list(CORE_FEATURES)})
        try:
            result = validate('InitializeResult', self.link.request('initialize', params))
            if result['protocolVersion'] not in SUPPORTED_VERSIONS:
                raise ProtocolError(ERRORS['unsupportedProtocolVersion'],
                                    f'The adapter chose protocol version {result["protocolVersion"]}',
                                    data={'supported': list(SUPPORTED_VERSIONS)})
        except ProtocolError as error:
            if error.code == ERRORS['unsupportedProtocolVersion']:
                self.record_health({'state': 'runtime_missing', 'code': 'protocol_version_unsupported',
                                    'detail': str(error), 'atMs': self._now_ms()})
            raise
        if result['capabilities']['channel'] != self.channel:
            raise ProtocolError(ERRORS['invalidParams'], 'The adapter declared another channel')
        self.version, self.descriptor = result['protocolVersion'], result['capabilities']
        self.adapter_info = dict(result['adapter'])
        checkpoint = (self.store.account(self.channel) or {}).get('protocolCheckpoint')
        self.link.request('start', validate('StartParams', {'checkpoint': checkpoint}))
        return self.descriptor

    def close(self, timeout: float) -> bool:
        self.catch_up.stop()
        if self.descriptor is not None:
            try:
                self.call('shutdown', {})
            except Exception as error:  # the binding may already be gone; closing proceeds
                logger.info('[Channels] %s adapter shutdown request failed: %s', self.channel,
                            getattr(error, 'code', type(error).__name__))
        closed = self.link.close(timeout)
        return self.catch_up.join(timeout) and closed

    def tick(self) -> None:
        """The supervisor's scheduler tick: an owed catch-up runs again (P3)."""
        self.catch_up.tick()

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    # ── core-to-adapter calls ────────────────────────────────────────
    def supports(self, method: str) -> bool:
        path = _CAPABILITY_PATHS.get(method)
        if path is None:
            return True
        value: Any = self.descriptor or {}
        for part in path:
            value = value.get(part) if isinstance(value, dict) else None
        return bool(value)

    def call(self, method: str, params: dict[str, Any]) -> Any:
        spec = METHODS[method]
        if spec['direction'] != 'core_to_adapter':
            raise ProtocolError(ERRORS['methodNotFound'], f'{method} is not a core-to-adapter method')
        if self.descriptor is None and method != 'initialize':
            raise ProtocolError(ERRORS['notInitialized'], 'The adapter session is not initialized')
        if not self.supports(method):
            raise ProtocolError(ERRORS['capabilityMissing'], f'The adapter declared no {spec["capability"]}')
        return validate(spec['result'], self.link.request(method, validate(spec['params'], params)))

    # ── adapter-to-core calls (CoreLink) ─────────────────────────────
    def events(self, batch: dict[str, Any], *, advance_watermark: bool = True) -> dict[str, Any]:
        """Ingest one ``EventBatch`` the adapter delivered.

        While a catch-up is owed, live events leave watermarks where its replay
        must start (C1); ``advance_watermark=False`` is a legacy adapter's own
        catch-up state (the compatibility shim).
        """
        return self.ingest_events(batch, advance_watermark=advance_watermark and not self.catch_up.owed)

    def ingest_events(self, batch: dict[str, Any], *, advance_watermark: bool) -> dict[str, Any]:
        """Ingest one ``EventBatch`` (live or the catch-up's replay) and persist its checkpoint."""
        validate('EventBatch', batch)
        if self.descriptor is None:
            raise ProtocolError(ERRORS['notInitialized'], 'The adapter session is not initialized')
        from lib.channels import ingress
        ingest = self._ingest or ingress.ingest_batch
        outcomes: list[str] = []
        messages: list[ChannelEvent] = []

        def flush() -> None:
            # Consecutive messages are one ingest (one delivery batch per target, C4).
            if messages:
                outcomes.extend(result.outcome for result in ingest(
                    self.owner, messages, store=self.store, media=self, advance_watermark=advance_watermark))
                messages.clear()
        for envelope in batch['events']:
            if envelope['type'] == 'message':
                messages.append(event_from_envelope(self.channel, envelope, store=self.store))
                continue
            # An edit or recall applies after the messages before it, in delivery order.
            flush()
            change = change_from_envelope(self.channel, envelope, store=self.store)
            outcomes.append(ingress.apply_change(self.owner, change, store=self.store).outcome)
        flush()
        if batch.get('checkpoint') is not None:
            checkpoint = batch['checkpoint']
            self.store.update_account(self.channel, lambda account: {**account, 'protocolCheckpoint': checkpoint})
        return validate('EventBatchAck', {'results': [
            {'eventId': envelope['eventId'], 'outcome': outcome}
            for envelope, outcome in zip(batch['events'], outcomes)]})

    def health(self, health: dict[str, Any]) -> None:
        """An adapter-reported transition; ``connected`` owes a catch-up, run off this thread (P3)."""
        connected = _valid_health(health)['state'] == 'connected'
        if connected:
            self.catch_up.owe()
        self.record_health(health)
        if connected:
            self.catch_up.start()

    def record_health(self, health: dict[str, Any]) -> None:
        _valid_health(health)
        with self._lock:
            self.health_log.append(dict(health))
        if self.record_status:
            state, detail = legacy_from_health(health)
            self.store.set_status(self.channel, state, detail,
                                  connected_at_ms=health['atMs'] if health['state'] == 'connected' else None)

    @property
    def last_health(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self.health_log[-1]) if self.health_log else None

    # ── ports for the existing core ──────────────────────────────────
    def download_media(self, item: ChannelMedia, path: str) -> dict[str, Any]:
        """``lib.channels.media.MediaPort``: the original behind ``item``'s locator."""
        if not self.supports('download'):
            return {'ok': False, 'detail': 'this channel cannot download media'}
        result = self.call('download', {'locator': item.source, 'path': path})
        return {'ok': result['ok'], 'contentType': result.get('contentType', ''),
                'detail': result.get('detail', '')}

    def identity_for(self, thread: ThreadKey) -> str:
        """The outbound identity of a send (the fallback chain starts at bot only in the robot's chat, C6)."""
        identities = (self.descriptor or {}).get('identities') or {}
        if 'self' not in identities:
            return 'bot'
        if 'bot' in identities:
            from lib.channels.policy import robot_thread
            if robot_thread(self.store.account(self.channel)) == str(thread):
                return 'bot'
        return 'self'

    def sender(self) -> 'HostSender':
        return HostSender(self)


class HostSender:
    """``lib.channels.outbox.OutboxSender`` over the protocol: one intent per platform message."""

    def __init__(self, host: AdapterHost):
        self.host = host

    def _intent(self, thread: ThreadKey, marker: str, part: dict[str, Any]) -> dict[str, Any]:
        return {'intentId': marker, 'idempotencyKey': marker, 'thread': thread_ref(thread),
                'identity': self.host.identity_for(thread), 'part': part}

    def _send(self, intent: dict[str, Any]) -> dict[str, Any]:
        held = self._rate_limit_remaining_ms()
        if held:
            # Not attempted: the adapter reported the platform's rate limit until resumeAtMs (P4, P6).
            return {'ok': False, 'clientId': '', 'retryAfterMs': held,
                    'error': {'code': 'rate_limited', 'message': 'the platform rate limit holds sends'}}
        receipt = self.host.call('send', intent)
        if receipt['intentId'] != intent['intentId']:
            raise ProtocolError(ERRORS['invalidParams'], 'The receipt names another intent')
        if (receipt['status'], receipt.get('code'), receipt.get('retryAfterMs')) == ('deferred', 'rate_limited', None):
            window = self._documented_window_ms(intent['identity'])
            if window is not None:
                receipt = {**receipt, 'retryAfterMs': window}
        return legacy_from_receipt(receipt)

    def _rate_limit_remaining_ms(self) -> int | None:
        health = self.host.last_health or {}
        if health.get('state') != 'rate_limited' or not health.get('resumeAtMs'):
            return None
        remaining = int(health['resumeAtMs']) - self.host._now_ms()
        return remaining if remaining > 0 else None

    def _documented_window_ms(self, identity: str) -> int | None:
        """A rate-limit refusal that names no time waits out the identity's shortest documented window
        (descriptor rateLimits, platform facts): the first moment a retry can be accepted; a refusal
        then defers again. Without a documented window the next pass tries again."""
        limits = (((self.host.descriptor or {}).get('identities') or {}).get(identity) or {}).get('rateLimits') or []
        return min((int(limit['windowMs']) for limit in limits), default=None)

    def send(self, thread: ThreadKey, text: str, *, marker: str,
             reply_to_external_id: str | None) -> dict[str, Any]:
        identity = (self.host.descriptor or {}).get('identities', {}).get(self.host.identity_for(thread)) or {}
        fmt = (identity.get('formats') or ['markdown'])[0]
        intent = self._intent(thread, marker, {'type': 'text', 'format': fmt, 'text': text})
        if reply_to_external_id:
            intent['replyTo'] = {'messageId': reply_to_external_id, 'seq': reply_to_external_id}
        return self._send(intent)

    def send_attachment(self, thread: ThreadKey, path: str, *, kind: str, marker: str) -> dict[str, Any]:
        part = {'type': 'attachment', 'kind': kind, 'name': os.path.basename(path), 'path': path,
                'sizeBytes': os.path.getsize(path), 'sha256': _sha256(path)}
        return self._send(self._intent(thread, marker, part))

    def _history(self, thread: ThreadKey, bound: dict[str, Any]) -> list[ChannelEvent]:
        if not self.host.supports('history'):
            return []  # today's outbox then sends an unknown item again (docs/CHANNEL_PROTOCOL.md P4)
        page = self.host.call('history', {'thread': thread_ref(thread), **bound})
        return [event_from_envelope(self.host.channel, envelope, store=self.host.store)
                for envelope in page['events'] if envelope['type'] == 'message']

    def history_since(self, thread: ThreadKey, since_ms: int) -> list[ChannelEvent]:
        return self._history(thread, {'sinceMs': int(since_ms)})

    def history_from(self, thread: ThreadKey, external_id: str) -> list[ChannelEvent]:
        if not ((self.host.descriptor or {}).get('history') or {}).get('byMessageId'):
            return []
        return self._history(thread, {'fromSeq': str(external_id)})

    def max_text_bytes(self, thread: ThreadKey) -> int | None:
        """The chosen identity's documented text limit (descriptor ``maxTextBytes``): the outbox sends a
        longer text as chunks, one intent each, reconciled by their markers (P4, P5). A markerless identity
        on a platform with history (大象's robot) is reconciled by its exact posted texts instead, which only
        its own send path records per item (C6 ``robotTexts``/``robotPosted``), so its texts are not chunked here."""
        descriptor = self.host.descriptor or {}
        identity = (descriptor.get('identities') or {}).get(self.host.identity_for(thread)) or {}
        if not identity.get('markers') and (descriptor.get('history') or {}).get('read'):
            return None
        return identity.get('maxTextBytes')


__all__ = ['AdapterHost', 'AdapterLink', 'HostSender', 'InProcessLink']
