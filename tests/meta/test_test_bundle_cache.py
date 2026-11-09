"""Cross-process test-bundle cache in tests/_runtime_sections.py.

The cache must reuse a bundle only while every input the bundler read is
byte-identical, and must never persist output tied to one session's view dir.
"""
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from tests import _runtime_sections as rs

pytestmark = pytest.mark.unit

_REAL_ENVIRONMENT_DIGEST = rs._bundle_environment_digest


@pytest.fixture
def fake_bundler(tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    monkeypatch.setenv('TOFU_TEST_BUNDLE_CACHE', str(cache))
    monkeypatch.setattr(rs, '_bundle_environment_digest', lambda: 'env-1')
    monkeypatch.setattr(rs, '_input_digest_memo', {})
    # Files created by the test are seconds old; pretend they have settled.
    monkeypatch.setattr(rs, '_bundle_clock_ns',
                        lambda: rs.time.time_ns() + 10_000_000_000)
    (tmp_path / 'src').mkdir()
    source = tmp_path / 'src' / 'owner.ts'
    source.write_text('export const a = 1;\n', encoding='utf-8')
    calls = []
    state = {'body': 'bundle-v1'}

    def run(argv, **_kwargs):
        calls.append(argv)
        manifest = next(a.split('=', 1)[1] for a in argv
                        if a.startswith('--inputs-manifest='))
        outfile = next(a.split('=', 1)[1] for a in argv
                       if a.startswith('--outfile='))
        Path(outfile).parent.mkdir(parents=True, exist_ok=True)
        Path(outfile).write_text(state['body'], encoding='utf-8')
        Path(manifest).write_text(json.dumps({
            'format': 'tofu.test-bundle/v1', 'complete': True, 'portable': True,
            'inputs': [str(source)], 'outputs': [str(outfile)],
        }), encoding='utf-8')

        class Result:
            returncode = 0
            stderr = ''
        return Result()

    monkeypatch.setattr(rs.subprocess, 'run', run)
    out = tmp_path / 'view' / 'owner.js'

    def bundle():
        rs._run_test_bundler([str(source), f'--outfile={out}'], [out],
                             timeout=5, failure='bundle failed')
        return out.read_text(encoding='utf-8')

    return bundle, calls, source, state, out


def test_identical_inputs_reuse_the_cached_bundle(fake_bundler):
    bundle, calls, _source, _state, out = fake_bundler
    assert bundle() == 'bundle-v1'
    out.unlink()
    assert bundle() == 'bundle-v1'
    assert len(calls) == 1


def test_changed_recorded_input_rebuilds(fake_bundler):
    bundle, calls, source, state, _out = fake_bundler
    bundle()
    source.write_text('export const a = 22;\n', encoding='utf-8')
    state['body'] = 'bundle-v2'
    assert bundle() == 'bundle-v2'
    assert len(calls) == 2


def test_deleted_recorded_input_rebuilds(fake_bundler):
    bundle, calls, source, _state, _out = fake_bundler
    bundle()
    source.unlink()
    bundle()
    assert len(calls) == 2


def test_new_file_beside_an_input_rebuilds(fake_bundler):
    """A new sibling can change module resolution without touching any
    recorded input; the input directory listing must invalidate the record."""
    bundle, calls, source, _state, _out = fake_bundler
    bundle()
    (source.parent / 'owner').mkdir()
    bundle()
    assert len(calls) == 2


def test_input_modified_inside_the_racy_window_is_never_recorded(
        fake_bundler, monkeypatch):
    """Whole-second mtimes cannot prove a just-written input was the version
    the build read, so such a build must not be cached."""
    bundle, calls, _source, _state, _out = fake_bundler
    monkeypatch.setattr(rs, '_bundle_clock_ns', rs.time.time_ns)
    bundle()
    bundle()
    assert len(calls) == 2


def test_digest_of_a_recently_modified_file_is_not_memoized(tmp_path, monkeypatch):
    """A same-second, same-size rewrite keeps the stat signature on a coarse
    mount; memoizing a fresh file's digest would hide that rewrite."""
    monkeypatch.setattr(rs, '_input_digest_memo', {})
    path = tmp_path / 'owner.ts'
    path.write_text('export const a = 1;\n', encoding='utf-8')
    assert rs._memoized_digest(str(path)) is not None
    assert rs._input_digest_memo == {}


def test_environment_change_rebuilds(fake_bundler, monkeypatch):
    bundle, calls, _source, _state, _out = fake_bundler
    bundle()
    monkeypatch.setattr(rs, '_bundle_environment_digest', lambda: 'env-2')
    bundle()
    assert len(calls) == 2


def test_harness_adapter_change_rebuilds(fake_bundler, monkeypatch, tmp_path):
    """The Python adapter composes generated entries outside Rollup's manifest."""
    bundle, calls, _source, _state, _out = fake_bundler
    environment_digest = _REAL_ENVIRONMENT_DIGEST
    adapter = tmp_path / 'adapter.py'
    adapter.write_text('# revision 1\n', encoding='utf-8')
    monkeypatch.setattr(rs, '__file__', str(adapter))
    monkeypatch.setattr(rs, '_bundle_environment_digest', environment_digest)
    monkeypatch.setattr(rs, '_frontend_source_listing', lambda: b'')
    bundle()
    adapter.write_text('# revision 2\n', encoding='utf-8')
    bundle()
    assert len(calls) == 2


def test_output_naming_the_session_view_dir_is_not_cached(fake_bundler):
    bundle, calls, _source, state, _out = fake_bundler
    state['body'] = f'require({str(rs._DIRECTORY)!r})'
    bundle()
    bundle()
    assert len(calls) == 2


def test_cache_can_be_disabled(fake_bundler, monkeypatch):
    bundle, calls, _source, _state, _out = fake_bundler
    monkeypatch.setenv('TOFU_TEST_BUNDLE_CACHE', 'off')
    bundle()
    bundle()
    assert len(calls) == 2


@pytest.fixture
def checkout_bundles(tmp_path, monkeypatch):
    """Identical sources at different roots, with real cache admission."""
    roots = [tmp_path / name for name in ('lane-a', 'lane-b')]
    installation = tmp_path / 'installed-modules'
    installation.mkdir()
    (installation / '.package-lock.json').write_text('{}')
    for root in roots:
        for name, text in {
            'scripts/vite_test_bundle.mjs': '// controlled bundler\n',
            'tests/_runtime_sections.py': '# controlled adapter\n',
            'package-lock.json': '{}',
            'package.json': '{"name":"bundle-fixture"}',
            'frontend/src/owner.ts': 'export const answer = 42;\n',
            'frontend/src/style.css': '.example { color: red; }\n',
        }.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (root / 'node_modules').symlink_to(installation)
    monkeypatch.setenv('TOFU_TEST_BUNDLE_CACHE', str(tmp_path / 'cache'))
    monkeypatch.setattr(rs, '_frontend_source_listing',
                        lambda: b'frontend/src/owner.ts\0frontend/src/style.css\0')
    monkeypatch.setattr(rs, '_input_digest_memo', {})
    monkeypatch.setattr(rs, '_bundle_clock_ns', lambda: rs.time.time_ns() + 10_000_000_000)
    calls = []
    state = {'path_bound': False, 'complete': True, 'portable': True}

    def run(argv, *, cwd, **kwargs):
        calls.append(Path(cwd))
        outfile = Path(next(a.split('=', 1)[1] for a in argv if a.startswith('--outfile=')))
        outfile.parent.mkdir(parents=True, exist_ok=True)
        body = (Path(cwd) / 'frontend/src/owner.ts').read_text()
        if state['path_bound']:
            body += f'\nconst origin = {str(cwd)!r};'
        outfile.write_text(body)
        asset = outfile.parent / 'style.css'
        asset.write_text((Path(cwd) / 'frontend/src/style.css').read_text())
        manifest = Path(next(a.split('=', 1)[1] for a in argv if a.startswith('--inputs-manifest=')))
        inputs = [str(Path(cwd) / 'frontend/src/owner.ts'),
                  str(Path(cwd) / 'frontend/src/style.css')]
        dependency = Path(cwd) / 'node_modules/fixture-package/owner.js'
        if dependency.exists():
            inputs.append(str(dependency.resolve()))
            outfile.write_text(body + dependency.read_text())
        manifest.write_text(json.dumps({
            'format': 'tofu.test-bundle/v1',
            'complete': state['complete'],
            'portable': state['portable'],
            'inputs': inputs,
            'outputs': [str(outfile), str(asset)],
        }))
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stderr='')

    monkeypatch.setattr(rs.subprocess, 'run', run)

    def bundle(root):
        view = root / 'private-view'
        monkeypatch.setattr(rs, 'ROOT', root)
        monkeypatch.setattr(rs, '__file__', str(root / 'tests/_runtime_sections.py'))
        monkeypatch.setattr(rs, '_DIRECTORY', view)
        output = view / 'owner.js'
        rs._run_test_bundler([str(root / 'frontend/src/owner.ts'), f'--outfile={output}'],
                            [output], timeout=5, failure='controlled build failed')
        return output

    return bundle, roots, calls, state


def test_identical_checkouts_share_all_emitted_artifacts(checkout_bundles):
    bundle, roots, calls, _state = checkout_bundles
    first, second = [bundle(root) for root in roots]
    assert second.read_bytes() == first.read_bytes()
    assert second.with_name('style.css').read_bytes() == first.with_name('style.css').read_bytes()
    assert calls == [roots[0]]


@pytest.mark.parametrize('entrypoint', ['native-module', 'import-port'])
def test_real_adapter_entrypoints_share_equivalent_builds(checkout_bundles, monkeypatch, entrypoint):
    _bundle, roots, calls, _state = checkout_bundles
    monkeypatch.setattr(rs, '_CACHE', {})
    monkeypatch.setattr(rs, '_require_frontend_bundler', lambda: None)
    outputs = []
    for root in roots:
        monkeypatch.setattr(rs, 'ROOT', root)
        monkeypatch.setattr(rs, '__file__', str(root / 'tests/_runtime_sections.py'))
        monkeypatch.setattr(rs, '_DIRECTORY', root / 'private-view')
        source = root / 'frontend/src/owner.ts'
        output = (rs.native_module_path('owner.js', source) if entrypoint == 'native-module'
                  else rs._import_port_path(source))
        outputs.append(Path(output))
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    assert calls == [roots[0]]


@pytest.mark.parametrize('changed', ['frontend/src/owner.ts', 'frontend/src/style.css',
                                     'package-lock.json', 'package.json', 'scripts/vite_test_bundle.mjs',
                                     'tests/_runtime_sections.py'])
def test_shared_artifacts_revalidate_the_receiving_checkout(checkout_bundles, changed):
    bundle, roots, calls, _state = checkout_bundles
    bundle(roots[0])
    path = roots[1] / changed
    path.write_text(path.read_text() + '\n// changed\n')
    bundle(roots[1])
    assert calls == roots


def test_shared_artifacts_revalidate_receiving_directory_membership(checkout_bundles):
    bundle, roots, calls, _state = checkout_bundles
    bundle(roots[0])
    (roots[1] / 'frontend/src/owner').mkdir()
    bundle(roots[1])
    assert calls == roots


def test_checkout_bound_output_is_never_shared(checkout_bundles):
    bundle, roots, calls, state = checkout_bundles
    state['path_bound'] = True
    bundle(roots[0])
    output = bundle(roots[1])
    assert str(roots[1]) in output.read_text()
    bundle(roots[0])
    assert calls == roots


def test_location_sensitive_modules_keep_checkout_local_reuse(checkout_bundles):
    bundle, roots, calls, state = checkout_bundles
    state['portable'] = False
    for root in roots + roots:
        bundle(root)
    assert calls == roots


def test_incomplete_output_receipts_are_not_cached(checkout_bundles):
    bundle, roots, calls, state = checkout_bundles
    state['complete'] = False
    for root in roots:
        bundle(root)
    assert calls == roots


def test_public_build_environment_changes_invalidate_reuse(checkout_bundles, monkeypatch):
    bundle, roots, calls, _state = checkout_bundles
    monkeypatch.setenv('VITE_TEST_BUNDLE_VALUE', 'before')
    bundle(roots[0])
    monkeypatch.setenv('VITE_TEST_BUNDLE_VALUE', 'after')
    bundle(roots[1])
    assert calls == roots


def test_dotenv_expansion_is_scoped_to_its_checkout(checkout_bundles):
    bundle, roots, calls, _state = checkout_bundles
    for root in roots:
        (root / '.env').write_text('VITE_LOCATION=${PWD}\n')
        bundle(root)
    assert calls == roots


def test_changed_symlink_target_invalidates_an_identical_listing(checkout_bundles):
    bundle, roots, calls, _state = checkout_bundles
    for root in roots:
        (root / 'frontend/src/alias.ts').symlink_to('owner.ts')
    bundle(roots[0])
    alias = roots[1] / 'frontend/src/alias.ts'
    alias.unlink()
    alias.symlink_to('style.css')
    bundle(roots[1])
    assert calls == roots


def test_a_changed_installed_input_invalidates_even_with_an_unchanged_lockfile(checkout_bundles):
    bundle, roots, calls, _state = checkout_bundles
    dependency = roots[0] / 'node_modules/fixture-package/owner.js'
    dependency.parent.mkdir()
    dependency.write_text('// installed revision one\n')
    bundle(roots[0])
    dependency.write_text('// installed revision two\n')
    output = bundle(roots[1])
    assert 'revision two' in output.read_text()
    assert calls == roots


def test_separate_installations_do_not_share_based_on_version_labels(checkout_bundles):
    bundle, roots, calls, _state = checkout_bundles
    bundle(roots[0])
    modules = roots[1] / 'node_modules'
    modules.unlink()
    modules.mkdir()
    (modules / '.package-lock.json').write_text('{}')
    bundle(roots[1])
    assert calls == roots


def test_corrupt_side_artifact_rebuilds_the_complete_bundle(checkout_bundles, tmp_path):
    bundle, roots, calls, _state = checkout_bundles
    bundle(roots[0])
    records = list((tmp_path / 'cache').glob('*/record.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    for index, artifact in enumerate(record['artifacts']):
        if artifact['path'].endswith('/style.css'):
            (records[0].parent / f'output-{index}').write_bytes(b'corrupted')
            break
    else:
        pytest.fail('complete artifact receipt must include the emitted CSS')
    output = bundle(roots[1])
    assert output.with_name('style.css').read_text() == '.example { color: red; }\n'
    assert calls == roots


@pytest.mark.skipif('fork' not in multiprocessing.get_all_start_methods(), reason='POSIX process locks')
def test_concurrent_collectors_build_the_same_artifacts_once(checkout_bundles, monkeypatch):
    bundle, roots, _calls, _state = checkout_bundles
    root = roots[0]
    context = multiprocessing.get_context('fork')
    missed = context.Barrier(5)
    entered = context.Event()
    release = context.Event()
    count = context.Value('i', 0)
    original_run = rs.subprocess.run
    original_restore = rs._restore_bundle
    first_probe = True

    def restore(*args):
        nonlocal first_probe
        result = original_restore(*args)
        if first_probe:
            first_probe = False
            assert not result
            missed.wait(timeout=30)
        return result

    def run(*args, **kwargs):
        with count.get_lock():
            count.value += 1
        entered.set()
        assert release.wait(30)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(rs, '_restore_bundle', restore)
    monkeypatch.setattr(rs.subprocess, 'run', run)

    def collect():
        rs.ROOT = root
        rs.__file__ = str(root / 'tests/_runtime_sections.py')
        rs._DIRECTORY = root / f'view-{os.getpid()}'
        out = rs._DIRECTORY / 'owner.js'
        rs._run_test_bundler([str(root / 'frontend/src/owner.ts'), f'--outfile={out}'],
                            [out], timeout=5, failure='controlled build failed')
        assert out.read_text() == 'export const answer = 42;\n'
        assert out.with_name('style.css').read_text() == '.example { color: red; }\n'

    processes = [context.Process(target=collect) for _ in range(4)]
    try:
        for process in processes:
            process.start()
        missed.wait(timeout=30)
        assert entered.wait(30)
        release.set()
        for process in processes:
            process.join(30)
        assert [process.exitcode for process in processes] == [0] * 4
        assert count.value == 1
    finally:
        release.set()
        for process in processes:
            if process.is_alive():
                process.kill()
            process.join()


@pytest.mark.skipif(rs.fcntl is None, reason='POSIX process locks')
def test_pruning_keeps_an_active_build_and_reclaims_its_expired_lease(tmp_path, monkeypatch):
    with rs._bundle_build_lease(tmp_path, 'fixture') as producer:
        assert producer
        lease, = list(tmp_path.glob('.lease-*'))
        os.utime(lease, (0, 0))
        monkeypatch.setattr(rs, '_bundle_cache_pruned', False)
        rs._prune_bundle_cache(tmp_path)
        assert lease.exists()
    monkeypatch.setattr(rs, '_bundle_cache_pruned', False)
    rs._prune_bundle_cache(tmp_path)
    assert not lease.exists()
    with rs._bundle_build_lease(tmp_path, 'fixture') as producer:
        assert producer


@pytest.mark.skipif(rs.fcntl is None, reason='POSIX process locks')
def test_distinct_checkouts_and_actions_never_wait_for_each_other(tmp_path, monkeypatch):
    with rs._bundle_build_lease(tmp_path, 'first') as first:
        with rs._bundle_build_lease(tmp_path, 'second') as second:
            assert first and second
        monkeypatch.setattr(rs, 'ROOT', tmp_path / 'another-checkout')
        with rs._bundle_build_lease(tmp_path, 'first') as other:
            assert other


@pytest.mark.skipif(rs.fcntl is None, reason='POSIX process locks')
def test_interrupted_lease_acquisition_closes_its_descriptor(tmp_path, monkeypatch):
    import builtins
    opened = []
    original = builtins.open

    def opening(*args, **kwargs):
        handle = original(*args, **kwargs)
        opened.append(handle)
        return handle

    def interrupted(*args):
        raise KeyboardInterrupt('controlled lock interruption')

    monkeypatch.setattr(rs, 'open', opening, raising=False)
    monkeypatch.setattr(rs.fcntl, 'flock', interrupted)
    with pytest.raises(KeyboardInterrupt):
        with rs._bundle_build_lease(tmp_path, 'fixture'):
            pytest.fail('an interrupted acquisition cannot own the build')
    assert opened and all(handle.closed for handle in opened)


@pytest.mark.skipif('fork' not in multiprocessing.get_all_start_methods(), reason='POSIX process locks')
def test_dead_producer_cannot_strand_later_collectors(tmp_path):
    context = multiprocessing.get_context('fork')
    ready = context.Event()
    hold = context.Event()

    def produce():
        with rs._bundle_build_lease(tmp_path, 'fixture'):
            ready.set()
            hold.wait()

    process = context.Process(target=produce)
    process.start()
    try:
        assert ready.wait(30)
        process.kill()
        process.join(30)
        assert process.exitcode is not None
        with rs._bundle_build_lease(tmp_path, 'fixture') as producer:
            assert producer
    finally:
        if process.is_alive():
            process.kill()
        process.join()
