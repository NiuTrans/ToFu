"""CSS-cascade regression for the Project Brain tab panels.

WHY
The tab panels each carry BOTH classes: `project-brain-col` AND `pb-tab-panel`
(+ `pb-tab-panel-active` on the visible one). The show/hide rules and the
`.project-brain-col` layout rule are ALL single-class selectors, so they have
equal specificity and the cascade falls back to SOURCE ORDER. Historically the
hide rule was a bare `.pb-tab-panel{display:none}` that appeared BEFORE
`.project-brain-col{display:flex}` — so `.project-brain-col` won and every panel
stayed `display:flex`, all four stacked, and clicking a tab did nothing visible.
The JS `_selectTab()` toggled the classes correctly the whole time, which is why
the jsdom test (`test_frontend_project_brain_tabs_clamp.py`) passed — jsdom
resolves `classList` but does NOT apply the external stylesheet, so it can't see
a cascade bug. This test closes that gap by resolving the cascade from the REAL
styles.css source: an INACTIVE panel must compute `display:none`, an ACTIVE one
`display:flex`.

NEGATIVE CONTROL is inline (`_resolve_display`): if you re-order the rules so the
bare-specificity hide loses to `.project-brain-col`, the inactive panel resolves
`flex` and the assertion fires — proving the guard is load-bearing.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')

# The element under test: an inactive vs. active tab panel, matching the
# real index.html markup (both classes present).
_INACTIVE_CLASSES = {'project-brain-col', 'pb-tab-panel'}
_ACTIVE_CLASSES = {'project-brain-col', 'pb-tab-panel', 'pb-tab-panel-active'}


def _iter_rules(css_text):
    """Yield (selector, decl_index, declarations_dict) for every simple rule.

    decl_index is the source order (position of the rule) — the cascade
    tie-breaker when specificity is equal.
    """
    idx = 0
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css_text):
        sel_group = m.group(1).strip()
        body = m.group(2)
        # Skip at-rule blocks / comments captured as selectors.
        if sel_group.startswith('@') or not sel_group:
            idx += 1
            continue
        decls = {}
        for part in body.split(';'):
            if ':' in part:
                k, _, v = part.partition(':')
                decls[k.strip()] = v.strip()
        for sel in sel_group.split(','):
            yield sel.strip(), idx, decls
        idx += 1


def _selector_classes(sel):
    """Return the set of class names in a pure class selector, or None if the
    selector uses anything other than class tokens (id/tag/attr/pseudo/combinator)."""
    sel = sel.strip()
    # Reject combinators / descendant / attribute / pseudo / id / tag.
    if re.search(r'[\s>+~\[\]#:]', sel):
        return None
    if not sel.startswith('.'):
        return None
    parts = sel.split('.')
    if parts[0] != '':
        return None  # had a leading tag name
    names = [p for p in parts[1:] if p]
    if not names:
        return None
    return set(names)


def _resolve_display(css_text, element_classes):
    """Resolve the computed `display` for an element with the given class set,
    honoring class-selector specificity (n classes) then source order."""
    winner = None  # (specificity, source_idx, value)
    for sel, idx, decls in _iter_rules(css_text):
        if 'display' not in decls:
            continue
        sel_classes = _selector_classes(sel)
        if sel_classes is None:
            continue
        if not sel_classes.issubset(element_classes):
            continue
        spec = len(sel_classes)
        cand = (spec, idx)
        if winner is None or cand >= (winner[0], winner[1]):
            winner = (spec, idx, decls['display'])
    return winner[2] if winner else None


@pytest.fixture(scope='module')
def css_text():
    with open(CSS, encoding='utf-8') as f:
        return f.read()


def test_inactive_panel_is_hidden(css_text):
    """An inactive panel (project-brain-col + pb-tab-panel) must be display:none."""
    disp = _resolve_display(css_text, _INACTIVE_CLASSES)
    assert disp == 'none', (
        f'inactive Project Brain tab panel resolves display:{disp!r} — it must '
        f'be "none". The hide rule must out-specify .project-brain-col{{display:flex}}, '
        f'otherwise all panels stack and tab clicks do nothing.')


def test_active_panel_is_shown(css_text):
    """The active panel must be display:flex (fills the panel)."""
    disp = _resolve_display(css_text, _ACTIVE_CLASSES)
    assert disp == 'flex', (
        f'active Project Brain tab panel resolves display:{disp!r} — it must be "flex".')


def test_hide_rule_outspecifies_col_layout(css_text):
    """Structural guard: the hide/show rules must be compound (.project-brain-col
    .pb-tab-panel…) so they out-specify the bare .project-brain-col layout rule —
    a bare .pb-tab-panel would tie on specificity and lose on source order."""
    assert '.project-brain-col.pb-tab-panel{' in css_text.replace(' ', ''), \
        'the hide rule must be qualified as .project-brain-col.pb-tab-panel'
    assert '.project-brain-col.pb-tab-panel-active{' in css_text.replace(' ', ''), \
        'the show rule must be qualified as .project-brain-col.pb-tab-panel-active'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
