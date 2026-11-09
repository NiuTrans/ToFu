"""Adapter side of the channel protocol: the base every chat-app adapter implements.

Responsibility: name the methods an adapter answers (``x-methods`` with
direction ``core_to_adapter``) and the link through which it calls the core
(``events``, ``health``). An adapter translates platform <-> protocol and
supplies raw platform identity only. It never decides authority (the owner is
whoever the core bound), reply policy, idempotency, retries, chunking or
degradation; it reports each connector transition once as ``Health`` and
leaves reconnect cadence to the core's supervisor and HTTP route recovery to
the shared network authority (docs/NETWORK.md). docs/CHANNEL_PROTOCOL.md owns
the semantics.

Entry points: ``ProtocolAdapter`` (subclass it), ``CoreLink`` (what
``ProtocolAdapter.core`` offers once attached), ``capability_missing``.
Dependencies: ``contract`` only, so an adapter can run in a separate process.
"""
from __future__ import annotations

from typing import Any, Protocol

from lib.channels.protocol.contract import ERRORS, PROTOCOL_VERSION, ProtocolError, negotiate_version


class CoreLink(Protocol):
    def events(self, batch: dict[str, Any]) -> dict[str, Any]:
        """Deliver an ``EventBatch``; returns its ``EventBatchAck`` once every event was ingested.

        Raises when the core could not ingest the batch: deliver it again later
        (at least once); its checkpoint was not persisted.
        """

    def health(self, health: dict[str, Any]) -> None:
        """Report one connector transition (``Health``)."""


def capability_missing(method: str) -> ProtocolError:
    return ProtocolError(ERRORS['capabilityMissing'], f'This adapter does not implement {method}')


class ProtocolAdapter:
    """Base adapter. Optional methods refuse with ``capabilityMissing`` until implemented.

    ``initialize`` negotiates the version from ``protocol_versions`` and declares
    ``capabilities()`` (a ``CapabilityDescriptor``); ``start`` begins delivery
    from the checkpoint the core persisted; ``send`` posts exactly one platform
    message per ``OutboundIntent`` and answers with a ``Receipt``.
    """

    name = 'adapter'
    version = '0'
    protocol_versions: tuple[int, ...] = (PROTOCOL_VERSION,)

    def __init__(self) -> None:
        self.core: CoreLink | None = None

    def attach(self, core: CoreLink) -> None:
        self.core = core

    def capabilities(self) -> dict[str, Any]:
        raise NotImplementedError

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        version = negotiate_version(self.protocol_versions, params['protocolVersions'])
        return {'protocolVersion': version, 'adapter': {'name': self.name, 'version': self.version},
                'capabilities': self.capabilities()}

    def start(self, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def identity(self, params: dict[str, Any]) -> dict[str, Any]:
        raise capability_missing('identity')

    def send(self, intent: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def lookup(self, query: dict[str, Any]) -> dict[str, Any]:
        raise capability_missing('lookup')

    def history(self, query: dict[str, Any]) -> dict[str, Any]:
        raise capability_missing('history')

    def threads(self, query: dict[str, Any]) -> dict[str, Any]:
        raise capability_missing('threads')

    def download(self, request: dict[str, Any]) -> dict[str, Any]:
        raise capability_missing('download')

    def shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        return {}


__all__ = ['CoreLink', 'ProtocolAdapter', 'capability_missing']
