"""Tests for lib/env_health.py — half-overwritten package detection.

Each test builds a synthetic ``site-packages`` tree in a tmp dir modelling a
real scenario (clean / duplicate dist-info / shadow .so) and asserts the pure
detectors classify it correctly. No real packages are imported.
"""

import os

import pytest

from lib.env_health import (
    EnvIssue,
    canonical_name,
    find_duplicate_dist_info,
    find_shadow_so,
    scan_site_packages,
    _strip_ext_suffix,
)


# ── helpers ────────────────────────────────────────────────────────────

def _write(path: str, content: str = '') -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(content)


def _make_dist_info(sp: str, name: str, version: str, record_lines: list[str]) -> str:
    """Create ``<name>-<version>.dist-info/RECORD`` with the given file lines."""
    di = os.path.join(sp, f'{name}-{version}.dist-info')
    os.makedirs(di, exist_ok=True)
    record = '\n'.join(record_lines) + ('\n' if record_lines else '')
    _write(os.path.join(di, 'RECORD'), record)
    return di


# ── canonical_name / _strip_ext_suffix unit tests ───────────────────────

def test_canonical_name():
    assert canonical_name('Flask-Compress') == 'flask-compress'
    assert canonical_name('pydantic_core') == 'pydantic-core'
    assert canonical_name('a.b_c--d') == 'a-b-c-d'


@pytest.mark.parametrize('fname,expected', [
    ('main.cpython-312-x86_64-linux-gnu.so', 'main'),
    ('_spropack.cpython-312-x86_64-linux-gnu.so', '_spropack'),
    ('__init__.cpython-312-x86_64-linux-gnu.so', '__init__'),
    ('foo.cp311-win_amd64.pyd', 'foo'),
    ('bar.py', None),
    ('baz.txt', None),
])
def test_strip_ext_suffix(fname, expected):
    assert _strip_ext_suffix(fname) == expected


# ── clean env: no issues ─────────────────────────────────────────────────

def test_clean_env_no_issues(tmp_path):
    sp = str(tmp_path)
    # A normal pure-python package with one dist-info and no stray .so.
    pkg = os.path.join(sp, 'goodpkg')
    _write(os.path.join(pkg, '__init__.py'), '')
    _write(os.path.join(pkg, 'core.py'), '')
    _make_dist_info(sp, 'goodpkg', '1.0.0', [
        'goodpkg/__init__.py,sha256=x,10',
        'goodpkg/core.py,sha256=y,20',
    ])
    assert scan_site_packages(sp) == []


def test_legit_compiled_module_not_flagged(tmp_path):
    """A .so listed in RECORD (even with a sibling .py) is legitimate."""
    sp = str(tmp_path)
    pkg = os.path.join(sp, 'cpkg')
    _write(os.path.join(pkg, '__init__.py'), '')
    _write(os.path.join(pkg, 'fast.py'), '')  # pure fallback
    so = 'cpkg/fast.cpython-312-x86_64-linux-gnu.so'
    _write(os.path.join(sp, so), 'binary')
    _make_dist_info(sp, 'cpkg', '2.0.0', [
        'cpkg/__init__.py,sha256=a,1',
        'cpkg/fast.py,sha256=b,2',
        f'{so},sha256=c,3',  # recorded → legit
    ])
    assert scan_site_packages(sp) == []


def test_c_extension_without_py_sibling_not_flagged(tmp_path):
    """A bare .so with no sibling .py is a normal C extension, not a shadow."""
    sp = str(tmp_path)
    pkg = os.path.join(sp, 'native')
    _write(os.path.join(pkg, '__init__.py'), '')
    _write(os.path.join(sp, 'native/_speedups.cpython-312-x86_64-linux-gnu.so'), 'bin')
    _make_dist_info(sp, 'native', '1.0', ['native/__init__.py,,'])
    assert find_shadow_so(sp) == []


# ── duplicate dist-info detection ────────────────────────────────────────

def test_duplicate_dist_info_detected(tmp_path):
    sp = str(tmp_path)
    _make_dist_info(sp, 'pydantic', '1.10.26', [])
    _make_dist_info(sp, 'pydantic', '2.13.4', [])
    issues = find_duplicate_dist_info(sp)
    assert len(issues) == 1
    iss = issues[0]
    assert iss.kind == 'duplicate_dist_info'
    assert iss.package == 'pydantic'
    assert iss.severity == 'warning'  # lone duplicate is benign
    assert '1.10.26' in iss.detail and '2.13.4' in iss.detail
    assert len(iss.paths) == 2


def test_duplicate_dist_info_canonical_match(tmp_path):
    """Underscore vs hyphen names collapse to one canonical package."""
    sp = str(tmp_path)
    _make_dist_info(sp, 'pydantic_core', '2.0.0', [])
    _make_dist_info(sp, 'pydantic-core', '2.46.4', [])
    issues = find_duplicate_dist_info(sp)
    assert len(issues) == 1
    assert issues[0].package == 'pydantic-core'


def test_single_dist_info_not_flagged(tmp_path):
    sp = str(tmp_path)
    _make_dist_info(sp, 'requests', '2.31.0', [])
    assert find_duplicate_dist_info(sp) == []


# ── shadow .so detection (the pydantic / scipy failure) ──────────────────

def test_shadow_so_detected_pydantic_scenario(tmp_path):
    """Model the real pydantic case: v2 .py files + orphaned v1 .so shadowing."""
    sp = str(tmp_path)
    pkg = os.path.join(sp, 'pydantic')
    # v2.13.4 pure-python files on disk
    _write(os.path.join(pkg, '__init__.py'), '')
    _write(os.path.join(pkg, 'main.py'), '')
    _write(os.path.join(pkg, 'type_adapter.py'), '')
    # Only the v2 dist-info survives; RECORD lists the .py, NOT the .so
    _make_dist_info(sp, 'pydantic', '2.13.4', [
        'pydantic/__init__.py,sha256=a,1',
        'pydantic/main.py,sha256=b,2',
        'pydantic/type_adapter.py,sha256=c,3',
    ])
    # Orphaned v1 compiled files left behind, shadowing __init__.py & main.py
    _write(os.path.join(pkg, '__init__.cpython-312-x86_64-linux-gnu.so'), 'v1')
    _write(os.path.join(pkg, 'main.cpython-312-x86_64-linux-gnu.so'), 'v1')

    issues = find_shadow_so(sp)
    assert len(issues) == 1
    iss = issues[0]
    assert iss.kind == 'shadow_so'
    assert iss.package == 'pydantic'
    assert iss.severity == 'error'  # decisive: imports resolve the stale .so
    assert len(iss.paths) == 2
    assert any('__init__' in p for p in iss.paths)
    assert any('main' in p for p in iss.paths)


def test_shadow_so_at_top_level(tmp_path):
    """A stray top-level (non-package) .so shadowing a .py is also caught."""
    sp = str(tmp_path)
    _write(os.path.join(sp, 'widget.py'), '')
    _write(os.path.join(sp, 'widget.cpython-312-x86_64-linux-gnu.so'), 'stale')
    _make_dist_info(sp, 'widget', '3.0', ['widget.py,,'])
    issues = find_shadow_so(sp)
    assert len(issues) == 1
    assert issues[0].paths == ['widget.cpython-312-x86_64-linux-gnu.so']


def test_full_scan_combines_both_signals(tmp_path):
    """The real repro trips BOTH detectors at once."""
    sp = str(tmp_path)
    pkg = os.path.join(sp, 'pydantic')
    _write(os.path.join(pkg, '__init__.py'), '')
    _write(os.path.join(pkg, '__init__.cpython-312-x86_64-linux-gnu.so'), 'v1')
    _make_dist_info(sp, 'pydantic', '1.10.26', [])  # stale metadata left behind
    _make_dist_info(sp, 'pydantic', '2.13.4', ['pydantic/__init__.py,,'])

    issues = scan_site_packages(sp)
    kinds = {i.kind for i in issues}
    assert 'duplicate_dist_info' in kinds
    assert 'shadow_so' in kinds
    # The duplicate is CORRELATED with the shadow .so → escalated to error.
    dupe = next(i for i in issues if i.kind == 'duplicate_dist_info')
    assert dupe.severity == 'error'
    assert 'half-overwrite' in dupe.detail


def test_duplicate_without_shadow_stays_warning(tmp_path):
    """A long-lived env with only leftover dist-info (no shadow .so) → warning,
    NOT an error. This is the common benign case (pip install -U leftovers)."""
    sp = str(tmp_path)
    _make_dist_info(sp, 'tqdm', '4.67.3', [])
    _make_dist_info(sp, 'tqdm', '4.68.3', [])
    issues = scan_site_packages(sp)
    assert len(issues) == 1
    assert issues[0].kind == 'duplicate_dist_info'
    assert issues[0].severity == 'warning'


# ── robustness / edge cases ──────────────────────────────────────────────

def test_missing_record_file_tolerated(tmp_path):
    """A dist-info with no RECORD → its files count as unrecorded, but a .so
    with no sibling .py is still not flagged; no crash."""
    sp = str(tmp_path)
    di = os.path.join(sp, 'weird-1.0.dist-info')
    os.makedirs(di, exist_ok=True)  # deliberately no RECORD
    _write(os.path.join(sp, 'weird/__init__.py'), '')
    # scan must not raise
    assert isinstance(scan_site_packages(sp), list)


def test_move_aside_backup_dir_ignored(tmp_path):
    """The reversible-fix backups (``pydantic.corrupt.<ts>``) are non-importable
    dotted dirs → must NOT be walked/flagged as shadow .so."""
    sp = str(tmp_path)
    backup = os.path.join(sp, 'pydantic.corrupt.20260709_082145')
    _write(os.path.join(backup, '__init__.py'), '')
    _write(os.path.join(backup, '__init__.cpython-312-x86_64-linux-gnu.so'), 'v1')
    assert find_shadow_so(sp) == []


def test_nonexistent_site_packages(tmp_path):
    missing = str(tmp_path / 'does-not-exist')
    assert scan_site_packages(missing) == []


def test_unparsable_dist_info_name_skipped(tmp_path):
    sp = str(tmp_path)
    os.makedirs(os.path.join(sp, 'not_a_version.dist-info'), exist_ok=True)
    # should not raise, should not falsely report a duplicate
    assert find_duplicate_dist_info(sp) == []


def test_envissue_str():
    iss = EnvIssue('shadow_so', 'pydantic', 'stale so present', ['pydantic/x.so'])
    s = str(iss)
    assert 'shadow_so' in s and 'pydantic' in s
