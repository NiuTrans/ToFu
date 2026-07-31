#!/usr/bin/env python3
"""Guard: the --force-reinstall gate must actually be conditional.

WHAT WENT WRONG (measured 2026-07-28, caught by the owner, not by this suite)
────────────────────────────────────────────────────────────────────────────
install.sh purges 10 CONDA_CONFLICT_PKGS before the main solve, then passes
``--force-reinstall`` so conda re-lays-down files whose metadata the purge made
stale. Unconditionally that re-downloads and re-links ~30 packages on EVERY
run, so it was narrowed to "only when the purge removed something".

The first narrowing judged **"was the package present BEFORE the purge?"** —
which is backwards. `conda remove` targets exactly those packages *because*
the solve installs them, so "present beforehand" is the normal steady state of
a healthy env. Measured: all **10 of 10** present in a working env, and the
loop breaks on the first hit, so ``_PURGED_SOMETHING=1`` **always**. The
narrowing saved nothing while reading as though it had.

WHY THE OLD GUARD MISSED IT
───────────────────────────
It asserted the SOURCE contained ``${_FORCE_REINSTALL}`` — i.e. that a variable
exists. A gate wired permanently ON still contains that variable, so the guard
was green either way. That is charter's "assert the result, not the
implementation" with the gate itself as the subject.

So these tests EXECUTE the real gate logic, lifted out of install.sh at run
time (never hand-copied — charter forbids transcribing production predicates
into a harness), against synthetic conda-list fixtures, and assert the decision
it produces.
"""

import os
import re
import subprocess
import textwrap

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_SH = os.path.join(ROOT, 'install.sh')


def _install_sh() -> str:
    with open(INSTALL_SH, encoding='utf-8') as f:
        return f.read()


def _extract_gate_source() -> str:
    """Lift the real gate block out of install.sh.

    Anchored on the semantic span (the _PURGED_SOMETHING computation through
    the _FORCE_REINSTALL assignment) rather than line numbers, so a reindent or
    a nearby edit does not silently turn this into a test of nothing. A missing
    anchor is a hard error, never a skip.
    """
    src = _install_sh()
    start_pat = '_PURGED_SOMETHING=0'
    end_pat = '_install_main_deps() {'
    assert start_pat in src, (
        'install.sh no longer computes _PURGED_SOMETHING — the force-reinstall '
        'gate was removed or renamed; re-point this guard at its replacement '
        'rather than deleting it')
    assert end_pat in src, 'install.sh no longer defines _install_main_deps()'
    block = src[src.index(start_pat):src.index(end_pat)]
    # Drop the pip-uninstall line: it shells out and is irrelevant to the gate.
    block = '\n'.join(l for l in block.split('\n')
                      if 'pip uninstall' not in l and not l.startswith('info '))
    return block


def _run_gate(before: str, after: str) -> str:
    """Run the REAL gate with the given before/after `conda list` output.

    Returns the resulting _FORCE_REINSTALL value ('' or '--force-reinstall').
    """
    script = textwrap.dedent('''\
        set -u
        info() { :; }
        ok() { :; }
        CONDA_CONFLICT_PKGS=(
            postgresql psycopg2
            trafilatura htmldate courlan
            lxml libxml2 libxml2-16 libxslt
            icu
        )
        _CONDA_PKGS_BEFORE_PURGE="$(cat "$1")"
        _CONDA_PKGS_AFTER_PURGE="$(cat "$2")"
    ''') + _extract_gate_source() + '\nprintf "%s" "${_FORCE_REINSTALL}"\n'

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        b = os.path.join(td, 'before'); a = os.path.join(td, 'after')
        with open(b, 'w') as f: f.write(before)
        with open(a, 'w') as f: f.write(after)
        r = subprocess.run(['bash', '-c', script, 'gate', b, a],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f'gate script failed: {r.stderr[-800:]}'
        return r.stdout.strip()


def _conda_list(pkgs) -> str:
    lines = ['# packages in environment:', '#', '# Name  Version  Build  Channel']
    lines += [f'{p}  1.0.0  h1234_0  conda-forge' for p in pkgs]
    return '\n'.join(lines) + '\n'


_ALL_CONFLICT = ['postgresql', 'psycopg2', 'trafilatura', 'htmldate', 'courlan',
                 'lxml', 'libxml2', 'libxml2-16', 'libxslt', 'icu']


def test_healthy_reinstall_does_not_force():
    """THE regression: a re-run of a correct env must NOT force-reinstall.

    Real shape, measured on this host: every conflict package is present before
    the purge, and `conda remove` leaves them in place (nothing to repair). The
    previous gate returned --force-reinstall here, re-laying ~30 packages.
    """
    same = _conda_list(_ALL_CONFLICT + ['numpy', 'requests'])
    got = _run_gate(before=same, after=same)
    assert got == '', (
        'a healthy re-run still passes --force-reinstall (got %r) — the gate '
        'is judging "was it present before?" instead of "did the purge remove '
        'it?", so all ~30 conda packages are re-installed every run' % got)


def test_purge_that_really_removed_something_forces():
    """Complement: when the purge DID remove packages, we must still force.

    Without this, "never force" would also pass the test above — and that
    reintroduces the stale-metadata bug the flag exists for.
    """
    before = _conda_list(_ALL_CONFLICT + ['numpy'])
    after = _conda_list(['postgresql', 'psycopg2', 'numpy'])  # 8 removed
    got = _run_gate(before=before, after=after)
    assert got == '--force-reinstall', (
        'the purge removed 8 packages but the gate did not force a reinstall '
        f'(got {got!r}) — conda metadata is left stale')


def test_partial_purge_still_forces():
    """A partially-successful purge still leaves stale metadata.

    `conda remove` is best-effort (`|| true`); it can drop some packages and
    error on others. One removed package is enough to require the repair.
    """
    before = _conda_list(_ALL_CONFLICT)
    after = _conda_list([p for p in _ALL_CONFLICT if p != 'lxml'])
    got = _run_gate(before=before, after=after)
    assert got == '--force-reinstall', (
        f'only lxml was purged but the gate skipped the repair (got {got!r})')


def test_fresh_env_with_no_conflict_packages_does_not_force():
    """A brand-new env has none of them; nothing was purged, nothing to repair."""
    fresh = _conda_list(['python', 'pip'])
    got = _run_gate(before=fresh, after=fresh)
    assert got == '', f'fresh env forced a reinstall unnecessarily (got {got!r})'


def test_gate_reads_the_after_snapshot():
    """Ratchet: the decision must consult the post-purge state.

    A gate that only ever looks at the BEFORE list cannot distinguish "removed"
    from "present", which is exactly the defect this file exists for. Anchored
    on the variable actually being read inside the gate block, not on prose.
    """
    block = _extract_gate_source()
    assert '_CONDA_PKGS_AFTER_PURGE' in block, (
        'the force-reinstall gate never reads the post-purge package list, so '
        'it cannot tell whether the purge removed anything')
    assert re.search(r'_CONDA_PKGS_AFTER_PURGE=', _install_sh()), \
        'install.sh never captures a post-purge snapshot'


def test_retry_branch_still_forces_unconditionally():
    """The deeper-reset retry must keep its unconditional --force-reinstall.

    That branch runs only after the first solve already FAILED, i.e. the env is
    genuinely broken — narrowing it would trade a rare slow path for a rare
    unrepairable one.
    """
    src = _install_sh()
    tail = src[src.index('First solve failed'):]
    tail = tail[:tail.index('\nfi')] if '\nfi' in tail else tail
    assert '--force-reinstall' in tail, (
        'the retry branch lost its unconditional --force-reinstall — a broken '
        'env can no longer be repaired')
