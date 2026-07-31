"""CLAUDE.md path-drift guard.

Why this exists
---------------
``CLAUDE.md`` is INJECTED INTO EVERY AGENT'S CONTEXT on every turn. Unlike a
normal doc, a stale line here does not sit quietly waiting to be noticed — it
is actively asserted to the agent as ground truth before it has read any code.
An agent that trusts it proposes edits to files that moved, cites line numbers
from a file that became a package, and "verifies" against a path that no longer
exists.

Measured drift when this guard was written: 13 of 91 fully-qualified path
references (14%) pointed at files that no longer existed. Every single one was
the SAME mechanical cause -- the file became a package
(``lib/memory/storage.py`` -> ``lib/memory/storage/``) or the module moved
(``routes/daily_report.py`` -> ``lib/daily_report/``). Those are precisely the
refactors this project performs constantly, which is why hand-maintenance loses:
the person splitting a module has no reason to remember that a 1,600-line rules
doc names their old filename.

What this guard does
--------------------
Scans CLAUDE.md for FULLY-QUALIFIED path references (containing a ``/``, so
they are unambiguous -- a bare ``body.py`` could legitimately mean
``lib/llm/body.py`` and is NOT checked here) and asserts each one resolves on
disk, accepting the package form ``foo/bar/`` for a referenced ``foo/bar.py``.

Deliberately NARROW. It checks only "does this path exist", which is
mechanically decidable and has no false positives. It does NOT try to validate
prose, architecture claims, or line numbers -- an over-eager guard that cries
wolf gets suppressed, and a suppressed guard is worse than none.

Fixing a failure
----------------
Update the path in CLAUDE.md to where the code actually lives. Do NOT add the
path to an ignore list to silence this -- that reintroduces exactly the
stale-context problem the guard exists to prevent.
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CLAUDE_MD = os.path.join(ROOT, 'CLAUDE.md')

# Fully-qualified path inside backticks: must contain a '/' so it is
# unambiguous. Bare basenames (`body.py`) are intentionally NOT checked --
# they are shorthand for a file inside a package named in the surrounding
# tree block, and resolving them would require guessing.
_PATH_RE = re.compile(
    r'`([a-zA-Z0-9_.-]+/[a-zA-Z0-9_./-]+\.(?:py|js|md|json|css|html))`'
)

# Paths that are legitimately absent from a fresh checkout: runtime-generated
# state, user-installed content, or deliberately git-ignored artifacts.
# Keep this list SHORT and justified -- it is not a place to hide drift.
#
# PREFER the gitignore check below over adding an entry here. A path that git
# itself ignores is BY CONSTRUCTION absent from any clean checkout while being
# present in every real deployment, so failing on it is the GUARD's category
# error, not doc rot -- and it fires in CI and in every worktree, i.e. exactly
# where the guard is supposed to be trustworthy. Measured 2026-07-28: three
# correct references (data/config/{api_keys,features,server_config}.json) were
# reported as stale for this reason; all three exist in the live deployment and
# are documented as runtime-created in lib/config_dir.py.
_RUNTIME_ABSENT = {
    # Written at runtime when a user installs a skill package.
    '.tofu/skills/separation-of-concerns-directive.md',
    # Optional operator-supplied config, absent by default.
    'data/config/cross_dc.json',
}


def _git_ignored(rel: str) -> bool:
    """True when git itself ignores `rel` (so a clean checkout cannot have it).

    Uses ``git check-ignore``, which consults the real .gitignore rules rather
    than a second hand-maintained copy of them here.
    """
    try:
        r = subprocess.run(
            ['git', 'check-ignore', '-q', '--', rel],
            cwd=ROOT, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        # No git / no timeout budget: fall back to treating it as NOT ignored,
        # so a genuine stale path still fails rather than being waved through.
        print(f'git check-ignore unavailable for {rel}: {e}')
        return False
    return r.returncode == 0


def _referenced_paths() -> set[str]:
    with open(CLAUDE_MD, encoding='utf-8') as fh:
        text = fh.read()
    return set(_PATH_RE.findall(text))


def _resolves(rel: str) -> bool:
    """True when `rel` exists as a file, or as the package that replaced it.

    A very common refactor here is ``foo/bar.py`` -> ``foo/bar/__init__.py``
    (facade-preserving package). A doc line naming the old single-file form is
    still pointing a reader at the right place, so accept the package form.
    """
    full = os.path.join(ROOT, rel)
    if os.path.exists(full):
        return True
    if rel.endswith('.py'):
        as_pkg = os.path.join(ROOT, rel[: -len('.py')], '__init__.py')
        if os.path.exists(as_pkg):
            return True
    return False


def test_claude_md_exists():
    assert os.path.exists(CLAUDE_MD), (
        'CLAUDE.md is missing — it is injected into every agent context and '
        'is the project rules SSOT.'
    )


def test_claude_md_has_checkable_paths():
    """Sanity: the regex still matches. Guards against a silent no-op."""
    paths = _referenced_paths()
    assert len(paths) >= 50, (
        f'Only {len(paths)} fully-qualified paths found in CLAUDE.md — the '
        'extraction regex likely broke (it matched 91 when written). A guard '
        'that scans nothing always passes and protects nothing.'
    )


def test_claude_md_paths_are_not_stale():
    """Every fully-qualified path in CLAUDE.md MUST resolve on disk.

    CLAUDE.md is force-fed to every agent before it reads any code, so a stale
    path is not a doc nit — it is misinformation delivered with authority.
    """
    stale = sorted(
        p for p in _referenced_paths()
        if p not in _RUNTIME_ABSENT and not _resolves(p) and not _git_ignored(p)
    )
    if stale:
        listing = '\n'.join(f'    {p}' for p in stale)
        pytest.fail(
            f'{len(stale)} path(s) referenced in CLAUDE.md no longer exist:\n'
            f'{listing}\n\n'
            'CLAUDE.md is injected into EVERY agent context, so these are '
            'asserted to the agent as ground truth before it reads any code.\n'
            'Fix by pointing each reference at the real location (the usual '
            'cause is a file that became a package, or a module that moved).\n'
            'Do NOT silence this by adding entries to _RUNTIME_ABSENT '
            '(git-ignored runtime files are already excluded automatically).'
        )
