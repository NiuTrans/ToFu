#!/usr/bin/env python3
"""Unit tests for lib.boot_identity.code_fingerprint.

Verifies the source-tree fingerprint that lets the restart client prove the
NEW process loaded the code the operator edited (not just that SOME new
process answered). Covers determinism, sensitivity to HEAD + uncommitted
diff, the dirty flag, non-git graceful-None, and the frozen-cache contract.

Run standalone (``python tests/test_code_fingerprint.py``) or via pytest.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _fake_git(monkeypatch, *, available=True, head='abc123def456', diff=''):
    """Patch lib.self_update._git so _compute_code_fingerprint is deterministic.

    _compute_code_fingerprint imports (_head_sha, _run_git) from
    lib.self_update._git INSIDE the function, so patching the module attributes
    takes effect on the next call. ``available=False`` simulates a genuine
    non-git deploy: _head_sha returns None and `git diff HEAD` fails (non-zero
    rc, empty stdout), the two conditions the all-None guard keys on. (We do
    NOT patch git_available — the fingerprint no longer calls it, because that
    predicate false-negatives on real checkouts; see _compute_code_fingerprint.)
    """
    import lib.self_update._git as g

    monkeypatch.setattr(g, '_head_sha', lambda: head if available else None)

    def _run_git(args, timeout=30):
        # Only 'diff HEAD' is exercised by the fingerprint.
        if not available:
            return subprocess.CompletedProcess(args, 128, stdout='',
                                               stderr='not a git repository')
        return subprocess.CompletedProcess(args, 0, stdout=diff, stderr='')

    monkeypatch.setattr(g, '_run_git', _run_git)


def _compute(monkeypatch, **kw):
    """Call the uncached compute fn with a fake git environment."""
    _fake_git(monkeypatch, **kw)
    from lib.boot_identity import _compute_code_fingerprint
    return _compute_code_fingerprint()


def test_deterministic_same_inputs():
    """Same HEAD + same diff → identical digest across calls."""
    with pytest.MonkeyPatch().context() as mp:
        a = _compute(mp, head='deadbeef0001', diff='--- a\n+++ b\n+x')
    with pytest.MonkeyPatch().context() as mp:
        b = _compute(mp, head='deadbeef0001', diff='--- a\n+++ b\n+x')
    assert a['digest'] == b['digest']
    assert a['digest'] is not None
    _ok('deterministic: same HEAD + diff yields same digest')


def test_head_change_flips_digest():
    """A different HEAD sha changes the digest even with no working-tree edits."""
    with pytest.MonkeyPatch().context() as mp:
        a = _compute(mp, head='1111111111aa', diff='')
    with pytest.MonkeyPatch().context() as mp:
        b = _compute(mp, head='2222222222bb', diff='')
    assert a['digest'] != b['digest']
    _ok('sensitive: HEAD sha change flips the digest')


def test_uncommitted_diff_flips_digest():
    """An uncommitted edit (same HEAD) changes the digest — the whole point."""
    with pytest.MonkeyPatch().context() as mp:
        clean = _compute(mp, head='cafe12345678', diff='')
    with pytest.MonkeyPatch().context() as mp:
        edited = _compute(mp, head='cafe12345678',
                          diff='--- a/x.py\n+++ b/x.py\n+print(1)')
    assert clean['digest'] != edited['digest']
    assert clean['dirty'] is False
    assert edited['dirty'] is True
    _ok('sensitive: uncommitted tracked edit flips digest + sets dirty')


def test_head_short_and_present():
    """head is reported as a short (<=12) sha when in a checkout."""
    with pytest.MonkeyPatch().context() as mp:
        fp = _compute(mp, head='0123456789abcdef0123', diff='')
    assert fp['head'] == '0123456789ab'
    assert len(fp['head']) == 12
    _ok('head reported as 12-char short sha')


def test_non_git_returns_none():
    """Outside a git checkout → all-None (client falls back to bootId rule)."""
    with pytest.MonkeyPatch().context() as mp:
        fp = _compute(mp, available=False)
    assert fp == {'head': None, 'dirty': None, 'digest': None}
    _ok('non-git deploy: fingerprint is all-None (graceful degrade)')


def test_git_import_failure_returns_none():
    """If the git helper import itself fails, degrade to all-None, never raise."""
    import lib.boot_identity as bi
    with pytest.MonkeyPatch().context() as mp:
        # Force the inner `from lib.self_update._git import ...` to explode.
        import builtins
        real_import = builtins.__import__

        def _boom(name, *a, **k):
            if name == 'lib.self_update._git' or name.endswith('_git'):
                raise ImportError('simulated missing git module')
            return real_import(name, *a, **k)

        mp.setattr(builtins, '__import__', _boom)
        fp = bi._compute_code_fingerprint()
    assert fp == {'head': None, 'dirty': None, 'digest': None}
    _ok('git-helper import failure degrades to all-None (no raise)')


def test_code_fingerprint_is_frozen():
    """code_fingerprint() computes once and freezes — later git changes ignored.

    This is the load-bearing contract: the value must reflect code-as-loaded-
    at-boot, so a mid-run edit cannot retroactively change the OLD process's
    reported digest (which would defeat the pre/post restart comparison).
    """
    import lib.boot_identity as bi
    with pytest.MonkeyPatch().context() as mp:
        # Reset the frozen cache so this test controls the first computation.
        mp.setattr(bi, '_FINGERPRINT_CACHE', None, raising=False)
        _fake_git(mp, head='aaaa00001111', diff='')
        first = bi.code_fingerprint()
        # Now change the underlying git state...
        _fake_git(mp, head='bbbb22223333', diff='+edited')
        second = bi.code_fingerprint()
    assert first == second, 'frozen fingerprint must NOT change after first call'
    _ok('code_fingerprint() is frozen after first call (boot-time snapshot)')


def test_neuter_would_break_sensitivity():
    """NEUTER control: proves the diff bytes are load-bearing in the digest.

    If _compute folded ONLY the head (ignoring the diff), an uncommitted edit
    would NOT change the digest and the whole 'edits are live' verdict would be
    a lie. We simulate that broken variant and assert it fails to distinguish —
    confirming the real implementation's diff-inclusion is what carries the
    signal.
    """
    import hashlib
    # Broken variant: digest from head only.
    def broken(head, diff):
        return hashlib.sha256((head or '').encode()).hexdigest()[:12]

    same_head = 'ffff00001234'
    assert broken(same_head, '') == broken(same_head, '+edit'), \
        'head-only digest (broken) is insensitive to edits — as expected'

    # Real implementation DOES distinguish (already covered above); assert the
    # contrast holds here too so the NEUTER is self-contained.
    with pytest.MonkeyPatch().context() as mp:
        clean = _compute(mp, head=same_head, diff='')
    with pytest.MonkeyPatch().context() as mp:
        edited = _compute(mp, head=same_head, diff='+edit')
    assert clean['digest'] != edited['digest'], \
        'real impl MUST distinguish (diff bytes are load-bearing)'
    _ok('NEUTER: head-only digest is insensitive; real impl folds diff (load-bearing)')


def main():
    print()
    print(_color('═══ code_fingerprint Unit Tests ═══', '36'))
    print()
    tests = [
        test_deterministic_same_inputs,
        test_head_change_flips_digest,
        test_uncommitted_diff_flips_digest,
        test_head_short_and_present,
        test_non_git_returns_none,
        test_git_import_failure_returns_none,
        test_code_fingerprint_is_frozen,
        test_neuter_would_break_sensitivity,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
