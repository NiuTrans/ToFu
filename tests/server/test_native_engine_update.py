"""Engine updates must prepare before shutdown and attest actual build inputs."""
import hashlib
import json
from pathlib import Path
import sys
import os
import time

import pytest

from serverctl_pkg import native_engine as engines

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, '_validate_candidate', lambda *_: None)
    crate = tmp_path / 'packages/tofu-db'
    (crate / 'src').mkdir(parents=True)
    for name in ('Cargo.toml', 'Cargo.lock', 'build.rs', 'src/lib.rs'):
        (crate / name).write_text(name + '\n')
    (crate / 'Cargo.toml').write_text('[package]\nname = "tofu-db"\nversion = "0.2.0"\n')
    (tmp_path / '.env').write_text('TOFU_PERSONAL_STORAGE_BACKEND=tofudb\nTOFUDB_EXPECTED_SHA256=' + 'a' * 64 + '\n')
    (tmp_path / 'VERSION').write_text('1.2.3')
    return tmp_path


def binary(path, fingerprint):
    path.parent.mkdir(parents=True, exist_ok=True)
    info = {'format': 'tofu-db-build.v1', 'source_sha256': fingerprint, 'version': 'test',
            'embedded_allocator': 'mimalloc' if sys.platform == 'linux' else 'system'}
    path.write_text(f'#!{sys.executable}\nimport sys\nprint({json.dumps(json.dumps(info))} if sys.argv[1:] == ["--build-info"] else "usage: tofu-db")\n')
    path.chmod(0o755)
    return path


def test_source_identity_detects_dirty_edits_without_git_and_ignores_docs(project):
    before = engines.source_fingerprint(project)
    (project / 'README.md').write_text('documentation')
    assert engines.source_fingerprint(project) == before
    (project / 'packages/tofu-db/src/new.rs').write_text('new engine behavior')
    assert engines.source_fingerprint(project) != before


def test_launch_refuses_stale_engine_by_default_even_if_provenance_claims_current(project, monkeypatch):
    import lib.storage.supervisor as supervisor
    engine = binary(project / 'old/tofu-db', '0' * 64)
    (engine.parent / 'build.json').write_text(json.dumps({'source_head': 'current', 'source_sha256': engines.source_fingerprint(project)}))
    monkeypatch.setattr(supervisor, '_tofudb_checkout_root', lambda: project)
    monkeypatch.delenv('TOFUDB_REQUIRE_FRESH_ENGINE', raising=False)
    with pytest.raises(engines.EnginePreparationError, match='does not match'):
        supervisor._advise_tofudb_engine_staleness(str(engine))


def test_prepare_downloads_matching_engine_without_cargo_and_reuses_it(project, monkeypatch):
    dest = project / 'packages/tofu-db/target/release/tofu-db'
    binary(dest, '0' * 64)
    old = dest.read_bytes()
    calls = []
    def download(root, staging, env):
        calls.append('download')
        return binary(staging / 'tofu-db', engines.source_fingerprint(root))
    monkeypatch.setattr(engines, '_prebuilt', download)
    monkeypatch.setattr(engines, '_build', lambda *_: pytest.fail('matching release needs no compiler'))
    assert engines.prepare(project, {})
    engines.verify_current(dest, project)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    assert f'TOFUDB_EXPECTED_SHA256={digest}' in (project / '.env').read_text()
    assert next(dest.parent.glob('tofu-db.bak-*')).read_bytes() == old
    assert not engines.prepare(project, {})
    assert calls == ['download']


def test_stale_release_falls_back_to_build_before_publication(project, monkeypatch):
    calls = []
    monkeypatch.setattr(engines, '_prebuilt', lambda root, staging, env: binary(staging / 'old', '0' * 64))
    def build(root, staging, env):
        calls.append('build')
        return binary(staging / 'new', engines.source_fingerprint(root))
    monkeypatch.setattr(engines, '_build', build)
    assert engines.prepare(project, {})
    assert calls == ['build']


def test_failed_build_preserves_engine_pin_and_project_configuration(project, monkeypatch):
    dest = binary(project / 'packages/tofu-db/target/release/tofu-db', '0' * 64)
    old, config = dest.read_bytes(), (project / '.env').read_bytes()
    monkeypatch.setattr(engines, '_prebuilt', lambda *_: None)
    def fail(*_):
        raise engines.EnginePreparationError('compiler failed')
    monkeypatch.setattr(engines, '_build', fail)
    with pytest.raises(engines.EnginePreparationError, match='compiler failed'):
        engines.prepare(project, {})
    assert dest.read_bytes() == old
    assert (project / '.env').read_bytes() == config


def test_source_change_during_build_never_publishes_candidate(project, monkeypatch):
    dest = binary(project / 'packages/tofu-db/target/release/tofu-db', '0' * 64)
    old = dest.read_bytes()
    monkeypatch.setattr(engines, '_prebuilt', lambda *_: None)
    def build(root, staging, env):
        candidate = binary(staging / 'new', engines.source_fingerprint(root))
        (root / 'packages/tofu-db/src/lib.rs').write_text('concurrent source edit')
        return candidate
    monkeypatch.setattr(engines, '_build', build)
    with pytest.raises(engines.EnginePreparationError):
        engines.prepare(project, {})
    assert dest.read_bytes() == old


def test_explicit_process_pin_is_not_silently_rewritten(project, monkeypatch):
    monkeypatch.setattr(engines, '_prebuilt', lambda *_: pytest.fail('explicit pin must be respected'))
    with pytest.raises(engines.EnginePreparationError, match='external.*pin'):
        engines.prepare(project, {'TOFUDB_EXPECTED_SHA256': 'b' * 64})


def test_old_worker_pin_advances_only_with_verified_deployment_receipt(project):
    dest = binary(project / 'packages/tofu-db/target/release/tofu-db', engines.source_fingerprint(project))
    current = hashlib.sha256(dest.read_bytes()).hexdigest()
    prior = 'c' * 64
    config = {'TOFUDB_EXPECTED_SHA256': current}
    inherited = {'TOFUDB_EXPECTED_SHA256': prior}
    receipt = {'previous_binary_sha256': prior, 'binary_sha256': current}
    (dest.parent / 'build.json').write_text(json.dumps(receipt))
    assert engines.reconcile_project_pin(project, config, inherited)
    assert inherited['TOFUDB_EXPECTED_SHA256'] == current
    inherited['TOFUDB_EXPECTED_SHA256'] = 'd' * 64
    assert not engines.reconcile_project_pin(project, config, inherited)
    inherited['TOFUDB_EXPECTED_SHA256'] = prior
    dest.write_text('corrupted candidate')
    assert not engines.reconcile_project_pin(project, config, inherited)
    assert inherited['TOFUDB_EXPECTED_SHA256'] == prior


def test_external_override_is_never_overwritten(project, tmp_path, monkeypatch):
    external = binary(tmp_path / 'external/tofu-db', '0' * 64)
    monkeypatch.setattr(engines, '_prebuilt', lambda *_: pytest.fail('external override is not managed'))
    with pytest.raises(engines.EnginePreparationError, match='external'):
        engines.prepare(project, {'TOFUDB_BIN': str(external)})


def test_gui_update_cannot_report_success_when_engine_preparation_fails(monkeypatch):
    import serverctl
    from lib.self_update._apply import _prepare_updated_engine
    monkeypatch.setattr(serverctl, 'prepare_native_engine_artifact', lambda *_a, **_kw: 'engine unavailable')
    events = []
    result = _prepare_updated_engine({'ok': True, 'changed': True, 'needs_restart': True}, lambda *args: events.append(args))
    assert not result['ok'] and not result['needs_restart']
    assert result['detail'] == 'engine unavailable'
    assert events[-1][1] == 'error'


def test_gui_restart_checks_engine_before_frontend(monkeypatch):
    import serverctl
    from serverctl_pkg.application_restart import main
    monkeypatch.setattr(serverctl, 'prepare_native_engine_artifact', lambda *_, **__: 'engine unavailable')
    monkeypatch.setattr(serverctl, 'prepare_source_frontend_artifact', lambda *_: pytest.fail('must stop at engine refusal'))
    assert main(['--stage', 'prepare']) == 1


def test_sql_backend_never_downloads_or_builds_native_engine(project, monkeypatch):
    monkeypatch.setattr(engines, '_prebuilt', lambda *_: pytest.fail('SQL must not prepare native engine'))
    assert not engines.prepare(project, {'TOFU_PERSONAL_STORAGE_BACKEND': 'sqlite'})


def test_unconfigured_personal_backend_keeps_canonical_sqlite_default(project, monkeypatch):
    (project / '.env').write_text('')
    monkeypatch.setattr(engines, '_prebuilt', lambda *_: pytest.fail('default SQLite must not prepare an engine'))
    assert not engines.prepare(project, {})


def test_cli_uses_canonical_native_selection(project, monkeypatch):
    import serverctl
    monkeypatch.setenv('TOFU_PERSONAL_STORAGE_BACKEND', 'tofudb')
    calls = []
    monkeypatch.setattr(engines, '_run', lambda command, **kw: calls.append((command, kw)) or '')
    assert serverctl.prepare_native_engine_artifact('test', project=str(project)) == ''
    assert len(calls) == 1
    assert calls[0][0][1:3] == ['-m', 'serverctl_pkg.native_engine']
    assert calls[0][1]['progress']
    assert calls[0][1]['termination_grace'] > 5  # Worker must finish compiler cleanup.


def test_cold_boot_prepares_before_storage_and_stops_on_failure(monkeypatch):
    import serverctl
    import lib.server_assembly as assembly
    import lib.storage as storage
    calls = []
    monkeypatch.setattr(assembly, '_boot', lambda *_: None)
    monkeypatch.setattr(assembly, '_server_log', __import__('logging').getLogger('test'))
    monkeypatch.setattr(serverctl, 'prepare_native_engine_artifact',
                        lambda *a, **k: calls.append('prepare') or '')
    monkeypatch.setattr(storage, 'start_storage', lambda: calls.append('storage'))
    monkeypatch.setattr(storage, 'storage_status', lambda: {'ready': True})
    assembly._start_storage_sidecar()
    assert calls == ['prepare', 'storage']
    calls.clear()
    monkeypatch.setattr(serverctl, 'prepare_native_engine_artifact', lambda *a, **k: 'engine unavailable')
    with pytest.raises(RuntimeError, match='engine unavailable'):
        assembly._start_storage_sidecar()
    assert calls == []


def test_failed_semantic_probe_never_publishes_candidate(project, monkeypatch):
    dest = binary(project / 'packages/tofu-db/target/release/tofu-db', '0' * 64)
    old = dest.read_bytes()
    monkeypatch.setattr(engines, '_prebuilt', lambda root, stage, env: binary(stage / 'new', engines.source_fingerprint(root)))
    def reject(*_):
        raise engines.EnginePreparationError('semantic mismatch')
    monkeypatch.setattr(engines, '_validate_candidate', reject)
    with pytest.raises(engines.EnginePreparationError, match='semantic mismatch'):
        engines.prepare(project, {})
    assert dest.read_bytes() == old


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-group lifecycle')
def test_preparation_timeout_reaps_compiler_group(tmp_path):
    pid_file = tmp_path / 'child.pid'
    program = ('import subprocess,sys,time; from pathlib import Path; '
               'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
               f'Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(60)')
    with pytest.raises(engines.EnginePreparationError, match='budget'):
        engines._run([sys.executable, '-c', program], cwd=tmp_path,
                     environment=dict(os.environ), timeout=1)
    pid = int(pid_file.read_text())
    proc = Path(f'/proc/{pid}/stat')
    if proc.exists():
        assert proc.read_text().split()[2] == 'Z'  # Exited child awaiting its OS reaper.


def test_build_uses_owned_source_snapshot_and_launch_capacity(project, monkeypatch, tmp_path):
    from types import SimpleNamespace
    import runtime_guards
    monkeypatch.setattr(engines.shutil, 'which', lambda *_a, **_kw: '/toolchain/cargo')
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda _: SimpleNamespace(
        effective_cpu_count=12, effective_memory_available_mb=4096))
    stage = tmp_path / 'stage'; stage.mkdir()
    def compile(command, *, cwd, environment, timeout, progress):
        expected = ['/toolchain/cargo', 'build', '--release', '--locked']
        if sys.platform == 'linux':
            expected.extend(['--features', 'embedded-mimalloc'])
        assert command == expected
        assert environment['CARGO_BUILD_JOBS'] == '4'
        assert environment['CARGO_PROFILE_RELEASE_CODEGEN_UNITS'] == '4'
        assert environment['CARGO_PROFILE_RELEASE_LTO'] == 'thin'
        assert environment['CARGO_PROFILE_RELEASE_INCREMENTAL'] == 'true'
        assert cwd == project / 'packages/tofu-db/target/update-source'
        assert (cwd / 'src/lib.rs').read_bytes() == (project / 'packages/tofu-db/src/lib.rs').read_bytes()
        assert timeout == engines.PREPARATION_TOTAL_TIMEOUT_SECONDS - engines.BUILD_FINALIZATION_RESERVE_SECONDS
        return ''
    monkeypatch.setattr(engines, '_run', compile)
    engines._build(project, stage, {})


@pytest.mark.skipif(os.name == 'nt', reason='POSIX executable toolchain fixtures')
@pytest.mark.parametrize('location', ['configured_home', 'default_home', 'path'])
def test_offline_restart_builds_with_service_toolchain(project, tmp_path, monkeypatch, location):
    """Real child execution proves both Cargo lookup and sibling rustc lookup."""
    from tofu_dotenv import read_dotenv_values, update_dotenv_file

    service_home = tmp_path / 'service-home'
    cargo_home = (service_home / '.cargo' if location == 'default_home'
                  else tmp_path / 'custom-cargo')
    rustup_home = tmp_path / 'custom-rustup'
    tools = cargo_home / 'bin'
    tools.mkdir(parents=True)
    rustup_home.mkdir()
    rustc = tools / 'rustc'
    rustc.write_text(f'#!{sys.executable}\nprint("local-rustc-ok")\n')
    rustc.chmod(0o755)
    candidate_text = (
        f'#!{sys.executable}\nimport sys\n'
        f'print({json.dumps(json.dumps({"format": "tofu-db-build.v1", "source_sha256": engines.source_fingerprint(project)}))}'
        ' if sys.argv[1:] == ["--build-info"] else "usage: tofu-db")\n')
    cargo = tools / 'cargo'
    cargo.write_text(
        f'#!{sys.executable}\nimport os,subprocess\nfrom pathlib import Path\n'
        f'assert os.environ["RUSTUP_HOME"] == {str(rustup_home)!r}\n'
        'assert subprocess.check_output(["rustc"], text=True).strip() == "local-rustc-ok"\n'
        'dest = Path(os.environ["CARGO_TARGET_DIR"]) / "release/tofu-db"\n'
        'dest.parent.mkdir(parents=True, exist_ok=True)\n'
        f'dest.write_text({candidate_text!r})\ndest.chmod(0o755)\n')
    cargo.chmod(0o755)
    (tools / 'cc').write_text('#!/bin/sh\nexit 0\n')
    (tools / 'cc').chmod(0o755)
    configuration = {'RUSTUP_HOME': str(rustup_home)}
    if location == 'configured_home':
        configuration['CARGO_HOME'] = str(cargo_home)
    update_dotenv_file(project / '.env', configuration, remove=('TOFUDB_EXPECTED_SHA256',))
    environment = {'HOME': str(service_home), 'PATH': str(tools) if location == 'path' else os.defpath}
    before = dict(environment)

    def unavailable(*_):
        raise OSError('synthetic offline release download')

    monkeypatch.setattr(engines, '_prebuilt', unavailable)
    assert engines.prepare(project, environment)
    assert environment == before  # Build-only PATH must not mutate the service.
    dest = project / 'packages/tofu-db/target/release/tofu-db'
    engines.verify_current(dest, project)
    saved = read_dotenv_values(project / '.env')
    assert saved['RUSTUP_HOME'] == str(rustup_home)
    if location == 'configured_home':
        assert saved['CARGO_HOME'] == str(cargo_home)
    assert not engines.prepare(project, environment)  # Published artifact reused.


def test_missing_service_toolchain_reports_deployment_repair(project, tmp_path):
    environment = {'HOME': str(tmp_path / 'home'), 'CARGO_HOME': str(tmp_path / 'cargo'),
                   'PATH': str(tmp_path / 'empty-path')}
    with pytest.raises(engines.EnginePreparationError, match='CARGO_HOME.*RUSTUP_HOME.*project .env'):
        engines._build(project, tmp_path, environment)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX executable permission')
def test_nonexecutable_cargo_is_not_selected(project, tmp_path):
    cargo_home = tmp_path / 'cargo'
    (cargo_home / 'bin').mkdir(parents=True)
    (cargo_home / 'bin/cargo').write_text('not executable')
    with pytest.raises(engines.EnginePreparationError, match='Cargo was not found'):
        engines._build(project, tmp_path,
                       {'CARGO_HOME': str(cargo_home), 'PATH': str(tmp_path / 'empty-path')})


def test_build_does_not_borrow_ambient_path(project, monkeypatch, tmp_path):
    import runtime_guards
    from types import SimpleNamespace

    monkeypatch.setenv('PATH', '/unrelated/interactive-shell')
    paths = []

    def lookup(_name, *, path):
        paths.append(path)
        return '/service/toolchain/cargo'

    monkeypatch.setattr(engines.shutil, 'which', lookup)
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda _: SimpleNamespace(
        effective_cpu_count=1, effective_memory_available_mb=1024))
    children = []
    monkeypatch.setattr(engines, '_run', lambda *_a, **kw: children.append(kw['environment']))
    engines._build(project, tmp_path, {})
    assert paths[0] == os.defpath
    assert all(path in (os.defpath, "/service/toolchain:" + os.defpath) for path in paths)
    assert children[0]['PATH'] == os.pathsep.join(('/service/toolchain', os.defpath))


def test_retry_reuses_matching_local_build_before_download_or_compile(project, monkeypatch):
    dest = binary(project / 'packages/tofu-db/target/certified/tofu-db', '0' * 64)
    cached = binary(project / 'packages/tofu-db/target/update-build/release/tofu-db', engines.source_fingerprint(project))
    monkeypatch.setattr(engines, '_prebuilt', lambda *_: pytest.fail('matching local build needs no download'))
    monkeypatch.setattr(engines, '_build', lambda *_: pytest.fail('matching local build needs no compile'))
    probes = []
    monkeypatch.setattr(engines, '_validate_candidate', lambda candidate, *_: probes.append(candidate))
    assert engines.prepare(project, {'TOFUDB_BIN': str(dest)})
    assert probes == [cached]
    engines.verify_current(dest, project)
    assert not engines.prepare(project, {'TOFUDB_BIN': str(dest)})


def test_cached_engine_semantic_failure_preserves_live_artifact(project, monkeypatch):
    dest = binary(project / 'packages/tofu-db/target/certified/tofu-db', '0' * 64)
    old, configuration = dest.read_bytes(), (project / '.env').read_bytes()
    binary(project / 'packages/tofu-db/target/update-build/release/tofu-db', engines.source_fingerprint(project))
    def refuse(*_):
        raise engines.EnginePreparationError('cached engine semantic mismatch')
    monkeypatch.setattr(engines, '_validate_candidate', refuse)
    with pytest.raises(engines.EnginePreparationError, match='semantic mismatch'):
        engines.prepare(project, {'TOFUDB_BIN': str(dest)})
    assert dest.read_bytes() == old
    assert (project / '.env').read_bytes() == configuration


def test_stale_cached_engine_is_not_reused(project, monkeypatch):
    dest = binary(project / 'packages/tofu-db/target/certified/tofu-db', '0' * 64)
    binary(project / 'packages/tofu-db/target/update-build/release/tofu-db', '1' * 64)
    monkeypatch.setattr(engines, '_prebuilt', lambda *_: None)
    def build(root, staging, _environment):
        return binary(staging / 'fresh/tofu-db', engines.source_fingerprint(root))
    monkeypatch.setattr(engines, '_build', build)
    assert engines.prepare(project, {'TOFUDB_BIN': str(dest)})
    engines.verify_current(dest, project)


def test_embedded_cache_requires_matching_library_identity(project, monkeypatch):
    dest = binary(project / 'packages/tofu-db/target/certified/tofu-db', '0' * 64)
    cached = binary(project / 'packages/tofu-db/target/update-build/release/tofu-db', engines.source_fingerprint(project))
    (cached.parent / 'libtofu_db.so').write_bytes(b'obsolete library')
    monkeypatch.setattr(engines, '_prebuilt', lambda *_: None)
    built = []
    def build(root, staging, _environment):
        built.append(True)
        candidate = binary(staging / 'fresh/tofu-db', engines.source_fingerprint(root))
        (candidate.parent / 'libtofu_db.so').write_bytes(b'fresh library')
        return candidate
    monkeypatch.setattr(engines, '_build', build)
    assert engines.prepare(project, {'TOFUDB_BIN': str(dest), 'TOFUDB_TRANSPORT': 'embedded'})
    assert built == [True]
    assert (dest.parent / 'libtofu_db.so').read_bytes() == b'fresh library'


def test_source_snapshot_preserves_unchanged_inputs_and_removes_deleted_sources(project):
    crate = project / 'packages/tofu-db'
    snapshot = crate / 'target/update-source'
    removed = crate / 'src/old/module.rs'
    removed.parent.mkdir()
    removed.write_text('old source')
    engines._sync_build_snapshot(project, crate, snapshot)
    unchanged = snapshot / 'Cargo.toml'
    os.utime(unchanged, ns=(1_000_000_000, 1_000_000_000))
    modified = crate / 'src/lib.rs'
    modified.write_text('new source')
    removed.unlink()
    engines._sync_build_snapshot(project, crate, snapshot)
    assert unchanged.stat().st_mtime_ns == 1_000_000_000
    assert (snapshot / 'src/lib.rs').read_bytes() == modified.read_bytes()
    assert not (snapshot / 'src/old').exists()
    # The stable path and source bytes agree across repeated preparation.
    assert engines.source_fingerprint(project) is not None
    before = (snapshot / 'src/lib.rs').stat().st_mtime_ns
    engines._sync_build_snapshot(project, crate, snapshot)
    assert (snapshot / 'src/lib.rs').stat().st_mtime_ns == before


def test_build_snapshot_refuses_symlink_escape(project, tmp_path):
    snapshot = project / 'packages/tofu-db/target/update-source'
    snapshot.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (snapshot / 'src').symlink_to(outside, target_is_directory=True)
    with pytest.raises(engines.EnginePreparationError, match='unsafe path'):
        engines._sync_build_snapshot(project, project / 'packages/tofu-db', snapshot)
    assert list(outside.iterdir()) == []


def _write_path_dependency_chain(project):
    """Give the fixture crate the production shape: tofu-db -> tofu-runtime -> vendored crate."""
    crate = project / 'packages/tofu-db'
    (crate / 'Cargo.toml').write_text(
        '[package]\nname = "tofu-db"\nversion = "0.2.0"\n'
        '[dependencies]\ntofu-runtime = { path = "../tofu-runtime" }\n')
    runtime = project / 'packages/tofu-runtime'
    (runtime / 'src').mkdir(parents=True)
    (runtime / 'Cargo.toml').write_text(
        '[package]\nname = "tofu-runtime"\nversion = "0.1.0"\n'
        '[dependencies]\ntofu-rayon-core = { path = "vendor/rayon-core" }\n')
    (runtime / 'src/lib.rs').write_text('runtime source\n')
    rayon = runtime / 'vendor/rayon-core'
    (rayon / 'src').mkdir(parents=True)
    (rayon / 'Cargo.toml').write_text('[package]\nname = "tofu-rayon-core"\nversion = "1.13.0"\n')
    (rayon / 'src/lib.rs').write_text('rayon source\n')
    return runtime, rayon


def test_source_snapshot_mirrors_path_dependencies_as_sibling_crates(project):
    runtime, rayon = _write_path_dependency_chain(project)
    crate = project / 'packages/tofu-db'
    snapshot = crate / 'target/update-source'
    engines._sync_build_snapshot(project, crate, snapshot)
    assert (snapshot / 'Cargo.toml').is_file()
    runtime_snapshot = snapshot.parent / 'tofu-runtime'
    assert (runtime_snapshot / 'Cargo.toml').read_bytes() == (runtime / 'Cargo.toml').read_bytes()
    assert (runtime_snapshot / 'src/lib.rs').read_bytes() == (runtime / 'src/lib.rs').read_bytes()
    # Vendored path dependencies keep their Cargo-relative location below their
    # declaring crate so the compiler resolves the same relative path.
    rayon_snapshot = runtime_snapshot / 'vendor/rayon-core'
    assert (rayon_snapshot / 'Cargo.toml').read_bytes() == (rayon / 'Cargo.toml').read_bytes()
    assert (rayon_snapshot / 'src/lib.rs').is_file()
    # A synthetic workspace root materializes workspace inheritance for mirrored crates.
    assert (snapshot.parent / 'Cargo.toml').is_file()


def test_source_fingerprint_tracks_path_dependency_edits(project):
    runtime, rayon = _write_path_dependency_chain(project)
    before = engines.source_fingerprint(project)
    (runtime / 'src/lib.rs').write_text('changed runtime source\n')
    assert engines.source_fingerprint(project) != before
    after = engines.source_fingerprint(project)
    (rayon / 'src/lib.rs').write_text('changed rayon source\n')
    assert engines.source_fingerprint(project) != after


def test_source_snapshot_removes_deleted_path_dependencies(project):
    runtime, _rayon = _write_path_dependency_chain(project)
    crate = project / 'packages/tofu-db'
    snapshot = crate / 'target/update-source'
    engines._sync_build_snapshot(project, crate, snapshot)
    assert (snapshot.parent / 'tofu-runtime/Cargo.toml').is_file()
    (crate / 'Cargo.toml').write_text('[package]\nname = "tofu-db"\nversion = "0.2.0"\n')
    engines._sync_build_snapshot(project, crate, snapshot)
    assert not (snapshot.parent / 'tofu-runtime').exists()


@pytest.mark.parametrize("reported,accepted", [("0",False),("1",True),("2",False)])
def test_cached_library_attests_allocator_in_addition_to_source(project,monkeypatch,reported,accepted):
    monkeypatch.setattr(engines.sys,"platform","linux")
    path=project/"paired/tofu-db";path.parent.mkdir();path.write_bytes(b"paired")
    path.with_name("libtofu_db.so").write_bytes(b"candidate")
    expected="a"*64
    monkeypatch.setattr(engines,"_run",lambda *args,**kwargs:expected+":"+reported)
    assert engines._cached_library_matches_source(path,expected) is accepted


def test_cached_library_missing_allocator_metadata_fails_closed(project,monkeypatch):
    monkeypatch.setattr(engines.sys,"platform","linux")
    path=project/"paired/tofu-db";path.parent.mkdir();path.write_bytes(b"paired")
    path.with_name("libtofu_db.so").write_bytes(b"candidate")
    def absent(*args,**kwargs):raise engines.EnginePreparationError("missing allocator symbol")
    monkeypatch.setattr(engines,"_run",absent)
    assert not engines._cached_library_matches_source(path,"a"*64)


@pytest.mark.parametrize("allocator",[None,"system"])
def test_embedded_pair_rejects_missing_or_wrong_linux_allocator(project,monkeypatch,allocator):
    monkeypatch.setattr(engines.sys,"platform","linux")
    path=project/"paired/tofu-db";path.parent.mkdir();path.write_bytes(b"engine")
    library=path.with_name("libtofu_db.so");library.write_bytes(b"library")
    source="a"*64
    path.with_name("build.json").write_text(json.dumps({"source_sha256":source,"library_sha256":hashlib.sha256(library.read_bytes()).hexdigest()}))
    monkeypatch.setattr(engines,"build_identity",lambda _: {"source_sha256":source,"embedded_allocator":allocator})
    with pytest.raises(engines.EnginePreparationError,match="mismatched"):
        engines.verify_library(path)


def test_build_repairs_missing_compiler_only_in_child(project, tmp_path, monkeypatch):
    environment = {'CC': str(tmp_path / 'missing-gcc'), 'CXX': str(tmp_path / 'missing-g++'),
                   'PATH': os.defpath}
    original = dict(environment)
    children = []
    monkeypatch.setattr(engines.shutil, 'which', lambda name, **kw: '/tools/' + name)
    monkeypatch.setattr(engines, '_run', lambda *a, **kw: children.append(kw['environment']))
    engines._build(project, tmp_path, environment)
    assert environment == original
    assert children[0]['CC'] == '/tools/cc'
    assert 'CXX' not in children[0]


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux native allocator build')
def test_build_without_c_compiler_fails_before_cargo(project, tmp_path, monkeypatch):
    monkeypatch.setattr(engines.shutil, 'which', lambda name, **kw: '/tools/cargo' if name == 'cargo' else None)
    monkeypatch.setattr(engines, '_run', lambda *a, **kw: pytest.fail('must preflight compiler'))
    with pytest.raises(engines.EnginePreparationError, match='C compiler'):
        engines._build(project, tmp_path, {'PATH': str(tmp_path)})


@pytest.mark.parametrize('deadline, elapsed, expected_timeout', [
    (1200, 0, 1170), (1200, 495, 675), (1200, 599, 571),
    (1200, 1180, 0), (1200, 1230, 0),
    (600, 0, 570), (600, 210, 360), (600, 580, 0), (600, 630, 0)])
def test_build_uses_remaining_outer_budget_and_refuses_exhausted_budget(project, monkeypatch, tmp_path, deadline, elapsed, expected_timeout):
    from types import SimpleNamespace
    import runtime_guards

    monkeypatch.setattr(engines.shutil, 'which', lambda *_a, **_kw: '/toolchain/cargo')
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda _: SimpleNamespace(
        effective_cpu_count=12, effective_memory_available_mb=4096))
    monkeypatch.setattr(engines.time, 'monotonic', lambda: elapsed)
    real_run = engines._run
    observed = []

    def compile(command, **kwargs):
        observed.append(kwargs['timeout'])
        if kwargs['timeout'] == 0:
            return real_run(command, **kwargs)
        return ''

    monkeypatch.setattr(engines, '_run', compile)
    token = engines._deadline.set(deadline)
    try:
        if expected_timeout:
            engines._build(project, tmp_path, {})
        else:
            monkeypatch.setattr(engines, '_sync_build_snapshot', lambda *_a, **_kw: None)
            monkeypatch.setattr(engines.subprocess, 'Popen', lambda *_a, **_kw: pytest.fail('expired budget must not spawn'))
            with pytest.raises(engines.EnginePreparationError, match='time budget exhausted'):
                engines._build(project, tmp_path, {})
    finally:
        engines._deadline.reset(token)
    assert observed == [expected_timeout]


def test_retry_reuses_host_cached_engine_before_network_or_compilation(project, monkeypatch):
    cached = binary(project / 'packages/tofu-db/target/update-build-host-key/release/tofu-db',
                    engines.source_fingerprint(project))
    monkeypatch.setattr(engines, '_prebuilt', lambda *_: pytest.fail('verified cached engine needs no network'))
    monkeypatch.setattr(engines, '_build', lambda *_: pytest.fail('verified cached engine needs no compile'))
    assert engines.prepare(project, {})
    engines.verify_current(project / 'packages/tofu-db/target/release/tofu-db', project)


def test_build_preserves_explicit_profile_overrides(project, tmp_path, monkeypatch):
    monkeypatch.setattr(engines.shutil, 'which', lambda name, **kw: '/tools/' + name)
    children = []
    monkeypatch.setattr(engines, '_run', lambda *a, **kw: children.append(kw['environment']))
    choices = {'CARGO_BUILD_JOBS': '2', 'CARGO_PROFILE_RELEASE_CODEGEN_UNITS': '8',
               'CARGO_PROFILE_RELEASE_LTO': 'true', 'CARGO_PROFILE_RELEASE_INCREMENTAL': 'false'}
    engines._build(project, tmp_path, choices)
    assert all(children[0][key] == value for key, value in choices.items())


def test_staged_path_dependency_closure_is_accepted_by_cargo(project):
    """Exercise Cargo's workspace boundary, not merely the copied file layout."""
    import shutil
    import subprocess

    cargo = shutil.which('cargo')
    if cargo is None:
        pytest.skip('Cargo metadata requires the Cargo executable; no build is performed')
    runtime, _ = _write_path_dependency_chain(project)
    manifest = runtime / 'Cargo.toml'
    manifest.write_text(manifest.read_text().replace(
        'version = "0.1.0"', 'version = "0.1.0"\nedition.workspace = true'))
    crate = project / 'packages/tofu-db'
    # The generic project fixture has a deliberately non-TOML placeholder lock.
    (crate / 'Cargo.lock').unlink()
    snapshot = crate / 'target/update-source'
    engines._sync_build_snapshot(project, crate, snapshot)
    result = subprocess.run(
        [cargo, 'metadata', '--offline', '--no-deps', '--format-version', '1'],
        cwd=snapshot, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    packages = json.loads(result.stdout)['packages']
    engine = next(package for package in packages if package['name'] == 'tofu-db')
    dependency = next(item for item in engine['dependencies'] if item['name'] == 'tofu-runtime')
    assert Path(dependency['path']) == snapshot.parent / 'tofu-runtime'
