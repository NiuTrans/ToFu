"""Tests for the persistent tree index (lib/project_mod/tree_index.py) and its
integration into grep_search / find_files (lib/project_mod/read_tools.py).

The index exists to keep search tools off the live directory walk (FUSE /
cross-DC mounts turn a full-tree walk into a >60s timeout). These tests pin:
  * build / disk-persistence / LRU acquire semantics,
  * find_files served from the index (format + glob parity),
  * grep_search served from the index (match parity, include glob, count_only,
    context lines, subdirectory scoping, hidden/ignore rules),
  * write-hook freshness (note_write) and .gitignore invalidation,
  * honest fallback to the live walk when the index is absent/disabled.
"""

_AUDIT_SYNTHETIC_REPO_PATHS = {'docs/guide.md', 'scripts/keep.py'}

import os
import threading
import time
from types import SimpleNamespace

import pytest

from lib.project_mod import read_tools, tree_index

pytestmark = pytest.mark.unit


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    """A small project tree on local disk + index disk-state redirected to tmp."""
    idx_store = tmp_path / '.idx-store'
    idx_store.mkdir()
    (tmp_path / 'src' / 'pkg').mkdir(parents=True)
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'node_modules' / 'junk').mkdir(parents=True)
    (tmp_path / '.git').mkdir()
    (tmp_path / 'src' / 'pkg' / 'alpha.py').write_text(
        'def alpha():\n    return "needle-one"\n\nclass Beta:\n    pass\n')
    (tmp_path / 'src' / 'pkg' / 'beta.py').write_text(
        'import alpha\n\n# needle-two\nx = 1\n')
    (tmp_path / 'src' / 'top.py').write_text('print("needle-three")\n')
    (tmp_path / 'docs' / 'guide.md').write_text('# Guide\nneedle-four in docs\n')
    (tmp_path / 'node_modules' / 'junk' / 'dep.js').write_text('// needle-ignored\n')
    (tmp_path / '.hidden.py').write_text('needle-hidden\n')
    (tmp_path / '.git' / 'config').write_text('needle-git\n')
    monkeypatch.setattr(tree_index, '_index_dir', lambda: str(idx_store))
    # Fresh state per test: the module keeps process-global memory.
    monkeypatch.setattr(tree_index, '_mem', {})
    monkeypatch.setattr(tree_index, '_building', set())
    return str(tmp_path)


def _build(root):
    """Synchronously build the index for *root*."""
    tree_index._build_sync(root)


class TestBuildAndAcquire:
    def test_build_indexes_expected_files(self, proj):
        _build(proj)
        entry = tree_index.acquire(proj)
        assert entry is not None and entry.complete
        paths = set(entry.paths)
        assert 'src/pkg/alpha.py' in paths
        assert 'docs/guide.md' in paths
        # IGNORE_DIRS + hidden entries are excluded (rg/fd parity).
        assert 'node_modules/junk/dep.js' not in paths
        assert '.hidden.py' not in paths
        assert '.git/config' not in paths

    def test_disk_persistence_roundtrip(self, proj, monkeypatch):
        _build(proj)
        on_disk = set(tree_index.acquire(proj).paths)
        # Drop memory; acquire must reload from the persisted blob.
        monkeypatch.setattr(tree_index, '_mem', {})
        entry = tree_index.acquire(proj)
        assert entry is not None
        assert set(entry.paths) == on_disk
        assert list(entry.sizes) and all(isinstance(s, int) for s in entry.sizes)

    def test_oversized_disk_index_is_rejected_before_body_read(
            self, monkeypatch):
        header = tree_index.struct.pack(
            '<8sdIH', tree_index._DISK_MAGIC, time.time(), 600_001, 0)
        read_sizes = []

        class HeaderOnlyFile:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1):
                read_sizes.append(size)
                if len(read_sizes) > 1:
                    pytest.fail('oversized index body must not be read')
                return header

        monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'distributed')
        monkeypatch.setattr(tree_index, '_disk_path', lambda _root: '/ignored')
        monkeypatch.setattr(
            tree_index, 'open', lambda *_args, **_kwargs: HeaderOnlyFile(),
            raising=False)

        assert tree_index._load_disk('/project') is None
        assert read_sizes == [len(header)]

    def test_stale_entry_served_while_refresh_kicked(self, proj, monkeypatch):
        _build(proj)
        entry = tree_index.acquire(proj)
        # Age the entry past STALE_AFTER but below MAX_AGE.
        entry.built_at = time.time() - tree_index._stale_after_s() - 1
        assert tree_index.acquire(proj) is entry  # stale-while-revalidate

    def test_ancient_entry_not_served(self, proj, monkeypatch):
        _build(proj)
        entry = tree_index.acquire(proj)
        entry.built_at = time.time() - tree_index._max_age_s() - 5
        monkeypatch.setattr(tree_index, '_building', set())
        monkeypatch.setattr(tree_index, '_builder',
                            _NullBuilder())  # never actually rebuilds
        monkeypatch.setattr(tree_index, '_scanner', _NullBuilder())
        assert tree_index.acquire(proj) is None

    def test_disabled_env_shortcircuits(self, proj, monkeypatch):
        monkeypatch.setenv('TOFU_TREE_INDEX_DISABLE', '1')
        assert tree_index.acquire(proj) is None
        tree_index.warm(proj)
        assert tree_index._mem == {}


class _NullBuilder:
    def submit(self, *_a, **_k):
        pass


def test_warm_builder_retires_between_batches_and_rebuilds_capacity(
        tmp_path, monkeypatch):
    roots = [tmp_path / 'first', tmp_path / 'second']
    for root in roots:
        root.mkdir()

    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = 0
    calls_lock = threading.Lock()

    def controlled_build(_root, *, scan_executor=None):
        nonlocal calls
        assert scan_executor is not None
        with calls_lock:
            batch = calls // 2
            calls += 1
            if calls % 2 == 0:
                entered[batch].set()
        release[batch].wait(1)

    monkeypatch.setattr(tree_index, '_builder', None)
    monkeypatch.setattr(tree_index, '_scanner', None)
    monkeypatch.setattr(tree_index, '_building', set())
    monkeypatch.setattr(tree_index, '_build_sync', controlled_build)

    try:
        prior_threads = None
        for batch in range(2):
            for root in roots:
                tree_index.warm(str(root))
            assert entered[batch].wait(1)
            snapshot = tree_index.background_builder_snapshot()
            assert snapshot['activeBuilds'] == 2
            assert snapshot['executorActive'] is True
            assert snapshot['residentThreads'] == 2
            assert snapshot['scanExecutorActive'] is True
            assert snapshot['scanResidentThreads'] == 0
            with tree_index._lock:
                current_builder = tree_index._builder
                current_threads = tuple(current_builder._threads)
            if prior_threads is not None:
                assert not set(current_threads).intersection(prior_threads)

            release[batch].set()
            deadline = time.monotonic() + 1
            while (tree_index.background_builder_snapshot()['activeBuilds']
                   or tree_index.background_builder_snapshot()[
                       'executorActive']
                   or any(thread.is_alive() for thread in current_threads)):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            prior_threads = current_threads
    finally:
        for event in release:
            event.set()


def test_parallel_root_builds_share_one_scan_thread_budget(
        tmp_path, monkeypatch):
    roots = [tmp_path / 'first', tmp_path / 'second']
    for root in roots:
        root.mkdir()

    builds_entered = threading.Event()
    release_scans = threading.Event()
    state_lock = threading.Lock()
    build_count = 0
    active_scans = 0
    peak_scans = 0
    scan_thread_names = set()

    def scan_work():
        nonlocal active_scans, peak_scans
        with state_lock:
            active_scans += 1
            peak_scans = max(peak_scans, active_scans)
            scan_thread_names.add(threading.current_thread().name)
        try:
            release_scans.wait(2)
        finally:
            with state_lock:
                active_scans -= 1

    def controlled_build(_root, *, scan_executor=None):
        nonlocal build_count
        assert scan_executor is not None
        futures = [scan_executor.submit(scan_work) for _ in range(6)]
        with state_lock:
            build_count += 1
            if build_count == 2:
                builds_entered.set()
        for future in futures:
            future.result()

    monkeypatch.setenv('TOFU_TREE_INDEX_WALK_JOBS', '3')
    monkeypatch.setattr(tree_index, '_builder', None)
    monkeypatch.setattr(tree_index, '_scanner', None)
    monkeypatch.setattr(tree_index, '_building', set())
    monkeypatch.setattr(tree_index, '_build_sync', controlled_build)

    try:
        for root in roots:
            tree_index.warm(str(root))
        assert builds_entered.wait(1)
        snapshot = tree_index.background_builder_snapshot()
        assert snapshot['activeBuilds'] == 2
        assert snapshot['scanExecutorActive'] is True
        assert snapshot['scanWorkerCapacity'] == 3
        assert snapshot['scanResidentThreads'] == 3

        with tree_index._lock:
            builder = tree_index._builder
            scanner = tree_index._scanner
            resident_threads = tuple(builder._threads) + tuple(scanner._threads)

        release_scans.set()
        deadline = time.monotonic() + 2
        while (tree_index.background_builder_snapshot()['executorActive']
               or tree_index.background_builder_snapshot()[
                   'scanExecutorActive']
               or any(thread.is_alive() for thread in resident_threads)):
            assert time.monotonic() < deadline
            time.sleep(0.01)

        assert peak_scans == 3
        assert len(scan_thread_names) == 3
    finally:
        release_scans.set()


def test_tree_index_resource_overrides_are_hard_bounded(monkeypatch):
    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'distributed')
    monkeypatch.setenv('TOFU_TREE_INDEX_WALK_JOBS', '999999')
    monkeypatch.setenv('TOFU_TREE_INDEX_MAX_ENTRIES', '999999999')
    monkeypatch.setenv('TOFU_TREE_INDEX_MEM_ROOTS', '999999')

    assert tree_index._walk_jobs() == 16
    assert tree_index._max_entries() == 600_000
    assert tree_index._mem_roots() == 8


def test_memory_entry_budget_is_process_wide(monkeypatch):
    monkeypatch.setenv('TOFU_TREE_INDEX_MAX_ENTRIES', '10000')
    monkeypatch.setenv('TOFU_TREE_INDEX_MEM_ROOTS', '8')
    monkeypatch.setattr(tree_index, '_mem', {
        'oldest': SimpleNamespace(paths=[None] * 4_000),
        'middle': SimpleNamespace(paths=[None] * 4_000),
        'newest': SimpleNamespace(paths=[None] * 4_000),
    })

    tree_index._evict_mem_over_cap()

    assert tuple(tree_index._mem) == ('middle', 'newest')
    assert sum(len(entry.paths) for entry in tree_index._mem.values()) == 8_000


class TestFind:
    def test_glob_and_format(self, proj):
        _build(proj)
        out = read_tools.tool_find_files(proj, '*.py')
        assert 'src/pkg/alpha.py' in out
        assert 'src/top.py' in out
        assert 'docs/guide.md' not in out
        assert '3 found' in out
        # Size annotation parity with the fd/python walkers.
        assert 'src/pkg/alpha.py (' in out

    def test_case_insensitive_basename(self, proj):
        _build(proj)
        out = read_tools.tool_find_files(proj, '*.PY')
        assert 'src/pkg/alpha.py' in out

    def test_explicit_case_sensitive_basename(self, proj):
        _build(proj)
        out = read_tools.tool_find_files(
            proj, '*.PY', case_sensitive=True)
        assert 'No files matching' in out

    def test_subdirectory_scope(self, proj):
        _build(proj)
        out = read_tools.tool_find_files(proj, '*.py', rel_path='src/pkg')
        assert 'src/pkg/alpha.py' in out
        assert 'src/top.py' not in out

    def test_relpath_glob(self, proj):
        _build(proj)
        out = read_tools.tool_find_files(proj, 'docs/*.md')
        assert 'docs/guide.md' in out
        assert 'src/top.py' not in out

    def test_cap_and_no_match(self, proj):
        _build(proj)
        out = read_tools.tool_find_files(proj, '*.py', max_results=1)
        assert '(1 found)' in out
        assert 'results capped at 1' in out
        exact = read_tools.tool_find_files(proj, '*.py', max_results=3)
        assert '(3 found)' in exact
        assert 'results capped' not in exact
        assert 'No files matching' in read_tools.tool_find_files(proj, '*.rs')

    def test_shell_compatible_search_does_not_use_filtered_index(
            self, proj, monkeypatch):
        _build(proj)
        monkeypatch.setattr(read_tools, '_FD_BIN', None)
        out = read_tools.tool_find_files(
            proj,
            '*.py',
            max_results=20,
            case_sensitive=True,
            shell_output=True,
            respect_project_ignores=False,
        )
        assert './.hidden.py' in out
        assert './src/pkg/alpha.py' in out

    def test_python_fallback_has_explicit_scan_budget(self, proj, monkeypatch):
        monkeypatch.setattr(read_tools, '_FD_BIN', None)
        monkeypatch.setattr(read_tools, '_FIND_SCAN_LIMIT', 2)
        out = read_tools.tool_find_files(
            proj,
            '*',
            max_results=20,
            shell_output=True,
            respect_project_ignores=False,
        )
        assert 'find: [search stopped after scanning 2 entries' in out

    def test_python_fallback_reports_depth_budget(self, proj, monkeypatch):
        monkeypatch.setattr(read_tools, '_FD_BIN', None)
        monkeypatch.setattr(read_tools, '_TOOL_MAX_DEPTH', 0)
        out = read_tools.tool_find_files(
            proj,
            '*',
            max_results=20,
            shell_output=True,
            respect_project_ignores=False,
        )
        assert 'find: [search depth capped at 0' in out

    def test_fd_timeout_does_not_double_scan(self, proj, monkeypatch):
        monkeypatch.setattr(read_tools, '_FD_BIN', '/fake/fd')
        monkeypatch.setattr(
            read_tools.subprocess,
            'run',
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                read_tools.subprocess.TimeoutExpired('/fake/fd', 30)),
        )
        monkeypatch.setattr(
            read_tools,
            '_python_find',
            lambda *_args, **_kwargs: pytest.fail(
                'fd timeout must not start a second full-tree scan'),
        )
        out = read_tools.tool_find_files(
            proj,
            '*.py',
            shell_output=True,
            respect_project_ignores=False,
        )
        assert 'find: [search timed out after' in out

    def test_shell_mutation_invalidates_index(self, proj):
        from lib.project_mod.tools import execute_standalone_command
        _build(proj)
        assert tree_index.acquire(proj) is not None
        execute_standalone_command(
            'run_command',
            {'command': "printf 'fresh' > fresh.py"},
            working_dir=proj,
        )
        assert tree_index.acquire(proj) is None
        assert 'fresh.py' in read_tools.tool_find_files(proj, 'fresh.py')

    def test_fallback_when_no_index(self, proj):
        # No build: fd/python walker path must still answer correctly.
        out = read_tools.tool_find_files(proj, '*.md')
        assert 'docs/guide.md' in out


class TestGrep:
    def test_matches_parity_with_live_walk(self, proj):
        indexed = read_tools.tool_grep(proj, 'needle')
        live = read_tools.tool_grep(proj, 'needle')  # second call — also indexed
        for out in (indexed, live):
            assert 'src/pkg/alpha.py:2:' in out
            assert 'src/pkg/beta.py:3:' in out
            assert 'src/top.py:1:' in out
            assert 'docs/guide.md:2:' in out
            # Ignored/hidden trees stay out.
            assert 'node_modules' not in out
            assert '.hidden' not in out
        _build(proj)  # now pin the explicit index path too
        via_index = read_tools.tool_grep(proj, 'needle')
        assert '4 matches' in via_index
        assert 'src/pkg/alpha.py:2:' in via_index

    def test_include_glob(self, proj):
        _build(proj)
        out = read_tools.tool_grep(proj, 'needle', include='*.md')
        assert 'docs/guide.md' in out
        assert 'src/pkg/alpha.py' not in out

    def test_subdirectory_scope(self, proj):
        _build(proj)
        out = read_tools.tool_grep(proj, 'needle', rel_path='src')
        assert 'src/top.py' in out
        assert 'docs/guide.md' not in out

    def test_count_only(self, proj):
        _build(proj)
        out = read_tools.tool_grep(proj, 'needle', count_only=True)
        assert '4 matches (count only)' in out

    def test_context_lines(self, proj):
        _build(proj)
        out = read_tools.tool_grep(proj, 'needle-two', context_lines=2)
        assert 'import alpha' in out  # -C 2 pulls surrounding lines
        assert 'x = 1' in out

    def test_no_matches(self, proj):
        _build(proj)
        out = read_tools.tool_grep(proj, 'definitely-absent-xyz')
        assert 'No matches found' in out

    def test_case_insensitive_via_index(self, proj):
        """Pins the rg-flag regression class: GNU `-s` (no-messages) vs rg `-s`
        (case-SENSITIVE) — the index chunk argv must stay case-insensitive."""
        _build(proj)
        out = read_tools.tool_grep(proj, 'NEEDLE-ONE')
        assert 'src/pkg/alpha.py:2:' in out
        out = read_tools.tool_grep(proj, 'NEEDLE', count_only=True)
        assert '4 matches (count only)' in out

    def test_single_explicit_file_uses_live_semantics(self, proj):
        _build(proj)
        # A hidden file is NOT in the index, but an explicit file operand must
        # still be searched (rg single-file behavior).
        out = read_tools.tool_grep(proj, 'needle-hidden', rel_path='.hidden.py')
        assert 'needle-hidden' in out

    def test_regex_pattern(self, proj):
        _build(proj)
        out = read_tools.tool_grep(proj, r'needle-(one|two)')
        assert 'src/pkg/alpha.py' in out
        assert 'src/pkg/beta.py' in out
        assert 'src/top.py' not in out

    def test_fallback_when_disabled(self, proj, monkeypatch):
        monkeypatch.setenv('TOFU_TREE_INDEX_DISABLE', '1')
        out = read_tools.tool_grep(proj, 'needle')
        assert 'src/pkg/alpha.py:2:' in out


class TestFreshness:
    def test_note_write_upserts_immediately(self, proj):
        _build(proj)
        assert os.path.isfile(tree_index._disk_path(proj))
        new_file = os.path.join(proj, 'src', 'fresh.py')
        with open(new_file, 'w') as f:
            f.write('needle-five\n')
        tree_index.note_write(new_file)
        assert not os.path.exists(tree_index._disk_path(proj))
        out = read_tools.tool_find_files(proj, 'fresh.py')
        assert 'src/fresh.py' in out
        out = read_tools.tool_grep(proj, 'needle-five')
        assert 'src/fresh.py:1:' in out

    def test_note_write_removes_vanished_file(self, proj):
        _build(proj)
        gone = os.path.join(proj, 'src', 'top.py')
        os.unlink(gone)
        tree_index.note_write(gone)  # stat fails → entry removed
        out = read_tools.tool_find_files(proj, 'top.py')
        assert 'No files matching' in out

    def test_gitignore_write_invalidates(self, proj):
        _build(proj)
        assert tree_index.acquire(proj) is not None
        gi = os.path.join(proj, '.gitignore')
        with open(gi, 'w') as f:
            f.write('docs/\n')
        tree_index.note_write(gi)
        assert tree_index._mem.get(proj) is None  # invalidated
        _build(proj)
        entry = tree_index.acquire(proj)
        assert 'docs/guide.md' not in set(entry.paths)

    def test_write_tool_hook_fires(self, proj, monkeypatch):
        """_record_write_freshness (the write-tools choke point) feeds the index."""
        from lib.project_mod.write_tools import _ops
        seen = []
        monkeypatch.setattr(tree_index, 'note_write', seen.append)
        _ops._record_write_freshness('conv-x', '/some/abs/file.py')
        assert seen == ['/some/abs/file.py']


class TestGitignoreCompile:
    def test_dir_pattern_prunes_subtree(self, tmp_path):
        (tmp_path / 'gen' / 'deep').mkdir(parents=True)
        (tmp_path / 'gen' / 'deep' / 'x.py').write_text('x')
        (tmp_path / 'keep.py').write_text('y')
        (tmp_path / '.gitignore').write_text('gen/\n')
        _build(str(tmp_path))
        entry = tree_index.acquire(str(tmp_path))
        assert set(entry.paths) == {'keep.py'}

    def test_negation_keeps_file(self, tmp_path):
        (tmp_path / 'a.log').write_text('x')
        (tmp_path / 'keep.log').write_text('x')
        (tmp_path / '.gitignore').write_text('*.log\n!keep.log\n')
        _build(str(tmp_path))
        entry = tree_index.acquire(str(tmp_path))
        assert 'a.log' not in set(entry.paths)
        assert 'keep.log' in set(entry.paths)

    def test_contents_level_rule_does_not_prune_parent_dir(self, tmp_path):
        """`/scripts/*` must not prune `scripts/` itself — git descends so the
        `!/scripts/keep.py` negation can resurrect whitelisted children."""
        (tmp_path / 'scripts').mkdir()
        (tmp_path / 'scripts' / 'keep.py').write_text('x')
        (tmp_path / 'scripts' / 'drop.py').write_text('x')
        (tmp_path / '.gitignore').write_text('/scripts/*\n!/scripts/keep.py\n')
        _build(str(tmp_path))
        paths = set(tree_index.acquire(str(tmp_path)).paths)
        assert 'scripts/keep.py' in paths
        assert 'scripts/drop.py' not in paths

    def test_nested_gitignore_scoped_to_subtree(self, tmp_path):
        (tmp_path / 'shots').mkdir()
        (tmp_path / 'shots' / '.gitignore').write_text('*.png\n')
        (tmp_path / 'shots' / 'a.png').write_text('x')
        (tmp_path / 'shots' / 'keep.txt').write_text('x')
        (tmp_path / 'other.png').write_text('x')  # outside the nested scope
        _build(str(tmp_path))
        paths = set(tree_index.acquire(str(tmp_path)).paths)
        assert 'shots/a.png' not in paths
        assert 'shots/keep.txt' in paths
        assert 'other.png' in paths

    def test_hidden_whitelist_resurrected(self, tmp_path):
        """rg parity: `!.env.example` resurrects a hidden file past the
        hidden-filter; `.env` itself stays hidden."""
        (tmp_path / '.env.example').write_text('x')
        (tmp_path / '.env').write_text('secret')
        (tmp_path / '.gitignore').write_text('.env\n.env.*\n!.env.example\n')
        _build(str(tmp_path))
        paths = set(tree_index.acquire(str(tmp_path)).paths)
        assert '.env.example' in paths
        assert '.env' not in paths

    def test_git_info_exclude_honored(self, tmp_path):
        (tmp_path / '.git' / 'info').mkdir(parents=True)
        (tmp_path / '.git' / 'info' / 'exclude').write_text('/scratch.md\n')
        (tmp_path / 'scratch.md').write_text('x')
        (tmp_path / 'kept.md').write_text('x')
        _build(str(tmp_path))
        paths = set(tree_index.acquire(str(tmp_path)).paths)
        assert 'scratch.md' not in paths
        assert 'kept.md' in paths

    def test_note_write_refuses_non_indexable_paths(self, proj):
        """Writes into ignored/hidden trees must NOT become searchable via
        the write hook (a rebuild would exclude them)."""
        _build(proj)
        sneaky = os.path.join(proj, 'node_modules', 'junk', 'sneaky.js')
        with open(sneaky, 'w') as f:
            f.write('needle-sneaky\n')
        hidden = os.path.join(proj, '.secret.py')
        with open(hidden, 'w') as f:
            f.write('needle-secret\n')
        tree_index.note_write(sneaky)
        tree_index.note_write(hidden)
        paths = set(tree_index.acquire(proj).paths)
        assert 'node_modules/junk/sneaky.js' not in paths
        assert '.secret.py' not in paths
        out = read_tools.tool_grep(proj, 'needle-sneaky')
        assert 'No matches found' in out
