"""Channel vocabulary: thread keys, normalized events and outbox items.

Responsibility: the channel-neutral shapes shared by every channel module,
derived from the single contract ``contracts/channels_v1.schema.json``
(enums, defaults, limits, provider limits, tool schemas).
Entry points: ``ThreadKey``, ``ChannelEvent``, ``OutboxItem``, ``ChannelError``
and the contract vocabularies below. Dependencies: the contract file only; no
storage, network or task imports, so adapters and tests can use it freely.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONTRACT = json.loads((Path(__file__).resolve().parents[2]
                       / 'contracts/channels_v1.schema.json').read_text(encoding='utf-8'))
_DEFS = CONTRACT['$defs']

NAMESPACE = CONTRACT['x-namespace']
DISCLOSURE_LABEL = CONTRACT['x-disclosureLabel']
OUTBOX_MARKER_FIELD = CONTRACT['x-outboxMarkerField']
LIMITS = CONTRACT['x-limits']
PROVIDER_LIMITS = CONTRACT['x-providerLimits']
BRIDGE_CONTRACT = CONTRACT['x-bridge']
# Per-channel capabilities: whether the channel has a command chat (owner
# messages that are human input) and whether quote replies exist.
CHANNEL_TRAITS = CONTRACT['x-channelTraits']
GITHUB = CONTRACT['x-github']
DEFAULT_MODE_BY_KIND = dict(CONTRACT['x-defaults']['modeByKind'])
DEFAULT_REPLY_POLICY = CONTRACT['x-defaults']['replyPolicy']

CHANNELS = tuple(_DEFS['Channel']['enum'])
THREAD_KINDS = tuple(_DEFS['ThreadKind']['enum'])
THREAD_MODES = tuple(_DEFS['ThreadMode']['enum'])
CONFIGURABLE_MODES = tuple(_DEFS['ConfigurableThreadMode']['enum'])
REPLY_POLICIES = tuple(_DEFS['ReplyPolicy']['enum'])
ACCOUNT_STATES = tuple(_DEFS['AccountState']['enum'])
# States a running or restarting session reports for itself. An owner command
# on an account that is already enabled leaves them alone: a session writes each
# transition once, so a 'starting' written over its 'connected' would stick.
SESSION_REPORTED_STATES = frozenset({'starting', 'connected', 'disconnected'})
EVENT_DISPOSITIONS = tuple(_DEFS['EventDisposition']['enum'])
OUTBOX_STATES = tuple(_DEFS['OutboxState']['enum'])
# Terminal outbox states leave the per-thread active index; ``failed`` stays
# listed (non-blocking) until the owner discards it.
TERMINAL_OUTBOX_STATES = frozenset({'sent', 'discarded'})
ACTIVE_OUTBOX_STATES = tuple(s for s in OUTBOX_STATES if s not in TERMINAL_OUTBOX_STATES)

TOOLS = CONTRACT['tools']
TOOL_NAMES = frozenset(tool['function']['name'] for tool in TOOLS)

_THREAD_KEY_RE = re.compile(_DEFS['ThreadKey']['pattern'])
_EVENT_ID_RE = re.compile(_DEFS['EventId']['pattern'])
_EXTERNAL_ID_RE = re.compile(r'^[0-9]{1,32}$')
# The thread part of a GitHub key: ``<owner>/<repo>#<number>``. GitHub owner
# and repository names are case-insensitive, so keys are stored lowercase and
# one issue or pull request has exactly one spelling (exact dedup and lookup).
_GITHUB_THREAD_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,38}/[a-z0-9._-]{1,100}#[1-9][0-9]{0,9}$')
# Kinds per channel, as in the contract ThreadKey pattern. GitHub's REST API
# treats every pull request as an issue (same number space, same comments
# endpoint), so both are one kind.
_KINDS_BY_CHANNEL = {'dx': frozenset({'gid', 'uid', 'pid'}), 'github': frozenset({'issue'})}
_GITHUB_PREFIX = 'github:'
# Event keys sort by the provider ID; the contract caps IDs at 32 digits.
_ORDER_KEY_WIDTH = 32


class ChannelError(ValueError):
    """A caller-visible channel refusal with a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require_external_id(value: Any, *, field_name: str = 'external_id') -> str:
    text = str(value or '')
    if not _EXTERNAL_ID_RE.fullmatch(text):
        raise ChannelError('invalid_external_id', f'{field_name} must be a provider numeric ID')
    return text


def _canonical_github(text: str) -> str:
    """Lowercase the owner/repository part of a GitHub key (event ID suffix kept)."""
    return _GITHUB_PREFIX + text[len(_GITHUB_PREFIX):].lower()


@dataclass(frozen=True, order=True)
class ThreadKey:
    """One event stream.

    大象: ``dx:gid:<gid>``, ``dx:uid:<uid>`` or ``dx:pid:<pid>``. GitHub:
    ``github:<owner>/<repo>#<number>`` (kind ``issue``; ``external_id`` holds
    ``<owner>/<repo>#<number>``), one thread per issue or pull request.
    """

    channel: str
    kind: str
    external_id: str

    def __post_init__(self):
        kinds = _KINDS_BY_CHANNEL.get(self.channel)
        if self.channel not in CHANNELS or kinds is None or self.kind not in kinds:
            raise ChannelError('invalid_thread', 'Invalid channel thread key')
        if self.channel == 'github':
            valid = (_GITHUB_THREAD_RE.fullmatch(self.external_id) is not None
                     # '.' and '..' are path segments, never repository names.
                     and self.external_id.split('#', 1)[0].split('/', 1)[1] not in ('.', '..'))
        else:
            valid = _EXTERNAL_ID_RE.fullmatch(self.external_id) is not None
        if not valid:
            raise ChannelError('invalid_thread', 'Invalid channel thread key')

    def __str__(self) -> str:
        if self.channel == 'github':
            return _GITHUB_PREFIX + self.external_id
        return f'{self.channel}:{self.kind}:{self.external_id}'

    @classmethod
    def parse(cls, text: Any) -> 'ThreadKey':
        if isinstance(text, str) and text[:len(_GITHUB_PREFIX)].lower() == _GITHUB_PREFIX:
            text = _canonical_github(text)
        if not isinstance(text, str) or not _THREAD_KEY_RE.fullmatch(text):
            raise ChannelError('invalid_thread', 'Thread key must look like dx:gid:<digits> or '
                                                 'github:<owner>/<repo>#<number>')
        if text.startswith(_GITHUB_PREFIX):
            return cls('github', 'issue', text[len(_GITHUB_PREFIX):])
        channel, kind, external_id = text.split(':', 2)
        return cls(channel, kind, external_id)

    @classmethod
    def github(cls, repository: str, number: int) -> 'ThreadKey':
        """The thread of issue or pull request ``number`` in ``owner/repo``."""
        return cls('github', 'issue', f'{str(repository).lower()}#{int(number)}')

    @property
    def is_private(self) -> bool:
        return self.kind == 'uid'

    @property
    def repository(self) -> str:
        """``owner/repo`` of a GitHub thread."""
        return self.external_id.split('#', 1)[0]

    @property
    def number(self) -> int:
        """Issue or pull request number of a GitHub thread."""
        return int(self.external_id.rsplit('#', 1)[1])


def parse_event_id(text: Any) -> tuple[ThreadKey, str]:
    """Split ``<thread key>/<external id>`` (the chain-of-recall event ID).

    External IDs are digits and GitHub thread keys contain ``/``, so the split
    is at the last slash.
    """
    if isinstance(text, str) and text[:len(_GITHUB_PREFIX)].lower() == _GITHUB_PREFIX:
        text = _canonical_github(text)
    if not isinstance(text, str) or not _EVENT_ID_RE.fullmatch(text):
        raise ChannelError('invalid_event_id', 'Event ID must look like dx:gid:<digits>/<digits> or '
                                               'github:<owner>/<repo>#<number>/<digits>')
    thread, external_id = text.rsplit('/', 1)
    return ThreadKey.parse(thread), external_id


def order_key(external_id: str) -> str:
    return require_external_id(external_id).zfill(_ORDER_KEY_WIDTH)


def external_id_after(left: str, right: str | None) -> bool:
    """Numeric comparison of provider IDs (snowflakes are time ordered)."""
    return right is None or right == '' or int(left) > int(right)


@dataclass(frozen=True)
class ChannelEvent:
    """One inbound message normalized by an adapter; facts only, no policy.

    ``sent_at_ms`` is the provider server time when the adapter can derive it.
    ``mentioned_ids``/``mentions_all`` and ``reply_to_sender_id`` are protocol
    facts; ingress compares them with the bound owner identity.
    """

    thread: ThreadKey
    external_id: str
    sender_id: str
    sender_name: str
    text: str
    sent_at_ms: int
    thread_title: str = ''
    message_type: int = 1
    mentioned_ids: tuple[str, ...] = ()
    mentions_all: bool = False
    reply_to_external_id: str = ''
    reply_to_sender_id: str = ''
    client_id: str = ''
    outbox_marker: str = ''
    own_connection: bool = False
    url: str = ''

    def __post_init__(self):
        require_external_id(self.external_id)
        if not isinstance(self.text, str) or not isinstance(self.sender_id, str):
            raise ChannelError('invalid_event', 'Channel event text and sender must be strings')

    @property
    def channel(self) -> str:
        return self.thread.channel

    @property
    def event_id(self) -> str:
        return f'{self.thread}/{self.external_id}'

    def to_record(self) -> dict[str, Any]:
        return {
            'eventId': self.event_id, 'thread': str(self.thread),
            'externalId': self.external_id, 'senderId': self.sender_id,
            'senderName': self.sender_name, 'text': self.text,
            'sentAtMs': int(self.sent_at_ms), 'threadTitle': self.thread_title,
            'messageType': int(self.message_type),
            'mentionedIds': list(self.mentioned_ids), 'mentionsAll': bool(self.mentions_all),
            'replyToExternalId': self.reply_to_external_id,
            'replyToSenderId': self.reply_to_sender_id, 'clientId': self.client_id,
            'url': self.url,
        }

    @classmethod
    def from_record(cls, value: dict[str, Any]) -> 'ChannelEvent':
        return cls(
            thread=ThreadKey.parse(value['thread']), external_id=value['externalId'],
            sender_id=value.get('senderId', ''), sender_name=value.get('senderName', ''),
            text=value.get('text', ''), sent_at_ms=int(value.get('sentAtMs') or 0),
            thread_title=value.get('threadTitle', ''),
            message_type=int(value.get('messageType') or 1),
            mentioned_ids=tuple(value.get('mentionedIds') or ()),
            mentions_all=bool(value.get('mentionsAll')),
            reply_to_external_id=value.get('replyToExternalId', ''),
            reply_to_sender_id=value.get('replyToSenderId', ''),
            client_id=value.get('clientId', ''), url=value.get('url', ''))


@dataclass
class OutboxItem:
    """One durable outbound message; ``state`` follows ``OutboxState``."""

    id: str
    thread: str
    text: str
    idempotency_key: str
    state: str
    origin: dict[str, str]
    created_at_ms: int
    updated_at_ms: int
    queued_at_ms: int = 0
    queue_seq: int = 0
    reply_to: str | None = None
    external_id: str | None = None
    client_id: str | None = None
    error: str | None = None
    attempt_at_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            'id': self.id, 'thread': self.thread, 'text': self.text,
            'idempotencyKey': self.idempotency_key, 'state': self.state,
            'origin': dict(self.origin), 'createdAtMs': self.created_at_ms,
            'updatedAtMs': self.updated_at_ms, 'queuedAtMs': self.queued_at_ms,
            'queueSeq': self.queue_seq, 'replyTo': self.reply_to, 'externalId': self.external_id,
            'clientId': self.client_id, 'error': self.error,
            'attemptAtMs': self.attempt_at_ms, **self.extra,
        }

    @classmethod
    def from_record(cls, value: dict[str, Any]) -> 'OutboxItem':
        known = {'id', 'thread', 'text', 'idempotencyKey', 'state', 'origin', 'createdAtMs',
                 'updatedAtMs', 'queuedAtMs', 'queueSeq', 'replyTo', 'externalId', 'clientId', 'error',
                 'attemptAtMs'}
        if value.get('state') not in OUTBOX_STATES:
            raise ChannelError('invalid_outbox_item', 'Stored outbox item has an unknown state')
        return cls(
            id=value['id'], thread=value['thread'], text=value['text'],
            idempotency_key=value['idempotencyKey'], state=value['state'],
            origin=dict(value.get('origin') or {}),
            created_at_ms=int(value.get('createdAtMs') or 0),
            updated_at_ms=int(value.get('updatedAtMs') or 0),
            queued_at_ms=int(value.get('queuedAtMs') or 0),
            queue_seq=int(value.get('queueSeq') or 0), reply_to=value.get('replyTo'),
            external_id=value.get('externalId'), client_id=value.get('clientId'),
            error=value.get('error'), attempt_at_ms=int(value.get('attemptAtMs') or 0),
            extra={k: v for k, v in value.items() if k not in known})

    def view(self, thread_title: str = '') -> dict[str, Any]:
        """The contract ``OutboxItemView`` projection."""
        return {
            'id': self.id, 'thread': self.thread, 'threadTitle': thread_title,
            'text': self.text, 'replyTo': self.reply_to, 'state': self.state,
            'origin': {'kind': self.origin.get('kind', 'tool'),
                       'conversationId': self.origin.get('conversationId', ''),
                       'turnId': self.origin.get('turnId', '')},
            'createdAtMs': self.created_at_ms, 'updatedAtMs': self.updated_at_ms,
            'externalId': self.external_id, 'error': self.error,
        }


def session_owns_status(account: dict[str, Any] | None) -> bool:
    """Whether an enable command must keep this account's status (``SESSION_REPORTED_STATES``)."""
    account = account or {}
    return bool(account.get('enabled')) and (account.get('status') or {}).get('state') in SESSION_REPORTED_STATES


__all__ = [
    'ACCOUNT_STATES', 'ACTIVE_OUTBOX_STATES', 'BRIDGE_CONTRACT', 'CHANNELS', 'CHANNEL_TRAITS',
    'CONFIGURABLE_MODES', 'CONTRACT', 'ChannelError', 'ChannelEvent', 'DEFAULT_MODE_BY_KIND',
    'DEFAULT_REPLY_POLICY', 'DISCLOSURE_LABEL', 'EVENT_DISPOSITIONS', 'GITHUB', 'LIMITS',
    'NAMESPACE', 'OUTBOX_MARKER_FIELD',
    'OUTBOX_STATES', 'OutboxItem', 'PROVIDER_LIMITS', 'REPLY_POLICIES', 'SESSION_REPORTED_STATES',
    'TERMINAL_OUTBOX_STATES', 'THREAD_KINDS', 'THREAD_MODES', 'TOOLS', 'TOOL_NAMES', 'ThreadKey',
    'external_id_after', 'order_key', 'parse_event_id', 'require_external_id', 'session_owns_status',
]
