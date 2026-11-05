"""Deterministic channel policy: modes, reply policies and event classification.

Responsibility: the C2 fast path (docs/CHANNELS.md) as pure functions over a
thread record, the account record and one normalized event. No storage, no
model calls, no keyword heuristics: every decision is a protocol fact (sender,
thread kind, @-mention, quote reply) compared with the bound owner identity.
Entry points: ``effective_mode``, ``effective_reply_policy``, ``classify``.
"""
from __future__ import annotations

from typing import Any

from lib.channels.model import (
    CHANNEL_TRAITS, DEFAULT_MODE_BY_KIND, DEFAULT_REPLY_POLICY, ChannelEvent, ThreadKey,
)


def is_command_chat(thread: ThreadKey | str, account: dict[str, Any] | None) -> bool:
    return bool(account and account.get('commandChat') == str(thread))


def effective_mode(thread: ThreadKey, record: dict[str, Any] | None,
                   account: dict[str, Any] | None) -> str:
    """The command chat is always ``command``; otherwise override, then default."""
    if is_command_chat(thread, account):
        return 'command'
    override = (record or {}).get('mode')
    return override if override else DEFAULT_MODE_BY_KIND[thread.kind]


def effective_reply_policy(record: dict[str, Any] | None) -> str:
    return (record or {}).get('replyPolicy') or DEFAULT_REPLY_POLICY


def owner_uid(account: dict[str, Any] | None) -> str:
    return str(((account or {}).get('identity') or {}).get('uid') or '')


def mentions_owner(event: ChannelEvent, owner: str) -> bool:
    return bool(owner) and (owner in event.mentioned_ids or event.mentions_all)


def replies_to_owner(event: ChannelEvent, owner: str) -> bool:
    return bool(owner) and event.reply_to_sender_id == owner


def has_command_chat(channel: str) -> bool:
    return bool(CHANNEL_TRAITS[channel]['commandChat'])


def classify(event: ChannelEvent, mode: str, owner: str) -> str:
    """Return ``command``, ``candidate``, ``context`` or ``drop`` (C2 table).

    ``ignore`` never reaches here (ingress keeps only thread metadata).
    """
    owner_authored = bool(owner) and event.sender_id == owner
    if owner_authored:
        if not has_command_chat(event.channel):
            # A channel without a command chat (GitHub, C8) carries no owner
            # commands. In a watched thread the owner's own text is a
            # nonhuman candidate like everyone else's, so the router can relate
            # it to other channels; elsewhere it is context.
            return 'candidate' if mode == 'watch' else 'context'
        return 'command' if mode == 'command' else 'context'
    if mode in ('command', 'watch'):
        return 'candidate'
    if mode == 'mentions':
        if (event.thread.is_private or mentions_owner(event, owner)
                or replies_to_owner(event, owner)):
            return 'candidate'
        return 'drop'
    raise ValueError(f'unclassifiable thread mode {mode!r}')


__all__ = ['classify', 'effective_mode', 'effective_reply_policy', 'has_command_chat',
           'is_command_chat', 'mentions_owner', 'owner_uid', 'replies_to_owner']
