"""Logical shadow segments preserve lineage, bounds, and crash tails."""

from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from lib.storage_sidecar import logical_shadow
from lib.storage_sidecar.logical_shadow import (
    LogicalCommitShadow,
    LogicalShadowCapacityError,
    LogicalShadowCorruptionError,
    LogicalShadowPermissionError,
    LogicalShadowUnavailableError,
)


pytestmark = pytest.mark.unit


def _append(shadow: LogicalCommitShadow, value: int, *, blob: int = 0):
    return shadow.append(
        operation='conversation.turn.append',
        tenant_id='tenant-test',
        owner_user_id=7,
        command_id=f'command-{value}',
        payload={'value': value, 'blob': 'x' * blob},
        committed_at_ms=1_700_000_000_000 + value,
    )


def _data_files(root: Path) -> list[Path]:
    return sorted(path for path in root.iterdir()
                  if path.name.startswith('segment-'))


def test_append_reopen_and_continue_sequence(tmp_path):
    root = tmp_path / 'logical-shadow'
    shadow = LogicalCommitShadow(root, stream_id='authority-123')
    first = _append(shadow, 1)
    second = _append(shadow, 2)
    assert first.sequence == 1
    assert second.sequence == 2
    assert first.request_digest != second.request_digest
    assert len(first.record_digest) == 64
    assert [record['sequence'] for record in shadow.read_records()] == [1, 2]
    status = shadow.status()
    assert not status.authoritative
    assert status.next_sequence == 3
    assert status.records == 2
    shadow.close()

    reopened = LogicalCommitShadow(root, stream_id='authority-123')
    third = _append(reopened, 3)
    assert third.sequence == 3
    assert [record['payload']['value']
            for record in reopened.read_records()] == [1, 2, 3]
    reopened.close()


def test_read_records_pages_from_checkpoint_sequence(tmp_path):
    root = tmp_path / 'logical-shadow-page'
    with LogicalCommitShadow(root, stream_id='authority-page') as shadow:
        for value in range(1, 7):
            _append(shadow, value)
        assert [
            record['sequence']
            for record in shadow.read_records(
                start_sequence=3, max_records=2)
        ] == [3, 4]
        assert shadow.read_records(start_sequence=7, max_records=2) == []


def test_expected_sequence_retry_is_idempotent_across_restart(tmp_path):
    root = tmp_path / 'idempotent-retry'
    shadow = LogicalCommitShadow(root, stream_id='authority-retry')
    first = shadow.append(
        operation='record.put',
        tenant_id='tenant-test',
        owner_user_id=7,
        payload={'request': {'key': 'one'}},
        command_id='command-one',
        request_digest='a' * 64,
        committed_at_ms=10,
        event_id='event-one',
        expected_sequence=1,
    )
    shadow.close()

    reopened = LogicalCommitShadow(root, stream_id='authority-retry')
    replay = reopened.append(
        operation='record.put',
        tenant_id='tenant-test',
        owner_user_id=7,
        payload={'request': {'key': 'one'}},
        command_id='command-one',
        request_digest='a' * 64,
        committed_at_ms=10,
        event_id='event-one',
        expected_sequence=1,
    )
    assert replay.duplicate
    assert replay.record_digest == first.record_digest
    assert reopened.status().records == 1
    reopened.close()


def test_expected_sequence_rejects_gap_or_divergent_retry(tmp_path):
    root = tmp_path / 'sequence-guard'
    shadow = LogicalCommitShadow(root, stream_id='authority-sequence')
    with pytest.raises(LogicalShadowCorruptionError, match='sequence gap'):
        shadow.append(
            operation='record.put', tenant_id='tenant-test', owner_user_id=7,
            payload={'value': 2}, expected_sequence=2, event_id='event-two')
    shadow.append(
        operation='record.put', tenant_id='tenant-test', owner_user_id=7,
        payload={'value': 1}, expected_sequence=1, event_id='event-one',
        committed_at_ms=1)
    with pytest.raises(LogicalShadowCorruptionError, match='different event'):
        shadow.append(
            operation='record.put', tenant_id='tenant-test', owner_user_id=7,
            payload={'value': 'different'}, expected_sequence=1,
            event_id='event-one', committed_at_ms=1)
    shadow.close()


def test_directory_segments_and_lock_are_private(tmp_path):
    root = tmp_path / 'private-shadow'
    with LogicalCommitShadow(root, stream_id='authority-private') as shadow:
        _append(shadow, 1)
        assert stat.S_IMODE(root.stat().st_mode) & 0o077 == 0
        for path in [root / '.writer.lock', *_data_files(root)]:
            assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def test_existing_broad_directory_permissions_fail_closed(tmp_path):
    root = tmp_path / 'broad-shadow'
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    with pytest.raises(LogicalShadowPermissionError, match='group/world'):
        LogicalCommitShadow(root, stream_id='authority-private')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX group-mode contract')
def test_explicit_group_access_supports_shared_service_accounts(tmp_path):
    root = tmp_path / 'group-shadow'
    root.mkdir(mode=0o770)
    os.chmod(root, 0o2770)
    with LogicalCommitShadow(
        root,
        stream_id='authority-group',
        access_mode='group',
    ) as shadow:
        _append(shadow, 1)
        assert shadow.status().access_mode == 'group'
        for path in [root / '.writer.lock', *_data_files(root)]:
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode & 0o060 == 0o060
            assert mode & 0o007 == 0


@pytest.mark.skipif(os.name == 'nt', reason='POSIX group-mode contract')
def test_group_access_still_rejects_world_or_incomplete_group_permissions(
    tmp_path,
):
    world = tmp_path / 'world-shadow'
    world.mkdir(mode=0o777)
    os.chmod(world, 0o777)
    with pytest.raises(LogicalShadowPermissionError):
        LogicalCommitShadow(
            world, stream_id='authority-group', access_mode='group')

    read_only_group = tmp_path / 'group-readonly-shadow'
    read_only_group.mkdir(mode=0o750)
    os.chmod(read_only_group, 0o750)
    with pytest.raises(LogicalShadowPermissionError):
        LogicalCommitShadow(
            read_only_group,
            stream_id='authority-group',
            access_mode='group',
        )


def test_only_one_writer_can_hold_a_shadow(tmp_path):
    root = tmp_path / 'single-writer'
    owner = LogicalCommitShadow(root, stream_id='authority-lock')
    try:
        with pytest.raises(LogicalShadowUnavailableError, match='another writer'):
            LogicalCommitShadow(root, stream_id='authority-lock')
    finally:
        owner.close()
    successor = LogicalCommitShadow(root, stream_id='authority-lock')
    successor.close()


def test_incomplete_open_tail_is_truncated_on_recovery(tmp_path):
    root = tmp_path / 'tail-recovery'
    shadow = LogicalCommitShadow(root, stream_id='authority-tail')
    _append(shadow, 1)
    active = root / shadow.status().active_segment
    valid_size = active.stat().st_size
    shadow.close()

    with active.open('ab', buffering=0) as stream:
        stream.write(b'\x00\x01')  # half of the four-byte frame length
        os.fsync(stream.fileno())
    assert active.stat().st_size == valid_size + 2

    recovered = LogicalCommitShadow(root, stream_id='authority-tail')
    assert recovered.status().repaired_tail_bytes == 2
    assert active.stat().st_size == valid_size
    assert [record['sequence'] for record in recovered.read_records()] == [1]
    recovered.close()


def test_complete_checksum_corruption_is_never_truncated_as_a_tail(tmp_path):
    root = tmp_path / 'checksum-corruption'
    shadow = LogicalCommitShadow(root, stream_id='authority-checksum')
    _append(shadow, 1)
    active = root / shadow.status().active_segment
    shadow.close()
    with active.open('r+b', buffering=0) as stream:
        stream.seek(-1, os.SEEK_END)
        final_byte = stream.read(1)
        stream.seek(-1, os.SEEK_END)
        stream.write(bytes([final_byte[0] ^ 0xFF]))
        os.fsync(stream.fileno())
    with pytest.raises(LogicalShadowCorruptionError, match='checksum'):
        LogicalCommitShadow(root, stream_id='authority-checksum')


def test_stream_lineage_mismatch_fails_closed(tmp_path):
    root = tmp_path / 'lineage'
    shadow = LogicalCommitShadow(root, stream_id='authority-a')
    _append(shadow, 1)
    shadow.close()
    with pytest.raises(LogicalShadowCorruptionError, match='stream mismatch'):
        LogicalCommitShadow(root, stream_id='authority-b')


def test_rotation_keeps_every_segment_under_its_byte_ceiling(tmp_path):
    root = tmp_path / 'rotation'
    shadow = LogicalCommitShadow(
        root,
        stream_id='authority-rotation',
        max_segment_bytes=1400,
        max_record_bytes=800,
        max_total_bytes=16_000,
    )
    for value in range(8):
        _append(shadow, value, blob=280)
    status = shadow.status()
    assert status.sealed_segments >= 2
    assert status.records == 8
    assert status.bytes_used <= status.max_total_bytes
    assert all(path.stat().st_size <= status.max_segment_bytes
               for path in _data_files(root))
    assert [record['sequence'] for record in shadow.read_records()] == list(
        range(1, 9))
    shadow.close()


def test_total_budget_refuses_append_without_deleting_history(tmp_path):
    root = tmp_path / 'bounded'
    shadow = LogicalCommitShadow(
        root,
        stream_id='authority-budget',
        max_segment_bytes=1400,
        max_record_bytes=800,
        max_total_bytes=2200,
    )
    appended = 0
    with pytest.raises(LogicalShadowCapacityError, match='max_total_bytes'):
        while True:
            _append(shadow, appended, blob=300)
            appended += 1
    status = shadow.status()
    assert appended >= 1
    assert status.records == appended
    assert status.bytes_used <= status.max_total_bytes
    assert [record['sequence'] for record in shadow.read_records()] == list(
        range(1, appended + 1))
    shadow.close()


def test_append_fsync_failure_poison_fences_future_writes(
        tmp_path, monkeypatch):
    root = tmp_path / 'fsync-failure'
    shadow = LogicalCommitShadow(root, stream_id='authority-fsync')

    def fail_fsync(_descriptor):
        raise OSError('injected fsync failure')

    monkeypatch.setattr(logical_shadow.os, 'fsync', fail_fsync)
    with pytest.raises(LogicalShadowUnavailableError, match='append/fsync'):
        _append(shadow, 1)
    assert shadow.status().records == 0
    assert shadow.status().poisoned
    with pytest.raises(LogicalShadowUnavailableError, match='poisoned'):
        _append(shadow, 2)
    shadow.close()


def test_oversized_record_is_rejected_before_any_record_bytes_are_written(
        tmp_path):
    root = tmp_path / 'oversized'
    shadow = LogicalCommitShadow(
        root,
        stream_id='authority-size',
        max_segment_bytes=1200,
        max_record_bytes=600,
        max_total_bytes=2400,
    )
    before = shadow.status()
    with pytest.raises(LogicalShadowCapacityError, match='max_record_bytes'):
        _append(shadow, 1, blob=2000)
    after = shadow.status()
    assert after.records == before.records == 0
    assert after.bytes_used == before.bytes_used
    shadow.close()


@pytest.mark.parametrize(
    'overrides',
    [
        {'operation': ''},
        {'tenant_id': ''},
        {'owner_user_id': -1},
        {'command_id': ''},
        {'request_digest': 'not-a-digest'},
    ],
)
def test_invalid_identity_or_idempotency_metadata_is_rejected(
        tmp_path, overrides):
    root = tmp_path / ('invalid-' + next(iter(overrides)))
    shadow = LogicalCommitShadow(root, stream_id='authority-invalid')
    arguments = {
        'operation': 'record.put',
        'tenant_id': 'tenant-test',
        'owner_user_id': 7,
        'payload': {'value': 1},
        'command_id': 'command-1',
        'committed_at_ms': 1,
    }
    arguments.update(overrides)
    with pytest.raises(ValueError):
        shadow.append(**arguments)
    assert shadow.status().records == 0
    shadow.close()
