"""Contracts for verified local replicas of slow-volume Python environments."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from serverctl_pkg import local_placement
from serverctl_pkg import runtime_replica as rr


pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux replica contract'),
]

_PURELIB = 'lib/python3.12/site-packages'


def _write(path: Path, text: str = '', mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    if mode is not None:
        path.chmod(mode)
    return path


def _synthetic_prefix(tmp_path: Path) -> rr.Environment:
    """A conda-like prefix whose interpreter never runs (verification is stubbed)."""
    prefix = tmp_path / 'slow-volume' / 'env'
    _write(prefix / 'bin' / 'python3.12', '#!/bin/sh\n', 0o755)
    (prefix / 'bin' / 'python').symlink_to('python3.12')
    _write(prefix / 'lib' / 'python3.12' / 'os.py', 'SEP = "/"\n')
    _write(prefix / _PURELIB / 'pkg' / '__init__.py', 'VALUE = 1\n')
    (prefix / _PURELIB / 'pkg-1.0.dist-info').mkdir()
    _write(prefix / _PURELIB / 'paths.pth', f'{prefix}/extra\n')
    _write(prefix / 'conda-meta' / 'pkg-1.0-0.json', '{}')
    return rr.Environment(prefix=str(prefix.resolve()), base_prefix=str(prefix.resolve()),
                          python='bin/python', purelib=_PURELIB, platlib=_PURELIB,
                          stdlib='lib/python3.12', cache_tag='cpython-312')


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    environment = _synthetic_prefix(tmp_path)
    root = tmp_path / 'replicas'
    root.mkdir(mode=0o700)
    monkeypatch.setattr(rr, 'describe_environment', lambda _python: environment)
    monkeypatch.setattr(rr, '_verify', lambda *_args: '')
    python = str(Path(environment.prefix) / 'bin' / 'python')
    return SimpleNamespace(environment=environment, root=root, python=python,
                           registry=rr.registry_path(root, environment.prefix))


def _live(registry: Path) -> Path:
    return registry / os.readlink(registry / rr.LIVE_NAME)


def _status(registry: Path) -> dict:
    return json.loads((registry / rr.STATUS_NAME).read_text())


def _record(registry: Path, result: dict) -> None:
    # What the builder process persists after each build.
    registry.mkdir(parents=True, exist_ok=True)
    (registry / rr.STATUS_NAME).write_text(json.dumps({'finishedAt': time.time(), **result}))


def test_fingerprint_tracks_installs_but_not_bytecode(tmp_path):
    environment = _synthetic_prefix(tmp_path)
    prefix = Path(environment.prefix)
    purelib = prefix / _PURELIB
    original = rr.fingerprint(environment)

    (purelib / '__pycache__').mkdir()
    _write(purelib / '__pycache__' / 'six.cpython-312.pyc', 'bytecode')
    assert rr.fingerprint(environment) == original

    changes = [
        lambda: (purelib / 'new-2.0.dist-info').mkdir(),
        lambda: os.utime(purelib / 'pkg-1.0.dist-info', ns=(1, 1)),  # reinstall, same name
        lambda: _write(purelib / 'paths.pth', f'{prefix}/extra\n{prefix}/more\n'),
        lambda: _write(prefix / 'conda-meta' / 'other-1.0-0.json', '{}'),
        lambda: _write(prefix / 'bin' / 'tool', '#!/bin/sh\n', 0o755),
    ]
    seen = {original}
    for change in changes:
        change()
        digest = rr.fingerprint(environment)
        assert digest not in seen
        seen.add(digest)


def test_build_publishes_an_immutable_generation_and_rebuilds_share_bytes(synthetic):
    first = rr.build_environment(synthetic.python, synthetic.root)
    assert first['state'] == 'published', first
    generation = _live(synthetic.registry)
    copied = generation / _PURELIB / 'pkg' / '__init__.py'
    manifest = json.loads((generation / rr.MANIFEST_NAME).read_text())

    assert manifest['sourcePrefix'] == synthetic.environment.prefix
    assert (generation / 'bin' / 'python').is_symlink()
    assert os.readlink(generation / 'bin' / 'python') == 'python3.12'
    assert (generation / _PURELIB / 'paths.pth').read_text() == f'{generation}/extra\n'
    assert not os.access(copied, os.W_OK) or os.geteuid() == 0
    assert rr._fresh_generation(synthetic.root, synthetic.environment,
                                rr.fingerprint(synthetic.environment)) == generation

    lease = rr.lease_generation(generation)
    _write(Path(synthetic.environment.prefix) / _PURELIB / 'extra_mod.py', 'X = 2\n')
    second = rr.build_environment(synthetic.python, synthetic.root)
    rebuilt = _live(synthetic.registry)

    assert second['state'] == 'published' and rebuilt != generation
    assert second['copiedBytes'] < second['bytes']
    assert (rebuilt / _PURELIB / 'pkg' / '__init__.py').stat().st_ino == copied.stat().st_ino
    assert (rebuilt / _PURELIB / 'paths.pth').read_text() == f'{rebuilt}/extra\n'
    assert generation.exists(), 'a leased generation survives collection'
    os.close(lease)
    assert rr.collect_garbage(synthetic.registry) == 1
    assert not generation.exists() and rebuilt.exists()


def test_collection_spares_generations_mapped_by_live_processes(synthetic):
    """Children that outlive their worker keep the generation they run from."""
    rr.build_environment(synthetic.python, synthetic.root)
    generation = _live(synthetic.registry)
    mapped_file = generation / _PURELIB / 'pkg' / '__init__.py'
    holder = subprocess.Popen(
        [sys.executable, '-c',
         'import mmap, sys\n'
         'handle = open(sys.argv[1], "rb")\n'
         'view = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)\n'
         'print("mapped", flush=True)\n'
         'sys.stdin.read()\n', str(mapped_file)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'mapped'
        assert str(generation.resolve()) in rr.mapped_generations(synthetic.root)
        _write(Path(synthetic.environment.prefix) / _PURELIB / 'extra_mod.py', 'X = 2\n')
        assert rr.build_environment(synthetic.python, synthetic.root)['state'] == 'published'
        assert _live(synthetic.registry) != generation and generation.exists()
    finally:
        holder.communicate('', timeout=30)
    assert rr.collect_garbage(synthetic.registry, rr.mapped_generations(synthetic.root)) == 1


def test_leases_never_protect_a_generation_being_collected(synthetic):
    rr.build_environment(synthetic.python, synthetic.root)
    generation = _live(synthetic.registry)
    (generation / rr.MANIFEST_NAME).unlink()

    assert rr.lease_generation(generation) is None
    assert rr.lease_generation(synthetic.root / 'missing' / 'g-x') is None


def test_registry_retirement_waits_for_every_generation(synthetic):
    rr.build_environment(synthetic.python, synthetic.root)
    old = _live(synthetic.registry)
    _write(Path(synthetic.environment.prefix) / _PURELIB / 'extra_mod.py', 'X = 2\n')
    old_lease = rr.lease_generation(old)
    rr.build_environment(synthetic.python, synthetic.root)

    kept = rr.collect_idle_registries(synthetic.root, everything=True)
    assert kept['registriesRetired'] == 0 and old.exists()

    os.close(old_lease)
    retired = rr.collect_idle_registries(synthetic.root, everything=True)
    assert retired['registriesRetired'] == 1 and not synthetic.registry.exists()


def test_orphaned_registry_is_retired_only_when_removal_is_provable(synthetic):
    rr.build_environment(synthetic.python, synthetic.root)
    prefix = Path(synthetic.environment.prefix)
    hidden = prefix.parent.with_name('unmounted')

    prefix.parent.rename(hidden)  # the whole volume disappears: keep the cache
    assert rr.collect_idle_registries(synthetic.root)['registriesRetired'] == 0
    hidden.rename(prefix.parent)

    (prefix / 'bin' / 'python3.12').unlink()  # the environment itself was removed
    (prefix / 'bin' / 'python').unlink()
    assert rr.collect_idle_registries(synthetic.root)['registriesRetired'] == 1


def test_failure_outcomes_defer_rebuilds_by_cause(synthetic, monkeypatch):
    digest = rr.fingerprint(synthetic.environment)

    monkeypatch.setattr(rr, '_verify', lambda *_args: 'replica-maps-source-files')
    unsupported = rr.build_environment(synthetic.python, synthetic.root)
    _record(synthetic.registry, unsupported)
    assert unsupported['state'] == rr.UNSUPPORTED_STATE
    assert not rr._build_admissible(synthetic.root, synthetic.environment, digest)
    assert not list(synthetic.registry.glob('g-*'))

    monkeypatch.setattr(rr, '_verify', lambda *_args: 'interpreter-probe-failed')
    _record(synthetic.registry, {'fingerprint': 'older-source', 'state': 'unsupported'})
    transient = rr.build_environment(synthetic.python, synthetic.root)
    _record(synthetic.registry, transient)
    assert (transient['state'], transient['failures']) == ('failed', 1)
    assert not rr._build_admissible(synthetic.root, synthetic.environment, digest)
    _record(synthetic.registry, {**transient, 'finishedAt': time.time() - 2 * 3600})
    assert rr._build_admissible(synthetic.root, synthetic.environment, digest)

    crashed = {'fingerprint': digest, 'state': rr.BUILDING_STATE, 'startedAt': time.time()}
    _record(synthetic.registry, crashed)
    assert not rr._build_admissible(synthetic.root, synthetic.environment, digest)
    _record(synthetic.registry, {**crashed, 'startedAt': time.time() - 3600})
    assert rr._build_admissible(synthetic.root, synthetic.environment, digest)


def test_space_shortage_retries_only_when_space_returns(synthetic, monkeypatch):
    digest = rr.fingerprint(synthetic.environment)
    free = {'value': False}
    monkeypatch.setattr(local_placement, 'has_free_space',
                        lambda _path, _required=0, **_kwargs: free['value'])

    result = rr.build_environment(synthetic.python, synthetic.root)
    _record(synthetic.registry, result)

    assert result['state'] == 'insufficient-space' and result['copiedBytes'] > 0
    assert not rr._build_admissible(synthetic.root, synthetic.environment, digest)
    free['value'] = True
    assert rr._build_admissible(synthetic.root, synthetic.environment, digest)


def test_inode_shortage_is_refused_before_copying_and_gates_the_retry(synthetic, monkeypatch):
    digest = rr.fingerprint(synthetic.environment)
    demands = []

    def no_room(path, _required=0, *, required_inodes=0, **_kwargs):
        if Path(path) == synthetic.registry:
            demands.append(required_inodes)
        return False

    monkeypatch.setattr(local_placement, 'has_free_space', no_room)
    result = rr.build_environment(synthetic.python, synthetic.root)
    _record(synthetic.registry, result)

    # Every directory, symlink and copied file needs its own inode.
    assert result['state'] == 'insufficient-space' and result['copiedInodes'] > 0
    assert demands and set(demands) == {result['copiedInodes']}
    assert not list(synthetic.registry.glob('g-*')), 'no partial generation is started'
    demands.clear()
    assert not rr._build_admissible(synthetic.root, synthetic.environment, digest)
    assert demands == [result['copiedInodes']]


def test_a_source_changing_mid_copy_is_retried_not_penalized(synthetic, monkeypatch):
    freeze = rr._freeze

    def install_during_copy(environment, generation, inventory):
        freeze(environment, generation, inventory)
        (Path(environment.prefix) / _PURELIB / 'late-1.0.dist-info').mkdir(exist_ok=True)

    monkeypatch.setattr(rr, '_freeze', install_during_copy)
    result = rr.build_environment(synthetic.python, synthetic.root)
    _record(synthetic.registry, result)

    assert result['state'] == 'source-changed'
    assert not list(synthetic.registry.glob('g-*'))
    assert rr._build_admissible(synthetic.root, synthetic.environment,
                                rr.fingerprint(synthetic.environment))


def test_repeated_rejection_stops_rebuilding_the_same_source(synthetic):
    digest = rr.fingerprint(synthetic.environment)
    for attempt in range(1, rr.REJECTIONS_BEFORE_UNSUPPORTED + 1):
        result = rr.build_environment(synthetic.python, synthetic.root)
        _record(synthetic.registry, {**result, 'finishedAt': time.time()})
        assert result['state'] == 'published', result
        rr.reject_generation(_live(synthetic.registry), 'worker failed before readiness')
        status = _status(synthetic.registry)
        assert status['rejections'] == attempt
        assert rr._fresh_generation(synthetic.root, synthetic.environment, digest) is None
        _record(synthetic.registry, {**status, 'finishedAt': 0.0})

    assert _status(synthetic.registry)['state'] == rr.UNSUPPORTED_STATE
    assert not rr._build_admissible(synthetic.root, synthetic.environment, digest)


def test_selection_leases_fresh_replicas_and_reports_missing_ones(synthetic, monkeypatch):
    selected = SimpleNamespace(selected=True, root=str(synthetic.root), reason='selected',
                               as_status=lambda: {'selected': True})
    monkeypatch.setattr(rr, '_placement', lambda _python, _environ: selected)
    environ = {'TOFU_RUNTIME_REPLICA': 'auto'}

    pending = rr.select_worker_runtime(synthetic.python, environ)
    assert (pending.replica, pending.reason) == (False, 'replica-pending')
    assert pending.build_pythons == [synthetic.python]
    assert pending.build_root == str(synthetic.root)

    rr.build_environment(synthetic.python, synthetic.root)
    generation = _live(synthetic.registry)
    runtime = rr.select_worker_runtime(synthetic.python, environ)
    try:
        assert runtime.replica and runtime.build_pythons == []
        assert runtime.python == str(generation / 'bin' / 'python')
        assert runtime.generations == [str(generation)] and len(runtime.lease_fds) == 1
    finally:
        runtime.close_parent_lock()
    suspect = rr.select_worker_runtime(synthetic.python, environ, avoid=str(generation))
    assert (suspect.replica, suspect.reason) == (False, 'replica-suspect')
    disabled = rr.select_worker_runtime(synthetic.python, {'TOFU_RUNTIME_REPLICA': 'off'})
    assert (disabled.replica, disabled.reason) == (False, 'disabled')


def test_identity_helpers_map_replicas_back_to_their_source(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    _write(source / 'bin' / 'python', '#!/bin/sh\n', 0o755)
    generation = tmp_path / 'replicas' / 'env-0' / 'g-1'
    _write(generation / 'bin' / 'python', '#!/bin/sh\n', 0o755)
    _write(generation / rr.MANIFEST_NAME, json.dumps({
        'schema': rr.SCHEMA, 'sourcePrefix': str(source.resolve()),
        'sourcePython': str(source / 'bin' / 'python'), 'purelib': _PURELIB,
        'platlib': _PURELIB}))
    unrelated = str(tmp_path / 'other' / 'bin' / 'python')

    assert rr.authoritative_interpreter(str(generation / 'bin' / 'python')) == str(
        source / 'bin' / 'python')
    assert rr.authoritative_interpreter(unrelated) == unrelated
    assert rr.serves_environment(str(generation), str(source))
    assert not rr.serves_environment(str(tmp_path / 'other'), str(source))
    assert rr.source_package_directories(str(generation)) == [
        str(source.resolve() / _PURELIB)]

    monkeypatch.setattr(sys, 'prefix', str(generation))
    monkeypatch.setattr(sys, 'executable', str(generation / 'bin' / 'python'))
    assert rr.runs_marked_environment(str(source / 'bin' / 'python'), str(source))
    assert rr.runs_marked_environment(str(source / 'bin' / 'python'))
    assert not rr.runs_marked_environment(unrelated, str(tmp_path / 'other'))


def test_build_requests_run_detached_on_the_source_interpreter(tmp_path, monkeypatch):
    root = tmp_path / 'replicas'
    launched = []

    class _Process:
        def __init__(self, argv, **options):
            launched.append((argv, options))

    monkeypatch.setattr(subprocess, 'Popen', _Process)
    monkeypatch.setattr(rr, 'authoritative_interpreter', lambda python=None: '/source/python')

    assert rr.request_build(['/env/bin/python', '/env/bin/python'], root,
                            launcher='/replica/bin/python')
    argv, options = launched[0]
    assert argv[:4] == ['/source/python', '-s', '-m', 'serverctl_pkg.runtime_replica']
    assert argv.count('--python') == 1 and options['start_new_session'] is True
    assert not any(key.startswith(('PYTHON', 'VIRTUAL_ENV', 'CONDA'))
                   and key != 'PYTHONDONTWRITEBYTECODE' for key in options['env'])

    held = rr._open_lock(root / rr.LOCK_NAME, exclusive=True, create=True)
    try:
        assert not rr.request_build(['/env/bin/python'], root)
    finally:
        os.close(held)
    assert len(launched) == 1


@pytest.fixture(scope='module')
def source_venv(tmp_path_factory):
    path = tmp_path_factory.mktemp('slow-volume') / 'venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(path)],
                   check=True, capture_output=True, timeout=300)
    purelib = Path(subprocess.run(
        [str(path / 'bin' / 'python'), '-I', '-c',
         'import sysconfig; print(sysconfig.get_path("purelib"))'],
        check=True, capture_output=True, text=True, timeout=120).stdout.strip())
    _write(purelib / 'demo_pkg' / '__init__.py', 'VALUE = 1\n')
    (purelib / 'demo_pkg-1.0.dist-info').mkdir()
    _write(path / 'extra' / 'extra_mod.py', 'X = 2\n')
    _write(purelib / 'demo.pth', f'{path.resolve()}/extra\n')
    return path


def test_real_virtualenv_replica_imports_from_its_own_tree(source_venv, tmp_path, monkeypatch):
    python = str(source_venv / 'bin' / 'python')
    root = tmp_path / 'replicas'
    root.mkdir(mode=0o700)

    environment = rr.describe_environment(python)
    assert environment is not None and environment.kind == 'venv'
    assert environment.prefix == str(source_venv.resolve())
    result = rr.build_environment(python, root)
    assert result['state'] == 'published', result
    generation = _live(rr.registry_path(root, environment.prefix))

    probe = subprocess.run(
        [str(generation / environment.python), '-I', '-c',
         'import demo_pkg, extra_mod, sys\n'
         'print(sys.prefix); print(demo_pkg.__file__); print(extra_mod.__file__)'],
        check=True, capture_output=True, text=True, timeout=120).stdout.split()
    assert all(path.startswith(str(generation)) for path in probe), probe

    monkeypatch.setattr(rr, '_placement', lambda candidate, _environ: SimpleNamespace(
        selected=candidate == python, root=str(root), reason='selected', as_status=dict))
    runtime = rr.select_worker_runtime(python, {'TOFU_RUNTIME_REPLICA': 'auto'})
    try:
        assert runtime.replica, runtime.status
        assert rr.authoritative_interpreter(runtime.python) == python
    finally:
        runtime.close_parent_lock()
