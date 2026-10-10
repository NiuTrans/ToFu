"""Durable task-event format envelope — read-side refuse/validate helpers.

Why this exists
---------------
Every persisted ``storage_events`` / ``storage_attempt_events`` row is stamped
at the single write authority (``compile_event_item`` in the storage sidecar)
with a single integer format ``v`` (and, for non-essential events, an
``ignorable`` marker). The version is a cross-language constant owned by
``contracts/tofudb_ir_v1.json`` → ``eventFormat.version`` and codegen'd to both
Rust (``EVENT_FORMAT_VERSION``) and Python (here).

The rule mirrors the projection codec: **"parses without error" is not
"semantically readable."** A reader that meets a payload whose format version
is newer than it understands *and* that is not marked ``ignorable`` must refuse
rather than silently resume a gutted stream. A newer ``ignorable`` payload is
skipped, never resumed-as-if-understood. An absent envelope is the
pre-versioning format (``v0``) and stays readable.

This module is dependency-free so the event fold, cold replay, request
inspector, and turn trace all consume one classification instead of
duplicating the rule.
"""

from __future__ import annotations

from typing import Any

# Canonical source: contracts/tofudb_ir_v1.json → eventFormat.version.
# Keep in lockstep with the codegen'd Rust ``EVENT_FORMAT_VERSION``; the
# contract test asserts they match. Rows written by an older binary carry no
# envelope (implicit v0); rows this binary writes carry exactly this version.
EVENT_FORMAT_VERSION = 1

# A reader understands any envelope version up to and including its own.
# Anything newer is judged by its ``ignorable`` marker.
_ENVELOPE_KEY = 'v'
_IGNORABLE_KEY = 'ignorable'


class EventFormatError(ValueError):
    """A persisted event is newer than this reader and not marked ignorable."""


def execution_event_time_ms(payload: Any, fallback_ms: int) -> int:
    """Validated producer clock for the metadata spine, or legacy write time."""
    if not isinstance(payload, dict) or not any(
            key in payload for key in ('executionAtMs', 'executionOriginAtMs', 'executionClockId')):
        return fallback_ms
    timestamp = payload.get('executionAtMs')
    origin = payload.get('executionOriginAtMs')
    clock_id = payload.get('executionClockId')
    if (isinstance(timestamp, bool) or not isinstance(timestamp, int)
            or not 0 < timestamp <= 9_007_199_254_740_991):
        raise EventFormatError('invalid server execution clock')
    if payload.get('type') == 'delta' and origin is None and clock_id is None \
            and 'executionOriginAtMs' not in payload and 'executionClockId' not in payload:
        return timestamp
    if (isinstance(origin, bool) or not isinstance(origin, int)
            or not 0 < origin <= timestamp
            or not isinstance(clock_id, str) or not 1 <= len(clock_id) <= 128):
        raise EventFormatError('invalid server execution clock')
    return timestamp


def stamp_event_format_envelope(payload: Any) -> Any:
    """Stamp the durable format envelope onto one event before persistence.

    Called at the storage authority (``_append_event_row``). The caller is not
    the format authority: a payload that already carries ``v`` or ``ignorable``
    is rejected, since accepting it could persist an unvalidated or
    future-version envelope. Non-dict payloads (legacy scalar rows) carry no
    envelope and pass through unchanged.
    """
    if not isinstance(payload, dict):
        return payload
    if _ENVELOPE_KEY in payload or _IGNORABLE_KEY in payload:
        raise EventFormatError(
            'event payload uses the reserved format-envelope namespace '
            f'({_ENVELOPE_KEY!r}/{_IGNORABLE_KEY!r})')
    stamped = dict(payload)
    stamped[_ENVELOPE_KEY] = EVENT_FORMAT_VERSION
    return stamped


def event_envelope_version(payload: Any) -> int:
    """Return the durable format version of one persisted event payload.

    Absent/invalid envelope ⇒ the pre-versioning format ``0``. Non-dict
    payloads (legacy scalar rows) are also ``0``.
    """
    if not isinstance(payload, dict):
        return 0
    version = payload.get(_ENVELOPE_KEY)
    if isinstance(version, bool) or not isinstance(version, int):
        return 0
    return version


def event_is_ignorable(payload: Any) -> bool:
    """Return true when a newer-than-understood event may be skipped."""
    return (
        isinstance(payload, dict)
        and payload.get(_IGNORABLE_KEY) is True
    )


def check_event_readable(payload: Any) -> str:
    """Classify one persisted event: ``'ok'`` | ``'skip'`` | raise.

    * ``'ok'``   — the reader understands this format (``v <= current``).
    * ``'skip'`` — newer than understood but marked ``ignorable``; drop it.
    * raises :class:`EventFormatError` — newer than understood and *not*
      ignorable: the stream must not be silently resumed.
    """
    version = event_envelope_version(payload)
    if version <= EVENT_FORMAT_VERSION:
        return 'ok'
    if event_is_ignorable(payload):
        return 'skip'
    raise EventFormatError(
        f'persisted event format v{version} is newer than supported '
        f'v{EVENT_FORMAT_VERSION} and is not marked ignorable')


def validate_event_stream_readable(payloads: Any) -> list[Any]:
    """Validate an ordered persisted event stream, returning readable payloads.

    Drops newer-``ignorable`` payloads; raises :class:`EventFormatError` on the
    first newer-required payload. Used by cold replay / the inspector before
    folding a stream it did not write.
    """
    readable: list[Any] = []
    for payload in payloads or []:
        if check_event_readable(payload) == 'ok':
            readable.append(payload)
    return readable


__all__ = [
    'EVENT_FORMAT_VERSION',
    'EventFormatError',
    'check_event_readable',
    'event_envelope_version',
    'event_is_ignorable',
    'execution_event_time_ms',
    'stamp_event_format_envelope',
    'validate_event_stream_readable',
]
