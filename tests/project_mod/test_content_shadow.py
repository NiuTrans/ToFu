"""Demand-filled search cache: full results, freshness, retention and failure."""
import os
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from lib.project_mod import content_shadow as shadow, read_tools, tree_index

pytestmark = [pytest.mark.unit, pytest.mark.skipif(os.name != 'posix', reason='POSIX cache leases')]


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'a.txt').write_text('alpha\nsecond alpha\n')
    (root / 'b.txt').write_text('beta\n')
    home = tmp_path / 'cache'
    home.mkdir(mode=0o700)
    monkeypatch.setenv('TOFU_CONTENT_SHADOW_DIR', str(home))
    monkeypatch.setattr(shadow, '_ENABLED', True)
    monkeypatch.setattr(shadow, '_MIN_FILE_BYTES', 0)
    real_time_ns = time.time_ns
    # The synthetic fixture is observed after the source quiet window; no
    # real sleep is needed for every cache-I/O contract below.
    monkeypatch.setattr(shadow.local_placement.time, 'time_ns',
                        lambda: real_time_ns() + 3_000_000_000)
    # Production warms after releasing a query lease. Tests drive that step
    # explicitly, so no background threads escape their fixture lifetime.
    monkeypatch.setattr(shadow, 'warm', lambda *a, **k: None)
    return str(root), home


def _fill(project, paths=None):
    root, home = project
    candidates = paths or [str(Path(root) / 'a.txt'), str(Path(root) / 'b.txt')]
    shadow._sync_worker(root, candidates, home)
    return candidates


@pytest.mark.parametrize('context,count,cap', [(0, False, 100), (1, False, 1), (0, True, 100)])
def test_mixed_cache_source_matches_live_output_and_remainder(project, context, count, cap,
                                                             monkeypatch, tmp_path):
    # rg emits explicit multi-file results in parallel completion order, so
    # live and cached output are byte-identical only when the file order
    # matches; production keeps that parallelism. Pin argv order for this
    # equivalence comparison instead of asserting a scheduler accident.
    config = tmp_path / 'ripgrep.conf'
    config.write_text('--threads=1\n')
    monkeypatch.setenv('RIPGREP_CONFIG_PATH', str(config))
    root, _home = project
    candidates = [str(Path(root) / name) for name in ('a.txt', 'b.txt')]
    _fill(project, candidates[:1])
    live = read_tools._run_index_chunks(root, 'a', context, cap, count, candidates,
                                       time.monotonic() + 30)
    with shadow.candidate_view(root, candidates) as view:
        assert view.paths[0] != candidates[0] and view.paths[1] == candidates[1]
        cached = read_tools._run_index_chunks(root, 'a', context, cap, count, view.paths,
                                             time.monotonic() + 30)
        assert view.current()
        assert view.rewrite(cached[0]) == live[0]
        assert view.rewrite_withheld(cached[3]) == live[3]


def test_same_size_edit_preserving_mtime_is_not_a_cache_hit(project):
    candidates = _fill(project)
    target = Path(candidates[0])
    before = target.stat()
    target.write_bytes(b'omega\nsecond omega\n')
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    with shadow.candidate_view(project[0], candidates) as view:
        assert view.paths[0] == candidates[0]
        assert view.paths[1] != candidates[1]
    _fill(project)
    with shadow.candidate_view(project[0], candidates) as view:
        assert Path(view.paths[0]).read_bytes() == target.read_bytes()


def test_post_search_freshness_detects_edit_delete_and_replacement(project):
    candidates = _fill(project)
    with shadow.candidate_view(project[0], candidates) as view:
        assert view.current()
        Path(candidates[0]).unlink()
        assert not view.current()


def test_cache_pressure_never_removes_leased_data_or_source(project, monkeypatch):
    root, home = project
    candidates = _fill(project)
    monkeypatch.setattr(shadow, '_GLOBAL_MAX_BYTES', 0)
    with shadow.candidate_view(root, candidates) as view:
        shadow._prune(home)
        shadow.invalidate(root)
        assert all(Path(path).exists() for path in view.paths)
    shadow._prune(home)
    assert not shadow._root_dir(root, home).exists()
    assert all(Path(path).exists() for path in candidates)


def test_large_file_and_tiny_cache_keep_complete_live_candidates(project, monkeypatch):
    root, home = project
    large = Path(root) / 'large.dat'
    with large.open('wb') as stream:
        stream.truncate(100 * 1024 ** 3)
    candidates = [str(Path(root) / 'a.txt'), str(large)]
    monkeypatch.setattr(shadow, '_ROOT_MAX_BYTES', 8192)
    shadow._sync_worker(root, candidates, home)
    with shadow.candidate_view(root, candidates) as view:
        assert view.paths[0] != candidates[0]
        assert view.paths[1] == str(large)
    manifest = shadow._manifest(shadow._root_dir(root, home))
    assert list(manifest['entries']) == ['a.txt']
    monkeypatch.setattr(shadow.local_placement, 'has_free_space', lambda *args: False)
    shadow._sync_worker(root, [str(Path(root) / 'b.txt')], home)
    assert all(Path(path).exists() for path in candidates)


@pytest.mark.parametrize('failure', ['lost', 'corrupt', 'symlink'])
def test_cache_loss_corruption_and_symlink_fall_back(project, failure):
    root, home = project
    candidates = _fill(project)
    cached = shadow._cached_path(shadow._root_dir(root, home), 'a.txt')
    if failure == 'corrupt':
        before = cached.stat()
        cached.write_bytes(b'xxxxx\nxxxxxx xxxxx\n')
        os.utime(cached, ns=(before.st_atime_ns, before.st_mtime_ns))
    else:
        cached.unlink()
        if failure == 'symlink':
            cached.symlink_to(candidates[1])
    with shadow.candidate_view(root, candidates) as view:
        assert view.paths[0] == candidates[0]
    assert Path(candidates[1]).read_text() == 'beta\n'


def test_copy_race_cannot_publish_mismatched_bytes(project, monkeypatch):
    root, home = project
    target = Path(root) / 'a.txt'
    original = shadow._copy

    def race(root, path, directory, relative, identity):
        target.write_text('new bytes\n')
        return original(root, path, directory, relative, identity)

    monkeypatch.setattr(shadow, '_copy', race)
    shadow._sync_worker(root, [str(target)], home)
    with shadow.candidate_view(root, [str(target)]) as view:
        assert view.paths == [str(target)]


def test_recovery_reclaims_only_owned_staging_and_unreferenced_blobs(project):
    root, home = project
    candidates = _fill(project)
    directory = shadow._root_dir(root, home)
    orphan = directory / (64 * 'a')
    orphan.write_text('unused')
    stage = directory / '.copy-interrupted'
    stage.write_text('partial')
    unrelated = directory / 'operator-note.txt'
    unrelated.write_text('keep')
    shadow._sync_worker(root, candidates, home)
    assert not orphan.exists() and not stage.exists()
    assert unrelated.read_text() == 'keep'


def test_rewrite_preserves_matched_content_even_when_it_contains_cache_path(project):
    candidates = _fill(project)
    with shadow.candidate_view(project[0], candidates) as view:
        cached = os.fsencode(view.paths[0])
        raw = cached + b'\0' + b'1:' + cached + b'\n'
        assert view.rewrite(raw) == os.fsencode(candidates[0]) + b'\0' + b'1:' + cached + b'\n'


def test_search_retries_live_when_source_changes_during_cached_search(project, monkeypatch):
    root, _home = project
    candidates = _fill(project)
    entry = tree_index.TreeIndex(root, ['a.txt', 'b.txt'], [19, 5], [0, 0], time.time(), True)
    monkeypatch.setattr(tree_index, 'acquire', lambda _: entry)
    monkeypatch.setattr(tree_index, 'covers', lambda *_: True)
    monkeypatch.setattr(tree_index, 'grep_candidates', lambda *_: (candidates, 24))
    calls = []
    original = read_tools._run_index_chunks

    def search(*args, **kwargs):
        calls.append(args[5])
        result = original(*args, **kwargs)
        if len(calls) == 1:
            Path(candidates[0]).write_text('alpha CURRENT\n')
        return result

    monkeypatch.setattr(read_tools, '_run_index_chunks', search)
    result = read_tools._grep_via_index(root, root, 'alpha', None, 0, 100, False, 30)
    assert 'CURRENT' in result and 'second alpha' not in result
    assert calls[0][0] != candidates[0] and calls[1] == candidates


def test_one_off_search_does_not_start_copying_and_heat_expires(project, monkeypatch):
    root, _home = project
    target = str(Path(root) / 'a.txt')
    calls = []
    monkeypatch.setattr(shadow, 'warm', lambda _root, pending, **_: calls.append(pending))
    for _ in range(3):
        with shadow.candidate_view(root, [target]):
            pass
    assert calls == [[], [], [target]]
    now = time.monotonic()
    monkeypatch.setattr(shadow.time, 'monotonic', lambda: now + shadow._HEAT_TTL_SECONDS + 1)
    with shadow.candidate_view(root, [target]):
        pass
    assert calls[-1] == []


def test_huge_file_prefix_does_not_starve_hot_code_admission(project, monkeypatch):
    root, _home = project
    paths = [str(Path(root) / name) for name in ('big.dat', 'code.py')]
    entry = tree_index.TreeIndex(root, ['big.dat', 'code.py'], [100 * 1024 ** 3, 10],
                                 [0, 0], time.time(), True)
    monkeypatch.setattr(shadow, '_SYNC_ROUND_FILES', 1)
    calls = []
    monkeypatch.setattr(shadow, 'warm', lambda _root, pending, **_: calls.append(pending))
    for _ in range(3):
        with shadow.candidate_view(root, paths, entry=entry):
            pass
    assert calls[-1] == [paths[1]]


def test_empty_file_metadata_counts_towards_cache_retention(project, monkeypatch):
    root, home = project
    candidates = []
    for index in range(10):
        path = Path(root) / f'empty-{index}'
        path.touch()
        candidates.append(str(path))
    monkeypatch.setattr(shadow, '_ROOT_MAX_BYTES', 1024)
    shadow._sync_worker(root, candidates, home)
    manifest = shadow._manifest(shadow._root_dir(root, home))
    assert sum(state['storageBytes'] for state in manifest['entries'].values()) <= 1024
    assert len(manifest['entries']) < 10
    assert all(Path(path).exists() for path in candidates)


def test_coarse_timestamps_must_leave_same_second_write_window():
    check = shadow.local_placement.source_timestamp_is_stable
    second = 100 * 1_000_000_000
    assert not check(second, second, now_ns=second + 999_999_999)
    assert not check(second, second, now_ns=second + 1_999_999_999)
    assert check(second, second, now_ns=second + 2_000_000_000)
    assert not check(second + 1, second + 1, now_ns=second + 2)
    assert check(second + 1, second + 1, now_ns=second + 2_000_000_001)
    assert not check(second * 2, second, now_ns=second + 3_000_000_000)


def test_source_churn_backs_off_cache_without_blocking_search(project, monkeypatch):
    root, _home = project
    candidates = _fill(project)
    with shadow.candidate_view(root, candidates) as view:
        Path(candidates[0]).write_text('new content\n')
        assert not view.current()
    _fill(project)
    with shadow.candidate_view(root, candidates) as view:
        assert view.paths == candidates
    now = time.monotonic()
    monkeypatch.setattr(shadow.time, 'monotonic', lambda: now + 61)
    with shadow.candidate_view(root, candidates) as view:
        assert len(view.replacements) == 2


def test_outside_candidates_never_get_copied(project, tmp_path):
    root, home = project
    other = tmp_path / 'private.txt'
    other.write_text('private')
    with shadow.candidate_view(root, [str(other)]) as view:
        assert view.paths == [str(other)] and not view.replacements
    assert not shadow._root_dir(root, home).exists()


def test_cache_checksum_detects_damage_with_matching_metadata(project):
    root, home = project
    candidates = _fill(project)
    directory = shadow._root_dir(root, home)
    target = shadow._cached_path(directory, 'a.txt')
    target.write_text('omega\nsecond omega\n')
    manifest = shadow._manifest(directory)
    metadata = target.stat()
    manifest['entries']['a.txt']['cachedTimes'] = [metadata.st_mtime_ns, metadata.st_ctime_ns]
    shadow._publish(directory, manifest)
    with shadow.candidate_view(root, candidates) as view:
        assert view.paths[0] == candidates[0]
        assert Path(candidates[0]).read_text() == 'alpha\nsecond alpha\n'


def test_tiny_files_use_live_reads_without_creating_content_copies(project, monkeypatch):
    root, home = project
    monkeypatch.setattr(shadow, '_MIN_FILE_BYTES', 64 * 1024)
    candidates = _fill(project)
    assert not shadow._manifest(shadow._root_dir(root, home))['entries']
    with shadow.candidate_view(root, candidates) as view:
        assert view.paths == candidates


def test_other_process_lease_survives_pressure_and_crash_releases_it(project, monkeypatch):
    import select
    import subprocess
    import sys

    root, home = project
    candidates = _fill(project)
    program = '''
import sys
from pathlib import Path
from lib.project_mod.content_shadow import _lease
with _lease(Path(sys.argv[1])) as held:
    print('held' if held else 'miss', flush=True)
    sys.stdin.readline()
'''
    process = subprocess.Popen(
        [sys.executable, '-c', program, str(shadow._root_dir(root, home))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        ready, _, _ = select.select([process.stdout], [], [], 20)
        assert ready and process.stdout.readline() == b'held\n'
        monkeypatch.setattr(shadow, '_GLOBAL_MAX_BYTES', 0)
        shadow._prune(home)
        assert shadow._root_dir(root, home).is_dir()
        process.kill()  # fault injection into this test-owned reader only
        process.wait(timeout=20)
        shadow._prune(home)
        assert not shadow._root_dir(root, home).exists()
        assert all(Path(path).is_file() for path in candidates)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=20)


@pytest.mark.parametrize('lost', [False, True])
def test_lost_or_corrupt_manifest_rebuilds_only_proven_owned_cache(project, lost):
    root, home = project
    candidates = _fill(project)
    directory = shadow._root_dir(root, home)
    if lost:
        (directory / 'manifest.json').unlink()
    else:
        (directory / 'manifest.json').write_text('{broken')
    shadow._sync_worker(root, candidates, home)
    with shadow.candidate_view(root, candidates) as view:
        assert len(view.replacements) == len(candidates)
    assert all(Path(path).exists() for path in candidates)


def test_unowned_directory_is_not_adopted_or_deleted(project):
    root, home = project
    directory = shadow._root_dir(root, home)
    directory.mkdir(mode=0o700)
    note = directory / 'user-data.txt'
    note.write_text('keep')
    _fill(project)
    shadow._prune(home)
    shadow.invalidate(root)
    assert note.read_text() == 'keep'
    assert not (directory / '.owner.json').exists()


def test_manifest_fifo_is_a_cache_miss_without_waiting_for_a_writer(project):
    root, home = project
    candidates = _fill(project)
    manifest = shadow._root_dir(root, home) / 'manifest.json'
    manifest.unlink()
    os.mkfifo(manifest)
    with shadow.candidate_view(root, candidates) as view:
        assert view.paths == candidates


def test_binary_notice_rewrites_only_its_filename(project):
    root, home = project
    path = Path(root) / 'binary.txt'
    path.write_bytes(b'alpha\n\x00data\n')
    candidates = [str(path)]
    shadow._sync_worker(root, candidates, home)
    live = read_tools._run_index_chunks(root, 'alpha', 0, 100, False,
                                        candidates, time.monotonic() + 30)
    with shadow.candidate_view(root, candidates) as view:
        assert view.replacements
        cached = read_tools._run_index_chunks(root, 'alpha', 0, 100, False,
                                              view.paths, time.monotonic() + 30)
        assert view.rewrite(cached[0]) == live[0]


@pytest.mark.parametrize('alias', [False, True])
@pytest.mark.parametrize('override', [False, True])
def test_cache_inside_source_is_refused_before_creation(project, tmp_path, monkeypatch, alias, override):
    root, _home = project
    parent = Path(root)
    if alias:
        parent = tmp_path / 'alias'
        parent.symlink_to(root, target_is_directory=True)
    destination = parent / 'content-cache'
    if override:
        monkeypatch.setenv('TOFU_CONTENT_SHADOW_DIR', str(destination))
    else:
        monkeypatch.delenv('TOFU_CONTENT_SHADOW_DIR')
        monkeypatch.setattr(shadow.local_placement, 'index_cache_placement',
                            lambda _: SimpleNamespace(selected=True, root=destination))
    candidates = [str(Path(root) / 'a.txt')]
    with shadow.candidate_view(root, candidates) as view:
        assert view.paths == candidates
    assert not destination.exists()
