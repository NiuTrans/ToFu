"""A managed daemon restarts the artifact admitted by its application image."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

import lib.storage.supervisor as supervisor_mod
from lib.storage.supervisor import StorageSupervisor
from lib.storage.runtime import StorageRuntime
from serverctl_pkg import native_engine


pytestmark = pytest.mark.unit


class _SpawnReached(RuntimeError):
    """Stop at the process boundary without opening an authority."""


@pytest.fixture(autouse=True)
def _private_admitted_engines(tmp_path, monkeypatch):
    import lib.storage.admitted_engine as admitted_engine
    root = tmp_path / 'admitted-engines'
    monkeypatch.setattr(admitted_engine, 'private_root', lambda: (
        admitted_engine.ensure_private_directory(root), root)[1])
    return root


def _launched_bytes(command) -> bytes:
    """Bytes of the executable a native launch command runs."""
    return Path(command[3]).read_bytes()


@pytest.fixture
def launch_admission(tmp_path, monkeypatch):
    engine = tmp_path / 'engine' / 'tofu-db'
    engine.parent.mkdir()
    engine.write_bytes(b'initial-engine')
    engine.chmod(0o755)
    pin = Path(str(engine) + '.sha256')
    pin.write_text(hashlib.sha256(engine.read_bytes()).hexdigest())
    source_root = tmp_path / 'source'
    (source_root / 'packages' / 'tofu-db').mkdir(parents=True)
    (source_root / 'packages' / 'tofu-db' / 'build.rs').touch()
    source = {'sha256': 'a' * 64}
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.setenv('TOFUDB_TRANSPORT', 'daemon')
    monkeypatch.delenv('TOFUDB_EXPECTED_SHA256', raising=False)
    monkeypatch.delenv('TOFU_STORAGE_CONNECTION_FILE', raising=False)
    monkeypatch.delenv('TOFU_DB_FASTPATH_DIR', raising=False)
    monkeypatch.delenv('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS', raising=False)
    monkeypatch.setattr(supervisor_mod, '_tofudb_checkout_root', lambda: source_root)
    monkeypatch.setattr(native_engine, 'source_fingerprint', lambda _root: source['sha256'])
    monkeypatch.setattr(native_engine, 'build_identity', lambda _binary: {'source_sha256': 'a' * 64})
    spawns = []

    def stop_before_spawn(command, **_kwargs):
        spawns.append(command)
        raise _SpawnReached('admission reached spawn')

    monkeypatch.setattr(supervisor_mod.subprocess, 'Popen', stop_before_spawn)
    return engine, pin, source, spawns


@pytest.mark.parametrize('transport', ['daemon', 'auto'])
@pytest.mark.parametrize('pinned', [False, True])
def test_daemon_recovery_keeps_admitted_artifact_after_source_edits(
    launch_admission, tmp_path, monkeypatch, transport, pinned,
):
    engine, pin, source, spawns = launch_admission
    if not pinned:
        pin.unlink()
    monkeypatch.setenv('TOFUDB_TRANSPORT', transport)
    # Auto falls back to daemon when an embedded library is absent.
    monkeypatch.setattr('lib.storage.volume_class.volume_class', lambda _path: 'local')
    supervisor = StorageSupervisor(backend='tofudb', project_root=tmp_path / 'project')
    with pytest.raises(_SpawnReached):
        supervisor.start()

    source['sha256'] = 'b' * 64
    with pytest.raises(_SpawnReached):
        supervisor.start()
    assert len(spawns) == 2
    assert [_launched_bytes(command) for command in spawns] == [b'initial-engine'] * 2
    assert all(str(engine) not in command for command in spawns)

    # A new application image still admits against its current source.
    fresh = StorageSupervisor(backend='tofudb', project_root=tmp_path / 'new-project')
    with pytest.raises(native_engine.EnginePreparationError, match='does not match'):
        fresh.start()
    assert len(spawns) == 2


@pytest.mark.parametrize('deployment', ['rename', 'in_place'])
@pytest.mark.parametrize('repin', [False, True])
def test_daemon_recovery_relaunches_the_admitted_bytes_after_a_deployment(
    launch_admission, tmp_path, deployment, repin,
):
    """2026-10-27: a staged deployment must never strand a restart.

    The admitted engine keeps serving until an application restart admits
    the replacement; until then every relaunch runs the admitted bytes.
    """
    engine, pin, _source, spawns = launch_admission
    supervisor = StorageSupervisor(backend='tofudb', project_root=tmp_path / 'project')
    with pytest.raises(_SpawnReached):
        supervisor.start()

    if deployment == 'rename':
        staged = engine.with_name('tofu-db.staged')
        staged.write_bytes(b'replacement-engine')
        staged.chmod(0o755)
        staged.replace(engine)
    else:
        engine.write_bytes(b'replacement-engine')
    if repin:
        pin.write_text(hashlib.sha256(engine.read_bytes()).hexdigest())
    with pytest.raises(_SpawnReached):
        supervisor.start()
    assert [_launched_bytes(command) for command in spawns] == [b'initial-engine'] * 2

    # The next application admission judges the deployed artifact.
    fresh = StorageSupervisor(backend='tofudb', project_root=tmp_path / 'new-project')
    if repin:
        with pytest.raises(_SpawnReached):
            fresh.start()
        assert _launched_bytes(spawns[-1]) == b'replacement-engine'
    else:
        with pytest.raises(RuntimeError, match='SHA-256 mismatch'):
            fresh.start()
        assert len(spawns) == 2


def test_a_planned_fastpath_exit_runs_the_admitted_engine(
    launch_admission, tmp_path, monkeypatch,
):
    """The pre-launch fastpath export runs the bytes the launch will serve."""
    from lib.storage_authority_sql import native_fastpath_auto

    engine, _pin, _source, spawns = launch_admission
    exits = []
    monkeypatch.setattr(
        native_fastpath_auto, 'complete_planned_exit',
        lambda _data_dir, binary, _environ, **_kwargs: exits.append(
            Path(binary).read_bytes()))
    supervisor = StorageSupervisor(backend='tofudb', project_root=tmp_path / 'project')
    with pytest.raises(_SpawnReached):
        supervisor.start()

    staged = engine.with_name('tofu-db.staged')
    staged.write_bytes(b'replacement-engine')
    staged.chmod(0o755)
    staged.replace(engine)
    with pytest.raises(_SpawnReached):
        supervisor.start()
    assert exits == [b'initial-engine'] * 2
    assert [_launched_bytes(command) for command in spawns] == [b'initial-engine'] * 2


def test_daemon_recovery_does_not_reresolve_a_changed_binary_override(
    launch_admission, tmp_path, monkeypatch,
):
    engine, _pin, _source, spawns = launch_admission
    supervisor = StorageSupervisor(backend='tofudb', project_root=tmp_path / 'project')
    with pytest.raises(_SpawnReached):
        supervisor.start()

    monkeypatch.setenv('TOFUDB_BIN', str(tmp_path / 'different-engine'))
    with pytest.raises(_SpawnReached):
        supervisor.start()
    assert len(spawns) == 2
    assert _launched_bytes(spawns[-1]) == b'initial-engine'


def test_a_pin_changed_after_admission_binds_the_next_admission(launch_admission, tmp_path):
    """The pin judged the admitted bytes once; recovery keeps those bytes."""
    _engine, pin, _source, spawns = launch_admission
    supervisor = StorageSupervisor(backend='tofudb', project_root=tmp_path / 'project')
    with pytest.raises(_SpawnReached):
        supervisor.start()

    pin.write_text('0' * 64)
    with pytest.raises(_SpawnReached):
        supervisor.start()
    assert [_launched_bytes(command) for command in spawns] == [b'initial-engine'] * 2
    fresh = StorageSupervisor(backend='tofudb', project_root=tmp_path / 'new-project')
    with pytest.raises(RuntimeError, match='SHA-256 mismatch'):
        fresh.start()
    assert len(spawns) == 2


def test_the_first_admission_still_enforces_the_declared_pin(launch_admission, tmp_path):
    _engine, pin, _source, spawns = launch_admission
    pin.write_text('0' * 64)
    supervisor = StorageSupervisor(backend='tofudb', project_root=tmp_path / 'project')
    with pytest.raises(RuntimeError, match='SHA-256 mismatch'):
        supervisor.start()
    assert spawns == []


def test_native_crash_recovery_preserves_durable_rows_after_source_edits(
    tmp_path, monkeypatch,
):
    source_root = tmp_path / 'source'
    monkeypatch.setattr(supervisor_mod, '_tofudb_checkout_root', lambda: source_root)
    monkeypatch.setenv('TOFUDB_TRANSPORT', 'daemon')
    monkeypatch.delenv('TOFU_STORAGE_CONNECTION_FILE', raising=False)
    monkeypatch.delenv('TOFU_DB_FASTPATH_DIR', raising=False)
    monkeypatch.delenv('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS', raising=False)
    try:
        binary = supervisor_mod._resolve_tofudb_binary()
    except RuntimeError as exc:
        if os.environ.get('TOFUDB_BIN') or os.environ.get('TOFUDB_EXPECTED_SHA256'):
            pytest.fail(str(exc))
        pytest.skip(str(exc))

    owned_engine = tmp_path / 'tofu-db'
    shutil.copyfile(binary, owned_engine)
    owned_engine.chmod(0o755)
    binary = str(owned_engine)
    monkeypatch.setenv('TOFUDB_BIN', binary)
    source = {'sha256': native_engine.build_identity(Path(binary))['source_sha256']}
    (source_root / 'packages' / 'tofu-db').mkdir(parents=True)
    (source_root / 'packages' / 'tofu-db' / 'build.rs').touch()
    monkeypatch.setattr(native_engine, 'source_fingerprint', lambda _root: source['sha256'])
    supervisor = StorageSupervisor(
        backend='tofudb', project_root=tmp_path / 'project', startup_timeout=30.0,
    )
    runtime = StorageRuntime(supervisor)
    try:
        client = runtime.start()
        client.command('conversation.create', {
            'conv_id': 'restart-survivor', 'user_id': 1, 'title': 'Durable survivor',
        }, 'create-restart-survivor')
        process = supervisor._process
        assert process is not None
        source['sha256'] = 'b' * 64
        process.kill()
        with runtime._ready_condition:
            recovered = runtime._ready_condition.wait_for(
                lambda: runtime.ready
                and supervisor._process is not None
                and supervisor._process.pid != process.pid,
                timeout=30.0,
            )
        assert recovered, runtime.status()
        assert runtime.client().health()['ready'] is True
        rows = runtime.client().query('conversation.list', {'limit': 10}, deadline=10.0)
        assert 'restart-survivor' in json.dumps(rows)
        assert runtime.status()['degraded'] is False
    finally:
        runtime.stop(timeout=20.0)


def test_native_restart_after_a_staged_deployment_runs_the_admitted_engine(
    tmp_path, monkeypatch, _private_admitted_engines,
):
    """Fault injection with real engines: deploy by rename, then crash.

    Reproduces the 2026-10-27 sequence on disposable data: the configured
    engine is replaced and re-pinned while the admitted engine serves, then
    the engine dies. Recovery must run the admitted bytes, not refuse.
    """
    source_root = tmp_path / 'source'
    monkeypatch.setattr(supervisor_mod, '_tofudb_checkout_root', lambda: source_root)
    monkeypatch.setenv('TOFUDB_TRANSPORT', 'daemon')
    monkeypatch.delenv('TOFU_STORAGE_CONNECTION_FILE', raising=False)
    monkeypatch.delenv('TOFU_DB_FASTPATH_DIR', raising=False)
    monkeypatch.delenv('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS', raising=False)
    try:
        binary = supervisor_mod._resolve_tofudb_binary()
    except RuntimeError as exc:
        if os.environ.get('TOFUDB_BIN') or os.environ.get('TOFUDB_EXPECTED_SHA256'):
            pytest.fail(str(exc))
        pytest.skip(str(exc))

    engine = tmp_path / 'certified' / 'tofu-db'
    engine.parent.mkdir()
    shutil.copyfile(binary, engine)
    engine.chmod(0o755)
    admitted_sha256 = hashlib.sha256(engine.read_bytes()).hexdigest()
    pin = Path(str(engine) + '.sha256')
    pin.write_text(f'{admitted_sha256}  tofu-db\n')
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', admitted_sha256)
    source = {'sha256': native_engine.build_identity(engine)['source_sha256']}
    (source_root / 'packages' / 'tofu-db').mkdir(parents=True)
    (source_root / 'packages' / 'tofu-db' / 'build.rs').touch()
    monkeypatch.setattr(native_engine, 'source_fingerprint', lambda _root: source['sha256'])
    supervisor = StorageSupervisor(
        backend='tofudb', project_root=tmp_path / 'project', startup_timeout=30.0,
    )
    runtime = StorageRuntime(supervisor)
    try:
        client = runtime.start()
        client.command('conversation.create', {
            'conv_id': 'deployment-survivor', 'user_id': 1, 'title': 'Survives deployment',
        }, 'create-deployment-survivor')
        process = supervisor._process
        assert process is not None

        # A staged deployment: a different runnable engine renamed over the
        # configured path and re-pinned, exactly how native_engine publishes.
        staged = engine.with_name('tofu-db.staged')
        shutil.copyfile(engine, staged)
        with staged.open('ab') as handle:
            handle.write(b'staged deployment\n')
        staged.chmod(0o755)
        deployed_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
        staged.replace(engine)
        pin.write_text(f'{deployed_sha256}  tofu-db\n')

        process.kill()
        with runtime._ready_condition:
            recovered = runtime._ready_condition.wait_for(
                lambda: runtime.ready
                and supervisor._process is not None
                and supervisor._process.pid != process.pid,
                timeout=60.0,
            )
        assert recovered, runtime.status()
        relaunched = Path(f'/proc/{supervisor._process.pid}/exe')
        assert hashlib.sha256(relaunched.read_bytes()).hexdigest() == admitted_sha256
        assert runtime.status()['degraded'] is False
        assert runtime.status()['refusal'] is None
        rows = runtime.client().query('conversation.list', {'limit': 10}, deadline=10.0)
        assert 'deployment-survivor' in json.dumps(rows)
    finally:
        runtime.stop(timeout=20.0)
