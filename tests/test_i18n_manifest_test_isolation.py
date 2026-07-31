#!/usr/bin/env python3
"""Order-dependence guard: no i18n/bundle test file may poison a later one.

WHY THIS EXISTS (owner-caught, 2026-07-29)
------------------------------------------
``tests/test_i18n_pack_boot_floor.py`` shipped with a save-mutate-restore
block around ``lib.js_bundler._pack_filenames`` / ``_bundle_includes_i18n``.
Those globals are PUBLISHED as a side effect of building, so the snapshot was
taken BEFORE the build the test itself triggered. Replaying it stamped the
pre-build values (``{}`` / ``True``) over the real published state, leaving the
module permanently reporting "packs inactive". Reproduced verbatim::

    before:                  {} | includes_i18n = True     <- captured
    after get_i18n_pack_tag: {'zh': 'i18n-zh-…'} | False    <- real state
    after "restore":         {} | includes_i18n = True     <- stale, stamped

    >>> get_i18n_pack_tag('zh') for the NEXT file: None

Result: 7 failures in ``test_i18n_pack_serving.py`` — and BOTH files pass in
isolation, so no per-file run reveals it. Both carry ``@pytest.mark.unit``, so
``make test-unit`` / ``make ci`` collect them together: this was a live CI
break, not a flake.

WHAT THIS GUARD DOES
--------------------
Runs the file pairs IN ONE pytest process, in the poisoning order, and asserts
both green. A per-file green cannot substitute — the failure only exists in
combination. Subprocess-based on purpose: the pollution is process-global
module state, so it must be observed in a fresh interpreter.

The complementary structural fix is ``js_bundler.reset_manifest_for_tests()``:
teardown must INVALIDATE the manifest (next reader rebuilds), never replay a
snapshot. ``test_no_test_replays_a_manifest_snapshot`` below keeps new tests
from reintroducing the hazard.

Run: python3 tests/test_i18n_manifest_test_isolation.py
"""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
except ImportError:
    pytest = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, 'tests')

pytestmark = [] if pytest is None else [pytest.mark.unit]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


# The manifest data globals. Mutating them is fine; RESTORING a snapshot of
# them is the trap, because they are build side effects.
_MANIFEST_GLOBALS = ('_pack_filenames', '_bundle_includes_i18n',
                     '_bundle_filename', '_feature_filename', '_bundle_mtime')

# Files that legitimately READ these globals to assert on published state.
# Reading is always safe; only snapshot-replay is not.
_ORDER_PAIRS = [
    ('test_i18n_pack_boot_floor.py', 'test_i18n_pack_serving.py'),
    ('test_stale_i18n_pack_self_heal.py', 'test_i18n_pack_serving.py'),
    ('test_i18n_pack_emission.py', 'test_i18n_pack_serving.py'),
    ('test_stale_bundle_self_heal.py', 'test_i18n_pack_serving.py'),
]


def _run_pair(first: str, second: str):
    """Run two test files in ONE pytest process, in order. Returns (rc, out)."""
    env = dict(os.environ)
    env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    proc = subprocess.run(
        [sys.executable, '-m', 'pytest',
         os.path.join('tests', first), os.path.join('tests', second),
         '-q', '-p', 'no:cacheprovider', '--timeout=300'],
        cwd=ROOT, capture_output=True, text=True, timeout=1200, env=env)
    return proc.returncode, (proc.stdout + proc.stderr)


@_unit
def test_file_order_does_not_poison_the_manifest():
    """Each pair must be green TOGETHER, not merely green apart."""
    failures = []
    for first, second in _ORDER_PAIRS:
        if not os.path.exists(os.path.join(TESTS, first)):
            continue
        if not os.path.exists(os.path.join(TESTS, second)):
            continue
        rc, out = _run_pair(first, second)
        if rc != 0:
            tail = '\n'.join(
                l for l in out.splitlines()
                if l.startswith('FAILED') or ' passed' in l or ' failed' in l)
            failures.append(f'{first} THEN {second}:\n{tail}')
    assert not failures, (
        'test file order changes the result — an earlier file is leaving '
        'lib.js_bundler manifest state that breaks a later one. Both files '
        'likely pass ALONE, which is why a per-file run misses this; '
        'make test-unit collects them together, so CI breaks.\n\n'
        + '\n\n'.join(failures)
        + '\n\nFix: never restore a snapshot of the manifest globals '
          '(they are build side effects — the snapshot predates the build). '
          'Use js_bundler.reset_manifest_for_tests() in teardown instead.')


@_unit
def test_the_reset_seam_exists_and_forces_a_rebuild():
    """The supported way to undo manifest mutation."""
    import lib.js_bundler as jb
    assert hasattr(jb, 'reset_manifest_for_tests'), (
        'lib/js_bundler must expose reset_manifest_for_tests() so tests have '
        'a supported way to undo manifest mutation without snapshot replay')
    # Publish real state, poison it, reset, and confirm the next read recovers.
    name = jb.get_bundle_filename() or jb.build_bundle()
    assert name, 'core bundle must build in the test env'
    packs_before = dict(jb._pack_filenames)
    jb._pack_filenames = {}
    jb._bundle_includes_i18n = True
    jb.reset_manifest_for_tests()
    assert jb._bundle_filename is None and jb._bundle_mtime == 0, (
        'reset must clear the pointer AND the mtime, or the staleness gate '
        'lets a reader serve the cleared manifest')
    recovered = jb.get_bundle_filename()
    assert recovered, 'manifest did not rebuild after reset'
    if packs_before:
        assert jb._pack_filenames == packs_before, (
            f'rebuild after reset did not re-publish the same packs: '
            f'{jb._pack_filenames!r} != {packs_before!r}')


@_unit
def test_no_test_replays_a_manifest_snapshot():
    """Structural: forbid CAPTURE-then-RESTORE of the manifest globals.

    ★ WHAT IS AND ISN'T THE HAZARD.
    MUTATING these globals to force a state is FINE — that is how a test pins
    a scenario. The hazard is RESTORING a previously-captured snapshot, because
    the capture predates the build that publishes them.

    Two earlier versions of this check were wrong in OPPOSITE directions:
      * anchoring on a ``_?saved`` NAME missed the ``self._saved`` form
        (NEUTER-F planted exactly that and the guard stayed green);
      * matching any opaque right-hand side flagged legitimate SETUP
        (``_pack_filenames = self._packs``) — a guard that forces edits to
        correct code is worse than no guard.

    The discriminator is the PAIRING: some name is first assigned FROM one of
    these globals (the capture), and later assigned BACK INTO them (the
    replay). Only that pair is reported.

    Parsed with ``ast``, not line regexes. A line-based scan needs to rejoin
    continuation lines, and counting parens to do so is defeated by an
    unbalanced paren inside a STRING literal (this file's own regexes contain
    them) — that swallowed the rest of the file and silently hid a planted
    offender. The parser cannot be fooled that way.
    """
    import ast

    def _target_names(node):
        """Yield dotted names assigned by an Assign target (incl. tuples)."""
        for t in node.targets:
            for el in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]):
                if isinstance(el, ast.Attribute):
                    yield f'{ast.unparse(el.value)}.{el.attr}', el.attr
                elif isinstance(el, ast.Name):
                    yield el.id, el.id

    def _value_names(node):
        """Yield dotted names READ on the right-hand side."""
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Attribute):
                yield f'{ast.unparse(sub.value)}.{sub.attr}', sub.attr
            elif isinstance(sub, ast.Name):
                yield sub.id, sub.id

    offenders = []
    for fn in sorted(os.listdir(TESTS)):
        if not fn.startswith('test_') or not fn.endswith('.py'):
            continue
        try:
            with open(os.path.join(TESTS, fn), encoding='utf-8') as f:
                src = f.read()
            tree = ast.parse(src)
        except (OSError, SyntaxError):
            continue

        assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
        # CAPTURE: `x = <mod>._pack_filenames` → remember the holder name `x`.
        captured = set()
        for node in assigns:
            if any(attr in _MANIFEST_GLOBALS for _, attr in _value_names(node)):
                for full, _ in _target_names(node):
                    captured.add(full)
        # REPLAY: `<mod>._pack_filenames = x` where x was such a holder.
        for node in assigns:
            tgt_attrs = [a for _, a in _target_names(node)]
            if not any(a in _MANIFEST_GLOBALS for a in tgt_attrs):
                continue
            for full, _ in _value_names(node):
                if full in captured:
                    offenders.append(
                        f'{fn}:{node.lineno}: '
                        f'{ast.unparse(node)[:110]}')
                    break

    assert not offenders, (
        'test(s) RESTORE a snapshot of lib.js_bundler manifest globals. Those '
        'globals are published as a side effect of building, so a snapshot '
        'taken before the build replays STALE values over the real ones and '
        'poisons every later test in the process:\n  '
        + '\n  '.join(offenders)
        + '\nUse js_bundler.reset_manifest_for_tests() instead.')


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('ok  ', name)
            except AssertionError as e:
                failures += 1
                print('FAIL', name)
                print('     ', e)
    print('ALL PASSED' if not failures else f'{failures} FAILED')
    sys.exit(1 if failures else 0)
