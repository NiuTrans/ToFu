"""New-file permissions use kernel creation masks without process-global changes."""
from pathlib import Path
import subprocess
import sys

import pytest
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mask,expected', [(0o077, 0o600), (0o022, 0o644), (0o002, 0o664)])
def test_new_file_never_reads_umask_by_mutating_it(tmp_path, mask, expected):
    # A subprocess owns this test's umask; the threaded pytest process is untouched.
    script = '''
import os, stat, sys
from lib.project_mod.write_tools import _ops
os.umask(int(sys.argv[2]))
def forbidden(*args):
    raise AssertionError('write code modified process-wide umask')
os.umask = forbidden
_ops._atomic_write_bytes(sys.argv[1], b'content')
assert stat.S_IMODE(os.stat(sys.argv[1]).st_mode) == int(sys.argv[3])
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'new.txt'), str(mask), str(expected)],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('symlink', [False, True])
def test_temporary_collision_never_overwrites_existing_file(tmp_path, monkeypatch, symlink):
    import secrets
    collision = tmp_path / '.tofu_atomic_collision'
    if symlink:
        protected = tmp_path / 'protected'
        protected.write_bytes(b'not ours')
        collision.symlink_to(protected)
    else:
        collision.write_bytes(b'not ours')
    names = iter(['collision', 'collision', 'available'])
    monkeypatch.setattr(secrets, 'token_hex', lambda size: next(names))
    _ops._atomic_write_bytes(str(tmp_path / 'new.txt'), b'content')
    assert collision.read_bytes() == b'not ours'
    assert (tmp_path / 'new.txt').read_bytes() == b'content'
    assert not (tmp_path / '.tofu_atomic_available').exists()


def test_temporary_collisions_are_bounded_and_preserve_target(tmp_path, monkeypatch):
    import secrets
    collision = tmp_path / '.tofu_atomic_collision'
    collision.write_bytes(b'not ours')
    target = tmp_path / 'target'
    target.write_bytes(b'original')
    calls = []
    def collide(size):
        calls.append(size)
        assert len(calls) <= 32, 'unbounded allocation retry'
        return 'collision'
    monkeypatch.setattr(secrets, 'token_hex', collide)
    with pytest.raises(FileExistsError):
        _ops._atomic_write_bytes(str(target), b'replacement')
    assert calls
    assert target.read_bytes() == b'original'
    assert collision.read_bytes() == b'not ours'
