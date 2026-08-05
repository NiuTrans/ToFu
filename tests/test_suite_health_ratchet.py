"""Meta-guard: the test suite's own health must not silently degrade.

WHY THIS EXISTS
---------------
This project's JOURNAL records SEVEN separate incidents of one family
("守卫过期家族"): a guard test stopped guarding anything and nobody noticed
until it was found by accident. The charter's verdict is blunt — "守卫死了没人
知道,比没有守卫更危险——因为它制造了『有保护』的错觉."

Every one of those incidents was individually fixed. None of them was
*prevented from recurring*, because the suite had no way to observe its own
health: with ~1160 test files and ~320k lines, "read them one by one" is not a
review anyone can actually perform, so the failure modes accumulated unseen.

``scripts/audit_tests.py`` performs that review mechanically (AST only — no
imports, no execution, ~6s over the whole suite). THIS test is what makes it
binding: a one-way ratchet. Debt can be paid down; it cannot silently
re-accumulate. When a category grows, CI names the category, the file and the
line.

WHAT IS GATED (and why these are the failure modes that matter)
    A0  a test file that does not parse
    A1  a test whose only exit is pytest.skip() — structurally cannot fail, so
        it reports nothing while looking like a check
    A2  the call under test wrapped in bare ``except Exception: pass`` — "it
        worked" and "it exploded" produce the same green result
    A   no assertion anywhere in the test's 1-2 level call closure
    B   ``assert ... or True`` — green by construction
    C   assertions swallowed by a try/except that cannot fail
    D   unconditional skip/xfail — a dead test on life support
    E   a source-anchor guard whose needle is absent from the file it reads:
        THE detector for the 守卫过期家族
    F   a reference to a repo path that no longer exists — how a scanning guard
        degrades into scanning nothing while staying green

Category G (implementation-face) is measured but NOT gated: the charter
explicitly permits reading shipped source in RATCHET guards.

HOW TO RESPOND TO A FAILURE HERE
    Fix the finding. Do NOT raise the baseline to make it green — that is the
    exact move the charter forbids ("禁止:为了让数字变绿而上调 BASELINE"),
    and it converts a real signal back into the illusion of protection.
    A baseline may only be regenerated DOWNWARD, deliberately:
        python3 scripts/audit_tests.py --write-baseline
    and the commit must say which findings were fixed to earn the new number.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from lib.mcp.registry import is_opensource_build

pytestmark = pytest.mark.unit

# The audit baseline (tests/audit_baseline.json) is a census of the SOURCE
# tree. The opensource export is structurally different — it deliberately
# strips files the suite legitimately references (scripts/cache_waste_report.py
# et al, see tests/test_gitignore_covers_export_excludes.py), which shows up
# as phantom F-category growth against a baseline those files were counted in.
# The two baseline-comparison guards therefore self-skip on a public build.
_OPENSOURCE = is_opensource_build()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
AUDIT = os.path.join(ROOT, 'scripts', 'audit_tests.py')
BASELINE_PATH = os.path.join(HERE, 'audit_baseline.json')


def _census() -> dict:
    """Run the audit tool and return its JSON census."""
    out = os.path.join(
        os.environ.get('TMPDIR', '/tmp'), f'tofu_census_{os.getpid()}.json')
    r = subprocess.run([sys.executable, AUDIT, '--json', out],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, (
        f'scripts/audit_tests.py failed (rc={r.returncode}):\n'
        f'{(r.stderr or r.stdout)[-2000:]}')
    try:
        with open(out, encoding='utf-8') as f:
            return json.load(f)
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def test_audit_tool_is_runnable():
    """The census tool itself must work — a broken tool would make every other
    assertion here vacuously green."""
    assert os.path.isfile(AUDIT), 'scripts/audit_tests.py is missing'
    census = _census()
    assert census['files'] > 500, (
        f"census only saw {census['files']} test files — it is not reaching the "
        f'suite (expected >1000). A collapsed scan makes this whole guard inert.')
    assert census['tests'] > 5000, (
        f"census only found {census['tests']} test functions — scan collapsed")


@pytest.mark.skipif(_OPENSOURCE, reason='audit baseline is source-tree-specific; the export deliberately strips referenced files')
def test_no_category_regressed():
    """One-way ratchet over every gated failure mode."""
    with open(BASELINE_PATH, encoding='utf-8') as f:
        baseline = json.load(f)['counts']
    counts = _census()['counts']

    grew = []
    for cat, base in sorted(baseline.items()):
        now = counts.get(cat, 0)
        if now > base:
            grew.append((cat, base, now))

    if grew:
        detail = []
        for cat, base, now in grew:
            detail.append(f'  [{cat}] {base} → {now}  (+{now - base})')
        raise AssertionError(
            'Test-suite health regressed — new findings in these categories:\n'
            + '\n'.join(detail)
            + '\n\nSee the offending file:line with:\n'
            + '\n'.join(f'  python3 scripts/audit_tests.py --category {c}'
                        for c, _, _ in grew)
            + '\n\nFIX THE FINDING. Do not raise tests/audit_baseline.json to '
              'silence it (charter: 禁止为了让数字变绿而上调 BASELINE) — a '
              'baseline may only move DOWN, and the commit must say which '
              'findings were fixed.')


@pytest.mark.skipif(_OPENSOURCE, reason='audit baseline is source-tree-specific; the export deliberately strips referenced files')
def test_baseline_is_not_loose():
    """The baseline must track reality DOWNWARD too.

    Without this, a category could be cleaned up and then quietly re-dirtied
    back to the stale (higher) baseline with CI never objecting — the ratchet
    would only be one-way on paper. Deliberately mirrors the tightness check on
    the frontend ratchets (which had to be fixed for exactly this reason: they
    ``skip``ped instead of failing, so a loose baseline was unobservable).
    """
    with open(BASELINE_PATH, encoding='utf-8') as f:
        baseline = json.load(f)['counts']
    counts = _census()['counts']
    loose = [(c, b, counts.get(c, 0)) for c, b in sorted(baseline.items())
             if counts.get(c, 0) < b]
    assert not loose, (
        'tests/audit_baseline.json is LOOSE — these categories are now cleaner '
        'than the recorded baseline, so regressions would go unnoticed up to '
        'the stale bound:\n'
        + '\n'.join(f'  [{c}] baseline={b}, actual={n}' for c, b, n in loose)
        + '\n\nRegenerate: python3 scripts/audit_tests.py --write-baseline')


def test_event_contract_scan_targets_resolve():
    """Cross-check on the specific failure mode that motivated this file.

    ``tests/test_event_registry.py`` scans a list of emit sites for SSE event
    types. Measured 2026-07-27: 10 of its 21 listed paths had ceased to exist
    (each split into a package), the scanner skipped missing paths silently, and
    HALF the emit surface went unverified while the test stayed green. Its own
    header comment documented an EARLIER instance of the same accident.

    That test now fails on an unresolvable target. This assertion is the
    independent tripwire: if someone reintroduces the silent-skip pattern there,
    the F-category ratchet above catches the dead path, and this catches the
    behaviour directly.
    """
    sys.path.insert(0, ROOT)
    from tests.test_event_registry import (
        _BACKEND_FILES, _FRONTEND_FILES, _resolve_scan_targets)

    back_ok, back_bad = _resolve_scan_targets(_BACKEND_FILES)
    front_ok, front_bad = _resolve_scan_targets(_FRONTEND_FILES)
    assert not back_bad and not front_bad, (
        f'event-contract scan targets do not resolve: {back_bad + front_bad}')
    assert len(back_ok) >= 20, (
        f'event-contract backend scan reaches only {len(back_ok)} file(s) — '
        'it has drifted off the real emit surface')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
