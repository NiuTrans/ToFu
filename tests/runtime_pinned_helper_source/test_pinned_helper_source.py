"""A running process spawns helpers from the source it imported, not the checkout."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap

import pytest

from lib.runtime.pinned_helper_source import PinnedHelperSource

pytestmark = pytest.mark.unit

_REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.fixture
def temp_root(monkeypatch, tmp_path):
    root = tmp_path / 'system-temp'
    root.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(root))
    return root


@pytest.fixture
def helper(tmp_path):
    path = tmp_path / 'checkout' / 'snapshot_helper.py'
    path.parent.mkdir()
    path.write_text('print("loaded with the caller")\n')
    return path


def _private_directories(root):
    return [entry for entry in root.iterdir() if entry.name.startswith('tofu-pinned-helpers')]


def test_spawn_path_keeps_import_time_source_after_checkout_update(helper, temp_root):
    pinned = PinnedHelperSource(helper)
    helper.write_text('print("written by a later checkout")\n')
    copy = Path(pinned.path())
    assert copy.name == helper.name
    assert copy.read_text() == 'print("loaded with the caller")\n'
    assert subprocess.check_output([sys.executable, '-I', '-S', str(copy)], text=True) == 'loaded with the caller\n'
    [private] = _private_directories(temp_root)
    assert copy.is_relative_to(private)
    if os.name == 'posix':
        assert stat.S_IMODE(private.stat().st_mode) == 0o700


def test_removed_copy_or_directory_is_rewritten_from_memory(helper, temp_root):
    pinned = PinnedHelperSource(helper)
    copy = Path(pinned.path())
    helper.unlink()
    copy.unlink()
    assert Path(pinned.path()) == copy
    assert copy.read_text() == 'print("loaded with the caller")\n'
    shutil.rmtree(_private_directories(temp_root)[0])
    assert Path(pinned.path()).read_text() == 'print("loaded with the caller")\n'


def test_versions_loaded_by_different_callers_never_overwrite_each_other(helper, temp_root):
    older = PinnedHelperSource(helper)
    helper.write_text('print("newer caller")\n')
    newer = PinnedHelperSource(helper)
    assert Path(older.path()).read_text() == 'print("loaded with the caller")\n'
    assert Path(newer.path()).read_text() == 'print("newer caller")\n'
    assert older.path() != newer.path()


def test_unavailable_source_fails_at_spawn_not_at_import(tmp_path, temp_root):
    pinned = PinnedHelperSource(tmp_path / 'absent_helper.py')
    with pytest.raises(OSError, match='absent_helper.py was unavailable'):
        pinned.path()
    assert _private_directories(temp_root) == []


def test_concurrent_spawns_share_one_complete_copy(tmp_path, temp_root):
    path = tmp_path / 'large_helper.py'
    path.write_text('VALUE = %r\n' % ('x' * 1_000_000))
    pinned = PinnedHelperSource(path)
    with ThreadPoolExecutor(max_workers=16) as pool:
        paths = set(pool.map(lambda _: pinned.path(), range(64)))
    assert len(paths) == 1
    copy = Path(paths.pop())
    assert copy.read_bytes() == path.read_bytes()
    assert sorted(item.name for item in copy.parent.iterdir()) == [copy.name]


@pytest.mark.skipif(not hasattr(signal, 'SIGKILL'), reason='POSIX process kill')
def test_killed_and_reexecuted_processes_leave_no_per_process_copies(helper, temp_root):
    # Server restarts re-exec in place and crashes skip interpreter exit hooks.
    script = textwrap.dedent('''
        import os, sys, tempfile
        tempfile.tempdir = sys.argv[2]
        from lib.runtime.pinned_helper_source import PinnedHelperSource
        print(PinnedHelperSource(sys.argv[1]).path(), flush=True)
        if sys.argv[3] == 'kill':
            os.kill(os.getpid(), 9)
        os.execv(sys.executable, [sys.executable, '-c', 'pass'])
    ''')
    copies = set()
    for ending in ('kill', 'exec', 'kill'):
        result = subprocess.run([sys.executable, '-c', script, str(helper), str(temp_root), ending],
                                cwd=_REPOSITORY, env={**os.environ, 'PYTHONPATH': str(_REPOSITORY)},
                                capture_output=True, text=True, timeout=120)
        assert result.stdout.strip(), result.stderr
        copies.add(result.stdout.strip())
    assert len(copies) == 1
    [private] = _private_directories(temp_root)
    assert [path.name for path in private.rglob('*') if path.is_file()] == [helper.name]


@pytest.mark.skipif(os.name != 'posix', reason='POSIX ownership and mode bits')
@pytest.mark.parametrize('foreign', ['shared mode', 'symlink'])
def test_directory_not_private_to_this_user_is_refused(helper, temp_root, tmp_path, foreign):
    name = f'tofu-pinned-helpers-{os.getuid()}'
    if foreign == 'symlink':
        (tmp_path / 'elsewhere').mkdir(mode=0o700)
        (temp_root / name).symlink_to(tmp_path / 'elsewhere')
    else:
        (temp_root / name).mkdir()
        os.chmod(temp_root / name, 0o755)
    with pytest.raises(OSError, match='not private to this user'):
        PinnedHelperSource(helper).path()
    if foreign == 'symlink':
        assert not any((tmp_path / 'elsewhere').iterdir())
