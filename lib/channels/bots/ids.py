"""Bot platform identifiers (docs/CHANNELS.md C12).

Responsibility: carry a platform's opaque provider IDs (企业微信 userid or
chatid, QQ openids, 微信 user IDs) in thread keys, and derive the numeric,
time-ordered external IDs the store orders events by. Thread keys hold only
lowercase ASCII (``ChannelStore`` keys), so a provider ID is encoded as
unpadded lowercase RFC 4648 base32: reversible, so a key alone addresses its
chat, and one spelling per ID, so the event record decides duplicates.

Entry points: ``encode``, ``decode``, ``thread_key``, ``peer``,
``external_id``. Dependencies: ``lib.channels.model`` only.
"""
from __future__ import annotations

import base64
import hashlib

from lib.channels.model import ChannelError, ThreadKey

# Event external IDs are at most 32 digits (contract EventId): 10 for the
# provider's epoch seconds, the rest a digest of its message ID, so order
# follows time and a redelivery of the same message gets the same ID.
_SECONDS_DIGITS = 10
_DIGEST_DIGITS = 22


def encode(provider_id: str) -> str:
    if not isinstance(provider_id, str) or not provider_id:
        raise ChannelError('invalid_thread', 'A provider ID is required')
    return base64.b32encode(provider_id.encode('utf-8')).decode('ascii').rstrip('=').lower()


def decode(key: str) -> str:
    padded = key.upper() + '=' * (-len(key) % 8)
    try:
        return base64.b32decode(padded).decode('utf-8')
    except (ValueError, UnicodeDecodeError) as error:
        raise ChannelError('invalid_thread', 'Not a bot thread key') from error


def thread_key(channel: str, kind: str, provider_id: str) -> ThreadKey:
    """The thread of a private chat (``uid``: the user) or a group (``gid``)."""
    return ThreadKey(channel, kind, encode(provider_id))


def peer(thread: ThreadKey) -> str:
    """The provider ID a bot thread addresses."""
    return decode(thread.external_id)


def external_id(seconds: int, provider_message_id: str) -> str:
    digest = int(hashlib.sha256(str(provider_message_id).encode('utf-8')).hexdigest(), 16)
    return f'{int(seconds):0{_SECONDS_DIGITS}d}{digest % 10 ** _DIGEST_DIGITS:0{_DIGEST_DIGITS}d}'


__all__ = ['decode', 'encode', 'external_id', 'peer', 'thread_key']
