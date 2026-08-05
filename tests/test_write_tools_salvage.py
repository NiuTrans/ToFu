"""write_file path-shape guards + failed-write payload salvage.

Incident 2026-08-05: a model emitted write_file WITHOUT the required ``path``
argument. The empty path flowed through _safe_path → resolved to the project
ROOT DIRECTORY itself → the atomic write staged its temp file in the root's
PARENT directory and died with a bare EISDIR ("Is a directory"). The model's
whole payload was thrown away and the error named neither the mistake nor a
recovery. Now:

  * path-shape problems (missing/empty path, existing-directory target) are
    refused BEFORE any file I/O — no .tofu_atomic_* temp file is created;
  * any failed write stages its payload under <tmp>/tofu_write_salvage/ and
    the error carries the staged path + an exact run_command mv recovery.

Prefix contract pinned here: failures must keep the 'Write failed' prefix —
consumed by lib/tasks_pkg/handlers/_read_gate.py::_result_indicates_success
and lib/tools/meta.py::_build_write_file.
"""
from __future__ import annotations

import os
import re
import stat
import time

import pytest


@pytest.fixture
def base(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir()
    return str(proj)


@pytest.fixture
def salvage_root(tmp_path, monkeypatch):
    from lib.project_mod.write_tools import _ops
    root = tmp_path / 'salvage'
    monkeypatch.setattr(_ops, '_salvage_root', lambda: str(root))
    return str(root)


def _atomic_tmps(directory):
    return [n for n in os.listdir(directory) if n.startswith('.tofu_atomic_')]


def _salvaged_files(root):
    if not os.path.isdir(root):
        return []
    return [os.path.join(root, n) for n in os.listdir(root)]


@pytest.mark.unit
def test_missing_path_refused_before_any_io(base, salvage_root):
    """The 2026-08-05 incident shape: no path argument at all."""
    from lib.project_mod.write_tools import _ops
    r = _ops.tool_write_file(base, '', 'some expensive content\n')
    assert not r['ok']
    assert 'path is required' in r['error']
    # Recovery contract: staged path + exact mv instruction in the message.
    m = re.search(r"staged to '([^']+)'", r['error'])
    assert m, r['error']
    assert "run_command(command=\"mv '" in r['error']
    staged = m.group(1)
    assert staged.startswith(salvage_root)
    with open(staged, encoding='utf-8') as f:
        assert f.read() == 'some expensive content\n'
    # THE incident pin: no temp file was ever created — not in the project,
    # and NOT in the project root's PARENT (that's where the incident put it).
    assert _atomic_tmps(base) == []
    assert _atomic_tmps(os.path.dirname(base)) == []


@pytest.mark.unit
def test_non_string_path_refused(base, salvage_root):
    from lib.project_mod.write_tools import _ops
    r = _ops.tool_write_file(base, None, 'x\n')
    assert not r['ok']
    assert 'path is required' in r['error']


@pytest.mark.unit
def test_directory_target_refused_and_salvaged(base, salvage_root):
    """path='.' (or any existing dir) must be refused, never EISDIR'd."""
    from lib.project_mod.write_tools import _ops
    r = _ops.tool_write_file(base, '.', 'payload\n')
    assert not r['ok']
    assert 'resolves to a directory' in r['error']
    assert 'Is a directory' not in r['error']  # the raw errno shape is gone
    assert re.search(r"staged to '([^']+)'", r['error'])
    assert _atomic_tmps(os.path.dirname(base)) == []

    sub = os.path.join(base, 'docs')
    os.makedirs(sub)
    r2 = _ops.tool_write_file(base, 'docs', 'payload\n')
    assert not r2['ok']
    assert 'resolves to a directory' in r2['error']


@pytest.mark.unit
def test_atomic_failure_salvages_and_preserves_target(base, salvage_root, monkeypatch):
    """A genuine IO failure (FUSE hiccup / ENOSPC) still salvages the payload
    and leaves the OLD target byte-for-byte intact."""
    from lib.project_mod.write_tools import _ops
    p = os.path.join(base, 'e.py')
    with open(p, 'w') as f:
        f.write('original\n')

    def boom(src, dst):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(_ops.os, 'replace', boom)
    r = _ops.tool_write_file(base, 'e.py', 'new expensive content\n')
    assert not r['ok']
    assert 'No space left' in r['error']
    m = re.search(r"staged to '([^']+)'", r['error'])
    assert m, r['error']
    with open(m.group(1), encoding='utf-8') as f:
        assert f.read() == 'new expensive content\n'
    with open(p, encoding='utf-8') as f:
        assert f.read() == 'original\n'
    assert _atomic_tmps(base) == []


@pytest.mark.unit
def test_salvage_recovery_roundtrip(base, salvage_root):
    """The mv instruction in the error message actually works: moving the
    staged file to the intended target lands the content."""
    from lib.project_mod.write_tools import _ops
    r = _ops.tool_write_file(base, '', 'the whole file\n')
    staged = re.search(r"staged to '([^']+)'", r['error']).group(1)
    intended = os.path.join(base, 'docs', 'RUNBOOK.md')
    os.makedirs(os.path.dirname(intended))
    os.replace(staged, intended)  # what run_command "mv" does
    with open(intended, encoding='utf-8') as f:
        assert f.read() == 'the whole file\n'


@pytest.mark.unit
def test_salvage_sweep_ttl_and_count_cap(base, salvage_root):
    from lib.project_mod.write_tools import _ops
    os.makedirs(salvage_root)
    old = os.path.join(salvage_root, 'ancient.tmp')
    with open(old, 'w') as f:
        f.write('old')
    past = time.time() - 25 * 3600
    os.utime(old, (past, past))
    fresh = os.path.join(salvage_root, 'fresh.tmp')
    with open(fresh, 'w') as f:
        f.write('fresh')

    _ops._salvage_failed_content('trigger sweep\n', 'x.py')
    assert not os.path.exists(old)    # TTL swept
    assert os.path.exists(fresh)      # young file kept

    # Count cap: 105 young files + the new salvage → bounded at 100.
    for i in range(105):
        p = os.path.join(salvage_root, f'f{i:03d}.tmp')
        with open(p, 'w') as f:
            f.write('x')
    _ops._salvage_failed_content('trigger cap\n', 'x.py')
    assert len(_salvaged_files(salvage_root)) <= 100


@pytest.mark.unit
def test_salvage_staging_failure_degrades_gracefully(base, salvage_root, monkeypatch):
    """If staging itself fails, the ORIGINAL error comes back unchanged —
    no salvage note, no secondary crash."""
    from lib.project_mod.write_tools import _ops

    def boom(*a, **k):
        raise OSError('simulated temp-dir failure')

    monkeypatch.setattr(_ops.tempfile, 'mkstemp', boom)
    r = _ops.tool_write_file(base, '', 'payload\n')
    assert not r['ok']
    assert 'path is required' in r['error']
    assert 'staged to' not in r['error']


@pytest.mark.unit
def test_salvage_file_perms_owner_only(base, salvage_root):
    """Staged payloads can hold secrets — dir 0700, file 0600."""
    from lib.project_mod.write_tools import _ops
    r = _ops.tool_write_file(base, '', 'secret\n')
    staged = re.search(r"staged to '([^']+)'", r['error']).group(1)
    assert stat.S_IMODE(os.stat(staged).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(salvage_root).st_mode) == 0o700


@pytest.mark.unit
def test_exec_wrapper_keeps_load_bearing_prefix(base, salvage_root):
    """Read-gate / meta-builder contract: failures start with 'Write failed'.

    _read_gate._result_indicates_success treats a result starting with
    'Write failed' as a FAILURE (a failed write must NOT satisfy
    read-before-edit); meta.py keys the badge off the same prefix family.
    The salvage note rides AFTER the prefix, never before it.
    """
    from lib.project_mod.tools import _exec_write_file
    out = _exec_write_file({'path': '', 'content': 'x\n'}, base, None, None, {})
    assert out.startswith('Write failed:'), out
    assert 'path is required' in out
    assert 'staged to' in out


@pytest.mark.unit
def test_resolve_rejection_uses_load_bearing_prefix(base, salvage_root):
    """The _resolve_base ValueError arm (unknown root name) must ALSO carry
    the 'Write failed' prefix — previously 'write_file: ...' (lowercase)
    slipped PAST the read-gate's failure detector, letting a failed write
    count as satisfying evidence."""
    from lib.project_mod.tools import _exec_write_file
    out = _exec_write_file({'path': 'nonexistent_root:x.py', 'content': 'x\n'},
                           base, None, None, {})
    assert out.startswith('Write failed:'), out


@pytest.mark.unit
def test_successful_write_not_affected(base, salvage_root):
    """Guard rail for the guards: the normal path behaves exactly as before
    and stages nothing."""
    from lib.project_mod.write_tools import _ops
    r = _ops.tool_write_file(base, 'ok.py', 'x = 1\n')
    assert r['ok'], r
    with open(os.path.join(base, 'ok.py'), encoding='utf-8') as f:
        assert f.read() == 'x = 1\n'
    assert _salvaged_files(salvage_root) == []
