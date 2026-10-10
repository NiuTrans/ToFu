"""Durable event-format envelope (item F): write stamp + read refuse.

The single write authority (the sidecar's ``event.append``/``append_batch``)
stamps every persisted task-event payload with a format ``v``; readers refuse
a newer *required* payload and skip a newer *ignorable* one. The version is a
cross-language constant owned by ``contracts/tofudb_ir_v1.json`` and codegen'd
to Rust. These tests pin (a) the stamp is applied at the authority, (b) the
read-side classification, and (c) the Python/Rust version lockstep.
"""

from __future__ import annotations

import json
import re
import uuid

import pytest

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)


def _payload(row):
    value = row.get('payload', row.get('event'))
    return value if isinstance(value, dict) else json.loads(value)


def test_python_rust_event_format_version_lockstep():
    """The codegen'd Rust constant must equal the Python read-side version."""
    from lib.event_envelope import EVENT_FORMAT_VERSION

    generated = open(
        'packages/tofu-db/src/generated_tofudb_ir.rs', encoding='utf-8').read()
    match = re.search(r'pub const EVENT_FORMAT_VERSION: u32 = (\d+);', generated)
    assert match, 'EVENT_FORMAT_VERSION missing from generated Rust'
    assert int(match.group(1)) == EVENT_FORMAT_VERSION


def test_envelope_stamp_applied_at_write_authority(chat_sidecar):
    from lib.storage import get_storage_client
    from lib.event_envelope import EVENT_FORMAT_VERSION

    tid = f'envelope-{uuid.uuid4().hex[:10]}'
    client = get_storage_client(write=True)
    client.command('event.append', {
        'task_id': tid, 'sequence': 1,
        'event': {'type': 'round_usage', 'roundNum': 1},
    }, None, priority='event')
    rows = client.query(
        'event.list', {'task_id': tid, 'after_sequence': -1, 'limit': 10})
    stored = _payload(rows[0])
    assert stored['v'] == EVENT_FORMAT_VERSION
    # Producer content is preserved alongside the envelope.
    assert stored['type'] == 'round_usage'


def test_envelope_rejects_caller_supplied_format_keys(chat_sidecar):
    """The caller is not the format authority; a supplied v/ignorable fails."""
    from lib.storage import get_storage_client

    tid = f'envelope-spoof-{uuid.uuid4().hex[:10]}'
    client = get_storage_client(write=True)
    with pytest.raises(Exception):
        client.command('event.append', {
            'task_id': tid, 'sequence': 1,
            'event': {'type': 'round_usage', 'v': 99},
        }, None, priority='event')


def test_read_side_classification():
    from lib.event_envelope import (
        EVENT_FORMAT_VERSION,
        EventFormatError,
        check_event_readable,
    )

    # Current and older versions are readable.
    assert check_event_readable({'type': 'x', 'v': EVENT_FORMAT_VERSION}) == 'ok'
    assert check_event_readable({'type': 'x', 'v': 0}) == 'ok'
    # Legacy payload with no envelope is readable (pre-versioning v0).
    assert check_event_readable({'type': 'x'}) == 'ok'
    # Newer + ignorable -> skip, never raise.
    assert check_event_readable(
        {'type': 'x', 'v': EVENT_FORMAT_VERSION + 1, 'ignorable': True}) == 'skip'
    # Newer + required -> refuse.
    with pytest.raises(EventFormatError):
        check_event_readable({'type': 'x', 'v': EVENT_FORMAT_VERSION + 1})


def test_validate_event_stream_drops_ignorable_and_refuses_required():
    from lib.event_envelope import (
        EVENT_FORMAT_VERSION,
        EventFormatError,
        validate_event_stream_readable,
    )

    newer = EVENT_FORMAT_VERSION + 1
    stream = [
        {'type': 'a', 'v': EVENT_FORMAT_VERSION},
        {'type': 'b', 'v': newer, 'ignorable': True},
        {'type': 'c', 'v': 1},
    ]
    readable = validate_event_stream_readable(stream)
    assert [p['type'] for p in readable] == ['a', 'c']

    with pytest.raises(EventFormatError):
        validate_event_stream_readable(
            [{'type': 'a', 'v': EVENT_FORMAT_VERSION},
             {'type': 'b', 'v': newer}])


def test_cold_replay_fails_closed_on_newer_required_event(chat_sidecar):
    """read_events must raise (not silently fold) a newer-required stream."""
    from lib.tasks_pkg import event_log
    from lib.event_envelope import (
        EVENT_FORMAT_VERSION,
        EventFormatError,
    )

    tid = f'envelope-replay-{uuid.uuid4().hex[:10]}'
    # Forge a future-version payload straight from the storage client, past
    # the write authority's stamp rejection.
    rows = [{'sequence': 1,
             'event': {'type': 'round_usage', 'v': EVENT_FORMAT_VERSION + 1}}]

    class _Forge:
        def query(self, op, params):
            return rows

    import lib.storage as _storage
    real_get = _storage.get_storage_client
    _storage.get_storage_client = lambda *a, **k: _Forge()
    try:
        with pytest.raises(EventFormatError):
            event_log.read_events(tid, raise_on_error=False)
    finally:
        _storage.get_storage_client = real_get
