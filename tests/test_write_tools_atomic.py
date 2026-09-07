"""Tests for the atomic-write primitive in ``lib/project_mod/write_tools/_ops.py``.

Every write path (write_file / apply_diff / insert_content / upload) must go
through tmp-file + os.replace so a concurrent reader always sees the complete
OLD or complete NEW file — never a half-written one. On the shared checkout
(multi-conversation writes), a half-written .py is an IndentationError window
for every other conversation (the ~160 s incident in JOURNAL 2026-07-25).
"""
from __future__ import annotations

import os
import stat

import pytest


@pytest.fixture
def base(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir()
    return str(proj)


def _leftover_tmps(directory):
    return [n for n in os.listdir(directory) if n.startswith('.tofu_atomic_')]


@pytest.mark.unit
def test_write_file_content_and_no_tmp_leftover(base):
    from lib.project_mod.write_tools import tool_write_file
    r = tool_write_file(base, 'a.py', 'def f():\n    return 1\n')
    assert r['ok'], r
    with open(os.path.join(base, 'a.py'), encoding='utf-8') as f:
        assert f.read() == 'def f():\n    return 1\n'
    assert _leftover_tmps(base) == []


@pytest.mark.unit
def test_write_goes_through_replace(base, monkeypatch):
    """Mechanism pin: the write MUST use os.replace (tmp+rename), not a
    direct open('w') on the target."""
    from lib.project_mod.write_tools import _ops
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(_ops.os, 'replace', spy)
    r = _ops.tool_write_file(base, 'b.py', 'x = 1\n')
    assert r['ok'], r
    assert len(calls) == 1
    src, dst = calls[0]
    assert os.path.basename(src).startswith('.tofu_atomic_')
    assert os.path.dirname(src) == base  # same dir → same filesystem
    assert dst == os.path.join(base, 'b.py')


@pytest.mark.unit
def test_existing_file_mode_preserved(base):
    from lib.project_mod.write_tools import tool_write_file
    p = os.path.join(base, 'c.py')
    with open(p, 'w') as f:
        f.write('old\n')
    os.chmod(p, 0o640)
    r = tool_write_file(base, 'c.py', 'new\n')
    assert r['ok'], r
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o640


@pytest.mark.unit
def test_new_file_mode_respects_umask(base):
    """A fresh file gets 0o666 & ~umask — the same as plain open('w')."""
    from lib.project_mod.write_tools import tool_write_file
    mask = os.umask(0)
    os.umask(mask)
    r = tool_write_file(base, 'd.py', 'x\n')
    assert r['ok'], r
    mode = stat.S_IMODE(os.stat(os.path.join(base, 'd.py')).st_mode)
    assert mode == (0o666 & ~mask)


@pytest.mark.unit
def test_failed_write_leaves_old_content_intact(base, monkeypatch):
    """NEUTER-adjacent causal test: if the replace never happens, the OLD
    file must survive byte-for-byte and no tmp file may leak."""
    from lib.project_mod.write_tools import _ops
    p = os.path.join(base, 'e.py')
    with open(p, 'w') as f:
        f.write('original\n')

    def boom(src, dst):
        raise OSError('simulated replace failure')

    monkeypatch.setattr(_ops.os, 'replace', boom)
    r = _ops.tool_write_file(base, 'e.py', 'clobber\n')
    assert not r['ok']
    with open(p, encoding='utf-8') as f:
        assert f.read() == 'original\n'
    assert _leftover_tmps(base) == []


@pytest.mark.unit
def test_apply_diff_atomic(base):
    from lib.project_mod.write_tools import _apply_one_diff, tool_write_file
    tool_write_file(base, 'f.py', 'a = 1\nb = 2\n')
    r = _apply_one_diff(base, 'f.py', 'a = 1', 'a = 10')
    assert r['ok'], r
    with open(os.path.join(base, 'f.py'), encoding='utf-8') as f:
        assert f.read() == 'a = 10\nb = 2\n'
    assert _leftover_tmps(base) == []


@pytest.mark.unit
def test_insert_content_atomic(base):
    from lib.project_mod.write_tools import _insert_one, tool_write_file
    tool_write_file(base, 'g.py', 'a = 1\n')
    r = _insert_one(base, 'g.py', 'a = 1\n', 'b = 2\n', position='after')
    assert r['ok'], r
    with open(os.path.join(base, 'g.py'), encoding='utf-8') as f:
        assert f.read() == 'a = 1\nb = 2\n'
    assert _leftover_tmps(base) == []


@pytest.mark.unit
def test_symlink_written_through(base):
    """Historic behaviour: open('w') follows symlinks. os.replace would
    replace the LINK — the helper must resolve the referent instead."""
    from lib.project_mod.write_tools import tool_write_file
    real = os.path.join(base, 'real.py')
    with open(real, 'w') as f:
        f.write('old\n')
    link = os.path.join(base, 'link.py')
    os.symlink(real, link)
    # Sanity: _resolve_write_path must accept the in-base symlink path.
    r = tool_write_file(base, 'link.py', 'through\n')
    assert r['ok'], r
    assert os.path.islink(link)  # link itself survived
    with open(real, encoding='utf-8') as f:
        assert f.read() == 'through\n'


@pytest.mark.unit
def test_upload_bytes_atomic(base):
    from lib.project_mod.write_tools._ops import save_uploaded_file
    payload = bytes(range(256)) * 4
    r = save_uploaded_file(base, 'bin.dat', payload)
    assert r['ok'], r
    with open(os.path.join(base, 'bin.dat'), 'rb') as f:
        assert f.read() == payload
    assert _leftover_tmps(base) == []
