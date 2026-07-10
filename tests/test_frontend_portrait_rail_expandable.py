"""Portrait-mode project-rail expandability drift-guard.

Symptom this guards against (fixed 2026-07-08): on a drawer viewport (portrait
phone ≤768px, or portrait touch-tablet ≤1024px + coarse pointer) the vertical
project rail was FORCE-locked to icon-only with `!important`, overriding the
`.rail-collapsed` toggle the expand/collapse chevron drives. So tapping the
chevron toggled the class but the width/label locks ignored it — folder NAMES
could never be shown ("folders in portrait mode cannot be expanded").

Root-cause fix: the drawer-block icon-only compaction is gated on
`.sidebar.has-rail.rail-collapsed …` (not `.sidebar.has-rail …`), mirroring the
desktop base rules — so the drawer respects the SAME toggle as desktop: default
labeled/expandable, tap the chevron to collapse to an icon strip.

INVARIANT (this test): inside a drawer `@media` block, any rule that applies the
icon-only rail compaction (hiding `.folder-tab-name` via display:none, or
shrinking `.project-rail` to the collapsed width) MUST be scoped to
`.rail-collapsed`. A rule that hides the folder name on `.sidebar.has-rail`
WITHOUT `.rail-collapsed` is the bug (labels unconditionally hidden → the rail
can never expand).

Env-independent: parses static/styles.css directly. NEUTER included.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _iter_media_blocks(css: str):
    """Yield (header, body) for every top-level @media block, brace-matched."""
    for m in re.finditer(r'@media([^{]*)\{', css):
        header = m.group(1)
        brace = m.end() - 1
        depth = 0
        i = brace
        while i < len(css):
            if css[i] == '{':
                depth += 1
            elif css[i] == '}':
                depth -= 1
                if depth == 0:
                    yield header, css[brace + 1:i]
                    break
            i += 1


def _is_drawer_header(header: str) -> bool:
    """A drawer viewport block: phone ≤768 union, or ≤1024 + coarse pointer.

    Matches the rail-compaction block `@media(max-width:768px),(max-width:1024px)
    and (pointer:coarse)`. We accept any block whose condition mentions a
    max-width ≤1024 AND a folder-rail selector (the block we care about); the
    positive test below pins the exact one.
    """
    widths = [int(n) for n in re.findall(r'max-width:\s*(\d+)px', header)]
    return bool(widths) and min(widths) <= 1024


def _rules_hiding_folder_name(body: str):
    """Yield each selector-list that sets .folder-tab-name to display:none."""
    compact = re.sub(r'\s+', '', body)
    for mm in re.finditer(r'([^{}]*\.folder-tab-name[^{}]*)\{([^{}]*)\}', compact):
        selectors, decls = mm.group(1), mm.group(2)
        if 'display:none' in decls:
            yield selectors


def _offenders(css: str) -> list[str]:
    """Drawer blocks that hide the folder NAME without a .rail-collapsed scope."""
    bad = []
    for header, body in _iter_media_blocks(css):
        if not _is_drawer_header(header):
            continue
        for selectors in _rules_hiding_folder_name(body):
            # Every selector in the (comma-joined) list that carries
            # .folder-tab-name must be scoped to .rail-collapsed.
            for sel in selectors.split(','):
                if '.folder-tab-name' in sel and 'rail-collapsed' not in sel:
                    bad.append(f'@media{header.strip()} :: {sel.strip()}')
    return bad


def test_drawer_rail_name_hide_is_collapse_scoped():
    """In a drawer viewport the folder name may only be hidden when the rail is
    .rail-collapsed — otherwise the rail can never expand in portrait."""
    css = _read(CSS)
    offenders = _offenders(css)
    assert not offenders, (
        'A drawer @media block hides .folder-tab-name WITHOUT a .rail-collapsed '
        'scope — the project rail is force-locked to icon-only and cannot be '
        'expanded in portrait. Gate the compaction on '
        '`.sidebar.has-rail.rail-collapsed`:\n' + '\n'.join(offenders))


def test_drawer_block_does_scope_the_compaction():
    """Positive lock: the drawer rail-compaction block that hides
    .folder-tab-name exists AND scopes it on .rail-collapsed (so the fix is
    actually present, not merely absent)."""
    css = _read(CSS)
    found = False
    for header, body in _iter_media_blocks(css):
        if not _is_drawer_header(header):
            continue
        for selectors in _rules_hiding_folder_name(body):
            if '.folder-tab-name' in selectors:
                found = True
                assert 'rail-collapsed' in selectors, (
                    'the drawer folder-name hide lost its .rail-collapsed scope '
                    f'— portrait rail unexpandable again. header=@media{header.strip()}')
    assert found, 'drawer .folder-tab-name compaction block not found (structure changed?)'


def test_nc_unscoped_hide_is_flagged():
    """POISONED-FIXTURE NC: inject a drawer block that hides .folder-tab-name
    UNCONDITIONALLY (no .rail-collapsed) → the scanner must FLAG it. Proves the
    guard is load-bearing."""
    css = _read(CSS)
    assert not _offenders(css), 'real CSS is not clean; fix before NC'
    poisoned = css + (
        '\n@media(max-width:1024px) and (pointer:coarse){'
        '.sidebar.has-rail .folder-tab-name{display:none}}\n')
    offenders = _offenders(poisoned)
    assert offenders, (
        'the scanner did NOT catch an injected unconditional folder-name hide '
        'in a drawer block — it is not detecting the regression class.')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
