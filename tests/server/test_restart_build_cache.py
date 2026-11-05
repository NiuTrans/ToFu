"""Restart compilation reuses a stable profile and private disposable cache."""
from pathlib import Path
from types import SimpleNamespace
import json
import os
import shutil
import subprocess

import pytest

from serverctl_pkg import local_placement, native_engine as engines
from tests.server.test_native_engine_update import binary, project as project

pytestmark = pytest.mark.unit


def test_build_profile_does_not_change_with_host_headroom(project, tmp_path, monkeypatch):
    import runtime_guards

    monkeypatch.setattr(engines.shutil, 'which', lambda name, **kw: '/tools/' + name)
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    children = []
    monkeypatch.setattr(engines, '_run', lambda *a, **kw: children.append(kw))
    for memory in (1024, 8192):
        monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda _: SimpleNamespace(
            effective_cpu_count=16, effective_memory_available_mb=memory))
        engines._build(project, tmp_path, {'CARGO_LOG': 'info'}, build_root=tmp_path / 'build')
    assert children[0]['environment'] == children[1]['environment']
    assert 'CARGO_BUILD_JOBS' not in children[0]['environment']
    assert int(children[0]['environment']['CARGO_PROFILE_RELEASE_CODEGEN_UNITS']) > 1
    assert children[0]['environment']['CARGO_PROFILE_RELEASE_LTO'] == 'false'
    assert children[0]['environment']['CARGO_PROFILE_RELEASE_INCREMENTAL'] == 'true'


def test_restart_profile_preserves_explicit_operator_optimization(project, tmp_path, monkeypatch):
    monkeypatch.setattr(engines.shutil, 'which', lambda name, **kw: '/tools/' + name)
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    children = []
    monkeypatch.setattr(engines, '_run', lambda *a, **kw: children.append(kw))
    overrides = {'CARGO_PROFILE_RELEASE_LTO': 'thin',
                 'CARGO_PROFILE_RELEASE_CODEGEN_UNITS': '8',
                 'CARGO_PROFILE_RELEASE_INCREMENTAL': 'false',
                 'CARGO_PROFILE_RELEASE_OPT_LEVEL': '2'}
    engines._build(project, tmp_path, overrides, build_root=tmp_path / 'build')
    assert {key: children[0]['environment'][key] for key in overrides} == overrides


@pytest.mark.parametrize('diagnostics', [False, True])
def test_build_does_not_depend_on_diagnostic_probe(project, tmp_path, monkeypatch, diagnostics):
    import runtime_guards
    def unavailable(*_):
        assert diagnostics, 'ordinary restarts must not probe host capacity'
        raise OSError('probe unavailable')
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', unavailable)
    monkeypatch.setattr(engines.shutil, 'which', lambda name, **kw: '/tools/' + name)
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    children = []
    monkeypatch.setattr(engines, '_run', lambda *a, **kw: children.append(kw))
    environment = {'CARGO_LOG': 'info'} if diagnostics else {}
    engines._build(project, tmp_path, environment, build_root=tmp_path / 'build')
    assert len(children) == 1
    assert 'CARGO_BUILD_JOBS' not in children[0]['environment']


def test_toolchain_directory_alias_reuses_cache_without_renaming_cargo_proxy(project, tmp_path, monkeypatch):
    installation = tmp_path / 'installation'
    (installation / 'bin').mkdir(parents=True)
    (installation / 'bin/rustup').write_text('fixture proxy')
    (installation / 'bin/cargo').symlink_to('rustup')
    (installation / 'bin/cc').write_text('fixture compiler')
    alias = tmp_path / 'alias'
    alias.symlink_to(installation, target_is_directory=True)
    monkeypatch.setattr(engines.shutil, 'which', lambda name, path: str(Path(path.split(os.pathsep)[0]) / name))
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    calls = []
    monkeypatch.setattr(engines, '_run', lambda command, **kw: calls.append((command, kw)))
    for directory in (installation, alias):
        engines._build(project, tmp_path, {'PATH': str(directory / 'bin')}, build_root=tmp_path / 'build')
    assert calls[0][1]['environment']['CARGO_TARGET_DIR'] == calls[1][1]['environment']['CARGO_TARGET_DIR']
    assert all(Path(command[0]).name == 'cargo' for command, _ in calls)
    assert all(Path(command[0]).resolve().name == 'rustup' for command, _ in calls)


def test_real_cargo_keeps_alias_build_fresh_and_rebuilds_changed_source(project, tmp_path, monkeypatch):
    cargo_home = Path(os.environ.get('CARGO_HOME') or Path.home() / '.cargo')
    cargo = shutil.which('cargo') or shutil.which('cargo', path=str(cargo_home / 'bin'))
    rustc = shutil.which('rustc') or shutil.which('rustc', path=str(cargo_home / 'bin'))
    compiler = shutil.which('cc')
    if not cargo or not rustc or not compiler:
        pytest.skip('requires the local Rust and C toolchains')
    installation = tmp_path / 'toolchain'
    (installation / 'bin').mkdir(parents=True)
    (installation / 'bin/cargo').symlink_to(cargo)
    (installation / 'bin/rustc').symlink_to(rustc)
    alias = tmp_path / 'toolchain-alias'
    alias.symlink_to(installation, target_is_directory=True)
    crate = project / 'packages/tofu-db'
    (crate / 'Cargo.toml').write_text(
        '[package]\nname="tofu-db"\nversion="0.2.0"\nedition="2021"\n'
        '[features]\nembedded-mimalloc=[]\n')
    (crate / 'Cargo.lock').write_text('version=4\n[[package]]\nname="tofu-db"\nversion="0.2.0"\n')
    (crate / 'build.rs').write_text('fn main() {}\n')
    (crate / 'src/lib.rs').write_text('pub fn fixture() {}\n')
    source = crate / 'src/main.rs'
    source.write_text('fn main() { println!("first"); }\n')
    fresh = []

    def compile(command, *, cwd, environment, timeout, progress):
        result = subprocess.run(
            [*command, '--offline', '--message-format=json'], cwd=cwd, env=environment,
            capture_output=True, text=True, timeout=min(120, timeout))
        assert result.returncode == 0, result.stderr
        messages = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        binaries = [item for item in messages if item.get('reason') == 'compiler-artifact'
                    and 'bin' in item['target']['kind']]
        assert len(binaries) == 1
        assert binaries[0]['profile']['opt_level'] == '3'
        assert not binaries[0]['profile']['debug_assertions']
        fresh.append(binaries[0]['fresh'])
        return result.stdout

    monkeypatch.setattr(engines, '_run', compile)
    outputs = []
    for index, directory in enumerate((installation, alias, alias)):
        if index == 2:
            source.write_text('fn main() { println!("changed"); }\n')
        inherited = {key: value for key, value in os.environ.items()
                     if not key.startswith('CARGO_PROFILE_')}
        environment = {**inherited, 'CC': compiler,
                       'PATH': str(directory / 'bin') + os.pathsep + os.environ.get('PATH', os.defpath)}
        outputs.append(engines._build(project, tmp_path, environment, build_root=tmp_path / 'build'))
    assert len(set(outputs)) == 1
    assert fresh == [False, True, False]


def test_cache_keeps_distinct_compilers_and_link_flags_separate(project, tmp_path, monkeypatch):
    installations = [tmp_path / name for name in ('first', 'second')]
    for directory in installations:
        directory.mkdir()
        for name in ('cargo', 'cc'):
            (directory / name).write_text('fixture executable')
    monkeypatch.setattr(engines.shutil, 'which', lambda name, path: str(Path(path.split(os.pathsep)[0]) / name))
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    targets = []
    monkeypatch.setattr(engines, '_run', lambda command, **kw: targets.append(kw['environment']['CARGO_TARGET_DIR']))
    for directory, flags in ((installations[0], ''), (installations[1], ''), (installations[0], '-C target-cpu=native')):
        engines._build(project, tmp_path, {'PATH': str(directory), 'RUSTFLAGS': flags}, build_root=tmp_path / 'build')
    assert len(set(targets)) == 3


def test_local_cache_uses_shared_placement_and_canonical_project_identity(project, tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    calls = []

    def placement(source, purpose, **kwargs):
        calls.append((source, purpose, kwargs['environ']))
        cache.mkdir(mode=0o700, exist_ok=True)
        return local_placement.Placement(True, purpose, 'auto', 'selected', root=str(cache))

    monkeypatch.setattr(local_placement, 'choose_local_root', placement)
    alias = tmp_path / 'alias'
    alias.symlink_to(project, target_is_directory=True)
    environment = {'TOFU_LOCAL_DIR': str(tmp_path)}
    first = engines._build_cache_root(project, environment)
    assert first == engines._build_cache_root(alias, environment)
    assert first.is_relative_to(cache)
    assert first.stat().st_mode & 0o077 == 0
    assert calls[0][2] == environment
    other = tmp_path / 'other'; other.mkdir()
    assert first != engines._build_cache_root(other, environment)


@pytest.mark.parametrize('unavailable', [False, True])
def test_unavailable_local_cache_keeps_build_admitted(project, monkeypatch, unavailable):
    def placement(*args, **kwargs):
        if unavailable:
            raise OSError('local cache unavailable')
        return local_placement.Placement(False, 'native-build', 'auto', 'source-local')

    monkeypatch.setattr(local_placement, 'choose_local_root', placement)
    assert engines._build_cache_root(project, {}) == project / 'packages/tofu-db/target'


def test_cache_directory_symlink_cannot_escape_private_placement(project, tmp_path, monkeypatch):
    cache = tmp_path / 'cache'; cache.mkdir(mode=0o700)
    monkeypatch.setattr(local_placement, 'choose_local_root', lambda *a, **k:
        local_placement.Placement(True, 'native-build', 'auto', 'selected', root=str(cache)))
    selected = engines._build_cache_root(project, {})
    selected.rmdir()
    outside = tmp_path / 'outside'; outside.mkdir()
    selected.symlink_to(outside, target_is_directory=True)
    assert engines._build_cache_root(project, {}) == project / 'packages/tofu-db/target'
    assert not list(outside.iterdir())


def test_matching_local_engine_is_reused_before_network_or_compiler(project, tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    binary(cache / 'update-build-host/release/tofu-db', engines.source_fingerprint(project))
    monkeypatch.setattr(engines, '_build_cache_root', lambda *_: cache)
    monkeypatch.setattr(engines, '_prebuilt', lambda *_: pytest.fail('local candidate needs no download'))
    monkeypatch.setattr(engines, '_build', lambda *_: pytest.fail('local candidate needs no compile'))
    assert engines.prepare(project, {})
    engines.verify_current(project / 'packages/tofu-db/target/release/tofu-db', project)


def test_isolated_build_does_not_select_managed_cache(project, tmp_path, monkeypatch):
    monkeypatch.setattr(engines, '_build_cache_root', lambda *_: pytest.fail('private build owns its cache'))
    monkeypatch.setattr(engines.shutil, 'which', lambda name, **kw: '/tools/' + name)
    monkeypatch.setattr(engines, '_c_compiler_accepts_date_time', lambda _: True)
    calls = []
    monkeypatch.setattr(engines, '_run', lambda *a, **kw: calls.append(kw))
    private = tmp_path / 'private'
    candidate = engines._build(project, tmp_path, {}, build_root=private)
    assert candidate.is_relative_to(private)
    assert Path(calls[0]['environment']['CARGO_TARGET_DIR']).is_relative_to(private)


def test_optional_watched_directories_survive_snapshot_refresh(project, tmp_path):
    snapshot = tmp_path / 'snapshot/packages/tofu-db'
    crate = project / 'packages/tofu-db'
    engines._sync_build_snapshot(project, crate, snapshot)
    watched = snapshot / '.cargo'
    assert watched.is_dir()
    stamp = watched.stat().st_mtime_ns
    engines._sync_build_snapshot(project, crate, snapshot)
    assert watched.stat().st_mtime_ns == stamp
    (crate / '.cargo').mkdir()
    config = crate / '.cargo/config.toml'
    config.write_text('[build]\nincremental = true\n')
    engines._sync_build_snapshot(project, crate, snapshot)
    assert (watched / config.name).read_bytes() == config.read_bytes()
    config.unlink()
    engines._sync_build_snapshot(project, crate, snapshot)
    assert watched.is_dir() and not list(watched.iterdir())


def test_stale_cached_candidates_do_not_rescan_source_per_candidate(project, tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    for index in range(4):
        binary(cache / f'update-build-{index}/release/tofu-db', '0' * 64)
    expected = engines.source_fingerprint(project)
    monkeypatch.setattr(engines, '_build_cache_root', lambda *_: cache)
    original = engines.source_fingerprint
    scans = []
    def fingerprint(root):
        scans.append(root)
        return original(root)
    monkeypatch.setattr(engines, 'source_fingerprint', fingerprint)
    def download(root, stage, env):
        # Capture + initial deployed candidate check only; scanning obsolete
        # binaries must not multiply full source-tree reads.
        assert len(scans) <= 2
        return binary(stage / 'tofu-db', expected)
    monkeypatch.setattr(engines, '_prebuilt', download)
    assert engines.prepare(project, {})
