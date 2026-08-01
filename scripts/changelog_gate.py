#!/usr/bin/env python3
"""The single source of truth for "is VERSION documented in the CHANGELOG?".

WHY THIS FILE EXISTS
--------------------
Releasing is not a deliberate act in this repo — it is a TRIGGER. The
``build-desktop.yml`` gate builds whenever a push to main names a VERSION with
no complete release behind it, and the release step sets ``make_latest: "true"``.
So the next push by ANY of the concurrent sibling sessions on this shared tree
publishes whatever the docs happen to say at that instant, and pins it as the
release users see.

Measured 2026-07-31: ``VERSION`` is 0.15.2 while ``CHANGELOG.md``'s newest
version heading is ``## [0.10.0]`` — nine releases (0.11.0 … 0.15.2) have no
entry at all, and 91 lines sit stranded under ``[Unreleased]``. Nothing in the
repo could notice: ``grep -rl CHANGELOG tests/ scripts/`` returned NOTHING
before this module existed. The release plumbing had gates for "is the tag
real?", "does the release object exist?", "are the assets present?" and "are
they a plausible size?" — and no gate at all for "can a user find out what
changed?".

THE ROOT-CAUSE SHAPE
--------------------
"Remember to write the CHANGELOG" was a property of a human's memory. Every
other release invariant in this pipeline was long ago moved out of memory and
into a gate; this one was not, so it drifted for nine consecutive versions
without a single failure anywhere. The fix is not to write the missing entries
(that repairs today's data and leaves tomorrow's to memory again) — it is to
make an undocumented VERSION FAIL THE BUILD, so the drift can only ever be one
version deep.

This mirrors ``scripts/release_assets.py`` deliberately: the rule lives here
ONCE and both the workflow gate and the pytest guard call it, so the CI check
and the local check can never disagree about what "documented" means.

WHAT COUNTS AS DOCUMENTED
-------------------------
A heading ``## [<version>]`` for the exact VERSION string. Deliberately NOT
satisfied by:

  * ``## [Unreleased]`` — that is precisely the state that stranded 91 lines;
  * a bare mention of the version anywhere in prose — a negative/positive
    grep over free text is satisfiable by a sentence that merely NAMES the
    version (the project has been bitten by prose satisfying a structural
    assertion before), so the check is anchored to the heading GRAMMAR;
  * a heading for a DIFFERENT version — the common near-miss when someone
    bumps VERSION and forgets the changelog.

Exit codes mirror release_assets.py so the workflow can treat them the same
way::

    0  DOCUMENTED    — VERSION has its own heading
    1  UUNDOCUMENTED — no heading for VERSION (block the release)
    2  UNDETERMINED  — CHANGELOG.md unreadable/absent

Unlike the asset gate, UNDETERMINED here is NOT fail-open. A missing asset
resolves toward shipping because a redundant build is cheap and a missed
release is the expensive mistake. Here the asymmetry inverts: shipping an
undocumented release is the expensive, user-visible, hard-to-retract mistake
(``make_latest`` pins it), and the cheap correction is one commit. So an
unreadable CHANGELOG blocks.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

EXIT_DOCUMENTED = 0
EXIT_UNDOCUMENTED = 1
EXIT_UNDETERMINED = 2

#: Matches a Keep-a-Changelog release heading: ``## [1.2.3]`` optionally
#: followed by ``- 2026-05-09``. Anchored at line start with ``^##`` so a
#: heading nested in a code fence or quoted in prose does not count.
_HEADING_RE = re.compile(r'^##\s*\[(?P<version>[^\]]+)\]', re.MULTILINE)

#: The placeholder that must never be accepted as a version's entry.
UNRELEASED_LABEL = 'Unreleased'


def documented_versions(changelog_text: str) -> list[str]:
    """Every version that has its own ``## [x.y.z]`` heading, in file order.

    ``[Unreleased]`` is excluded: it is a staging area, not a released
    version, and treating it as one is the exact defect this module closes.
    """
    out: list[str] = []
    for m in _HEADING_RE.finditer(changelog_text or ''):
        label = (m.group('version') or '').strip()
        if not label or label.casefold() == UNRELEASED_LABEL.casefold():
            continue
        out.append(label)
    return out


def is_documented(version: str, changelog_text: str) -> bool:
    """True iff ``version`` has its own release heading in ``changelog_text``."""
    want = (version or '').strip()
    if not want:
        return False
    return want in documented_versions(changelog_text)


def undocumented_report(version: str, changelog_text: str) -> str:
    """The operator-facing explanation for a blocked release."""
    known = documented_versions(changelog_text)
    newest = known[0] if known else '(none)'
    return (
        f'UNDOCUMENTED — VERSION is {version} but CHANGELOG.md has no '
        f'"## [{version}]" heading.\n'
        f'  newest documented version: {newest}\n'
        f'  total documented versions: {len(known)}\n'
        '\n'
        '  Releasing here is automatic: a push to main whose VERSION has no\n'
        '  complete release triggers a build, and the release is published\n'
        '  with make_latest=true. Shipping now would pin a release whose\n'
        '  changelog does not mention it as the version users see.\n'
        '\n'
        f'  Fix: add a "## [{version}] - <date>" section to CHANGELOG.md\n'
        '  (move the relevant lines out of [Unreleased]).')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description='Decide whether VERSION is documented in the CHANGELOG.')
    ap.add_argument('--version', metavar='X.Y.Z',
                    help='version to check (default: read the VERSION file)')
    ap.add_argument('--changelog', metavar='PATH', default='CHANGELOG.md',
                    help='path to the changelog (default: CHANGELOG.md)')
    ap.add_argument('--version-file', metavar='PATH', default='VERSION',
                    help='path to the VERSION file (default: VERSION)')
    args = ap.parse_args(argv)

    version = (args.version or '').strip()
    if not version:
        vf = Path(args.version_file)
        try:
            version = vf.read_text(encoding='utf-8').strip()
        except OSError as e:
            print(f'UNDETERMINED — cannot read {vf}: {e}')
            return EXIT_UNDETERMINED
    if not version:
        print('UNDETERMINED — version is empty.')
        return EXIT_UNDETERMINED

    cl = Path(args.changelog)
    try:
        text = cl.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        # NOT fail-open: see the module docstring. An unreadable changelog
        # cannot prove the release is documented, and publishing is the
        # expensive direction here.
        print(f'UNDETERMINED — cannot read {cl}: {e}')
        return EXIT_UNDETERMINED

    if is_documented(version, text):
        print(f'DOCUMENTED — CHANGELOG.md has a "## [{version}]" section.')
        return EXIT_DOCUMENTED
    print(undocumented_report(version, text))
    return EXIT_UNDOCUMENTED


if __name__ == '__main__':
    raise SystemExit(main())
