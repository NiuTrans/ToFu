"""Fast-path authority: fail-closed activation, shadow shipping, recovery.

Pins the 2026-08-20 Layer-2 root fix contracts:

* ACTIVATION IS FAIL-CLOSED — off/no-candidate/same-device/insufficient
  speedup all leave the authority on the data dir; ``required`` refuses to
  boot; the benchmark verdict is recorded either way (the "measured win"
  guarantee is observability, not hope).
* THE SHADOW IS ALWAYS CONSISTENT — the shipper owns every checkpoint; the
  shadow WAL is a frame-aligned byte prefix of the front's WAL; a local-disk
  loss recovers from snapshot + native SQLite replay with only the unshipped
  tail forfeit.
* RECONCILIATION IS TWO-WAY — a surviving local front stays authoritative
  (its unshipped crash tail forward-ships once running); a uuid mismatch
  between front and shadow is split-brain and falls back to the classic
  authority with a CRITICAL, never a silent guess.
* THE FRONT IS PER-DEPLOYMENT — auto candidate dirs are keyed by the data
  dir, and a front whose manifest names another deployment's shadow (or no
  shadow at all) is quarantined, never served (2026-08-20 incident: the
  production sidecar served a certification test's authority).
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar import fastpath, shipper as shipper_module
from lib.storage_sidecar.shipper import (
    WalShipper,
    adaptive_wal_rebase_budget,
    filesystem_wal_rebase_maximum,
    proactive_wal_rebase_trigger,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- decide()

def _env(**overrides):
    env = {
        'TOFU_STORAGE_FASTPATH': 'auto',
        'XDG_STATE_HOME': '',
        'TOFU_STORAGE_FASTPATH_DIR': '',
    }
    env.update(overrides)
    return env


def test_decide_off_never_probes(tmp_path):
    decision = fastpath.decide(
        tmp_path, environ=_env(TOFU_STORAGE_FASTPATH='off'),
        benchmark=lambda d: 1.0)
    assert not decision.active
    assert 'off' in decision.reason


def test_decide_auto_without_candidates_stays_classic(tmp_path, monkeypatch):
    # Every auto candidate points back INTO the data dir → filtered.
    monkeypatch.setattr(fastpath.tempfile, 'gettempdir',
                        lambda: str(tmp_path))
    monkeypatch.setattr(fastpath.Path, 'home', lambda: tmp_path)
    decision = fastpath.decide(
        tmp_path, environ=_env(), benchmark=lambda d: 1.0)
    assert not decision.active


def test_decide_required_without_candidates_refuses_boot(tmp_path, monkeypatch):
    monkeypatch.setattr(fastpath.tempfile, 'gettempdir',
                        lambda: str(tmp_path))
    monkeypatch.setattr(fastpath.Path, 'home', lambda: tmp_path)
    with pytest.raises(RuntimeError):
        fastpath.decide(
            tmp_path, environ=_env(TOFU_STORAGE_FASTPATH='required'),
            benchmark=lambda d: 1.0)


def test_decide_activates_on_measured_win(tmp_path):
    candidate = tmp_path / 'fast'
    data_dir = tmp_path / 'data'
    data_dir.mkdir()

    def fake_benchmark(directory):
        return 1.0 if Path(directory) == candidate else 100.0

    decision = fastpath.decide(
        data_dir,
        environ=_env(TOFU_STORAGE_FASTPATH_DIR=str(candidate)),
        benchmark=fake_benchmark)
    assert decision.active, decision.reason
    assert decision.local_dir == candidate
    assert decision.benchmark['speedup'] == 100.0
    assert decision.benchmark['data_dir_median_fsync_ms'] == 100.0


def test_decide_skips_on_insufficient_measured_win(tmp_path):
    candidate = tmp_path / 'fast'
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    decision = fastpath.decide(
        data_dir,
        environ=_env(TOFU_STORAGE_FASTPATH_DIR=str(candidate)),
        benchmark=lambda d: 30.0)  # identical latency everywhere
    assert not decision.active
    assert decision.benchmark['speedup'] == 1.0


def test_decide_min_speedup_zero_activates_on_any_win(tmp_path):
    candidate = tmp_path / 'fast'
    data_dir = tmp_path / 'data'
    data_dir.mkdir()

    def fake_benchmark(directory):
        return 1.0 if Path(directory) == candidate else 1.5

    decision = fastpath.decide(
        data_dir,
        environ=_env(TOFU_STORAGE_FASTPATH_DIR=str(candidate),
                     TOFU_STORAGE_FASTPATH_MIN_SPEEDUP='0'),
        benchmark=fake_benchmark)
    assert decision.active


# ------------------------------------------------------- shipper primitives

def test_wal_rebase_budget_scales_with_authority_and_has_a_disk_ceiling():
    mib = 1024 ** 2
    gib = 1024 ** 3

    assert adaptive_wal_rebase_budget(512 * mib, 8 * gib) == 128 * mib
    assert adaptive_wal_rebase_budget(16 * gib, 8 * gib) == 4 * gib
    assert adaptive_wal_rebase_budget(80 * gib, 8 * gib) == 8 * gib
    assert adaptive_wal_rebase_budget(80 * gib, 16 * gib) == 16 * gib
    assert adaptive_wal_rebase_budget(80 * gib, 512 * mib) == 512 * mib


def test_proactive_wal_rebase_reserves_one_sixteenth_for_checkpoint_races():
    mib = 1024 ** 2
    gib = 1024 ** 3

    assert proactive_wal_rebase_trigger(16 * gib) == 15 * gib
    assert proactive_wal_rebase_trigger(64 * mib) == 60 * mib


def test_ship_pass_rebases_before_the_hard_write_pressure_fence(
        tmp_path, monkeypatch):
    pressure_bytes = 16 * 1024 ** 2
    shipper = WalShipper(
        tmp_path / 'front.sqlite3',
        tmp_path / 'shadow',
        authority_uuid='proactive-rebase',
        checkpoint_fn=lambda: None,
        wal_budget_bytes=pressure_bytes,
    )
    observed_wal_bytes = {'value': shipper._wal_rebase_trigger_bytes - 1}
    monkeypatch.setattr(shipper, '_frame_size', lambda: 1)
    monkeypatch.setattr(
        shipper, '_local_wal_size', lambda: observed_wal_bytes['value'])
    monkeypatch.setattr(
        shipper, '_maybe_write_manifest', lambda **_kwargs: None)
    shipper._wal_shipped = observed_wal_bytes['value']
    cycles = []
    monkeypatch.setattr(
        shipper, '_snapshot_cycle', lambda: cycles.append('rebase'))

    shipper._ship_pass_locked()
    assert cycles == []

    observed_wal_bytes['value'] = shipper._wal_rebase_trigger_bytes
    shipper._wal_shipped = observed_wal_bytes['value']
    shipper.notify_commit()
    shipper.assert_write_admitted()
    shipper._ship_pass_locked()

    assert cycles == ['rebase']
    assert shipper.status()['write_pressure_active'] is False
    assert shipper.status()['wal_write_headroom_bytes'] == (
        pressure_bytes - shipper._wal_rebase_trigger_bytes
    )


def test_wal_rebase_maximum_is_bounded_by_both_filesystems():
    mib = 1024 ** 2
    gib = 1024 ** 3

    assert filesystem_wal_rebase_maximum(
        16 * gib,
        local_free_bytes=100 * gib,
        shadow_free_bytes=1024 * gib,
    ) == 2 * gib
    assert filesystem_wal_rebase_maximum(
        512 * mib,
        local_free_bytes=100 * gib,
        shadow_free_bytes=1024 * gib,
    ) == 512 * mib
    assert filesystem_wal_rebase_maximum(
        16 * gib,
        local_free_bytes=None,
        shadow_free_bytes=None,
    ) == 16 * gib


def test_shipper_rechecks_local_and_shadow_free_space_for_default_budget(
        tmp_path, monkeypatch):
    gib = 1024 ** 3
    local_dir = tmp_path / 'front'
    shadow_dir = tmp_path / 'data' / fastpath.SHADOW_DIRNAME
    local_dir.mkdir()
    shadow_dir.mkdir(parents=True)
    local_db = local_dir / 'tofu.db'
    local_db.touch()
    os.truncate(local_db, 16 * gib)

    def disk_usage(path):
        free = 100 * gib if Path(path) == local_dir else 1024 * gib
        return SimpleNamespace(free=free)

    monkeypatch.setattr(shipper_module.shutil, 'disk_usage', disk_usage)
    shipper = WalShipper(
        local_db,
        shadow_dir,
        authority_uuid='test',
        checkpoint_fn=lambda: None,
        wal_budget_max_bytes=16 * gib,
    )

    assert shipper.status()['wal_rebase_budget_bytes'] == 2 * gib


def test_explicit_shipper_wal_budget_remains_authoritative(tmp_path):
    shipper = WalShipper(
        tmp_path / 'missing-front.sqlite3',
        tmp_path / 'shadow',
        authority_uuid='test',
        checkpoint_fn=lambda: None,
        wal_budget_bytes=2 * 1024 ** 2,
        wal_budget_max_bytes=8 * 1024 ** 3,
    )

    assert shipper.status()['wal_rebase_budget_bytes'] == 2 * 1024 ** 2


def test_rebase_write_pressure_fences_new_writes_at_the_wal_budget(
        tmp_path, monkeypatch):
    budget_bytes = 2 * 1024 ** 2
    observed_wal_bytes = {'value': 0}
    shipper = WalShipper(
        tmp_path / 'front.sqlite3',
        tmp_path / 'shadow',
        authority_uuid='pressure-authority',
        checkpoint_fn=lambda: None,
        wal_budget_bytes=budget_bytes,
    )
    monkeypatch.setattr(
        shipper, '_local_wal_size', lambda: observed_wal_bytes['value'])

    # The threshold protects the ordinary pre-checkpoint window too: a failed
    # capacity preflight must not leave the local WAL free to grow forever.
    shipper._set_rebase_active(False)
    shipper.assert_write_admitted()
    observed_wal_bytes['value'] = budget_bytes
    shipper.notify_commit()

    with pytest.raises(StorageError) as raised:
        shipper.assert_write_admitted()
    assert raised.value.code == 'database_busy'
    assert raised.value.retryable is True
    assert raised.value.retry_after_ms == 250
    status = shipper.status()
    assert status['rebase_active'] is False
    assert status['write_pressure_active'] is True
    assert status['write_pressure_activations'] == 1
    assert status['write_pressure_rejections'] == 1
    assert status['wal_write_pressure_bytes'] == budget_bytes
    assert status['local_wal_bytes'] == budget_bytes
    assert status['wal_write_headroom_bytes'] == 0

    shipper._set_rebase_active(True)
    assert shipper.status()['write_pressure_active'] is True

    # Publication alone must not release a full WAL: the next raw checkpoint
    # owns that transition. Once the checkpoint truncates the WAL, admission
    # reopens immediately.
    shipper._set_rebase_active(False)
    assert shipper.status()['write_pressure_active'] is True
    observed_wal_bytes['value'] = 0
    shipper._set_rebase_active(False)
    shipper.assert_write_admitted()
    assert shipper.status()['write_pressure_active'] is False

    def fail_wal_observation():
        raise OSError('injected WAL stat failure')

    monkeypatch.setattr(shipper, '_local_wal_size', fail_wal_observation)
    shipper._observe_current_write_pressure()
    status = shipper.status()
    assert status['write_pressure_active'] is True
    assert status['write_pressure_observation_failures'] == 1


def test_shipper_reclaims_only_dead_owner_private_snapshot_artifacts(
        tmp_path, monkeypatch):
    shadow_dir = tmp_path / 'data' / fastpath.SHADOW_DIRNAME
    shadow_dir.mkdir(parents=True)
    dead_copy = shadow_dir / 'snapshot.sqlite3.tmp-11'
    dead_journal = shadow_dir / 'snapshot.sqlite3.tmp-11-journal'
    live_copy = shadow_dir / 'shadow.wal.tmp-22'
    unrelated = shadow_dir / 'snapshot.sqlite3.tmp-not-a-pid'
    dead_copy.write_bytes(b'1234567')
    dead_journal.write_bytes(b'abc')
    live_copy.write_bytes(b'keep-live')
    unrelated.write_bytes(b'keep-user')
    monkeypatch.setattr(
        WalShipper, '_pid_is_alive', staticmethod(lambda pid: pid == 22))
    shipper = WalShipper(
        tmp_path / 'front' / 'tofu.db',
        shadow_dir,
        authority_uuid='test',
        checkpoint_fn=lambda: None,
    )

    shipper._cleanup_stale_artifacts()

    assert not dead_copy.exists()
    assert not dead_journal.exists()
    assert live_copy.read_bytes() == b'keep-live'
    assert unrelated.read_bytes() == b'keep-user'
    assert shipper.metrics['stale_artifacts_reclaimed'] == 2
    assert shipper.metrics['stale_artifact_bytes_reclaimed'] == 10


def test_new_shipper_generation_discovers_resumable_snapshot(tmp_path):
    local_db = tmp_path / 'front' / 'tofu.db'
    shadow_dir = tmp_path / 'data' / fastpath.SHADOW_DIRNAME
    local_db.parent.mkdir(parents=True)
    shadow_dir.mkdir(parents=True)
    connection = sqlite3.connect(local_db, isolation_level=None)
    connection.execute('CREATE TABLE items(k TEXT PRIMARY KEY, v INTEGER)')
    connection.close()
    fastpath.write_shadow_manifest(shadow_dir, {
        'format': 'tofu.fastpath-shadow.v1',
        'authority_uuid': 'restart-authority',
        'generation': 4,
        'wal_shipped_bytes': 0,
        'snapshot_bytes': local_db.stat().st_size,
        'updated_at': time.time(),
    })
    interrupted = WalShipper(
        local_db,
        shadow_dir,
        authority_uuid='restart-authority',
        checkpoint_fn=lambda: None,
    )
    interrupted._generation = 4
    interrupted._rebase_snapshot.write_bytes(b'partial')
    interrupted._write_rebase_state(
        fastpath._source_fingerprint(local_db),
        len(b'partial'),
        phase='copying_database',
    )

    replacement = WalShipper(
        local_db,
        shadow_dir,
        authority_uuid='restart-authority',
        checkpoint_fn=lambda: None,
    )
    replacement._resume_or_snapshot()

    assert replacement._generation == 4
    assert replacement._needs_snapshot is True
    assert replacement._shadow_wal_matches_local is False
    assert replacement.status()['snapshot_progress_bytes'] == len(b'partial')
    assert replacement._rebase_snapshot.read_bytes() == b'partial'


def test_rebase_capacity_credits_only_owned_resume_progress(
        tmp_path, monkeypatch):
    shipper = WalShipper(
        tmp_path / 'front' / 'tofu.db',
        tmp_path / 'shadow',
        authority_uuid='capacity-authority',
        checkpoint_fn=lambda: None,
    )
    shipper._shadow_dir.mkdir(parents=True)
    shipper._rebase_snapshot.write_bytes(b'reusable-prefix-plus-tail')
    captured = {}

    def capture(_directory, source_bytes, *, purpose, reusable_bytes):
        captured.update({
            'source_bytes': source_bytes,
            'purpose': purpose,
            'reusable_bytes': reusable_bytes,
        })

    monkeypatch.setattr(fastpath, '_require_copy_capacity', capture)

    shipper._require_rebase_capacity(1234, durable_bytes=8)

    assert captured == {
        'source_bytes': 1234,
        'purpose': 'fastpath shadow rebase',
        'reusable_bytes': 8,
    }


def test_rebase_state_write_discards_uncommitted_private_replacement(tmp_path):
    shipper = WalShipper(
        tmp_path / 'front' / 'tofu.db',
        tmp_path / 'shadow',
        authority_uuid='state-authority',
        checkpoint_fn=lambda: None,
    )
    shipper._shadow_dir.mkdir(parents=True)
    interrupted_replacement = shipper._rebase_state.with_name(
        shipper._rebase_state.name + '.new')
    interrupted_replacement.write_text('uncommitted', encoding='utf-8')

    shipper._write_rebase_state(
        {'size': 0}, 0, phase='copying_database')

    assert not interrupted_replacement.exists()
    assert json.loads(shipper._rebase_state.read_text(encoding='utf-8'))[
        'database_bytes'] == 0


def test_wal_copy_refuses_a_short_source_instead_of_zero_extending(tmp_path):
    local_db = tmp_path / 'front' / 'tofu.db'
    local_db.parent.mkdir(parents=True)
    local_db.write_bytes(b'database')
    local_wal = local_db.with_name(local_db.name + '-wal')
    local_wal.write_bytes(b'abc')
    shipper = WalShipper(
        local_db,
        tmp_path / 'shadow',
        authority_uuid='short-wal-authority',
        checkpoint_fn=lambda: None,
    )
    destination = tmp_path / 'partial-shadow.wal'

    with pytest.raises(RuntimeError, match='ended at 3 of 5'):
        shipper._copy_range_to(destination, 0, 5)

    assert destination.read_bytes() == b'abc'


@pytest.fixture()
def front(tmp_path):
    """A real front authority on a real writer, shipper-owned checkpoints."""
    from lib.storage_sidecar.adapters.sqlite import _FairWriter

    local_dir = tmp_path / 'front'
    shadow_dir = tmp_path / 'data' / fastpath.SHADOW_DIRNAME
    local_dir.mkdir(parents=True)
    shadow_dir.mkdir(parents=True)
    local_db = local_dir / 'tofu.db'
    connection = sqlite3.connect(
        local_db, isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA synchronous=FULL')
    connection.execute('PRAGMA wal_autocheckpoint=0')
    connection.execute(
        'CREATE TABLE items(k TEXT PRIMARY KEY, v INTEGER NOT NULL)')
    # Emulate the real authority's lineage row (the restore gate reads it).
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, '
        'meta_value TEXT NOT NULL)')
    connection.execute(
        'INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?)',
        ('authority_uuid', 'test-authority'))
    fastpath.write_local_manifest(
        local_dir, {'authority_uuid': 'test-authority',
                    'shadow_dir': str(shadow_dir)})

    writer = _FairWriter(connection, transaction_timeout_s=30.0)

    checkpoint_deadlines = []

    def checkpoint_at(deadline_at):
        checkpoint_deadlines.append(deadline_at)

        def op(session):
            row = session.fetch_one('PRAGMA wal_checkpoint(TRUNCATE)')
            busy = int(next(iter(row.values()))) if row else 1
            if busy:
                raise RuntimeError('checkpoint busy')

        writer.submit(op, 'maintenance', deadline_at,
                      operation_name='fastpath.checkpoint', raw=True)

    def checkpoint_fn():
        checkpoint_at(time.monotonic() + 30.0)

    def put(k, v):
        def op(session):
            session.execute(
                'INSERT INTO items(k, v) VALUES (?, ?) '
                'ON CONFLICT(k) DO UPDATE SET v = excluded.v', (k, v))
            return v

        return writer.submit(op, 'user', time.monotonic() + 30.0)

    shipper = WalShipper(
        local_db, shadow_dir, authority_uuid='test-authority',
        checkpoint_fn=checkpoint_fn,
        checkpoint_deadline_fn=checkpoint_at,
        wal_budget_bytes=1024 ** 2,
        tick_s=0.1,
    )
    writer._on_commit = shipper.notify_commit
    writer.set_write_admission_hook(shipper.assert_write_admitted)
    yield type('Front', (), {
        'db': local_db, 'shadow_dir': shadow_dir, 'writer': writer,
        'shipper': shipper, 'put': staticmethod(put),
        'connection': connection,
        'checkpoint_deadlines': checkpoint_deadlines,
    })
    shipper.stop()
    writer.close()


def _wait_for(predicate, timeout=15.0, label='condition'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f'timed out waiting for {label}')


def _shadow_manifest(shadow_dir):
    return fastpath.read_shadow_manifest(shadow_dir)


def test_initial_snapshot_then_incremental_shipping(front):
    shipper = front.shipper
    shipper.start()
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None,
              label='initial snapshot manifest')
    manifest = _shadow_manifest(front.shadow_dir)
    assert manifest['generation'] == 1
    assert manifest['authority_uuid'] == 'test-authority'

    for i in range(10):
        front.put(f'k{i}', i)
    _wait_for(lambda: _shadow_manifest(front.shadow_dir)[
        'wal_shipped_bytes'] > 0, label='WAL shipping')
    status = shipper.status()
    assert status['ships'] >= 1
    assert status['ship_lag_bytes'] >= 0
    assert status['snapshot_database_bytes_copied'] \
        == manifest['snapshot_bytes']
    assert status['snapshot_wal_bytes_copied'] >= 0


def test_backup_pin_forces_checkpointed_generation_and_reuses_shadow_inode(
        front, tmp_path):
    front.shipper.start()
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None,
              label='initial snapshot manifest')
    front.put('included-before-backup', 17)
    generation_before = front.shipper.status()['generation']
    destination = tmp_path / 'backup.sqlite3'

    deadline_at = time.monotonic() + 10
    result = front.shipper.pin_checkpointed_snapshot_for_backup(
        destination,
        deadline_at=deadline_at,
    )

    snapshot, _shadow_wal = fastpath.shadow_paths(front.shadow_dir)
    assert result['generation'] == generation_before + 1
    assert result['bytes'] == snapshot.stat().st_size
    assert result['copy_strategy'] == 'hardlink'
    assert 0 < result['recovery_point_at'] <= time.time()
    assert (destination.stat().st_dev, destination.stat().st_ino) == (
        snapshot.stat().st_dev, snapshot.stat().st_ino)
    assert _open_items(destination)['included-before-backup'] == 17
    assert front.checkpoint_deadlines[-1] == deadline_at


def test_timed_out_snapshot_resumes_from_last_durable_checkpoint(
        front, tmp_path, monkeypatch):
    """A multi-GiB backup timeout must not discard its fsynced prefix."""
    front.shipper._tick_s = 60
    front.shipper.start()
    front.shipper._wake.set()
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None,
              label='initial snapshot before resumable backup')
    front.writer._on_commit = None
    front.put('included-after-resume', 29)
    snapshot, _shadow_wal = fastpath.shadow_paths(front.shadow_dir)
    previous_snapshot = snapshot.read_bytes()
    previous_manifest = _shadow_manifest(front.shadow_dir)
    monkeypatch.setattr(fastpath, '_SEED_COPY_CHECKPOINT_BYTES', 4096)
    original_state_write = shipper_module.write_json_durable
    interrupted = False

    def interrupt_after_first_checkpoint(path, payload):
        nonlocal interrupted
        original_state_write(path, payload)
        if (Path(path) == front.shipper._rebase_state
                and int(payload.get('database_bytes') or 0) > 0
                and not interrupted):
            interrupted = True
            raise TimeoutError('simulated backup deadline')

    monkeypatch.setattr(
        shipper_module, 'write_json_durable',
        interrupt_after_first_checkpoint)
    with pytest.raises(TimeoutError, match='simulated backup deadline'):
        front.shipper.pin_checkpointed_snapshot_for_backup(
            tmp_path / 'timed-out.sqlite3',
            deadline_at=time.monotonic() + 10,
        )

    state = json.loads(front.shipper._rebase_state.read_text(encoding='utf-8'))
    durable_bytes = state['database_bytes']
    recovery_point_at = state['recovery_point_at']
    assert 0 < durable_bytes < front.db.stat().st_size
    assert front.shipper._rebase_snapshot.stat().st_size >= durable_bytes
    assert snapshot.read_bytes() == previous_snapshot
    assert _shadow_manifest(front.shadow_dir) == previous_manifest
    front.put('committed-after-recovery-boundary', 30)

    monkeypatch.setattr(
        shipper_module, 'write_json_durable', original_state_write)
    original_copy = fastpath._copy_file_checkpointed
    resume_offsets = []

    def record_resume(*args, **kwargs):
        if Path(args[0]) == front.db:
            resume_offsets.append(kwargs['durable_bytes'])
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(fastpath, '_copy_file_checkpointed', record_resume)
    destination = tmp_path / 'resumed.sqlite3'
    result = front.shipper.pin_checkpointed_snapshot_for_backup(
        destination,
        deadline_at=time.monotonic() + 10,
    )

    assert resume_offsets == [durable_bytes]
    assert result['generation'] == previous_manifest['generation'] + 1
    assert _open_items(destination)['included-after-resume'] == 29
    assert 'committed-after-recovery-boundary' not in _open_items(destination)
    assert result['recovery_point_at'] == recovery_point_at
    assert not front.shipper._rebase_state.exists()
    assert not front.shipper._rebase_snapshot.exists()
    status = front.shipper.status()
    assert status['snapshot_resume_count'] == 1
    assert status['snapshot_resumed_bytes'] == durable_bytes
    assert status['snapshot_progress_bytes'] == 0


def test_changed_snapshot_source_invalidates_resumable_prefix(
        front, tmp_path, monkeypatch):
    """A resume witness is authority only while its source identity matches."""
    front.shipper._tick_s = 60
    front.shipper.start()
    front.shipper._wake.set()
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None,
              label='initial snapshot before changed-source backup')
    front.writer._on_commit = None
    front.put('changed-source', 31)
    monkeypatch.setattr(fastpath, '_SEED_COPY_CHECKPOINT_BYTES', 4096)
    original_state_write = shipper_module.write_json_durable
    interrupted = False

    def interrupt_after_first_checkpoint(path, payload):
        nonlocal interrupted
        original_state_write(path, payload)
        if (Path(path) == front.shipper._rebase_state
                and int(payload.get('database_bytes') or 0) > 0
                and not interrupted):
            interrupted = True
            raise TimeoutError('simulated changed-source boundary')

    monkeypatch.setattr(
        shipper_module, 'write_json_durable',
        interrupt_after_first_checkpoint)
    with pytest.raises(TimeoutError, match='changed-source boundary'):
        front.shipper.pin_checkpointed_snapshot_for_backup(
            tmp_path / 'timed-out-changed.sqlite3',
            deadline_at=time.monotonic() + 10,
        )
    monkeypatch.setattr(
        shipper_module, 'write_json_durable', original_state_write)

    status = front.db.stat()
    os.utime(front.db, ns=(status.st_atime_ns, status.st_mtime_ns + 1))
    original_copy = fastpath._copy_file_checkpointed
    resume_offsets = []

    def record_restart(*args, **kwargs):
        if Path(args[0]) == front.db:
            resume_offsets.append(kwargs['durable_bytes'])
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(fastpath, '_copy_file_checkpointed', record_restart)
    destination = tmp_path / 'restarted.sqlite3'
    front.shipper.pin_checkpointed_snapshot_for_backup(
        destination,
        deadline_at=time.monotonic() + 10,
    )

    assert resume_offsets == [0]
    assert _open_items(destination)['changed-source'] == 31
    assert front.shipper.status()['snapshot_resume_count'] == 0


def test_backup_pin_wait_for_shipper_is_deadline_bounded(tmp_path):
    shipper = WalShipper(
        tmp_path / 'front.sqlite3',
        tmp_path / 'shadow',
        authority_uuid='test-authority',
        checkpoint_fn=lambda: None,
    )
    assert shipper._pass_lock.acquire(blocking=False)
    started_at = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match='waiting for shipper'):
            shipper.pin_checkpointed_snapshot_for_backup(
                tmp_path / 'never-created.sqlite3',
                deadline_at=time.monotonic() + 0.02,
            )
    finally:
        shipper._pass_lock.release()

    assert time.monotonic() - started_at < 0.5
    assert not (tmp_path / 'never-created.sqlite3').exists()


def test_backup_rebase_capacity_refuses_before_checkpoint(
        front, tmp_path, monkeypatch):
    front.shipper._tick_s = 60
    front.shipper.start()
    front.shipper._wake.set()
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None,
              label='initial snapshot before capacity refusal')
    front.writer._on_commit = None
    front.put('must-remain-in-old-generation', 37)
    snapshot, _shadow_wal = fastpath.shadow_paths(front.shadow_dir)
    previous_snapshot = snapshot.read_bytes()
    checkpoint_count = len(front.checkpoint_deadlines)

    monkeypatch.setattr(
        fastpath,
        '_require_copy_capacity',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError('needs more free bytes')),
    )

    with pytest.raises(StorageError, match='needs more free bytes') as raised:
        front.shipper.pin_checkpointed_snapshot_for_backup(
            tmp_path / 'capacity-refused.sqlite3',
            deadline_at=time.monotonic() + 10,
        )

    assert raised.value.code == 'database_unavailable'
    assert raised.value.retryable is False
    assert len(front.checkpoint_deadlines) == checkpoint_count
    assert snapshot.read_bytes() == previous_snapshot
    assert not front.shipper._rebase_state.exists()
    assert not front.shipper._rebase_snapshot.exists()


def test_backup_rebase_rechecks_capacity_after_wal_growth(
        front, tmp_path, monkeypatch):
    front.shipper._tick_s = 60
    front.shipper.start()
    front.shipper._wake.set()
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None,
              label='initial snapshot before late capacity refusal')
    front.writer._on_commit = None
    front.put('preserved-behind-late-capacity-gate', 41)
    snapshot, _shadow_wal = fastpath.shadow_paths(front.shadow_dir)
    previous_snapshot = snapshot.read_bytes()
    capacity_calls = []

    def refuse_publication(_directory, source_bytes, **kwargs):
        capacity_calls.append((source_bytes, kwargs['reusable_bytes']))
        if len(capacity_calls) == 3:
            raise RuntimeError('WAL growth exhausted shadow capacity')

    monkeypatch.setattr(
        fastpath, '_require_copy_capacity', refuse_publication)

    with pytest.raises(StorageError, match='WAL growth exhausted'):
        front.shipper.pin_checkpointed_snapshot_for_backup(
            tmp_path / 'late-capacity-refused.sqlite3',
            deadline_at=time.monotonic() + 10,
        )

    state = json.loads(
        front.shipper._rebase_state.read_text(encoding='utf-8'))
    assert len(capacity_calls) == 3
    assert capacity_calls[-1][0] >= front.db.stat().st_size
    assert 0 < capacity_calls[-1][1] <= front.db.stat().st_size
    assert state['database_bytes'] == front.db.stat().st_size
    assert snapshot.read_bytes() == previous_snapshot
    assert not front.shipper._rebase_wal.exists()


def test_backup_pin_cross_device_fallback_is_sequential_and_equivalent(
        front, tmp_path, monkeypatch):
    front.shipper.start()
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None,
              label='initial snapshot manifest')
    front.put('cross-device-backup', 23)
    monkeypatch.setattr(
        'lib.storage_sidecar.shipper.os.link',
        lambda _source, _destination: (_ for _ in ()).throw(
            OSError(errno.EXDEV, 'different filesystem')),
    )
    destination = tmp_path / 'copied-backup.sqlite3'

    result = front.shipper.pin_checkpointed_snapshot_for_backup(
        destination,
        deadline_at=time.monotonic() + 10,
    )

    snapshot, _shadow_wal = fastpath.shadow_paths(front.shadow_dir)
    assert result['copy_strategy'] == 'sequential-copy'
    assert destination.stat().st_size == snapshot.stat().st_size
    assert destination.stat().st_ino != snapshot.stat().st_ino
    assert _open_items(destination)['cross-device-backup'] == 23


def test_backup_pin_same_dir_link_reject_falls_back_to_rename(
        front, tmp_path, monkeypatch):
    front.shipper.start()
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None,
              label='initial snapshot manifest')
    front.put('cross-dir-link-backup', 42)
    real_link = os.link

    def beegfs_style_link(source, destination):
        if os.path.dirname(os.fspath(source)) != os.path.dirname(
                os.fspath(destination)):
            raise OSError(errno.EPERM, 'cross-directory link rejected')
        return real_link(source, destination)

    monkeypatch.setattr(
        'lib.storage_sidecar.shipper.os.link', beegfs_style_link)
    destination = tmp_path / 'renamed-backup.sqlite3'

    result = front.shipper.pin_checkpointed_snapshot_for_backup(
        destination,
        deadline_at=time.monotonic() + 10,
        require_hardlink=True,
    )

    snapshot, _shadow_wal = fastpath.shadow_paths(front.shadow_dir)
    assert result['copy_strategy'] == 'hardlink-rename'
    assert destination.stat().st_ino == snapshot.stat().st_ino
    assert _open_items(destination)['cross-dir-link-backup'] == 42


def test_budget_rotation_still_refuses_when_filesystem_has_no_links(
        front, tmp_path, monkeypatch):
    front.shipper.start()
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None,
              label='initial snapshot manifest')
    monkeypatch.setattr(
        'lib.storage_sidecar.shipper.os.link',
        lambda _source, _destination: (_ for _ in ()).throw(
            OSError(errno.EPERM, 'no hard links at all')),
    )
    destination = tmp_path / 'refused-nolink.sqlite3'

    with pytest.raises(StorageError, match='requires a same-filesystem'):
        front.shipper.pin_checkpointed_snapshot_for_backup(
            destination,
            deadline_at=time.monotonic() + 10,
            require_hardlink=True,
        )

    assert not destination.exists()

def test_budget_rotation_refuses_cross_device_full_copy_fallback(
        front, tmp_path, monkeypatch):
    front.shipper.start()
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None,
              label='initial snapshot manifest')
    monkeypatch.setattr(
        'lib.storage_sidecar.shipper.os.link',
        lambda _source, _destination: (_ for _ in ()).throw(
            OSError(errno.EXDEV, 'different filesystem')),
    )
    destination = tmp_path / 'refused-budget-copy.sqlite3'

    with pytest.raises(StorageError, match='requires a same-filesystem') as raised:
        front.shipper.pin_checkpointed_snapshot_for_backup(
            destination,
            deadline_at=time.monotonic() + 10,
            require_hardlink=True,
        )

    assert raised.value.retryable is False
    assert not destination.exists()


def test_first_shadow_hardlinks_unchanged_verified_classic_seed(tmp_path):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    classic = data_dir / 'tofu.db'
    connection = sqlite3.connect(classic, isolation_level=None)
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, '
        'meta_value TEXT NOT NULL)')
    connection.execute(
        "INSERT INTO storage_meta VALUES ('authority_uuid', 'seed-uuid')")
    connection.execute(
        'CREATE TABLE items(k TEXT PRIMARY KEY, v INTEGER NOT NULL)')
    connection.execute("INSERT INTO items VALUES ('seed', 1)")
    connection.close()

    local_db = tmp_path / 'front' / 'tofu.db'
    shadow_dir = data_dir / fastpath.SHADOW_DIRNAME
    fastpath._seed_from_classic(
        local_db, classic, retain_completion_state=True)
    fastpath._publish_seed_lineage(local_db, shadow_dir)
    assert fastpath.verified_classic_seed_provenance(
        local_db, classic, shadow_dir)

    checkpoint_calls = []
    shipper = WalShipper(
        local_db,
        shadow_dir,
        authority_uuid='seed-uuid',
        checkpoint_fn=lambda: checkpoint_calls.append(True),
        tick_s=10,
    )
    shipper.start()
    try:
        snapshot, _shadow_wal = fastpath.shadow_paths(shadow_dir)
        assert snapshot.stat().st_ino == classic.stat().st_ino
        assert snapshot.stat().st_dev == classic.stat().st_dev
        assert checkpoint_calls == []
        manifest = fastpath.read_shadow_manifest(shadow_dir)
        assert manifest['classic_seed_base_no_wal'] is True
        assert manifest['snapshot_bytes'] == classic.stat().st_size
        assert shipper.status()['snapshots'] == 1
        assert not fastpath.verified_classic_seed_provenance(
            local_db, classic, shadow_dir)
    finally:
        shipper.stop()


def test_snapshot_sequential_copy_captures_commits_in_concurrent_wal(
        front, monkeypatch):
    """A live write cannot restart the multi-GiB database-image copy.

    The checkpointed DB file stays immutable while the write lands in a new
    WAL generation; publication must pair the one sequential image copy with
    that frame prefix so local-loss recovery includes the concurrent commit.
    """
    original_copy = fastpath._copy_file_checkpointed
    image_copied = threading.Event()
    allow_publication = threading.Event()
    database_copy_calls = []

    def pause_after_database_copy(*args, **kwargs):
        result = original_copy(*args, **kwargs)
        if Path(args[0]) == front.db:
            database_copy_calls.append(kwargs['expected_bytes'])
            image_copied.set()
            assert allow_publication.wait(5)
        return result

    monkeypatch.setattr(
        fastpath, '_copy_file_checkpointed', pause_after_database_copy)
    front.shipper.start()
    assert image_copied.wait(5)

    # This commit must finish while the DB-image copy is paused; it belongs to
    # the new WAL, not to a restarted page-wise SQLite backup.
    front.put('during-snapshot', 91)
    allow_publication.set()
    _wait_for(
        lambda: (_shadow_manifest(front.shadow_dir) or {}).get(
            'wal_shipped_bytes', 0) > 0,
        label='snapshot plus concurrent WAL publication',
    )
    assert database_copy_calls == [front.db.stat().st_size]

    front.writer._on_commit = None
    front.shipper.stop()
    front.writer.close()
    front.connection.close()
    for suffix in ('', '-wal', '-shm'):
        front.db.with_name(front.db.name + suffix).unlink(missing_ok=True)

    decision = fastpath.FastpathDecision(
        True, 'activated', 'auto', local_dir=front.db.parent,
        shadow_dir=front.shadow_dir)
    fastpath.reconcile(
        decision, front.db.parent / 'classic-never-used.db')
    assert _open_items(front.db)['during-snapshot'] == 91


def test_shipper_stop_cancels_snapshot_without_restarting_it(
        front, monkeypatch):
    original_copy = fastpath._copy_file_checkpointed
    snapshot_copy_started = threading.Event()
    snapshot_copy_attempts = []

    def block_snapshot_copy(*args, **kwargs):
        if Path(args[0]) != front.db:
            return original_copy(*args, **kwargs)
        snapshot_copy_attempts.append(True)
        snapshot_copy_started.set()
        while True:
            kwargs['progress'](0)
            time.sleep(0.002)

    monkeypatch.setattr(
        fastpath, '_copy_file_checkpointed', block_snapshot_copy)
    front.shipper.start()
    assert snapshot_copy_started.wait(5)

    started_at = time.monotonic()
    front.shipper.stop(timeout_s=0.5)

    assert time.monotonic() - started_at < 1.0
    assert not front.shipper._thread.is_alive()
    assert snapshot_copy_attempts == [True]
    assert not list(front.shadow_dir.glob('snapshot.sqlite3.tmp-*'))
    assert front.shipper._rebase_state.is_file()
    assert front.shipper._rebase_state.read_text(encoding='utf-8')


def test_snapshot_cycle_rebases_on_wal_budget(front):
    forced_budget = 64 * 1024
    front.shipper._wal_budget_bytes = forced_budget  # force an early cycle
    front.shipper._wal_rebase_trigger_bytes = forced_budget
    front.shipper.metrics['wal_rebase_budget_bytes'] = forced_budget
    front.shipper.metrics['wal_rebase_trigger_bytes'] = forced_budget
    front.shipper.metrics['wal_write_pressure_bytes'] = forced_budget
    front.shipper.start()
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None,
              label='initial snapshot')
    blob = 'x' * 8192
    for i in range(40):
        retry_deadline = time.monotonic() + 5
        while True:
            try:
                front.put(f'big{i}', blob and i)  # modest but WAL-growing
                break
            except StorageError as exc:
                if (exc.code != 'database_busy'
                        or time.monotonic() >= retry_deadline):
                    raise
                # Rebase pressure is a typed transient refusal. The tiny test
                # image should publish quickly and release admission again.
                time.sleep(0.01)
    _wait_for(lambda: front.shipper.status()['generation'] >= 2,
              label='budget-triggered snapshot cycle')
    status = front.shipper.status()
    _, shadow_wal = fastpath.shadow_paths(front.shadow_dir)
    snapshot, _ = fastpath.shadow_paths(front.shadow_dir)
    assert snapshot.is_file() and snapshot.stat().st_size > 0
    # The re-base dropped history: the live shadow WAL carries only the
    # post-cycle tail, strictly less than everything ever shipped.
    shadow_wal_size = shadow_wal.stat().st_size if shadow_wal.is_file() else 0
    assert shadow_wal_size < status['bytes_shipped']


def _open_items(db_path: Path):
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.execute('PRAGMA busy_timeout=30000')
    return dict(connection.execute('SELECT k, v FROM items').fetchall())


def test_local_loss_recovers_from_shadow(front):
    front.shipper.start()
    for i in range(12):
        front.put(f'k{i}', i)
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None
              and _shadow_manifest(front.shadow_dir)[
                  'wal_shipped_bytes'] > 0, label='shipped state')
    # Force a snapshot cycle so the shadow holds a full image, then ship
    # the post-snapshot tail as well.
    front.shipper._ship_pass(final=True)
    time.sleep(0.2)
    front.shipper._ship_pass(final=True)

    # Simulate catastrophic local-disk loss: the front vanishes entirely.
    front.writer._on_commit = None
    front.writer.close()
    front.connection.close()
    for suffix in ('', '-wal', '-shm'):
        front.db.with_name(front.db.name + suffix).unlink(missing_ok=True)

    decision = fastpath.FastpathDecision(
        True, 'activated', 'auto', local_dir=front.db.parent,
        shadow_dir=front.shadow_dir)
    restored = fastpath.reconcile(decision, tmp_classic := front.db.parent /
                                  'classic-never-used.db')
    assert restored == front.db
    rows = _open_items(front.db)
    assert len(rows) == 12, f'shadow recovery lost rows: {sorted(rows)}'


def test_surviving_front_stays_authoritative(front):
    front.shipper.start()
    for i in range(5):
        front.put(f'k{i}', i)
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None,
              label='initial snapshot')
    # Crash WITHOUT a final ship: the front holds an unshipped tail.
    front.writer._on_commit = None
    front.shipper._stop = True
    front.shipper._thread.join(timeout=10)
    for i in range(5, 9):
        front.put(f'k{i}', i)

    decision = fastpath.FastpathDecision(
        True, 'activated', 'auto', local_dir=front.db.parent,
        shadow_dir=front.shadow_dir)
    chosen = fastpath.reconcile(decision, front.db.parent / 'classic.db')
    assert chosen == front.db  # the front's tail wins; no restore overwrite
    rows = _open_items(front.db)
    assert len(rows) == 9


def test_failed_rebase_preserves_previous_shadow_wal_generation(
    front, monkeypatch):
    front.shipper.start()
    _wait_for(
        lambda: _shadow_manifest(front.shadow_dir) is not None,
        label='initial snapshot before old WAL generation')
    front.put('published', 1)
    _wait_for(
        lambda: (_shadow_manifest(front.shadow_dir) or {}).get(
            'wal_shipped_bytes', 0) > 0,
        label='published old WAL generation')
    front.shipper.stop()
    _snapshot, shadow_wal = fastpath.shadow_paths(front.shadow_dir)
    previous_shadow_wal = shadow_wal.read_bytes()

    # Force a new local WAL generation after the old shadow is durable.
    front.shipper._checkpoint_fn()
    front.put('new-local-tail', 2)
    replacement = WalShipper(
        front.db,
        front.shadow_dir,
        authority_uuid='test-authority',
        checkpoint_fn=front.shipper._checkpoint_fn,
        tick_s=10,
    )
    replacement._resume_or_snapshot()
    assert replacement._needs_snapshot
    assert not replacement._shadow_wal_matches_local

    def fail_replacement_copy(*_args, **_kwargs):
        raise RuntimeError('simulated replacement snapshot failure')

    monkeypatch.setattr(
        fastpath, '_copy_file_checkpointed', fail_replacement_copy)
    with pytest.raises(RuntimeError, match='simulated replacement'):
        replacement._snapshot_cycle()

    assert shadow_wal.read_bytes() == previous_shadow_wal, (
        'a failed rebase must leave the last durable snapshot/WAL pair intact')


def test_split_brain_falls_back_to_classic(front):
    front.shipper.start()
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None,
              label='initial snapshot')
    # Forge a divergent lineage in the shadow.
    manifest = _shadow_manifest(front.shadow_dir)
    manifest['authority_uuid'] = 'somebody-else'
    fastpath.write_shadow_manifest(front.shadow_dir, manifest)
    # The local manifest must exist and disagree for the guard to fire.
    fastpath.write_local_manifest(
        front.db.parent, {'authority_uuid': 'test-authority',
                          'shadow_dir': str(front.shadow_dir)})

    classic = front.db.parent / 'classic.db'
    decision = fastpath.FastpathDecision(
        True, 'activated', 'auto', local_dir=front.db.parent,
        shadow_dir=front.shadow_dir)
    chosen = fastpath.reconcile(decision, classic)
    assert chosen == classic


def test_foreign_front_is_quarantined_not_served(tmp_path):
    """2026-08-20 incident regression: a front whose manifest names ANOTHER
    deployment's shadow dir must never be served — quarantine it and rebuild
    from this deployment's own classic authority."""
    local_dir = tmp_path / 'shared-front'
    local_dir.mkdir()
    local_db = local_dir / 'tofu.db'
    connection = sqlite3.connect(local_db, isolation_level=None)
    connection.execute(
        'CREATE TABLE items(k TEXT PRIMARY KEY, v INTEGER NOT NULL)')
    connection.execute("INSERT INTO items(k, v) VALUES ('foreign', 1)")
    connection.close()
    fastpath.write_local_manifest(local_dir, {
        'authority_uuid': 'foreign-lineage',
        'shadow_dir': str(tmp_path / 'other-deployment' / 'data' /
                          fastpath.SHADOW_DIRNAME)})

    # This deployment's own pre-fastpath authority.
    classic = tmp_path / 'data' / 'tofu.db'
    classic.parent.mkdir()
    connection = sqlite3.connect(classic, isolation_level=None)
    connection.execute(
        'CREATE TABLE items(k TEXT PRIMARY KEY, v INTEGER NOT NULL)')
    connection.execute("INSERT INTO items(k, v) VALUES ('mine', 1)")
    connection.close()

    decision = fastpath.FastpathDecision(
        True, 'activated', 'auto', local_dir=local_dir,
        shadow_dir=tmp_path / 'data' / fastpath.SHADOW_DIRNAME)
    chosen = fastpath.reconcile(decision, classic)
    assert chosen == local_db  # the front PATH is rebuilt in place…
    rows = _open_items(local_db)
    assert rows == {'mine': 1}, f'foreign rows served: {rows}'
    # …while the foreign bytes survive as quarantined evidence.
    assert list(local_dir.glob('tofu.db.foreign-*'))


def test_front_without_manifest_is_quarantined(tmp_path):
    """A front with no manifest at all has unverifiable lineage — same
    fail-closed treatment as a foreign one."""
    local_dir = tmp_path / 'front'
    local_dir.mkdir()
    local_db = local_dir / 'tofu.db'
    connection = sqlite3.connect(local_db, isolation_level=None)
    connection.execute(
        'CREATE TABLE items(k TEXT PRIMARY KEY, v INTEGER NOT NULL)')
    connection.execute("INSERT INTO items(k, v) VALUES ('mystery', 1)")
    connection.close()

    classic = tmp_path / 'data' / 'tofu.db'
    classic.parent.mkdir()
    connection = sqlite3.connect(classic, isolation_level=None)
    connection.execute(
        'CREATE TABLE items(k TEXT PRIMARY KEY, v INTEGER NOT NULL)')
    connection.execute("INSERT INTO items(k, v) VALUES ('mine', 1)")
    connection.close()

    decision = fastpath.FastpathDecision(
        True, 'activated', 'auto', local_dir=local_dir,
        shadow_dir=tmp_path / 'data' / fastpath.SHADOW_DIRNAME)
    fastpath.reconcile(decision, classic)
    assert _open_items(local_db) == {'mine': 1}
    assert list(local_dir.glob('tofu.db.foreign-*'))


def test_candidate_dirs_are_keyed_per_data_dir(tmp_path, monkeypatch):
    """Two deployments on one host must never share an auto front dir."""
    monkeypatch.setattr(fastpath.tempfile, 'gettempdir',
                        lambda: str(tmp_path / 'tmp'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    env = {'XDG_STATE_HOME': str(tmp_path / 'state'),
           'TOFU_STORAGE_FASTPATH_DIR': ''}
    data_a = tmp_path / 'deployment-a' / 'data'
    data_b = tmp_path / 'deployment-b' / 'data'
    candidates_a = fastpath._candidate_dirs(data_a, env)
    candidates_b = fastpath._candidate_dirs(data_b, env)
    assert candidates_a and candidates_b
    assert set(candidates_a).isdisjoint(set(candidates_b))


def test_torn_shadow_tail_is_overwritten_not_trusted(front):
    front.shipper.start()
    for i in range(6):
        front.put(f'k{i}', i)
    _wait_for(lambda: _shadow_manifest(front.shadow_dir) is not None
              and _shadow_manifest(front.shadow_dir)[
                  'wal_shipped_bytes'] > 0, label='shipped state')
    _, shadow_wal = fastpath.shadow_paths(front.shadow_dir)
    # Simulate a crashed copy: garbage appended beyond the durable prefix.
    with shadow_wal.open('ab') as stream:
        stream.write(os.urandom(512))
    front.put('k6', 6)
    front.shipper._ship_pass(final=True)
    # The ship pass must have re-based the torn region from the real WAL.
    front.shipper.stop()
    front.writer.close()
    front.connection.close()


# ------------------------------------------------------- seed-from-classic

def test_checkpointed_copy_releases_only_completed_clean_cache_ranges(
        tmp_path, monkeypatch):
    source = tmp_path / 'source'
    destination = tmp_path / 'destination'
    source.write_bytes(b'abcdefgh')
    advised = []
    monkeypatch.setattr(fastpath, '_SEED_COPY_CHECKPOINT_BYTES', 4)
    monkeypatch.setattr(fastpath.os, 'POSIX_FADV_DONTNEED', 4, raising=False)
    monkeypatch.setattr(
        fastpath.os,
        'posix_fadvise',
        lambda _fd, offset, length, _advice: advised.append(
            (offset, length)),
        raising=False,
    )

    fastpath._copy_file_checkpointed(
        source,
        destination,
        expected_bytes=8,
        durable_bytes=0,
        checkpoint=lambda _offset: None,
    )

    assert destination.read_bytes() == source.read_bytes()
    assert advised == [(0, 4), (0, 4), (4, 4), (4, 4)]

def _make_classic_with_wal_tail(tmp_path):
    """A classic authority holding a committed-but-uncheckpointed WAL tail.

    The sidecar runs with NO_CKPT_ON_CLOSE, so a clean shutdown can still
    leave committed frames that live ONLY in ``tofu.db-wal`` — exactly the
    bytes first-activation seeding must carry onto the front.
    """
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    classic = data_dir / 'tofu.db'
    connection = sqlite3.connect(classic, isolation_level=None)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA wal_autocheckpoint=0')
    connection.execute(
        'CREATE TABLE items(k TEXT PRIMARY KEY, v INTEGER NOT NULL)')
    connection.execute("INSERT INTO items(k, v) VALUES ('base', 1)")
    connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    # The tail: committed AFTER the checkpoint, living only in the WAL.
    connection.execute("INSERT INTO items(k, v) VALUES ('tail', 2)")
    wal = classic.with_name(classic.name + '-wal')
    assert wal.is_file() and wal.stat().st_size > 0
    # A second open reader keeps the WAL alive when the writer closes
    # (a last-connection close would otherwise checkpoint it away).
    reader = sqlite3.connect(classic, isolation_level=None)
    connection.close()
    return classic, reader


def test_seed_from_classic_carries_the_wal_tail(tmp_path):
    """Regression: the seeded front must include the classic authority's
    committed WAL tail.  The first implementation copied the WAL beside the
    TEMP name and never renamed it — an orphaned ``*.seed-tmp-wal`` SQLite
    never replays, so the tail silently vanished on first open."""
    classic, reader = _make_classic_with_wal_tail(tmp_path)
    try:
        local_db = tmp_path / 'front' / 'tofu.db'
        fastpath._seed_from_classic(local_db, classic)
    finally:
        reader.close()
    assert _open_items(local_db) == {'base': 1, 'tail': 2}, (
        'seed dropped the committed-but-uncheckpointed tail')
    # No orphaned temp-name WAL may survive beside the front.
    assert not list(local_db.parent.glob('tofu.db.seed-tmp*'))


def test_seed_refuses_before_copy_when_local_capacity_is_too_small(
        tmp_path, monkeypatch):
    classic = tmp_path / 'data' / 'tofu.db'
    classic.parent.mkdir()
    classic.write_bytes(b'classic-authority')
    local_db = tmp_path / 'front' / 'tofu.db'

    class Usage:
        free = 1

    monkeypatch.setattr(fastpath.shutil, 'disk_usage', lambda _path: Usage())

    with pytest.raises(RuntimeError, match='refusing to fill'):
        fastpath._seed_from_classic(local_db, classic)
    assert not local_db.exists()
    assert not local_db.with_name(local_db.name + '.seed-tmp').exists()


def test_seed_resumes_from_last_durable_checkpoint_after_abrupt_stop(
        tmp_path, monkeypatch):
    classic = tmp_path / 'data' / 'tofu.db'
    classic.parent.mkdir()
    classic.write_bytes(b'abcdefghijklmnop')
    local_db = tmp_path / 'front' / 'tofu.db'
    original_write_json_durable = fastpath.write_json_durable
    monkeypatch.setattr(fastpath, '_SEED_COPY_CHECKPOINT_BYTES', 4)

    def crash_after_first_progress(path, payload):
        original_write_json_durable(path, payload)
        if (path.name.endswith(fastpath._SEED_STATE_SUFFIX)
                and payload.get('database_bytes') == 4):
            raise KeyboardInterrupt('simulated SIGTERM/SIGKILL boundary')

    monkeypatch.setattr(
        fastpath, 'write_json_durable', crash_after_first_progress)
    with pytest.raises(KeyboardInterrupt, match='simulated'):
        fastpath._seed_from_classic(local_db, classic)

    temporary, _temporary_wal, state_path = fastpath._seed_paths(local_db)
    assert temporary.read_bytes() == b'abcd'
    assert fastpath._load_seed_state(state_path)['database_bytes'] == 4

    monkeypatch.setattr(
        fastpath, 'write_json_durable', original_write_json_durable)
    original_copy = fastpath._copy_file_checkpointed
    resume_offsets = []

    def record_resume(*args, **kwargs):
        resume_offsets.append(kwargs['durable_bytes'])
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(fastpath, '_copy_file_checkpointed', record_resume)
    fastpath._seed_from_classic(local_db, classic)

    assert resume_offsets[0] == 4
    assert local_db.read_bytes() == classic.read_bytes()
    assert not temporary.exists()
    assert not state_path.exists()


def test_reconcile_recovers_installed_seed_before_lineage_publication(tmp_path):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    classic = data_dir / 'tofu.db'
    classic.write_bytes(b'current-classic-authority')
    local_dir = tmp_path / 'front'
    local_db = local_dir / 'tofu.db'
    shadow_dir = data_dir / fastpath.SHADOW_DIRNAME

    fastpath._seed_from_classic(
        local_db, classic, retain_completion_state=True)
    assert fastpath.read_local_manifest(local_dir) is None
    assert fastpath._completed_seed_is_recoverable(local_db, classic)

    decision = fastpath.FastpathDecision(
        active=True,
        reason='measured-win',
        mode=fastpath.MODE_AUTO,
        local_dir=local_dir,
        shadow_dir=shadow_dir,
    )
    assert fastpath.reconcile(decision, classic) == local_db
    assert local_db.read_bytes() == classic.read_bytes()
    assert not list(local_dir.glob('tofu.db.foreign-*'))
    assert fastpath.read_local_manifest(local_dir)['shadow_dir'] == str(
        shadow_dir)
    assert not fastpath._seed_paths(local_db)[2].exists()


def test_auto_reconcile_keeps_current_classic_when_seed_capacity_disappears(
        tmp_path, monkeypatch):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    classic = data_dir / 'tofu.db'
    classic.write_bytes(b'current-classic-authority')
    local_dir = tmp_path / 'front'
    local_dir.mkdir()
    decision = fastpath.FastpathDecision(
        active=True,
        reason='measured-win',
        mode=fastpath.MODE_AUTO,
        local_dir=local_dir,
        shadow_dir=data_dir / fastpath.SHADOW_DIRNAME,
    )

    class Usage:
        free = 1

    monkeypatch.setattr(fastpath.shutil, 'disk_usage', lambda _path: Usage())

    assert fastpath.reconcile(decision, classic) == classic
    assert not (local_dir / 'tofu.db').exists()


def test_seed_unlinks_stale_wal_sidecars_beside_the_target(tmp_path):
    """Debris from a prior failed attempt (a WAL from a DIFFERENT salt/
    generation) must never be replayed onto the freshly seeded image."""
    classic, reader = _make_classic_with_wal_tail(tmp_path)
    try:
        local_db = tmp_path / 'front' / 'tofu.db'
        local_db.parent.mkdir()
        local_db.with_name(local_db.name + '-wal').write_bytes(os.urandom(64))
        local_db.with_name(local_db.name + '-shm').write_bytes(b'junk')
        fastpath._seed_from_classic(local_db, classic)
        # The stale shm is gone; a WAL beside the final name is the SEEDED
        # tail (or absent), never the 64-byte garbage.
        assert not local_db.with_name(local_db.name + '-shm').exists()
        final_wal = local_db.with_name(local_db.name + '-wal')
        assert not (final_wal.is_file()
                    and final_wal.stat().st_size == 64)
    finally:
        reader.close()
    assert _open_items(local_db) == {'base': 1, 'tail': 2}


def test_restore_unlinks_stale_wal_sidecars_beside_the_target(tmp_path):
    """Same stale-lineage guard on the restore path (shadow → front)."""
    shadow_dir = tmp_path / 'shadow'
    shadow_dir.mkdir()
    source = tmp_path / 'source.db'
    connection = sqlite3.connect(source, isolation_level=None)
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, '
        'meta_value TEXT NOT NULL)')
    connection.execute(
        "INSERT INTO storage_meta VALUES ('authority_uuid', 'uuid-1')")
    connection.execute(
        'CREATE TABLE items(k TEXT PRIMARY KEY, v INTEGER NOT NULL)')
    connection.execute("INSERT INTO items VALUES ('k', 7)")
    connection.commit()
    snapshot = shadow_dir / fastpath.SNAPSHOT_NAME
    destination = sqlite3.connect(snapshot, isolation_level=None)
    connection.backup(destination)
    destination.close()
    connection.close()

    local_db = tmp_path / 'front' / 'tofu.db'
    local_db.parent.mkdir()
    local_db.with_name(local_db.name + '-wal').write_bytes(os.urandom(128))
    local_db.with_name(local_db.name + '-shm').write_bytes(b'junk')
    fastpath._restore_from_shadow(
        local_db, snapshot, shadow_dir / fastpath.SHADOW_WAL_NAME,
        {'authority_uuid': 'uuid-1', 'generation': 3})
    assert not local_db.with_name(local_db.name + '-shm').exists()
    assert _open_items(local_db) == {'k': 7}


def test_corrupt_manifest_is_loud_not_silent(tmp_path, caplog):
    """A manifest that exists but does not parse disables the lineage guards
    keyed on it — that must be a WARNING, not a silent None."""
    (tmp_path / fastpath.LOCAL_MANIFEST_NAME).write_text(
        '{not json', encoding='utf-8')
    with caplog.at_level('WARNING', logger='tofu.storage.sidecar.fastpath'):
        assert fastpath.read_local_manifest(tmp_path) is None
    assert any('unreadable' in record.message for record in caplog.records)


# --------------------------------------------------- supervisor end-to-end

@pytest.fixture()
def supervisor_env(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '2')
    # Relocation is opt-in (default off since the 2026-08-20 incident).
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'auto')
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH_DIR', str(tmp_path / 'front'))
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH_MIN_SPEEDUP', '0')
    return tmp_path


def test_supervisor_fastpath_round_trip_and_local_loss(supervisor_env):
    from lib.storage import StorageSupervisor

    project = supervisor_env
    supervisor = StorageSupervisor(
        project_root=project, backend='sqlite', startup_timeout=60)
    supervisor.start()
    try:
        metrics = supervisor.client.metrics()
        assert metrics['fastpath']['active'], metrics['fastpath']
        shadow = project / 'data' / fastpath.SHADOW_DIRNAME
        _wait_for(lambda: fastpath.read_shadow_manifest(shadow) is not None,
                  timeout=60, label='initial shadow snapshot')
        # Write AFTER the initial snapshot so the commit lands in the
        # incremental WAL stream (a write absorbed by the snapshot cycle
        # itself never appears in the shadow WAL — it IS the snapshot).
        supervisor.client.command('record.put', {
            'namespace': 'fastpath-e2e', 'key': 'k1',
            'value': {'proof': 1}}, 'fp-e2e-1')
        _wait_for(
            lambda: (fastpath.read_shadow_manifest(shadow) or {}).get(
                'wal_shipped_bytes', 0) > 0,
            timeout=60, label='shadow WAL shipping')
    finally:
        supervisor.stop()  # graceful: ships the tail on the way down

    # Catastrophic local-disk loss between runs.
    front_dir = project / 'front'
    for victim in front_dir.glob('tofu.db*'):
        victim.unlink()

    supervisor2 = StorageSupervisor(
        project_root=project, backend='sqlite', startup_timeout=60)
    supervisor2.start()
    try:
        row = supervisor2.client.query('record.get', {
            'namespace': 'fastpath-e2e', 'key': 'k1'})
        assert row is not None and row['value'] == {'proof': 1}, (
            'shadow recovery lost the committed record')
    finally:
        supervisor2.stop()


def test_supervisor_fastpath_backup_pins_one_checkpointed_shadow_generation(
        supervisor_env):
    from lib.storage import StorageSupervisor

    project = supervisor_env
    supervisor = StorageSupervisor(
        project_root=project, backend='sqlite', startup_timeout=60)
    supervisor.start()
    try:
        supervisor.client.command('record.put', {
            'namespace': 'fastpath-backup', 'key': 'included',
            'value': {'proof': 2}}, 'fp-backup-1')
        shadow_dir = project / 'data' / fastpath.SHADOW_DIRNAME
        _wait_for(
            lambda: fastpath.read_shadow_manifest(shadow_dir) is not None,
            timeout=60,
            label='initial shadow snapshot',
        )

        result = supervisor.client.maintenance('system.backup', deadline=30)

        backup = project / result['backup']
        manifest = json.loads(
            (project / result['manifest']).read_text(encoding='utf-8'))
        snapshot, _shadow_wal = fastpath.shadow_paths(shadow_dir)
        assert result['source_mode'] == 'fastpath-checkpointed-shadow'
        assert result['copy_strategy'] == 'hardlink'
        assert result['recovery_point_at'] == manifest['recovery_point_at']
        assert result['recovery_point_at'].endswith('+00:00')
        assert result['snapshot_generation'] >= 2
        assert manifest['snapshot_generation'] == result['snapshot_generation']
        assert manifest['sha256'] == result['sha256']
        assert (backup.stat().st_dev, backup.stat().st_ino) == (
            snapshot.stat().st_dev, snapshot.stat().st_ino)
    finally:
        supervisor.stop()
