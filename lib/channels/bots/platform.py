"""The bot platform transport port (docs/CHANNELS.md C12).

Responsibility: the shapes a platform transport exchanges with its session.
A transport is transport only: it turns provider frames into ``Inbound``
facts and sends text to a chat; it holds no policy and no durable state (its
session persists cursors, reply contexts and status).

``Transport.run(sink)`` blocks until ``sink.stopped`` is set or the
connection ends; it reports ``sink.connected(info)`` once the platform
accepted the credentials and ``sink.deliver(batch, checkpoint)`` per batch of
inbound messages (``checkpoint`` is what the session persists after the batch
was ingested). A refusal that a restart cannot fix raises ``SessionEnded``.
``Transport.send(kind, peer, text, *, item_id, reply)`` returns the outbox
sender result (``{'ok': True, 'externalId': ..}`` or ``{'ok': False,
'error': {'code': ..}}``), optionally with an updated ``reply`` context.
``Transport.close()`` unblocks ``run`` where the transport can.
"""
from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Inbound:
    """One user message to the bot, as provider facts.

    ``kind`` is ``uid`` (a private chat with ``peer``, the user) or ``gid``
    (group ``peer``). ``sent_at_s`` is the provider's time, or None when the
    platform sends none (the store then mints the event ID at first sight).
    ``reply`` is the chat's reply context to keep for the next send.
    """

    kind: str
    peer: str
    sender: str
    sender_name: str
    message_id: str
    text: str
    sent_at_s: int | None = None
    title: str = ''
    reply: Mapping[str, Any] = field(default_factory=dict)


class SessionEnded(Exception):
    """The platform ended the session for a reason a restart cannot fix.

    ``state`` is the account state to report: ``needs_login`` (credentials
    refused or expired) or ``kicked`` (another connection took over the bot).
    """

    def __init__(self, state: str, detail: str):
        super().__init__(detail)
        self.state = state
        self.detail = detail


class Sink(Protocol):
    stopped: threading.Event

    def connected(self, info: Mapping[str, Any]) -> None: ...

    def deliver(self, batch: list[Inbound], checkpoint: Mapping[str, Any] | None = None) -> None: ...


class Transport(Protocol):
    def run(self, sink: Sink) -> None: ...

    def send(self, kind: str, peer: str, text: str, *, item_id: str,
             reply: Mapping[str, Any]) -> dict[str, Any]: ...

    def close(self) -> None: ...


def failure(code: str, message: str = '') -> dict[str, Any]:
    return {'ok': False, 'error': {'code': code, 'message': message}}


__all__ = ['Inbound', 'SessionEnded', 'Sink', 'Transport', 'failure']
