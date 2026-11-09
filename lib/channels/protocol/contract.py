"""Channel protocol contract: version, methods, health codes, validation, adapter descriptors.

Responsibility: load ``contracts/channel_protocol_v1.schema.json`` (the
machine-readable authority of docs/CHANNEL_PROTOCOL.md) and expose its
vocabulary; validate protocol objects against one of its ``$defs``; negotiate
the protocol version; resolve the in-repository adapters' target capability
descriptors (``x-adapters``) against the platform facts that
``contracts/channels_v1.schema.json`` holds once.

Entry points: ``PROTOCOL_VERSION``, ``SUPPORTED_VERSIONS``, ``METHODS``,
``ERRORS``, ``HEALTH_CODES``, ``WATCHDOG_STATES``, ``ProtocolError``,
``validate``, ``negotiate_version``, ``health_entry``, ``adapter_descriptor``.
Dependencies: the two contract files; ``jsonschema`` (lazily, per definition).
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

from lib.channels.model import CONTRACT as CHANNELS_CONTRACT

CONTRACT = json.loads((Path(__file__).resolve().parents[3]
                       / 'contracts/channel_protocol_v1.schema.json').read_text(encoding='utf-8'))
_DEFS = CONTRACT['$defs']


def _table(name: str) -> dict[str, Any]:
    return {key: value for key, value in CONTRACT[name].items() if key != 'description'}


PROTOCOL = CONTRACT['x-protocol']
PROTOCOL_VERSION = int(PROTOCOL['version'])
# The core's N and N-1 (x-protocol versionPolicy), newest first.
SUPPORTED_VERSIONS = tuple(sorted((int(version) for version in PROTOCOL['supportedVersions']), reverse=True))
METHODS = _table('x-methods')
ERRORS = _table('x-errors')
HEALTH_CODES = _table('x-healthCodes')
HEALTH_STATES = tuple(_DEFS['HealthState']['enum'])
WATCHDOG_STATES = frozenset(CONTRACT['x-watchdogStates']['signal'])
THREAD_KEY_CODECS = _table('x-threadKeyCodecs')
EXTERNAL_IDS = CONTRACT['x-externalIds']
LEGACY = CONTRACT['x-legacy']
RECEIPT_STATUSES = tuple(_DEFS['ReceiptStatus']['enum'])
RECEIPT_CODES = tuple(_DEFS['ReceiptCode']['enum'])
# No optional protocol features exist in v1; the list is the negotiation port for later ones.
CORE_FEATURES: tuple[str, ...] = ()


class ProtocolError(Exception):
    """A protocol violation or refusal, carrying its JSON-RPC error code (``x-errors``)."""

    def __init__(self, code: int, message: str, *, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = int(code)
        self.data = dict(data or {})


@lru_cache(maxsize=None)
def _validator(name: str):
    from jsonschema import Draft202012Validator
    if name not in _DEFS:
        raise KeyError(f'unknown protocol definition {name!r}')
    return Draft202012Validator({**CONTRACT, '$ref': f'#/$defs/{name}'})


def validate(name: str, value: Any) -> Any:
    """Return ``value`` when it is a valid ``$defs/<name>``; else raise ``ProtocolError`` (invalidParams)."""
    from jsonschema.exceptions import best_match
    error = best_match(_validator(name).iter_errors(value))
    if error is not None:
        where = '/' + '/'.join(str(part) for part in error.absolute_path)
        raise ProtocolError(ERRORS['invalidParams'], f'{name} is invalid at {where}: {error.message[:300]}')
    return value


def negotiate_version(spoken: Iterable[int], offered: Iterable[int] = SUPPORTED_VERSIONS) -> int:
    """The highest version both sides speak; refuses with ``unsupportedProtocolVersion`` otherwise."""
    common = set(int(version) for version in spoken) & set(int(version) for version in offered)
    if not common:
        raise ProtocolError(ERRORS['unsupportedProtocolVersion'], 'No common channel protocol version',
                            data={'supported': sorted(set(offered), reverse=True)})
    return max(common)


def health_entry(code: str) -> dict[str, str]:
    """``state``, ``remedy``, ``retry``, ``legacyState`` and ``origin`` of a health code."""
    return dict(HEALTH_CODES[code])


def _pointer(document: dict[str, Any], pointer: str) -> Any:
    value: Any = document
    for part in pointer.lstrip('/').split('/'):
        value = value[part.replace('~1', '/').replace('~0', '~')]
    return value


def _attachment_facts(name: str) -> dict[str, dict[str, Any]]:
    """An attachment fact set (``x-dxAttachments``: suffix -> [kind, MIME], size bound) by kind."""
    facts = CHANNELS_CONTRACT[name]
    by_kind: dict[str, dict[str, Any]] = {}
    for suffix, (kind, _mime) in sorted(facts['types'].items()):
        by_kind.setdefault(kind, {'suffixes': [], 'sizeBelowBytes': int(facts['sizeBelowBytes'])})
        by_kind[kind]['suffixes'].append(suffix)
    return by_kind


def _resolve(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {'$fact'}:
            return _pointer(CHANNELS_CONTRACT, value['$fact'])
        if set(value) == {'$attachmentFacts'}:
            return _attachment_facts(value['$attachmentFacts'])
        return {key: _resolve(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item) for item in value]
    return value


def adapter_descriptor(channel: str) -> dict[str, Any]:
    """The target capability descriptor of an in-repository adapter, validated."""
    adapters = _table('x-adapters')
    if channel not in adapters:
        raise KeyError(f'no in-repository adapter descriptor for {channel!r}')
    return validate('CapabilityDescriptor', _resolve(adapters[channel]))


def adapter_channels() -> tuple[str, ...]:
    return tuple(_table('x-adapters'))


__all__ = [
    'CONTRACT', 'CORE_FEATURES', 'ERRORS', 'EXTERNAL_IDS', 'HEALTH_CODES', 'HEALTH_STATES', 'LEGACY', 'METHODS',
    'PROTOCOL_VERSION', 'ProtocolError', 'RECEIPT_CODES', 'RECEIPT_STATUSES', 'SUPPORTED_VERSIONS',
    'THREAD_KEY_CODECS', 'WATCHDOG_STATES', 'adapter_channels', 'adapter_descriptor', 'health_entry',
    'negotiate_version', 'validate',
]
