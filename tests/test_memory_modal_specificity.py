"""Patch→fundamental #4 (pilot): the memory-modal input styling is won by
NORMAL cascade, not by !important — via excluding the modal at the SOURCE of the
over-broad tofu bleed (`[data-theme="tofu"] .modal:not(.memory-modal) input`).

WHY A TEST (owner-held acceptance criterion)
--------------------------------------------
The pilot deleted ~18 `!important` lines from the memory-modal input cluster,
justified by "provable specificity math". Unproven specificity math is exactly
what silently regresses when someone later edits styles.css (e.g. widens the
tofu rule back to `.modal input`, or drops the `:not()`). This encodes the
invariant instead of asserting it. jsdom CANNOT do this — it resolves
`classList` but does not apply the external stylesheet or resolve cascade
specificity. So we parse the REAL styles.css and resolve the winning rule with a
correct CSS specificity calculator (a,b,c tuple + source order tie-break).

Three things (owner-specified):
  1. DELETION — every `.memory-modal .memory-*` input rule carries NO !important.
  2. EXCLUSION IS LOAD-BEARING (NC-1) — with the shipped CSS, a `.memory-modal`
     input's `background`/`border` resolve to its OWN `--m-*` tokens, NOT the
     tofu bleed (`#F5F3ED` / `rgba(184,176,160,0.4)`). Neuter: revert the tofu
     selector `:not(.memory-modal) input` → `.modal input` → the bleed now wins
     (higher specificity: it has the [data-theme] attribute the memory rule
     lacks) → the assertion flips. Restore byte-identical.
  3. NO OVER-NARROWING (NC-2 / control) — a GENERIC tofu modal input (a `.modal`
     that is NOT `.memory-modal`, e.g. the browser/apply modal) STILL resolves to
     the tofu styling under the shipped `:not()` rule — proving the exclusion
     removed memory-modal ONLY, not the whole theme.

The NC-1 neuter is applied ON DISK to the real styles.css in a subprocess and
restored byte-identical (mirrors the sibling conversions' double-neuter form).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')


# ─────────────────────────── CSS specificity engine ───────────────────────────

def _specificity(selector: str) -> tuple[int, int, int]:
    """Compute the (a,b,c) specificity of a selector.

    a = #id, b = #class + #attr + #pseudo-class, c = #type + #pseudo-element.
    `:not(...)` / `:is(...)` contribute their argument's specificity (per CSS
    Selectors L4 / L3 for :not). Combinators and the universal selector add 0.
    Good enough for the descendant + attribute + :not() selectors in play here.
    """
    sel = selector.strip()
    a = b = c = 0

    # Pull out and recurse into :not()/:is()/:where(:where=0) argument specificity.
    def _take_functional(name, s):
        nonlocal a, b, c
        pat = re.compile(r':%s\(' % name)
        out = []
        i = 0
        while i < len(s):
            m = pat.search(s, i)
            if not m:
                out.append(s[i:])
                break
            out.append(s[i:m.start()])
            # find matching close paren
            depth = 1
            j = m.end()
            while j < len(s) and depth:
                if s[j] == '(':
                    depth += 1
                elif s[j] == ')':
                    depth -= 1
                j += 1
            arg = s[m.end():j - 1]
            if name != 'where':  # :where() contributes 0
                aa, bb, cc = _specificity(arg)
                a += aa; b += bb; c += cc
            i = j
        return ''.join(out)

    for fn in ('not', 'is', 'where'):
        sel = _take_functional(fn, sel)

    # Remove combinators → treat as space-separated compound list.
    sel = re.sub(r'\s*[>+~]\s*', ' ', sel)

    for tok in sel.split():
        # id
        a += len(re.findall(r'#[\w-]+', tok))
        # attributes  [data-theme="tofu"]
        b += len(re.findall(r'\[[^\]]*\]', tok))
        # classes
        b += len(re.findall(r'\.[\w-]+', tok))
        # pseudo-elements ::x  (must count before pseudo-classes)
        pe = re.findall(r'::[\w-]+', tok)
        c += len(pe)
        # pseudo-classes :x  (excluding the :: already counted and the functional
        # ones we stripped)
        pc = re.findall(r'(?<!:):[\w-]+', tok)
        b += len(pc)
        # type / element (a bare leading tag token, e.g. `input`, `div`)
        stripped = re.sub(r'(::?[\w-]+(\([^)]*\))?|\.[\w-]+|#[\w-]+|\[[^\]]*\])', '', tok)
        if stripped and re.match(r'^[\w-]+$', stripped):
            c += 1
    return (a, b, c)


def _iter_rules(css_text: str):
    """Yield (selector, source_idx, declarations_dict) for every simple rule.
    Splits comma selector groups; skips at-rule headers / comments."""
    idx = 0
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css_text):
        sel_group = m.group(1)
        # strip block comments that leaked into the selector capture
        sel_group = re.sub(r'/\*.*?\*/', '', sel_group, flags=re.DOTALL).strip()
        body = m.group(2)
        if not sel_group or sel_group.startswith('@'):
            idx += 1
            continue
        decls = {}
        for part in body.split(';'):
            if ':' in part:
                k, _, v = part.partition(':')
                decls[k.strip().lower()] = v.strip()
        for sel in sel_group.split(','):
            s = sel.strip()
            if s:
                yield s, idx, decls
        idx += 1


class _Elem:
    """A test element: tag + set of classes + ancestor context flags."""
    def __init__(self, tag, classes, *, theme=None, ancestors=()):
        self.tag = tag
        self.classes = set(classes)
        self.theme = theme            # value of data-theme on an ancestor/root
        self.ancestors = [set(a) for a in ancestors]  # ancestor class sets


def _compound_parse(compound):
    """Parse a compound selector into its testable pieces.

    Returns dict with: tag (or None), pos_classes (set), neg_classes (set from
    :not(.x)), attrs (list of raw [..]), has_id (bool), pseudo_elem (bool),
    state_pseudos (set of :focus/:hover/... names)."""
    has_id = bool(re.search(r'#[\w-]+', compound))
    pseudo_elem = bool(re.search(r'::[\w-]+', compound))
    # classes NOT inside :not(...)
    stripped_not = re.sub(r':not\([^)]*\)', '', compound)
    pos_classes = set(re.findall(r'\.([\w-]+)', stripped_not))
    neg_classes = set(re.findall(r':not\(\.([\w-]+)\)', compound))
    attrs = re.findall(r'\[[^\]]*\]', compound)
    # state pseudo-classes (single colon), excluding functional :not already handled
    state = set(re.findall(r'(?<!:):(focus|hover|active|checked|disabled|visited|first-child|last-child)\b', compound))
    # leading bare tag token
    mtag = re.match(r'^([\w-]+)', compound)
    tag = mtag.group(1) if mtag else None
    return {
        'tag': tag, 'pos': pos_classes, 'neg': neg_classes,
        'attrs': attrs, 'has_id': has_id, 'pseudo_elem': pseudo_elem,
        'state': state,
    }


def _compound_matches_element(compound, tag, classes, *, theme=None):
    """Does *compound* match an element with the given tag+classes (+ optional
    data-theme value carried by this element)? Deliberately narrow to the
    selector shapes present in the memory-modal collision."""
    p = _compound_parse(compound)
    if p['has_id'] or p['pseudo_elem']:
        return False          # our test elements have no id and aren't ::pseudo
    if p['state']:
        return False          # base-state resolution only (skip :focus/:hover…)
    if p['tag'] and p['tag'] != tag:
        return False
    if not p['pos'].issubset(classes):
        return False
    if p['neg'] & classes:
        return False
    # attribute selectors: only [data-theme="X"] is modelled (as a theme match).
    for attr in p['attrs']:
        m = re.match(r'\[data-theme="([^"]+)"\]', attr)
        if m:
            if theme != m.group(1):
                return False
        else:
            return False      # any other attribute we don't model → no match
    return True


def _selector_matches(selector: str, el: _Elem) -> bool:
    """Whether *selector* matches *el*, honoring descendant combinators.

    The rightmost compound must match the element; each preceding compound must
    match SOME ancestor (the [data-theme] compound matches an ancestor carrying
    the theme). We approximate descendant matching with subset containment,
    which is exact for the flat ancestor model used here."""
    parts = re.split(r'\s+', selector.strip())
    key = parts[-1]
    if not _compound_matches_element(key, el.tag, el.classes, theme=el.theme):
        return False

    # Each ancestor compound must match some ancestor context. An ancestor is
    # modelled as a class-set; the theme lives on the root, so a [data-theme]
    # compound is satisfied by the element's theme.
    for comp in parts[:-1]:
        p = _compound_parse(comp)
        # A [data-theme="X"] ancestor compound.
        theme_attr = next((a for a in p['attrs']
                           if re.match(r'\[data-theme="', a)), None)
        if theme_attr:
            m = re.match(r'\[data-theme="([^"]+)"\]', theme_attr)
            if el.theme != m.group(1):
                return False
            # It may ALSO carry classes (rare) — require them on some ancestor.
            if p['pos'] and not any(p['pos'].issubset(a) for a in el.ancestors):
                return False
            continue
        # A class-only (possibly :not()) ancestor compound: match some ancestor.
        ok = any(p['pos'].issubset(a) and not (p['neg'] & a)
                 for a in el.ancestors)
        if not ok:
            return False
    return True


def _resolve(css_text: str, el: _Elem, prop: str):
    """Return the winning value of *prop* for *el* honoring !important, then
    (a,b,c) specificity, then source order."""
    winner = None  # (important, spec_tuple, idx, value)
    for sel, idx, decls in _iter_rules(css_text):
        if prop not in decls:
            continue
        if not _selector_matches(sel, el):
            continue
        raw = decls[prop]
        important = '!important' in raw
        value = raw.replace('!important', '').strip()
        spec = _specificity(sel)
        cand = (1 if important else 0, spec, idx)
        if winner is None or cand >= winner[0]:
            winner = (cand, value)
    return winner[1] if winner else None


# ─────────────────────────── fixtures + elements ───────────────────────────

def _css():
    with open(CSS, encoding='utf-8') as f:
        return f.read()


# A memory-modal search input under the tofu theme:
#   <div data-theme="tofu"> … <div class="modal memory-modal"> <input class="memory-search-input">
_MEM_INPUT = _Elem(
    'input', {'memory-search-input'}, theme='tofu',
    ancestors=[{'modal', 'memory-modal'}],
)
# A GENERIC tofu modal input (browser/apply modal — NOT memory-modal):
#   <div data-theme="tofu"> <div class="modal"> <input>
_GENERIC_INPUT = _Elem(
    'input', set(), theme='tofu',
    ancestors=[{'modal'}],
)

_TOFU_MEM_BG = '#F5F3ED'                      # the tofu bleed background
_TOFU_MEM_BORDER = '1px solid rgba(184,176,160,0.4)'  # the tofu bleed border


@pytest.fixture(scope='module')
def css_text():
    return _css()


# ─────────────────────────── 1. DELETION ───────────────────────────

def test_memory_input_rules_have_no_important(css_text):
    """Every .memory-modal .memory-* input rule must carry NO !important."""
    offenders = []
    for sel, _idx, decls in _iter_rules(css_text):
        if not sel.startswith('.memory-modal'):
            continue
        if not re.search(r'\.memory-(search-input|input|textarea|select)', sel):
            continue
        for prop, val in decls.items():
            if '!important' in val:
                offenders.append(f'{sel} {{ {prop}: {val} }}')
    assert not offenders, (
        'memory-modal input rules still carry !important (the pilot should have '
        'removed them — cascade now wins by specificity):\n' + '\n'.join(offenders))


# ─────────────────────────── 2. EXCLUSION LOAD-BEARING ───────────────────────────

def test_memory_input_resolves_own_tokens_not_tofu_bleed(css_text):
    """A memory-modal input's background/border resolve to its OWN --m-* tokens,
    NOT the tofu bleed — proving the source-exclusion works without !important."""
    bg = _resolve(css_text, _MEM_INPUT, 'background')
    border = _resolve(css_text, _MEM_INPUT, 'border')
    assert bg != _TOFU_MEM_BG, (
        f'memory input background resolved to the tofu bleed {bg!r} — the '
        f':not(.memory-modal) exclusion is not protecting it')
    assert bg == 'var(--m-white)', f'expected var(--m-white), got {bg!r}'
    assert border == 'var(--m-border-sm)', (
        f'expected var(--m-border-sm), got {border!r} (tofu bleed = {_TOFU_MEM_BORDER!r})')


def test_specificity_math_memory_beats_base_but_would_lose_to_bleed():
    """Encode the specificity relationship the pilot relies on:
    - memory rule `.memory-modal .memory-search-input` = (0,2,0)
    - base rule   `.modal input`                       = (0,1,1)  → memory wins
    - tofu bleed  `[data-theme=tofu] .modal input`      = (0,2,1)  → would BEAT
      memory (0,2,0) if it weren't excluded. That is WHY :not() at the source
      (not an !important on the memory rule) is the correct fix."""
    mem = _specificity('.memory-modal .memory-search-input')
    base = _specificity('.modal input')
    bleed = _specificity('[data-theme="tofu"] .modal input')
    excl = _specificity('[data-theme="tofu"] .modal:not(.memory-modal) input')
    assert mem == (0, 2, 0), mem
    assert base == (0, 1, 1), base
    assert bleed == (0, 2, 1), bleed
    assert mem > base, 'memory rule must out-specify the base .modal input'
    assert bleed > mem, 'the tofu bleed WOULD out-specify memory (hence the exclusion)'
    # :not(.memory-modal) adds a class to the arg → (0,3,1), still doesn't match
    # memory-modal at all, so it never competes. Its specificity rising is fine.
    assert excl == (0, 3, 1), excl


# ─────────────────────────── 3. NO OVER-NARROWING (control) ───────────────────────────

def test_generic_tofu_modal_input_still_gets_theme(css_text):
    """A generic tofu .modal input (NOT memory-modal) STILL resolves to the tofu
    styling — the exclusion removed memory-modal ONLY, not the whole theme."""
    bg = _resolve(css_text, _GENERIC_INPUT, 'background')
    assert bg == _TOFU_MEM_BG, (
        f'generic tofu modal input background resolved to {bg!r} — expected the '
        f'tofu bleed {_TOFU_MEM_BG!r}. The :not() over-narrowed and stripped the '
        f'theme from ALL modals, not just memory-modal.')


# ─────────────────────────── NC-1 double-neuter (on-disk, subprocess) ───────────────────────────

# NOTE: the shipped bleed selector also carries `:not(.recent-search-input)`
# (see tests/test_recent_search_tofu_specificity.py). This neuter removes ONLY
# the `:not(.memory-modal)` carve-out (its purpose) and preserves the
# recent-search one, so it stays surgical.
_NC1_FIND = '[data-theme="tofu"] .modal:not(.memory-modal) input:not(.recent-search-input){'
_NC1_REPL = '[data-theme="tofu"] .modal input:not(.recent-search-input){'


def _subrun_resolve_mem_bg():
    """In a FRESH subprocess, resolve the memory input background from the CURRENT
    on-disk styles.css. Returns the resolved value string."""
    code = (
        'import tests.test_memory_modal_specificity as t; '
        'print("BG=" + str(t._resolve(t._css(), t._MEM_INPUT, "background")))'
    )
    r = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                       capture_output=True, text=True)
    out = r.stdout + r.stderr
    for line in out.splitlines():
        if line.startswith('BG='):
            return line[3:]
    raise AssertionError(f'subprocess did not report BG=: {out}')


def test_nc1_reverting_exclusion_reintroduces_bleed(tmp_path):
    """DOUBLE-NEUTER: revert `:not(.memory-modal)` → `.modal input` on the tofu
    rule (ON DISK) → the memory input now inherits the tofu bleed → resolution
    flips to #F5F3ED. Restore byte-identical."""
    with open(CSS, encoding='utf-8') as f:
        original = f.read()
    assert original.count(_NC1_FIND) == 1, (
        f'NC-1 anchor not unique: count={original.count(_NC1_FIND)}')

    # Baseline (shipped): memory input is NOT the bleed.
    base_bg = _subrun_resolve_mem_bg()
    assert base_bg != _TOFU_MEM_BG, f'baseline already bleeding: {base_bg!r}'

    try:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original.replace(_NC1_FIND, _NC1_REPL, 1))
        neut_bg = _subrun_resolve_mem_bg()
        assert neut_bg == _TOFU_MEM_BG, (
            f'NC-1 did not bite: with the exclusion reverted the memory input '
            f'background resolved {neut_bg!r}, expected the tofu bleed '
            f'{_TOFU_MEM_BG!r}. The exclusion is not actually what protects it.')
    finally:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original)

    # Restore verified byte-identical.
    with open(CSS, encoding='utf-8') as f:
        assert f.read() == original, 'CSS not restored byte-identical after NC-1'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
