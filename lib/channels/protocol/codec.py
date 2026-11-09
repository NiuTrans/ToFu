"""Channel protocol codec: protocol objects <-> the core's shapes and today's vocabulary.

Responsibility: the core-owned translations of docs/CHANNEL_PROTOCOL.md, each
reproducing today's durable identities exactly (Backward compatibility):

- thread keys from ``ThreadRef`` and back (``x-threadKeyCodecs``: 大象 digits,
  GitHub ``owner/repo#n`` lowercased, base32 for the bot platforms and every
  channel added later);
- the ordered external ID of an event from its ``MessageRef`` (``x-externalIds``:
  ``seq`` verbatim, else platform seconds plus a digest of the message ID, minted
  once at first sighting when the platform sends no time);
- ``InboundEvent`` envelopes into ``ChannelEvent`` (messages) or
  ``ChannelChange`` (edits, recalls) and, for the compatibility shim,
  ``ChannelEvent`` into envelopes;
- outbox sender results <-> ``Receipt``; channels_v1 account states <-> ``Health``.

Pure functions apart from ``external_id``'s first-sighting mint through the
store. Entry points: ``thread_key``, ``thread_ref``, ``external_id``,
``event_from_envelope``, ``change_from_envelope``, ``envelope_from_event``, ``receipt_from_legacy``,
``legacy_from_receipt``, ``health_from_legacy``, ``legacy_from_health``.
Dependencies: ``lib.channels.model``, ``lib.channels.bots.ids``,
``lib.channels.outbox`` (its definite-failure vocabulary), ``contract``.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from lib.channels.bots import ids as bot_ids
from lib.channels.model import CHANGE_KINDS, MEDIA_KINDS, ChannelChange, ChannelError, ChannelEvent, ChannelMedia, ThreadKey
from lib.channels.protocol.contract import EXTERNAL_IDS, HEALTH_CODES, LEGACY, THREAD_KEY_CODECS

_SECONDS_DIGITS = int(EXTERNAL_IDS['secondsDigits'])
DEFERRED_SEND_CODES = frozenset(LEGACY['deferredSendCodes'])


def _codec(channel: str) -> str:
    return THREAD_KEY_CODECS.get(channel, THREAD_KEY_CODECS['default'])


def thread_key(channel: str, ref: Mapping[str, Any]) -> ThreadKey:
    """The core's thread key of a platform conversation (``ThreadKey`` validates it)."""
    codec, kind, ident = _codec(channel), str(ref['kind']), str(ref['id'])
    if codec == 'github_issue':
        if kind != 'issue':
            raise ChannelError('invalid_thread', 'GitHub threads are issues or pull requests')
        return ThreadKey.parse('github:' + ident)
    if codec == 'digits':
        return ThreadKey(channel, kind, ident)
    return ThreadKey(channel, kind, bot_ids.encode(ident))


def thread_ref(key: ThreadKey, title: str = '') -> dict[str, Any]:
    """The platform conversation a thread key addresses (inverse of ``thread_key``)."""
    codec = _codec(key.channel)
    ident = key.external_id if codec in ('digits', 'github_issue') else bot_ids.decode(key.external_id)
    ref: dict[str, Any] = {'kind': key.kind, 'id': ident}
    if title:
        ref['title'] = title
    return ref


def external_id(channel: str, message: Mapping[str, Any], *, store=None,
                clock: Callable[[], float] = time.time) -> str:
    """The ordered external ID of a message (``x-externalIds``)."""
    if message.get('seq'):
        return str(message['seq'])
    ident = str(message['id'])
    if message.get('timeMs') is not None:
        return bot_ids.external_id(int(message['timeMs']) // 1000, ident)
    if store is None:
        raise ChannelError('invalid_event', 'A message without seq or time is minted through the store')
    seconds = int(clock())
    return store.provider_message_id(channel, ident, lambda: bot_ids.external_id(seconds, ident))


def event_from_envelope(channel: str, envelope: Mapping[str, Any], *, store=None) -> ChannelEvent:
    """A ``message`` envelope as the core's ``ChannelEvent`` (facts only, no policy).

    Text parts concatenate in order (a card contributes its fallback text); an
    attachment's locator rides in ``ChannelMedia.source``, which is never
    recorded, rendered or logged, and comes back to the adapter in ``download``.
    """
    if envelope.get('type') != 'message':
        raise ChannelError('unsupported_event', f'{envelope.get("type")} events have no ChannelEvent form')
    message = envelope['message']
    thread = thread_key(channel, envelope['thread'])
    external = external_id(channel, message, store=store)
    texts: list[str] = []
    mentioned: list[str] = []
    everyone = False
    quote: Mapping[str, Any] | None = None
    media: list[ChannelMedia] = []
    for part in envelope.get('parts') or ():
        kind = part['type']
        if kind == 'text':
            texts.append(part['text'])
        elif kind == 'card':
            texts.append(part['fallbackText'])
        elif kind == 'mention':
            if part.get('userId'):
                mentioned.append(str(part['userId']))
            everyone = everyone or bool(part.get('all'))
        elif kind == 'quote' and quote is None:
            quote = part
        elif kind == 'attachment' and part['kind'] in MEDIA_KINDS:
            media.append(ChannelMedia(part['kind'], part['name'], mime_type=part.get('mimeType', ''),
                                      size_bytes=int(part.get('sizeBytes') or 0),
                                      duration_s=int(part.get('durationS') or 0), source=part['locator']))
    text = texts[0] if len(texts) == 1 else '\n'.join(piece for piece in texts if piece)
    if message.get('timeMs') is not None:
        sent_at_ms = int(message['timeMs'])
    else:
        sent_at_ms = 0 if message.get('seq') else int(external[:_SECONDS_DIGITS]) * 1000
    echo = envelope.get('echo') or {}
    sender = envelope['sender']
    return ChannelEvent(
        thread=thread, external_id=external, sender_id=str(sender['id']),
        sender_name=str(sender.get('name') or ''), sender_kind=str(sender['kind']), text=text, sent_at_ms=sent_at_ms,
        thread_title=str(envelope['thread'].get('title') or ''),
        mentioned_ids=tuple(dict.fromkeys(mentioned)), mentions_all=everyone,
        reply_to_external_id=str((quote or {}).get('seq') or ''),
        reply_to_sender_id=str((quote or {}).get('senderId') or ''),
        client_id=str(message.get('clientId') or ''), outbox_marker=str(echo.get('marker') or ''),
        own_connection=bool(echo.get('ownConnection')), host_sent=bool(echo.get('hostSent')),
        url=str(envelope.get('url') or ''), media=tuple(media))


def change_from_envelope(channel: str, envelope: Mapping[str, Any], *, store=None) -> ChannelChange:
    """An ``edit`` or ``recall`` envelope as the core's ``ChannelChange`` (facts only, no policy).

    The target is the changed message's ordered external ID: its ``seq``, else
    the ID the core minted at the message's first sighting (``x-externalIds``);
    '' when neither exists, and the core then drops the change. An edit's text
    joins its text parts (a card contributes its fallback text).
    """
    if envelope.get('type') not in CHANGE_KINDS:
        raise ChannelError('unsupported_event', f'{envelope.get("type")} events are not changes')
    target = envelope.get('target') or {}
    if target.get('seq'):
        changed = str(target['seq'])
    elif store is not None and target.get('messageId'):
        changed = store.provider_message_alias(channel, str(target['messageId'])) or ''
    else:
        changed = ''
    texts = [part['text'] if part['type'] == 'text' else part['fallbackText']
             for part in envelope.get('parts') or () if part['type'] in ('text', 'card')]
    return ChannelChange(
        kind=envelope['type'], thread=thread_key(channel, envelope['thread']), target=changed,
        change_id=str(envelope['eventId']), sender_id=str(envelope['sender']['id']),
        text='\n'.join(text for text in texts if text),
        sent_at_ms=int(envelope['message'].get('timeMs') or 0))


def envelope_from_event(event: ChannelEvent, *, source: str = 'live',
                        locator: Callable[[ChannelMedia], str] | None = None) -> dict[str, Any]:
    """A legacy adapter's ``ChannelEvent`` as a ``message`` envelope (compatibility shim).

    The external ID is already the ordered ID, so it is both the message ID and
    ``seq``; ``event_from_envelope`` turns the envelope back into the same event.
    Legacy adapters report no sender kind, so their senders are ``user``. ``locator``
    registers each media original and returns its opaque handle.
    """
    message: dict[str, Any] = {'id': event.external_id, 'seq': event.external_id}
    if event.sent_at_ms:
        message['timeMs'] = int(event.sent_at_ms)
    if event.client_id:
        message['clientId'] = event.client_id
    parts: list[dict[str, Any]] = [{'type': 'text', 'text': event.text}]
    parts.extend({'type': 'mention', 'userId': uid} for uid in event.mentioned_ids)
    if event.mentions_all:
        parts.append({'type': 'mention', 'all': True})
    if event.reply_to_external_id:
        quote: dict[str, Any] = {'type': 'quote', 'messageId': event.reply_to_external_id,
                                 'seq': event.reply_to_external_id}
        if event.reply_to_sender_id:
            quote['senderId'] = event.reply_to_sender_id
        parts.append(quote)
    for item in event.media:
        handle = locator(item) if locator is not None else item.source
        parts.append({'type': 'attachment', 'kind': item.kind, 'name': item.name or 'attachment',
                      'mimeType': item.mime_type, 'sizeBytes': int(item.size_bytes),
                      'durationS': int(item.duration_s), 'locator': handle})
    envelope: dict[str, Any] = {
        'type': 'message', 'eventId': event.event_id, 'thread': thread_ref(event.thread, event.thread_title),
        'message': message, 'sender': {'id': event.sender_id, 'name': event.sender_name, 'kind': event.sender_kind},
        'parts': parts, 'source': source}
    echo: dict[str, Any] = {}
    if event.outbox_marker:
        echo['marker'] = event.outbox_marker
    if event.own_connection:
        echo['ownConnection'] = True
    if event.host_sent:
        echo['hostSent'] = True
    if echo:
        envelope['echo'] = echo
    if event.url:
        envelope['url'] = event.url
    return envelope


def receipt_from_legacy(intent: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """An outbox sender result (``lib.channels.outbox.OutboxSender``) as a ``Receipt``."""
    from lib.channels.outbox import DEFINITE_SEND_FAILURES
    receipt: dict[str, Any] = {'intentId': intent['intentId'], 'identity': intent['identity']}
    if result.get('clientId'):
        receipt['clientId'] = str(result['clientId'])
    if result.get('ok'):
        receipt['status'] = 'accepted' if intent['part']['type'] == 'attachment' else 'sent'
        receipt['platformMessageId'] = str(result.get('externalId') or '') or None
        return receipt
    error = result.get('error') or {}
    code = str(error.get('code') or '')
    if code in DEFINITE_SEND_FAILURES:
        receipt.update(status='failed', code=code)
    elif code in DEFERRED_SEND_CODES:
        receipt.update(status='deferred', code=code)
    else:
        receipt.update(status='unknown', code='answer_lost')
    detail = ' '.join(piece for piece in (code if code != receipt['code'] else '', str(error.get('message') or ''))
                      if piece)
    if detail:
        receipt['detail'] = detail
    return receipt


def legacy_from_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """A ``Receipt`` in the outbox's sender-result vocabulary (a deferral keeps its ``retryAfterMs``)."""
    if receipt['status'] in ('sent', 'accepted'):
        return {'ok': True, 'externalId': receipt.get('platformMessageId') or '',
                'clientId': receipt.get('clientId') or ''}
    result = {'ok': False, 'clientId': receipt.get('clientId') or '',
              'error': {'code': receipt.get('code') or 'unknown', 'message': receipt.get('detail') or ''}}
    if receipt['status'] == 'deferred' and receipt.get('retryAfterMs') is not None:
        result['retryAfterMs'] = int(receipt['retryAfterMs'])
    return result


def health_from_legacy(state: str, detail: str = '', *, at_ms: int) -> dict[str, Any] | None:
    """A channels_v1 account state as ``Health`` (``x-legacy``); None for ``disabled``/``starting``."""
    entry = LEGACY['accountStateHealth'].get(state)
    if entry is None:
        return None
    code = entry['code']
    health: dict[str, Any] = {'state': HEALTH_CODES[code]['state'], 'code': code, 'atMs': int(at_ms)}
    if detail:
        health['detail'] = detail
    return health


def legacy_from_health(health: Mapping[str, Any]) -> tuple[str, str]:
    """``(AccountState, detail)`` that today's account view stores for a health transition."""
    detail = str(health.get('detail') or '')
    return HEALTH_CODES[health['code']]['legacyState'], (f'{health["code"]}: {detail}' if detail else health['code'])


__all__ = ['DEFERRED_SEND_CODES', 'change_from_envelope', 'envelope_from_event', 'event_from_envelope', 'external_id',
           'health_from_legacy',
           'legacy_from_health', 'legacy_from_receipt', 'receipt_from_legacy', 'thread_key', 'thread_ref']
