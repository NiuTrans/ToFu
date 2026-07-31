"""Matrix override-editor cascade-repair drift guard.

BACKGROUND (2026-07-24 fix)
---------------------------
The per-cell override editor (`.stg-mx-editor`, built by
`static/js/settings/providers/access_matrix.js` `_editMatrixCell`) renders
inside the settings shell, which carries BOTH classes `.modal.settings-panel`
(index.html:942). Two legacy generic-modal rules in `static/styles.css`
therefore match inside the editor and out-specify its single-class rules in
`static/settings.css`:

    .modal label { display:block; …; margin-bottom:6px }        (styles.css:2959, spec 0,1,1)
        vs  .stg-mxe-chk { display:inline-flex }                (settings.css,    spec 0,1,0)
    .modal input,.modal select { width:100%; padding:10px 12px;
                                 margin-bottom:16px; … }        (styles.css:2959, spec 0,1,1)
        vs  (nothing sizing the checkbox in settings.css)

Visible result (owner screenshot 2026-07-24): each override checkbox
(覆盖限速 / 覆盖能力) stretches to width:100% of its label — Blink paints the
~13px checkbox square CENTERED in that full-width box while the label text
wraps to the next line, so the checkbox appears ABOVE its own label with a
tall empty gap, and the RPM number input's margin-bottom:16px inflates its
row. The panel looks "weird": two orphaned checkboxes floating above their
labels.

The fix re-asserts the editor layout in settings.css at higher specificity:

    .settings-panel .stg-mx-editor .stg-mxe-chk                          (0,3,0)
    .settings-panel .stg-mx-editor .stg-mxe-chk input[type="checkbox"]   (0,4,1)
    .settings-panel .stg-mx-editor .stg-mxe-ovrow input[type="number"]   (0,4,1)

settings.css also loads AFTER styles.css (index.html:29-30), so it wins
specificity ties.

INVARIANT
---------
1. The three repair rules exist in settings.css with their load-bearing
   declarations (inline-flex on the label; width:auto on the checkbox;
   margin-bottom:0 on the number input).
2. Each repair selector's specificity strictly exceeds the legacy modal
   selectors it competes with, and index.html keeps the styles.css →
   settings.css load order.
3. Ratchet: the legacy clobbering rules are still detectable in styles.css.
   If a future cleanup deletes `.modal label{display:block}` /
   `.modal input{width:100%}`, this test flips red to signal that the repair
   block in settings.css may now be dead code — the same
   "no trivially-green guard" pattern as
   tests/test_frontend_chat_container_no_smooth_scroll.py.

MANUAL NEUTER-PROOF (run once at authoring; not automated)
----------------------------------------------------------
Delete the `.settings-panel .stg-mx-editor …` repair block from
static/settings.css, then:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \
        tests/test_frontend_matrix_editor_cascade.py
`test_repair_rules_reassert_editor_layout` MUST fail. Restore and confirm the
whole file goes green again. Result recorded in JOURNAL.md.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SETTINGS_CSS = os.path.join(ROOT, 'static', 'settings.css')
STYLES_CSS = os.path.join(ROOT, 'static', 'styles.css')
INDEX_HTML = os.path.join(ROOT, 'index.html')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _strip_comments(css: str) -> str:
    """Remove /* ... */ comments — load-bearing: the repair block's own
    commentary mentions `.modal label{display:block}` etc. and would
    false-positive a naive substring search.

    Delegates to the SINGLE shared implementation (charter #24).

    EQUIVALENCE, MEASURED on the real 22k-line static/styles.css rather than
    assumed: the local ``re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)`` this
    replaced and ``strip_comments(lang='css', inline=True)`` produce an
    IDENTICAL selector set (6466 rules, 0 selectors unique to either side) and
    a byte-identical whitespace-stripped content signature. They differ only in
    LINE NUMBERING -- the shared one blanks comment lines to preserve line
    count, the local one deleted them (20295 vs 22400 lines) -- which leaves 25
    rule bodies differing in whitespace alone. Every assertion here is
    whitespace-insensitive (substring / regex on a rule body), so the swap is
    behaviour-preserving; the suite is the proof.

    Keeping N copies of "what counts as a comment" is what let a fix land in one
    copy and not its duplicate -- incident 3 in the shared module's docstring.
    """
    from tests._source_scan import strip_comments
    return strip_comments(css, lang='css', inline=True)


_LEAF_BLOCK = re.compile(r'([^{}]+)\{([^{}]*)\}', re.DOTALL)


def _iter_leaf_rules(path: str):
    """Yield (selector_text, declarations_text, line) for every innermost
    CSS rule. @media wrappers contain `{` in their bodies, so `[^{}]*`
    naturally isolates the leaf rules where declarations actually live."""
    src = _strip_comments(_read(path))
    for m in _LEAF_BLOCK.finditer(src):
        line = src[:m.end(1)].count('\n') + 1
        yield m.group(1).strip(), m.group(2), line


def _split_selector_list(selector_text: str):
    """Split a selector group by commas at zero paren/bracket depth."""
    parts, depth, buf = [], 0, []
    for ch in selector_text:
        if ch in '([':
            depth += 1
        elif ch in ')]':
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    parts.append(''.join(buf).strip())
    return [p for p in parts if p]


def _decls(decls_text: str) -> dict:
    """Very small declaration parser: prop -> value (last wins, !important
    stripped but flagged with a leading '!')."""
    out = {}
    for chunk in decls_text.split(';'):
        if ':' not in chunk:
            continue
        prop, val = chunk.split(':', 1)
        prop = prop.strip().lower()
        val = val.strip()
        if not prop:
            continue
        imp = val.lower().endswith('!important')
        if imp:
            val = val[: -len('!important')].strip()
        out[prop] = ('!' if imp else '') + ' '.join(val.split())
    return out


def _specificity(sel: str) -> tuple:
    """Approximate (a, b, c) specificity for the simple selectors used here.

    a = #ids, b = classes + attributes + pseudo-classes (incl. :not args),
    c = type names + pseudo-elements. Sufficient — and only used — to compare
    the repair selectors against the legacy `.modal …` selectors.
    """
    a = len(re.findall(r'#[\w-]+', sel))
    b = len(re.findall(r'\.[\w-]+', sel))
    b += len(re.findall(r'\[[^\]]*\]', sel))
    b += len(re.findall(r':(?!:)[a-zA-Z-]+(?:\([^)]*\))?', sel))
    s = re.sub(r'#[\w-]+|\.[\w-]+|\[[^\]]*\]|:+[\w-]+(?:\([^)]*\))?', ' ', sel)
    c = len([t for t in re.split(r'[\s>+~*]+', s) if re.match(r'^[a-zA-Z][\w-]*$', t)])
    return (a, b, c)


# ── repair-rule contract (kept in sync with the settings.css fix block) ──

_REPAIR_RULES = {
    '.settings-panel .stg-mx-editor .stg-mxe-chk': {
        'display': 'inline-flex',
        'margin-bottom': '0',
        'text-transform': 'none',
    },
    '.settings-panel .stg-mx-editor .stg-mxe-chk input[type="checkbox"]': {
        'width': 'auto',
        'padding': '0',
        'margin': '0',
    },
    '.settings-panel .stg-mx-editor .stg-mxe-ovrow input[type="number"]': {
        'margin-bottom': '0',
    },
}

# Legacy selectors the repair competes with (must be out-specified). The
# substring is matched against each styles.css selector part; the expected
# clobbering declaration is verified to still exist by the ratchet test.
_LEGACY_CLOBBERERS = {
    '.settings-panel .stg-mx-editor .stg-mxe-chk': [
        ('.modal label', 'display', 'block'),
        ('.settings-panel label', 'text-transform', 'uppercase'),
    ],
    '.settings-panel .stg-mx-editor .stg-mxe-chk input[type="checkbox"]': [
        ('.modal input', 'width', '100%'),
    ],
    '.settings-panel .stg-mx-editor .stg-mxe-ovrow input[type="number"]': [
        ('.modal input', 'margin-bottom', '16px'),
    ],
}


def _find_decl(path: str, selector_part: str):
    """Return the declaration dict of the first leaf rule in `path` whose
    selector list contains an EXACT part equal to `selector_part`, else None.
    """
    for sel_text, decls_text, _line in _iter_leaf_rules(path):
        for part in _split_selector_list(sel_text):
            if part == selector_part:
                return _decls(decls_text)
    return None


# ─────────────────────────── the invariants ───────────────────────────


def test_repair_rules_reassert_editor_layout():
    """The three cascade-repair rules must exist in settings.css with their
    load-bearing declarations. NEUTER-sensitive: deleting the repair block
    (or any listed declaration) fails this test."""
    settings_rules = {}
    for sel_text, decls_text, line in _iter_leaf_rules(SETTINGS_CSS):
        for part in _split_selector_list(sel_text):
            settings_rules.setdefault(part, (_decls(decls_text), line))

    problems = []
    for sel, expected in _REPAIR_RULES.items():
        hit = settings_rules.get(sel)
        if not hit:
            problems.append(f'missing repair rule `{sel}` in settings.css')
            continue
        decls, line = hit
        for prop, want in expected.items():
            got = decls.get(prop)
            if got != want:
                problems.append(
                    f'settings.css:{line}: `{sel}` — expected '
                    f'{prop}:{want}, found {prop}:{got!r}'
                )
    assert not problems, (
        'matrix-editor cascade repair broken — the settings shell is '
        '`.modal.settings-panel`, so without these overrides the legacy '
        '`.modal label{display:block}` / `.modal input{width:100%}` rules '
        'stack each override checkbox ABOVE its own label.\n\n'
        + '\n'.join(problems)
    )


def test_repair_selectors_outspecify_legacy_modal_rules():
    """Each repair selector must strictly out-specify the legacy selectors it
    competes with, and index.html must keep the styles.css → settings.css
    load order (settings.css wins ties)."""
    problems = []
    for repair_sel, competitors in _LEGACY_CLOBBERERS.items():
        repair_spec = _specificity(repair_sel)
        for legacy_sel, _prop, _val in competitors:
            legacy_spec = _specificity(legacy_sel)
            if not repair_spec > legacy_spec:
                problems.append(
                    f'`{repair_sel}` spec {repair_spec} does not beat '
                    f'`{legacy_sel}` spec {legacy_spec}'
                )
    assert not problems, '\n'.join(problems)

    html = _read(INDEX_HTML)
    i_styles = html.find('static/styles.css')
    i_settings = html.find('static/settings.css')
    assert 0 < i_styles < i_settings, (
        'index.html stylesheet order changed — settings.css must load AFTER '
        'styles.css so the repair wins specificity ties'
    )


def test_legacy_clobberers_still_detected():
    """Ratchet: the legacy clobbering declarations are still present in
    styles.css. If a future cleanup removes `.modal label{display:block}` /
    `.modal input{width:100%;…margin-bottom:16px}` properly, THIS test must
    flip red — signalling the repair block in settings.css may now be dead
    code (remove both together)."""
    problems = []
    seen = set()
    for competitors in _LEGACY_CLOBBERERS.values():
        for legacy_sel, prop, want in competitors:
            if (legacy_sel, prop) in seen:
                continue
            seen.add((legacy_sel, prop))
            decls = _find_decl(STYLES_CSS, legacy_sel)
            if decls is None:
                problems.append(f'`{legacy_sel}` rule no longer found in styles.css')
            elif decls.get(prop) != want:
                problems.append(
                    f'`{legacy_sel}` in styles.css no longer sets '
                    f'{prop}:{want} (found {decls.get(prop)!r})'
                )
    assert not problems, (
        'legacy modal rules changed — review whether the settings.css repair '
        'block is still needed:\n' + '\n'.join(problems)
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
