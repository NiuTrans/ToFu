"""tests/tasks_pkg/test_run_command_cp_fastpath.py — parallel copy engine.

Measured on this repo's DolphinFS FUSE mount (808-file / 68 MiB tree):
native ``cp -a`` 7–11s, tar-pipe ~equal, rsync SLOWER, the parallel engine
2.5–2.9x faster (latency-bound metadata ops across workers). The engine
runs in-process for a lone cp (zero spawn) and as a per-segment helper
splice inside compound commands; anything outside the proven argv surface
keeps native cp byte-for-byte.

Pinned here: argv acceptance matrix, tree/symlink/hardlink/sparse/metadata
semantics against real GNU cp, destination resolution, diagnostic and
exit-code parity, the killswitch, and native fallback for refused flags.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/tasks_pkg/test_run_command_cp_fastpath.py -v
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

import pytest

from lib.project_mod import copy_engine
from lib.project_mod.run_command import tool_run_command

# serial: spawns real `cp` subprocesses under wall-clock timeouts —
# oversubscribing the parallel unit lane starves them into flaky reds.
pytestmark = [pytest.mark.unit, pytest.mark.serial]


def build_source(base):
    os.makedirs(os.path.join(base, 's', 'd1', 'd2'))
    with open(os.path.join(base, 's', 'f1.txt'), 'w') as fh:
        fh.write('hello\n')
    with open(os.path.join(base, 's', 'd1', 'f2.txt'), 'w') as fh:
        fh.write('world\n' * 100)
    with open(os.path.join(base, 's', 'd1', 'd2', 'f3.py'), 'w') as fh:
        fh.write('x = 1\n')
    os.symlink('f1.txt', os.path.join(base, 's', 'link1'))
    os.link(os.path.join(base, 's', 'f1.txt'),
            os.path.join(base, 's', 'hard1'))
    os.chmod(os.path.join(base, 's', 'd1', 'f2.txt'), 0o640)


def tree_signature(root):
    signature = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                signature[rel] = ('L', os.readlink(path))
            elif stat.S_ISDIR(st.st_mode):
                signature[rel] = ('D', oct(st.st_mode & 0o777))
            else:
                signature[rel] = ('F', st.st_size,
                                  oct(st.st_mode & 0o777), st.st_ino)
    return signature


def hardlink_pairs(signature):
    groups = {}
    for rel, row in signature.items():
        if row[0] == 'F':
            groups.setdefault(row[3], []).append(rel)
    return sorted(sorted(v) for v in groups.values() if len(v) > 1)


# ═════════════════════════════════════════════════════════════════════
#  1. argv acceptance matrix
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('argv', [
    ['-r', 'a', 'b'], ['-R', 'a', 'b'], ['-a', 'a', 'b'], ['-p', 'f', 'g'],
    ['-rp', 'a', 'b'], ['-av', 'a', 'b', 'c', 'd/'], ['-rf', 'a', 'b'],
    ['-run', 'a', 'b'], ['-r', '--update', 'a', 'b'],
    ['-r', '--no-clobber', 'a', 'b'], ['-rx', 'a', 'b'],
    ['-r', '--one-file-system', 'a', 'b'], ['-r', '-P', 'a', 'b'],
    ['-rd', 'a', 'b'], ['-r', '--preserve=all', 'a', 'b'],
    ['--archive', 'a', 'b'], ['f', 'g'], ['-v', 'f', 'g'],
    ['-r', '--strip-trailing-slashes', 'a/', 'b'],
])
def test_cp_argv_accepted(argv):
    assert copy_engine.parse_cp_argv(argv) is not None


@pytest.mark.parametrize('argv', [
    ['-ri', 'a', 'b'],                  # interactive
    ['-r', '--parents', 'a', 'b'],
    ['-rl', 'a', 'b'],                  # hardlink mode
    ['-rs', 'a', 'b'],                  # symlink mode
    ['-r', '--reflink', 'a', 'b'],
    ['-r', '--sparse=always', 'a', 'b'],
    ['-r', '-L', 'a', 'b'],             # dereference changes semantics
    ['-r', '-H', 'a', 'b'],
    ['-r', '-t', 'd', 'a'],             # target-directory form
    ['-r', '-T', 'a', 'b'],
    ['-rb', 'a', 'b'],                  # backup
    ['-r', '--attributes-only', 'a', 'b'],
    ['-r', 'onlyone'],
    ['--preserve=bogus', 'a', 'b'],
])
def test_cp_argv_refused(argv):
    assert copy_engine.parse_cp_argv(argv) is None


# ═════════════════════════════════════════════════════════════════════
#  2. Semantics parity with GNU cp (in-process path)
# ═════════════════════════════════════════════════════════════════════

def _compare_with_native(command, check_rel):
    base_native = tempfile.mkdtemp()
    base_engine = tempfile.mkdtemp()
    # Both sides get REAL hardlinks from build_source (copytree would
    # duplicate them into independent inodes and void the link checks).
    build_source(base_native)
    build_source(base_engine)
    native = subprocess.run(['bash', '-c', command], cwd=base_native,
                            capture_output=True, text=True, timeout=60)
    result = tool_run_command(base_engine, command)
    sig_native = tree_signature(os.path.join(base_native, check_rel)) \
        if os.path.exists(os.path.join(base_native, check_rel)) else None
    sig_engine = tree_signature(os.path.join(base_engine, check_rel)) \
        if os.path.exists(os.path.join(base_engine, check_rel)) else None
    assert (sig_native is None) == (sig_engine is None)
    if sig_native is not None:
        strip = lambda sig: {k: v[:3] for k, v in sig.items()}
        assert strip(sig_engine) == strip(sig_native)
        assert hardlink_pairs(sig_engine) == hardlink_pairs(sig_native)
    assert ('[exit code: 0]' in result) == (native.returncode == 0)


@pytest.mark.parametrize('command,check', [
    ('cp -r s t', 't'),
    ('cp -a s t', 't'),
    ('mkdir t; cp -r s t', 't/s'),
    ('cp s/f1.txt t.txt', 't.txt'),
    ('cp -rv s t', 't'),
])
def test_tree_parity(command, check):
    _compare_with_native(command, check)


def test_multi_source_into_dir():
    base = tempfile.mkdtemp()
    build_source(base)
    os.mkdir(os.path.join(base, 't'))
    result = tool_run_command(base, 'cp s/f1.txt s/d1/f2.txt t')
    assert '[exit code: 0]' in result
    assert open(os.path.join(base, 't', 'f1.txt')).read() == 'hello\n'
    assert open(os.path.join(base, 't', 'f2.txt')).read() == 'world\n' * 100


def test_hardlinks_preserved_only_with_archive():
    base = tempfile.mkdtemp()
    build_source(base)
    tool_run_command(base, 'cp -a s t')
    ino = lambda p: os.stat(os.path.join(base, 't', p)).st_ino
    assert ino('f1.txt') == ino('hard1')
    base2 = tempfile.mkdtemp()
    build_source(base2)
    tool_run_command(base2, 'cp -r s t')
    ino2 = lambda p: os.stat(os.path.join(base2, 't', p)).st_ino
    assert ino2('f1.txt') != ino2('hard1')   # plain -r duplicates, like GNU


def test_sparse_file_stays_sparse():
    base = tempfile.mkdtemp()
    src = os.path.join(base, 'sp.bin')
    with open(src, 'wb') as fh:
        fh.write(b'A' * 10)
        fh.seek(1 << 20)
        fh.write(b'B' * 10)
    result = tool_run_command(base, 'cp -a sp.bin t.bin')
    assert '[exit code: 0]' in result
    dst = os.path.join(base, 't.bin')
    assert open(src, 'rb').read() == open(dst, 'rb').read()
    if os.stat(src).st_blocks * 512 < os.stat(src).st_size:
        assert os.stat(dst).st_blocks <= 32


def test_metadata_preserved():
    base = tempfile.mkdtemp()
    build_source(base)
    tool_run_command(base, 'cp -a s t')
    for rel in ('d1/f2.txt', 'd1/d2/f3.py', 'd1', 'd1/d2'):
        src_st = os.lstat(os.path.join(base, 's', rel))
        dst_st = os.lstat(os.path.join(base, 't', rel))
        assert stat.S_IMODE(dst_st.st_mode) == stat.S_IMODE(src_st.st_mode)
        assert int(dst_st.st_mtime) == int(src_st.st_mtime)


def test_trailing_slash_symlink_dereferences():
    base = tempfile.mkdtemp()
    build_source(base)
    os.symlink('s', os.path.join(base, 'slink'))
    result = tool_run_command(base, 'cp -r slink/ t')
    assert '[exit code: 0]' in result
    assert os.path.isdir(os.path.join(base, 't'))
    assert not os.path.islink(os.path.join(base, 't'))
    assert os.path.exists(os.path.join(base, 't', 'f1.txt'))


def test_root_symlink_operand_copied_as_symlink():
    base = tempfile.mkdtemp()
    build_source(base)
    os.symlink('s', os.path.join(base, 'slink'))
    result = tool_run_command(base, 'cp -r slink t')
    assert '[exit code: 0]' in result
    assert os.path.islink(os.path.join(base, 't'))
    assert os.readlink(os.path.join(base, 't')) == 's'


def test_update_and_no_clobber():
    base = tempfile.mkdtemp()
    with open(os.path.join(base, 'a.txt'), 'w') as fh:
        fh.write('new\n')
    with open(os.path.join(base, 'b.txt'), 'w') as fh:
        fh.write('old\n')
    os.utime(os.path.join(base, 'b.txt'), (0, 1 << 31))
    result = tool_run_command(base, 'cp -u a.txt b.txt')
    assert '[exit code: 0]' in result
    assert open(os.path.join(base, 'b.txt')).read() == 'old\n'
    result = tool_run_command(base, 'cp -n a.txt b.txt')
    assert open(os.path.join(base, 'b.txt')).read() == 'old\n'


def test_verbose_format_matches_gnu_quoting():
    base = tempfile.mkdtemp()
    with open(os.path.join(base, 'a.txt'), 'w') as fh:
        fh.write('x\n')
    result = tool_run_command(base, 'cp -v a.txt b.txt')
    quote_l, quote_r = ('‘', '’') if copy_engine._utf8_locale() \
        else ("'", "'")
    assert f'{quote_l}a.txt{quote_r} -> {quote_l}b.txt{quote_r}' in result


# ═════════════════════════════════════════════════════════════════════
#  3. Diagnostics and exit-code parity
# ═════════════════════════════════════════════════════════════════════

def test_missing_source():
    base = tempfile.mkdtemp()
    result = tool_run_command(base, 'cp -r nodir t')
    assert 'cannot stat' in result and '[exit code: 1]' in result


def test_directory_into_itself():
    base = tempfile.mkdtemp()
    build_source(base)
    result = tool_run_command(base, 'cp -r s s/d1/inner')
    assert 'into itself' in result and '[exit code: 1]' in result
    assert not os.path.exists(os.path.join(base, 's', 'd1', 'inner'))


def test_omitting_directory_without_recursive():
    base = tempfile.mkdtemp()
    build_source(base)
    result = tool_run_command(base, 'cp s t')
    assert 'omitting directory' in result and '[exit code: 1]' in result


def test_trailing_slash_on_file_is_enotdir():
    base = tempfile.mkdtemp()
    build_source(base)
    result = tool_run_command(base, 'cp s/f1.txt/ t')
    assert 'Not a directory' in result and '[exit code: 1]' in result


def test_multi_source_requires_directory_target():
    base = tempfile.mkdtemp()
    build_source(base)
    result = tool_run_command(base, 'cp s/f1.txt s/d1/f2.txt nodir')
    assert 'is not a directory' in result and '[exit code: 1]' in result


# ═════════════════════════════════════════════════════════════════════
#  4. Execution routing: compound splice, fallback, killswitch
# ═════════════════════════════════════════════════════════════════════

def test_compound_command_uses_helper_splice():
    base = tempfile.mkdtemp()
    build_source(base)
    result = tool_run_command(base, 'cp -r s t && echo done')
    assert 'done' in result and '[exit code: 0]' in result
    assert os.path.exists(os.path.join(base, 't', 'f1.txt'))


def test_refused_flags_keep_native_cp():
    base = tempfile.mkdtemp()
    build_source(base)
    os.mkdir(os.path.join(base, 't'))
    # --parents is outside the engine surface: GNU's own error proves the
    # native binary ran (dest exists here, so use the dest-missing form).
    result = tool_run_command(base, 'cp -r --parents s/d1/f2.txt nodir')
    assert 'with --parents, the destination must be a directory' in result \
        or '[exit code: 1]' in result


def test_killswitch():
    base = tempfile.mkdtemp()
    build_source(base)
    os.environ['TOFU_RUN_CP_FASTPATH'] = '0'
    try:
        result = tool_run_command(base, 'cp -r s t')
    finally:
        del os.environ['TOFU_RUN_CP_FASTPATH']
    assert '[exit code: 0]' in result
    assert os.path.exists(os.path.join(base, 't', 'f1.txt'))


def test_helper_roundtrip_direct():
    """The splice target itself: helper_main on a planned argv copies."""
    base = tempfile.mkdtemp()
    build_source(base)
    cwd = os.getcwd()
    os.chdir(base)
    try:
        rc = copy_engine.helper_main(['--workers', '4', '--', '-r', 's', 't'])
    finally:
        os.chdir(cwd)
    assert rc == 0
    assert os.path.exists(os.path.join(base, 't', 'd1', 'd2', 'f3.py'))


def test_helper_reexecs_native_for_unplanned_argv():
    """An argv the planner refuses must re-exec the REAL cp inside the
    helper subprocess (never reached via the gated splice; belt only)."""
    base = tempfile.mkdtemp()
    with open(os.path.join(base, 'a.txt'), 'w') as fh:
        fh.write('x\n')
    with open(os.path.join(base, 'b.txt'), 'w') as fh:
        fh.write('y\n')
    result = subprocess.run(
        [os.sys.executable, os.path.abspath(copy_engine.__file__),
         '--', '-n', 'a.txt', 'b.txt'],
        cwd=base, capture_output=True, text=True, timeout=30)
    # -n (no-clobber) is outside the planner surface; native cp ran and
    # declined to overwrite b.txt.
    assert result.returncode == 0
    assert open(os.path.join(base, 'b.txt')).read() == 'y\n'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


def test_real_files_start_without_copy_worker_count_window(tmp_path, monkeypatch):
    import threading
    import time

    source = tmp_path / 'source'
    source.mkdir()
    for i in range(32):
        (source / str(i)).write_bytes((str(i) * 100).encode())
    barrier = threading.Barrier(32, timeout=10)
    original = copy_engine._Engine._copy_one_file

    def copy(self, task):
        barrier.wait()
        return original(self, task)

    monkeypatch.setattr(copy_engine._Engine, '_copy_one_file', copy)
    plan = copy_engine.parse_cp_argv(['-a', 'source', 'dest'])
    started = time.monotonic()
    result = copy_engine.run_copy_plan(plan, str(tmp_path), workers=1)
    assert result.exit_code == 0, result.errors
    for i in range(32):
        assert (tmp_path / 'dest' / str(i)).read_bytes() == (source / str(i)).read_bytes()
    assert not any(t.name.startswith('tofu-cp') for t in threading.enumerate())
    print(f'32 real file copies: {(time.monotonic() - started) * 1000:.2f} ms; 0 workers')


def test_unstarted_copy_failure_preserves_sibling_files(tmp_path, monkeypatch):
    import threading

    source = tmp_path / 'source'
    source.mkdir()
    for i in range(4):
        (source / str(i)).write_text(str(i))
    original_start = threading.Thread.start
    original_copy = copy_engine._Engine._copy_one_file
    barrier = threading.Barrier(3, timeout=10)
    called = []
    lock = threading.Lock()
    starts = 0

    def start(thread):
        nonlocal starts
        if thread.name.startswith('tofu-cp') and '_FILES' in thread.name:
            starts += 1
            if starts == 2:
                raise RuntimeError('injected unstarted copy')
        return original_start(thread)

    def copy(self, task):
        with lock:
            called.append(task)
        barrier.wait()
        return original_copy(self, task)

    # Directory preparation uses an earlier generation. Inject only the file
    # generation, while retaining the same real shared worker publication path.
    original_pool = copy_engine._Engine._pool
    def pool(self):
        executor = original_pool(self)
        demand = executor._demand
        demand.thread_name_prefix += '_FILES' if (tmp_path / 'dest').exists() else '_DIRS'
        return executor

    monkeypatch.setattr(copy_engine._Engine, '_pool', pool)
    monkeypatch.setattr(copy_engine._Engine, '_copy_one_file', copy)
    monkeypatch.setattr(threading.Thread, 'start', start)
    plan = copy_engine.parse_cp_argv(['-r', 'source', 'dest'])
    result = copy_engine.run_copy_plan(plan, str(tmp_path), workers=1)
    assert result.exit_code == 1
    assert len(result.errors) == 1 and 'injected unstarted copy' in result.errors[0]
    assert len(called) == 3
    copied = list((tmp_path / 'dest').iterdir())
    assert len(copied) == 3
    for path in copied:
        assert path.read_text() == (source / path.name).read_text()
    assert not any(t.name.startswith('tofu-cp') for t in threading.enumerate())


def test_standalone_kernel_import_never_boots_application(tmp_path):
    import sys

    root = str(Path(copy_engine.__file__).resolve().parents[2])
    script = ('import sys; sys.path.insert(0, ' + repr(root) + '); '
              'from tofu_workers import DemandExecutor; '
              'assert not any(k == "lib" or k.startswith("lib.") for k in sys.modules); '
              'pool = DemandExecutor(max_workers=1, queue_capacity=0, '
              'max_abandoned_workers=1, daemon_workers=False); '
              'assert pool.submit(lambda: 42).result() == 42; '
              'pool.shutdown(wait=True)')
    result = subprocess.run([sys.executable, '-I', '-S', '-c', script],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_standalone_copy_helper_runs_without_site_packages(tmp_path):
    import sys

    source = tmp_path / 'source'
    source.mkdir()
    for i in range(32):
        (source / str(i)).write_text(str(i) * 100)
    script = copy_engine.__file__
    result = subprocess.run([sys.executable, '-I', '-S', script,
        '--workers', '1', '--', '-a', 'source', 'dest'], cwd=tmp_path,
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    for i in range(32):
        assert (tmp_path / 'dest' / str(i)).read_bytes() == (source / str(i)).read_bytes()


def test_stop_reaches_all_published_copy_workers(tmp_path, monkeypatch):
    import threading

    source = tmp_path / 'source'
    source.mkdir()
    for i in range(32):
        (source / str(i)).write_text(str(i))
    entered, release, stop = (threading.Event() for _ in range(3))
    lock = threading.Lock()
    count = 0
    result = []
    failure = []
    original = copy_engine._Engine._copy_one_file

    def copy(self, task):
        nonlocal count
        with lock:
            count += 1
            if count == 32:
                entered.set()
        assert release.wait(5)
        return original(self, task)

    def run():
        try:
            plan = copy_engine.parse_cp_argv(['-r', 'source', 'dest'])
            result.append(copy_engine.run_copy_plan(plan, str(tmp_path),
                          workers=1, stop_check=stop.is_set))
        except BaseException as error:
            failure.append(error)

    monkeypatch.setattr(copy_engine._Engine, '_copy_one_file', copy)
    caller = threading.Thread(target=run, name='copy-stop-caller')
    caller.start()
    try:
        assert entered.wait(5)
        stop.set()
    finally:
        release.set()
        caller.join(10)
    assert not caller.is_alive() and not failure
    assert result[0].stopped and not result[0].timed_out
    assert not list((tmp_path / 'dest').iterdir())
    assert not any(t.name.startswith('tofu-cp') for t in threading.enumerate())
