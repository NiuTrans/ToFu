"""Deterministic channel policy: modes, reply policies and event classification.

Responsibility: the C2 fast path (docs/CHANNELS.md) as pure functions over a
thread record, the account record and one normalized event. No storage, no
model calls, no keyword heuristics: every decision is a protocol fact (sender,
thread kind, @-mention, quote reply) compared with the bound owner identity.
Entry points: ``effective_mode``, ``effective_reply_policy``, ``classify``,
``robot_thread``.
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


def robot_thread(account: dict[str, Any] | None) -> str:
    """The thread the account's notification robot posts into (C6), or ''.

    Only a group command chat: the robot posts into groups it is a member of,
    while a private chat with the robot would be a different conversation.
    The robot is the Settings one or the Internal package's built-in one
    (``lib.channels.dx.robot.effective_robot``, an immutable package file).
    Whether it may post there for this server is the binding's decision
    (``lib.channels.dx.robot.binding_refusal``).
    """
    from lib.channels.dx.robot import effective_robot
    account = account or {}
    chat = str(account.get('commandChat') or '')
    if not chat or ThreadKey.parse(chat).kind != 'gid':
        return ''
    return chat if effective_robot(account) is not None else ''


def owner_uid(account: dict[str, Any] | None) -> str:
    return str(((account or {}).get('identity') or {}).get('uid') or '')


def mentions_owner(event: ChannelEvent, owner: str) -> bool:
    return bool(owner) and (owner in event.mentioned_ids or event.mentions_all)


def replies_to_owner(event: ChannelEvent, owner: str) -> bool:
    return bool(owner) and event.reply_to_sender_id == owner


def has_command_chat(channel: str) -> bool:
    return bool(CHANNEL_TRAITS[channel]['commandChat'])


def classify(event: ChannelEvent, mode: str, owner: str, *, robot_chat: bool = False) -> str:
    """Return ``command``, ``candidate``, ``context`` or ``drop`` (C2 table).

    ``ignore`` never reaches here (ingress keeps only thread metadata).
    ``robot_chat`` marks the account's robot thread (``robot_thread``): one
    robot application serves many Tofu servers (C6), so there everyone but
    the owner is context. Another member may be another server's owner,
    answered by that server, and robot posts that are not this account's own
    echoes are another server's; a review of either would answer a second time
    or bounce robot posts between servers.

    A message the platform marks as a bot's or a platform notice
    (``sender_kind``) is at most context: kept where a person's message would
    be delivered, never a command or a candidate, so no review or auto-reply
    answers one automated account with another.
    """
    disposition = _classify_person(event, mode, owner, robot_chat=robot_chat)
    if event.sender_kind != 'user' and disposition in ('command', 'candidate'):
        return 'context'
    return disposition


def _classify_person(event: ChannelEvent, mode: str, owner: str, *, robot_chat: bool) -> str:
    owner_authored = bool(owner) and event.sender_id == owner
    if owner_authored:
        if not has_command_chat(event.channel):
            # A channel without a command chat (GitHub, C8) carries no owner
            # commands. In a watched thread the owner's own text is a
            # nonhuman candidate like everyone else's, so the router can relate
            # it to other channels; elsewhere it is context.
            return 'candidate' if mode == 'watch' else 'context'
        return 'command' if mode == 'command' else 'context'
    if robot_chat:
        return 'context'
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
