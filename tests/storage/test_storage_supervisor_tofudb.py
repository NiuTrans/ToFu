"""Native tofu-db Supervisor launch: spawn, readiness, health, ops, fencing.

These tests drive a real ``tofu-db serve`` child through
:class:`lib.storage.supervisor.StorageSupervisor` in ``tofudb`` mode. They
require a built tofu-db binary; when none is available the module skips.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess

import pytest

from lib.storage.errors import StorageError
from lib.storage.supervisor import (
    StorageSupervisor,
    _bundled_tofudb_candidates,
    _resolve_tofudb_binary,
    _tofudb_binary_name,
    _tofudb_scope_ids,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _daemon_transport(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """These tests exercise the supervised-daemon launch contract (child
    spawn, readiness line, crash fences). Since TOFUDB_TRANSPORT defaults
    to ``auto`` — which resolves embedded on the local volumes tmp_path
    lives on — pin the daemon transport explicitly; the embedded lifecycle
    has its own suites (test_storage_v2_embedded_client.py)."""
    monkeypatch.setenv('TOFUDB_TRANSPORT', 'daemon')
    # Resolver fixtures own tiny executable stand-ins, not this checkout's
    # engine sources. Source-identity enforcement has its own real protocol
    # coverage in test_native_engine_update and the compiled identity check.
    monkeypatch.setattr('lib.storage.supervisor._tofudb_checkout_root', lambda: tmp_path / 'source')


def _tofudb_binary() -> str:
    try:
        return _resolve_tofudb_binary()
    except RuntimeError as exc:
        if (os.environ.get('TOFUDB_BIN', '').strip()
                or os.environ.get('TOFUDB_EXPECTED_SHA256', '').strip()):
            pytest.fail(str(exc))
        pytest.skip(str(exc))


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / 'proj'
    (root / 'data').mkdir(parents=True)
    return root


def test_scope_ids_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('TOFUDB_OWNER_ID', raising=False)
    monkeypatch.delenv('TOFUDB_TENANT_ID', raising=False)
    assert _tofudb_scope_ids() == (1, 1)


def test_scope_ids_disable_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('TOFUDB_TENANT_ID', '0')
    assert _tofudb_scope_ids() == (1, None)


def test_resolve_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('TOFUDB_BIN', '/nonexistent/tofu-db')
    with pytest.raises(RuntimeError, match='TOFUDB_BIN'):
        _resolve_tofudb_binary()


def test_binary_name_is_platform_aware() -> None:
    """Windows bundles tofu-db.exe; every other platform bundles tofu-db."""
    import sys
    expected = 'tofu-db.exe' if sys.platform.startswith('win') else 'tofu-db'
    assert _tofudb_binary_name() == expected


def test_override_is_authoritative(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A valid TOFUDB_BIN wins outright; a bad one fails without falling back.

    The override exists so tests and operators can pin an exact engine. It
    must never silently degrade to a bundled/PATH binary, or a pinned-engine
    test could pass against the wrong binary.
    """
    fake = tmp_path / _tofudb_binary_name()
    fake.write_text('#!/bin/sh\nexit 0\n')
    fake.chmod(0o755)
    monkeypatch.setenv('TOFUDB_BIN', str(fake))
    assert _resolve_tofudb_binary() == str(fake.resolve())
    # A non-executable override is rejected, not skipped.
    not_exec = tmp_path / ('not-exec-' + _tofudb_binary_name())
    not_exec.write_text('x')
    not_exec.chmod(0o644)
    monkeypatch.setenv('TOFUDB_BIN', str(not_exec))
    with pytest.raises(RuntimeError, match='TOFUDB_BIN'):
        _resolve_tofudb_binary()


def test_bundled_binary_precedes_source_build(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A frozen bundle's engine is found before the checkout's release build.

    This is the packaged-install contract: the engine ships inside the app, so
    resolution must prefer the bundle (sys._MEIPASS) over a source tree that
    may not even exist on the user's machine.
    """
    bundle_dir = tmp_path / '_internal'
    bundle_dir.mkdir()
    engine = bundle_dir / _tofudb_binary_name()
    engine.write_text('#!/bin/sh\nexit 0\n')
    engine.chmod(0o755)
    monkeypatch.delenv('TOFUDB_BIN', raising=False)
    monkeypatch.setattr('sys._MEIPASS', str(bundle_dir), raising=False)
    try:
        candidates = _bundled_tofudb_candidates()
        assert candidates and candidates[0] == engine
        assert _resolve_tofudb_binary() == str(engine.resolve())
    finally:
        monkeypatch.delattr('sys._MEIPASS', raising=False)


def test_source_run_has_no_bundled_candidates() -> None:
    """A plain source checkout offers no bundled engine (dev/PATH fallback)."""
    import sys
    if getattr(sys, 'frozen', False) or getattr(sys, '_MEIPASS', None):
        pytest.skip('running under a frozen bundle')
    assert _bundled_tofudb_candidates() == []


@pytest.mark.parametrize('content_pinned', [False, True])
def test_native_launch_health_query_and_fence(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    content_pinned: bool,
) -> None:
    binary = _tofudb_binary()
    if content_pinned:
        import hashlib
        with open(binary, "rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        monkeypatch.setenv("TOFUDB_EXPECTED_SHA256", digest)
    authority_dir = project_root / 'data' / 'tofudb-authority'
    projection_dir = project_root / 'data' / 'projections'
    subprocess.run(
        [binary, 'init', '--data-dir', str(authority_dir)],
        check=True, capture_output=True, text=True, timeout=120,
    )
    projection_dir.mkdir(parents=True, exist_ok=True)

    supervisor = StorageSupervisor(
        project_root=str(project_root), backend='tofudb', startup_timeout=60.0)
    try:
        client = supervisor.start()
        assert supervisor.ready
        status = supervisor.status()
        assert status['ready'] is True
        assert status['backend'] == 'tofudb'
        assert isinstance(status['port'], int)

        health = client.health()
        assert health['ready'] is True
        assert health['backend'] == 'tofudb'
        assert health['protocol'] == 'storage.v2'
        assert health['resourceBudget']['memoryCapacityBytes'] > 0

        # Both backends must share the project lease, including after the
        # Python path resolver replaces itself with the native executable.
        from lib.storage_authority_sql.offline import (
            SQLiteAuthorityDiscoveryError, resolve_readonly_sqlite_authority,
        )
        with pytest.raises(SQLiteAuthorityDiscoveryError, match='tofudb'):
            resolve_readonly_sqlite_authority(project_root / 'data')
        competing = StorageSupervisor(
            project_root=str(project_root), backend='sqlite', startup_timeout=10.0)
        try:
            with pytest.raises(RuntimeError, match='project lease'):
                competing.start()
        finally:
            competing.stop()

        # A real bounded owner-scoped read against the authority.
        result = client.query('conversation.list', {'limit': 1}, deadline=10.0)
        assert result is not None

        metrics = client.metrics()
        assert metrics['backend'] == 'tofudb'
    finally:
        supervisor.stop(timeout=20.0)
    assert not supervisor.ready


def test_unexpected_exit_fires_crash_fence(project_root: Path) -> None:
    binary = _tofudb_binary()
    authority_dir = project_root / 'data' / 'tofudb-authority'
    subprocess.run(
        [binary, 'init', '--data-dir', str(authority_dir)],
        check=True, capture_output=True, text=True, timeout=120,
    )
    (project_root / 'data' / 'projections').mkdir(parents=True, exist_ok=True)

    crashed: list[int] = []
    supervisor = StorageSupervisor(
        project_root=str(project_root), backend='tofudb', startup_timeout=60.0,
        on_crash=crashed.append)
    supervisor.start()
    assert supervisor.ready
    process = supervisor._process
    assert process is not None
    process.kill()
    assert supervisor.wait_until_unready(timeout=20.0)
    import time as _time
    deadline = _time.monotonic() + 10.0
    while not crashed and _time.monotonic() < deadline:
        _time.sleep(0.05)
    assert crashed, 'crash fence did not fire after abrupt kill'


def test_intentional_stop_does_not_fire_crash_fence(
    project_root: Path,
) -> None:
    binary = _tofudb_binary()
    authority_dir = project_root / 'data' / 'tofudb-authority'
    subprocess.run(
        [binary, 'init', '--data-dir', str(authority_dir)],
        check=True, capture_output=True, text=True, timeout=120,
    )
    (project_root / 'data' / 'projections').mkdir(parents=True, exist_ok=True)

    crashed: list[int] = []
    supervisor = StorageSupervisor(
        project_root=str(project_root), backend='tofudb', startup_timeout=60.0,
        on_crash=crashed.append)
    supervisor.start()
    assert supervisor.ready
    process = supervisor._process
    supervisor.stop(timeout=20.0)
    assert not supervisor.ready
    assert crashed == []
    assert process.returncode == 0


def test_fresh_install_bootstraps_native_authority(project_root):
    """A true fresh install (no authority dir at all) auto-initializes once.

    The authority directory is entirely absent, so no prior data exists to
    destroy; startup initializes an empty authority and serves it. It must
    never fall back to SQLite.
    """
    _tofudb_binary()
    supervisor = StorageSupervisor(
        project_root=str(project_root), backend='tofudb', startup_timeout=60.0)
    try:
        client = supervisor.start()
        assert supervisor.ready
        status = supervisor.status()
        assert status['ready'] is True
        assert status['backend'] == 'tofudb'
        # The fresh authority now exists on disk, and no SQLite shadow appeared.
        assert (project_root / 'data' / 'tofudb-authority').is_dir()
        assert not (project_root / 'data' / 'tofu.db').exists()
        health = client.health()
        assert health['ready'] is True
        assert health['backend'] == 'tofudb'
    finally:
        supervisor.stop(timeout=20.0)
    assert not supervisor.ready


def test_partial_native_authority_never_recovers_or_falls_back(project_root):
    """A present-but-partial authority dir is evidence, not a fresh install.

    Startup must fail closed (and never initialize over it or fall back to
    SQLite) so a corrupt/interrupted migration is never silently discarded.
    """
    binary = _tofudb_binary()
    authority_dir = project_root / 'data' / 'tofudb-authority'
    authority_dir.mkdir(parents=True)
    (authority_dir / 'orphan.wal').write_bytes(b'partial')
    supervisor = StorageSupervisor(
        project_root=str(project_root), backend='tofudb', startup_timeout=10.0)
    try:
        with pytest.raises(StorageError) as failure:
            supervisor.start()
        assert failure.value.code == 'database_unavailable'
        assert not failure.value.retryable
        assert not supervisor.ready
        # The partial authority is untouched and no SQLite fallback appeared.
        assert (authority_dir / 'orphan.wal').read_bytes() == b'partial'
        assert not (project_root / 'data' / 'tofu.db').exists()
    finally:
        supervisor.stop()


def test_nonempty_native_authority_init_fails_closed(project_root):
    """``tofu-db init`` refuses a non-empty directory (crash-safe bootstrap).

    If a bootstrap is interrupted between directory creation and completion,
    the next start must fail closed rather than reinitialize over the remnant.
    """
    binary = _tofudb_binary()
    authority_dir = project_root / 'data' / 'tofudb-authority'
    authority_dir.mkdir(parents=True)
    (authority_dir / 'remnant').write_bytes(b'x')
    result = subprocess.run(
        [binary, 'init', '--data-dir', str(authority_dir)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0


@pytest.mark.parametrize('expected', ['not-a-digest', 'a' * 63, 'g' * 64])
def test_binary_content_pin_rejects_invalid_configuration(tmp_path, monkeypatch, expected):
    engine = tmp_path / 'tofu-db'
    engine.write_bytes(b'candidate-engine')
    engine.chmod(0o755)
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', expected)
    with pytest.raises(RuntimeError, match='TOFUDB_EXPECTED_SHA256'):
        _resolve_tofudb_binary()


def test_binary_content_pin_detects_replacement_without_fallback(tmp_path, monkeypatch):
    import hashlib
    engine = tmp_path / 'tofu-db'
    engine.write_bytes(b'verified-candidate')
    engine.chmod(0o755)
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', hashlib.sha256(engine.read_bytes()).hexdigest())
    assert _resolve_tofudb_binary() == str(engine.resolve())
    replacement = tmp_path / 'new-engine'
    replacement.write_bytes(b'different-candidate')
    replacement.chmod(0o755)
    replacement.replace(engine)
    monkeypatch.setattr('lib.storage.supervisor._bundled_tofudb_candidates',
                        lambda: pytest.fail('must not fall back after a pin mismatch'))
    with pytest.raises(RuntimeError, match='SHA-256 mismatch'):
        _resolve_tofudb_binary()


def test_binary_content_pin_applies_to_bundled_resolution(tmp_path, monkeypatch):
    import hashlib
    engine = tmp_path / 'tofu-db'
    engine.write_bytes(b'bundled-engine')
    engine.chmod(0o755)
    monkeypatch.delenv('TOFUDB_BIN', raising=False)
    monkeypatch.setattr('lib.storage.supervisor._bundled_tofudb_candidates', lambda: [engine])
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', hashlib.sha256(engine.read_bytes()).hexdigest().upper())
    assert _resolve_tofudb_binary() == str(engine.resolve())
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', '0' * 64)
    with pytest.raises(RuntimeError, match='SHA-256 mismatch'):
        _resolve_tofudb_binary()


def test_binary_content_pin_bounds_sparse_file_work(tmp_path, monkeypatch):
    engine = tmp_path / 'tofu-db'
    with engine.open('wb') as file:
        file.truncate(512 * 1024 * 1024 + 1)
    engine.chmod(0o755)
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', '0' * 64)
    with pytest.raises(RuntimeError, match='verification budget'):
        _resolve_tofudb_binary()


def test_binary_content_pin_detects_replacement_during_hashing(tmp_path, monkeypatch):
    import hashlib
    engine = tmp_path / 'tofu-db'
    engine.write_bytes(b'verified-candidate')
    engine.chmod(0o755)
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(b'changed-candidate')
    replacement.chmod(0o755)
    expected = hashlib.sha256(engine.read_bytes()).hexdigest()
    original = hashlib.sha256
    class ReplacingDigest:
        def __init__(self):
            self.digest = original()
        def update(self, chunk):
            self.digest.update(chunk)
            replacement.replace(engine)
        def hexdigest(self):
            return self.digest.hexdigest()
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', expected)
    monkeypatch.setattr('lib.storage.supervisor.hashlib.sha256', ReplacingDigest)
    with pytest.raises(RuntimeError, match='changed during verification'):
        _resolve_tofudb_binary()


def test_binary_pin_mismatch_rejects_before_child_spawn(project_root, tmp_path, monkeypatch):
    engine = tmp_path / 'tofu-db'
    engine.write_bytes(b'wrong-engine')
    engine.chmod(0o755)
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', '0' * 64)
    monkeypatch.setattr('lib.storage.supervisor.subprocess.Popen',
                        lambda *a, **k: pytest.fail('mismatch must fail before spawn'))
    supervisor = StorageSupervisor(project_root=project_root, backend='tofudb')
    with pytest.raises(RuntimeError, match='SHA-256 mismatch'):
        supervisor.start()
    assert not supervisor.ready
    assert not (project_root / 'data/tofudb-authority').exists()


def _sidecar_engine(tmp_path: Path, body: bytes = b'sidecar-candidate') -> Path:
    engine = tmp_path / 'tofu-db'
    engine.write_bytes(body)
    engine.chmod(0o755)
    return engine


def test_binary_sidecar_pin_verifies_deployed_artifact(tmp_path, monkeypatch):
    import hashlib
    engine = _sidecar_engine(tmp_path)
    digest = hashlib.sha256(engine.read_bytes()).hexdigest()
    (tmp_path / 'tofu-db.sha256').write_text(f'{digest}  tofu-db\n', encoding='ascii')
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.delenv('TOFUDB_EXPECTED_SHA256', raising=False)
    assert _resolve_tofudb_binary() == str(engine.resolve())


def test_binary_sidecar_pin_mismatch_names_source_remedy_and_provenance(tmp_path, monkeypatch):
    engine = _sidecar_engine(tmp_path)
    (tmp_path / 'tofu-db.sha256').write_text('0' * 64 + '  tofu-db\n', encoding='ascii')
    (tmp_path / 'build.json').write_text(
        '{"source_head": "abc123", "deployment_utc": "2026-09-17T08:05:29Z"}',
        encoding='utf-8')
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.delenv('TOFUDB_EXPECTED_SHA256', raising=False)
    with pytest.raises(RuntimeError, match='SHA-256 mismatch') as failure:
        _resolve_tofudb_binary()
    message = str(failure.value)
    assert 'tofu-db.sha256' in message          # names the pin source
    assert 'deploy_tofudb_engine.py' in message  # names the exact remedy
    assert 'abc123' in message                   # surfaces recorded provenance


def test_binary_env_pin_outranks_sidecar(tmp_path, monkeypatch):
    import hashlib
    engine = _sidecar_engine(tmp_path)
    digest = hashlib.sha256(engine.read_bytes()).hexdigest()
    # A stale sidecar cannot veto an explicit operator pin...
    (tmp_path / 'tofu-db.sha256').write_text('0' * 64 + '  tofu-db\n', encoding='ascii')
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', digest)
    assert _resolve_tofudb_binary() == str(engine.resolve())
    # ...and a fresh sidecar cannot rescue a wrong explicit pin.
    (tmp_path / 'tofu-db.sha256').write_text(f'{digest}  tofu-db\n', encoding='ascii')
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', '0' * 64)
    with pytest.raises(RuntimeError, match='SHA-256 mismatch'):
        _resolve_tofudb_binary()


@pytest.mark.parametrize('body', [
    b'',
    b'not-a-digest\n',
    b'g' * 64,
    b'\xff\xfe' + b'0' * 64,
])
def test_binary_sidecar_pin_fails_closed_when_malformed(tmp_path, monkeypatch, body):
    engine = _sidecar_engine(tmp_path)
    (tmp_path / 'tofu-db.sha256').write_bytes(body)
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.delenv('TOFUDB_EXPECTED_SHA256', raising=False)
    with pytest.raises(RuntimeError, match='pin sidecar'):
        _resolve_tofudb_binary()


def test_binary_without_any_pin_keeps_development_resolution(tmp_path, monkeypatch):
    engine = _sidecar_engine(tmp_path)
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.delenv('TOFUDB_EXPECTED_SHA256', raising=False)
    assert _resolve_tofudb_binary() == str(engine.resolve())


# --- Engine staleness advisory (deployment provenance vs checkout) ----------


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ['git', '-C', str(root), *args],
        check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture()
def engine_checkout(tmp_path: Path) -> Path:
    """A minimal git checkout whose engine subtree can accrete commits."""
    if shutil.which('git') is None:
        pytest.skip('git unavailable')
    root = tmp_path / 'checkout'
    (root / 'packages/tofu-db/src').mkdir(parents=True)
    _git(root, 'init', '-q')
    _git(root, 'config', 'user.email', 'tofu-test@example.invalid')
    _git(root, 'config', 'user.name', 'tofu-test')
    (root / 'packages/tofu-db/src/lib.rs').write_text('// engine v1\n')
    _git(root, 'add', '.')
    _git(root, 'commit', '-qm', 'engine v1')
    return root


def _deployed_engine(tmp_path: Path, source_head: str | None) -> Path:
    engine_dir = tmp_path / 'deployed'
    engine_dir.mkdir(exist_ok=True)
    engine = engine_dir / 'tofu-db'
    engine.write_text('#!/bin/sh\nexit 0\n')
    engine.chmod(0o755)
    if source_head is not None:
        (engine_dir / 'build.json').write_text(
            json.dumps({'source_head': source_head}), encoding='utf-8')
    return engine


def _resolve_deployed(monkeypatch: pytest.MonkeyPatch, engine: Path, checkout: Path) -> str:
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.delenv('TOFUDB_EXPECTED_SHA256', raising=False)
    monkeypatch.setattr(
        'lib.storage.supervisor._tofudb_checkout_root', lambda: checkout)
    return _resolve_tofudb_binary()


def _engine_commit(root: Path, name: str = 'engine v2') -> None:
    (root / 'packages/tofu-db/src/lib.rs').write_text(f'// {name}\n')
    _git(root, 'add', '.')
    _git(root, 'commit', '-qm', name)


def test_engine_staleness_silent_when_provenance_matches_head(
        engine_checkout, tmp_path, monkeypatch, caplog):
    head = _git(engine_checkout, 'rev-parse', 'HEAD')
    engine = _deployed_engine(tmp_path, head)
    with caplog.at_level(logging.WARNING, logger='tofu.storage.supervisor'):
        assert _resolve_deployed(monkeypatch, engine, engine_checkout) == str(engine.resolve())
    assert not caplog.records


def test_engine_staleness_warns_on_engine_commits_since_provenance(
        engine_checkout, tmp_path, monkeypatch, caplog):
    base = _git(engine_checkout, 'rev-parse', 'HEAD')
    engine = _deployed_engine(tmp_path, base)
    _engine_commit(engine_checkout)
    with caplog.at_level(logging.WARNING, logger='tofu.storage.supervisor'):
        assert _resolve_deployed(monkeypatch, engine, engine_checkout) == str(engine.resolve())
    (warning,) = [r.getMessage() for r in caplog.records]
    assert 'packages/tofu-db' in warning
    assert 'deploy_tofudb_engine.py' in warning


def test_engine_staleness_ignores_commits_outside_the_engine(
        engine_checkout, tmp_path, monkeypatch, caplog):
    base = _git(engine_checkout, 'rev-parse', 'HEAD')
    engine = _deployed_engine(tmp_path, base)
    (engine_checkout / 'README.md').write_text('docs only\n')
    _git(engine_checkout, 'add', '.')
    _git(engine_checkout, 'commit', '-qm', 'docs only')
    with caplog.at_level(logging.WARNING, logger='tofu.storage.supervisor'):
        assert _resolve_deployed(monkeypatch, engine, engine_checkout) == str(engine.resolve())
    assert not caplog.records


def test_engine_staleness_notes_dirty_provenance(
        engine_checkout, tmp_path, monkeypatch, caplog):
    base = _git(engine_checkout, 'rev-parse', 'HEAD')
    engine = _deployed_engine(tmp_path, base + '-dirty')
    _engine_commit(engine_checkout)
    with caplog.at_level(logging.WARNING, logger='tofu.storage.supervisor'):
        assert _resolve_deployed(monkeypatch, engine, engine_checkout) == str(engine.resolve())
    (warning,) = [r.getMessage() for r in caplog.records]
    assert 'dirty tree' in warning


def test_engine_staleness_warns_when_provenance_is_not_an_ancestor(
        engine_checkout, tmp_path, monkeypatch, caplog):
    engine = _deployed_engine(tmp_path, '0' * 40)
    with caplog.at_level(logging.WARNING, logger='tofu.storage.supervisor'):
        assert _resolve_deployed(monkeypatch, engine, engine_checkout) == str(engine.resolve())
    (warning,) = [r.getMessage() for r in caplog.records]
    assert 'not an ancestor' in warning


def test_engine_staleness_fail_closed_when_fresh_engine_required(
        engine_checkout, tmp_path, monkeypatch):
    base = _git(engine_checkout, 'rev-parse', 'HEAD')
    engine = _deployed_engine(tmp_path, base)
    _engine_commit(engine_checkout)
    monkeypatch.setenv('TOFUDB_REQUIRE_FRESH_ENGINE', '1')
    with pytest.raises(RuntimeError, match='deploy_tofudb_engine.py'):
        _resolve_deployed(monkeypatch, engine, engine_checkout)


def test_engine_staleness_silent_without_provenance(
        engine_checkout, tmp_path, monkeypatch, caplog):
    engine = _deployed_engine(tmp_path, None)
    _engine_commit(engine_checkout)
    with caplog.at_level(logging.WARNING, logger='tofu.storage.supervisor'):
        assert _resolve_deployed(monkeypatch, engine, engine_checkout) == str(engine.resolve())
    assert not caplog.records
