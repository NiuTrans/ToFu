"""Bounded progress supervision for slow Storage Sidecar startup work."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from lib.storage.protocol import PROTOCOL_VERSION
from lib.storage.startup_control import (
    STARTUP_PROGRESS_TYPE,
    StartupProgress,
    encode_startup_progress,
    parse_startup_progress,
)
from lib.storage.supervisor import StorageSupervisor, _StartupDeadline
from lib.storage_sidecar import fastpath


pytestmark = pytest.mark.unit


def test_startup_progress_envelope_round_trips_without_authority_details():
    encoded = encode_startup_progress(
        'fastpath.classic_seed.copy', 64, 128)
    message = json.loads(encoded)

    assert message['type'] == STARTUP_PROGRESS_TYPE
    assert parse_startup_progress(message) == StartupProgress(
        phase='fastpath.classic_seed.copy',
        completed_bytes=64,
        total_bytes=128,
        heartbeat=False,
    )
    assert 'database_path' not in message
    assert 'token' not in message


@pytest.mark.parametrize('message', [
    {
        'type': STARTUP_PROGRESS_TYPE,
        'protocol': 'future.storage',
        'phase': 'fastpath.copy',
        'completed_bytes': 1,
        'total_bytes': 2,
        'heartbeat': False,
    },
    {
        'type': STARTUP_PROGRESS_TYPE,
        'protocol': PROTOCOL_VERSION,
        'phase': '../authority',
        'completed_bytes': 1,
        'total_bytes': 2,
        'heartbeat': False,
    },
    {
        'type': STARTUP_PROGRESS_TYPE,
        'protocol': PROTOCOL_VERSION,
        'phase': 'fastpath.copy',
        'completed_bytes': 3,
        'total_bytes': 2,
        'heartbeat': False,
    },
])
def test_startup_progress_rejects_untrusted_control_fields(message):
    with pytest.raises(ValueError):
        parse_startup_progress(message)


def test_progress_renews_only_the_stall_deadline_not_the_hard_bound():
    deadline = _StartupDeadline(
        started_at=10.0, stall_timeout=5.0, hard_timeout=20.0)

    assert deadline.observe(StartupProgress(
        'fastpath.copy', 10, 100, False), now=14.0)
    assert deadline.remaining(18.0) == pytest.approx(1.0)
    # A duplicate observation is not progress and cannot keep a stuck copy up.
    assert not deadline.observe(StartupProgress(
        'fastpath.copy', 10, 100, False), now=18.0)
    assert deadline.remaining(19.0) == 0.0

    # An opaque fsync heartbeat may renew the stall watchdog, but never moves
    # the immutable hard deadline at t=30.
    assert deadline.observe(StartupProgress(
        'fastpath.fsync', 0, 0, True), now=19.0)
    assert deadline.remaining(23.0) == pytest.approx(1.0)
    assert deadline.observe(StartupProgress(
        'fastpath.fsync', 0, 0, True), now=23.0)
    assert deadline.observe(StartupProgress(
        'fastpath.fsync', 0, 0, True), now=27.0)
    assert deadline.remaining(29.5) == pytest.approx(0.5)
    assert 'hard timeout' in str(deadline.timeout_error(now=30.0))


def test_progress_rejects_regression_and_changed_phase_total():
    deadline = _StartupDeadline(
        started_at=0.0, stall_timeout=5.0, hard_timeout=20.0)
    deadline.observe(StartupProgress(
        'fastpath.copy', 10, 100, False), now=1.0)

    with pytest.raises(ValueError, match='backwards'):
        deadline.observe(StartupProgress(
            'fastpath.copy', 9, 100, False), now=2.0)
    with pytest.raises(ValueError, match='total changed'):
        deadline.observe(StartupProgress(
            'fastpath.copy', 11, 101, False), now=2.0)


class _DelayedStdout:
    def __init__(self, events: list[tuple[float, str]]) -> None:
        self._events = iter(events)
        self.read_count = 0

    def readline(self, _limit: int = -1) -> str:
        self.read_count += 1
        try:
            delay, line = next(self._events)
        except StopIteration:
            return ''
        time.sleep(delay)
        return line


class _FakeProcess:
    def __init__(self, events: list[tuple[float, str]]) -> None:
        self.stdout = _DelayedStdout(events)

    @staticmethod
    def wait(timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired('fake-sidecar', timeout)

    @staticmethod
    def poll() -> None:
        return None


def _line(payload: dict[str, object]) -> str:
    return json.dumps(payload, separators=(',', ':')) + '\n'


def test_supervisor_consumes_progress_before_the_final_ready_envelope():
    events = [
        (0.0, encode_startup_progress(
            'fastpath.copy', 0, 100) + '\n'),
        (0.02, encode_startup_progress(
            'fastpath.copy', 25, 100) + '\n'),
        (0.02, encode_startup_progress(
            'fastpath.copy', 75, 100) + '\n'),
        (0.02, _line({
            'type': 'storage.ready',
            'protocol': PROTOCOL_VERSION,
            'port': 12345,
            'backend': 'sqlite',
        })),
    ]
    supervisor = StorageSupervisor(
        startup_timeout=1.0, startup_stall_timeout=0.03)
    process = _FakeProcess(events)

    ready = supervisor._read_startup_envelope(process)

    assert isinstance(ready, dict)
    assert ready['type'] == 'storage.ready'
    assert process.stdout.read_count == len(events), (
        'the control reader must exit at ready instead of retaining one '
        'blocked daemon thread for the sidecar lifetime')


def test_supervisor_still_fails_a_progress_channel_that_stops_advancing():
    events = [
        (0.0, encode_startup_progress(
            'fastpath.copy', 10, 100) + '\n'),
        (0.2, encode_startup_progress(
            'fastpath.copy', 10, 100) + '\n'),
    ]
    supervisor = StorageSupervisor(
        startup_timeout=1.0, startup_stall_timeout=0.03)

    with pytest.raises(RuntimeError, match='stalled without progress'):
        supervisor._read_startup_envelope(_FakeProcess(events))


def test_resumable_seed_reports_monotonic_copy_progress(tmp_path, monkeypatch):
    classic = tmp_path / 'data' / 'tofu.db'
    classic.parent.mkdir()
    classic.write_bytes(b'abcdefghijklmnop')
    local_db = tmp_path / 'front' / 'tofu.db'
    monkeypatch.setattr(fastpath, '_SEED_COPY_CHECKPOINT_BYTES', 4)
    monkeypatch.setattr(fastpath, '_SEED_COPY_BUFFER_BYTES', 2)
    observations = []

    def observe(phase, completed_bytes, total_bytes, *, heartbeat=False):
        observations.append(
            (phase, completed_bytes, total_bytes, heartbeat))

    fastpath._seed_from_classic(
        local_db, classic, startup_progress=observe)

    assert observations[0] == (
        'fastpath.classic_seed.copy', 0, 16, False)
    assert observations[-1][1:3] == (16, 16)
    assert [item[1] for item in observations] == sorted(
        item[1] for item in observations)
    assert local_db.read_bytes() == classic.read_bytes()


def test_opaque_recovery_step_emits_heartbeat_until_completion(monkeypatch):
    monkeypatch.setattr(fastpath, '_STARTUP_HEARTBEAT_SECONDS', 0.005)
    observations = []

    def observe(phase, completed_bytes, total_bytes, *, heartbeat=False):
        observations.append(
            (phase, completed_bytes, total_bytes, heartbeat))

    fastpath._run_with_startup_heartbeat(
        lambda: time.sleep(0.02),
        phase='fastpath.shadow_restore.verify',
        startup_progress=observe,
    )

    assert observations[0] == (
        'fastpath.shadow_restore.verify', 0, 0, False)
    assert any(item[3] is True for item in observations[1:])


def test_source_change_during_seed_never_publishes_mixed_bytes(
        tmp_path, monkeypatch):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    classic = data_dir / 'tofu.db'
    classic.write_bytes(b'original-authority')
    local_dir = tmp_path / 'front'
    decision = fastpath.FastpathDecision(
        active=True,
        reason='measured-win',
        mode=fastpath.MODE_AUTO,
        local_dir=local_dir,
        shadow_dir=data_dir / fastpath.SHADOW_DIRNAME,
    )
    original_copy = fastpath._copy_file_checkpointed
    mutated = False

    def mutate_source_after_copy(*args, **kwargs):
        nonlocal mutated
        result = original_copy(*args, **kwargs)
        if not mutated:
            mutated = True
            classic.write_bytes(b'changed--authority')
        return result

    monkeypatch.setattr(
        fastpath, '_copy_file_checkpointed', mutate_source_after_copy)

    assert fastpath.reconcile(decision, classic) == classic
    local_db = local_dir / 'tofu.db'
    assert not local_db.exists()
    assert not fastpath._seed_paths(local_db)[2].exists()


def test_source_fingerprint_witness_detects_equal_size_rewrite(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'original-authority')
    before = fastpath._source_fingerprint(source)
    source.write_bytes(b'changed--authority')
    after = fastpath._source_fingerprint(source)

    assert before['size'] == after['size']
    assert (before['content_witness_sha256']
            != after['content_witness_sha256'])


def test_large_source_content_witness_has_fixed_read_budget(
        tmp_path, monkeypatch):
    source = tmp_path / 'large-sparse-source'
    source_size = 64 * 1024 ** 2
    with source.open('wb') as stream:
        stream.truncate(source_size)
    observed = {'bytes': 0}

    class CountingDigest:
        def update(self, payload):
            observed['bytes'] += len(payload)

        @staticmethod
        def hexdigest():
            return 'bounded'

    monkeypatch.setattr(
        fastpath.hashlib, 'sha256', lambda: CountingDigest())

    assert fastpath._bounded_content_witness(source, source_size) == 'bounded'
    maximum = (
        len(str(source_size))
        + fastpath._FINGERPRINT_SAMPLE_COUNT
        * (8 + fastpath._FINGERPRINT_SAMPLE_BYTES)
    )
    assert observed['bytes'] <= maximum


def test_private_seed_symlink_is_unlinked_without_touching_its_target(
        tmp_path):
    classic = tmp_path / 'data' / 'tofu.db'
    classic.parent.mkdir()
    classic.write_bytes(b'authority')
    local_db = tmp_path / 'front' / 'tofu.db'
    local_db.parent.mkdir()
    outside = tmp_path / 'outside-user-file'
    outside.write_bytes(b'keep-me')
    temporary, _temporary_wal, _state = fastpath._seed_paths(local_db)
    temporary.symlink_to(outside)

    fastpath._seed_from_classic(local_db, classic)

    assert outside.read_bytes() == b'keep-me'
    assert local_db.read_bytes() == b'authority'


def test_lineage_publication_failure_preserves_recoverable_install(
        tmp_path, monkeypatch):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    classic = data_dir / 'tofu.db'
    classic.write_bytes(b'authority')
    local_dir = tmp_path / 'front'
    local_db = local_dir / 'tofu.db'
    shadow_dir = data_dir / fastpath.SHADOW_DIRNAME
    decision = fastpath.FastpathDecision(
        active=True,
        reason='measured-win',
        mode=fastpath.MODE_AUTO,
        local_dir=local_dir,
        shadow_dir=shadow_dir,
    )
    original_write_manifest = fastpath.write_local_manifest

    def fail_lineage_publication(*_args, **_kwargs):
        raise RuntimeError('injected publication failure')

    monkeypatch.setattr(
        fastpath,
        'write_local_manifest',
        fail_lineage_publication,
    )

    assert fastpath.reconcile(decision, classic) == classic
    assert local_db.read_bytes() == classic.read_bytes()
    assert fastpath._completed_seed_is_recoverable(local_db, classic)
    assert not list(local_dir.glob('tofu.db.foreign-*'))

    monkeypatch.setattr(
        fastpath, 'write_local_manifest', original_write_manifest)
    assert fastpath.reconcile(decision, classic) == local_db
    assert fastpath.read_local_manifest(local_dir)['shadow_dir'] == str(
        shadow_dir)
