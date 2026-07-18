"""Frontend i18n missing-key ratchet.

Why
---
``static/js/i18n.js``'s ``t(key)`` returns the KEY STRING itself when the
key is missing — a silent fallback. Wrappers like ``project-brain.js``'s
``_t(key, fallback)`` call ``t(key)`` first, so ``t()``'s own missing-key
fallback fires and the wrapper's ``fallback`` arg is DEAD CODE on the
missing-key path. Net effect: reference a key that isn't defined in
i18n.js and the UI renders the raw key (owner saw literal
``projectBrain.actResume`` on the board's Parked lane, 2026-07-04 — the
key had never been added despite a JOURNAL entry claiming it was).

This ratchet closes that class of bug: it scans source JS for literal
``projectBrain.*`` keys and asserts every COMPLETE literal is defined in
i18n.js, so CI fails the moment someone references a key without adding
its entry — no more hand-spotting raw keys in the browser.

Scope / what is excluded
------------------------
Only the ``projectBrain.*`` namespace (the Project Brain panel) is
policed here — it is the namespace that just leaked. The scanner
EXCLUDES dynamic concatenation prefixes, where the key is built at
runtime from a variable and the trailing segment can never be a static
literal, e.g.::

    _t('projectBrain.kind.' + kind, kind)
    _t('projectBrain.boardVerb.' + tr.verb, tr.verb)
    _t('projectBrain.lane' + status.charAt(0).toUpperCase() + ..., status)
    _t('projectBrain.' + labelKey, fallbackLabel)

A token is treated as a dynamic PREFIX (and skipped) when it ends in
``.`` OR its closing quote is immediately followed by ``+`` (string
concatenation). Those prefixes carry their own runtime fallback and are
verified separately (the common subkeys exist in i18n.js).
"""

from __future__ import annotations

import os
import re

import pytest


# ── Configuration ────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
JS_DIR = os.path.normpath(os.path.join(HERE, '..', 'static', 'js'))
I18N_FILE = os.path.join(JS_DIR, 'i18n.js')

# The namespace this ratchet polices.
NAMESPACE = 'projectBrain.'

# A literal key reference, capturing the char that follows the closing
# quote so we can detect string concatenation (a dynamic prefix):
#   group 1 = the key text (e.g. projectBrain.actResume)
#   group 2 = the single char immediately after the closing quote
_KEY_REF_RE = re.compile(
    r"""['"`](projectBrain\.[A-Za-z0-9_.]*)['"`]\s*(.?)""",
    re.VERBOSE,
)

# A key DEFINITION line in i18n.js:  'projectBrain.foo': { zh: ..., en: ... }
_KEY_DEF_RE = re.compile(r"""['"](projectBrain\.[A-Za-z0-9_.]+)['"]\s*:""")


def _is_generated(name: str) -> bool:
    return name.startswith('bundle-') and name.endswith('.js')


def _is_dynamic_prefix(key: str, next_char: str) -> bool:
    """True when the token is a runtime-built prefix, not a complete key.

    - ends in '.'  → e.g. 'projectBrain.kind.' + kind
    - followed by '+' → string concatenation, e.g. 'projectBrain.lane' + status
    """
    return key.endswith('.') or next_char == '+'


# ── Extraction ───────────────────────────────────────────────────────
def _extract_referenced_keys(text: str) -> set[str]:
    """Return the set of COMPLETE literal projectBrain.* keys in `text`,
    excluding dynamic concatenation prefixes."""
    out: set[str] = set()
    for m in _KEY_REF_RE.finditer(text):
        key, nxt = m.group(1), m.group(2)
        if key == NAMESPACE:  # the bare 'projectBrain.' prefix
            continue
        if _is_dynamic_prefix(key, nxt):
            continue
        out.add(key)
    return out


def _scan_source_refs() -> dict[str, set[str]]:
    """Map source-JS relpath → set of complete literal projectBrain.* keys.

    Excludes i18n.js itself (it DEFINES keys) and bundle-*.js artifacts.
    """
    refs: dict[str, set[str]] = {}
    for root, dirs, files in os.walk(JS_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(('.', '__'))]
        for name in sorted(files):
            if not name.endswith('.js') or _is_generated(name):
                continue
            path = os.path.join(root, name)
            if os.path.samefile(path, I18N_FILE) if os.path.exists(I18N_FILE) else name == 'i18n.js':
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    text = f.read()
            except OSError:
                continue
            keys = _extract_referenced_keys(text)
            if keys:
                rel = os.path.relpath(path, JS_DIR).replace(os.sep, '/')
                refs[rel] = keys
    return refs


def _defined_keys() -> set[str]:
    with open(I18N_FILE, 'r', encoding='utf-8') as f:
        text = f.read()
    return set(_KEY_DEF_RE.findall(text))


# ── Namespace-agnostic coverage (ALL keys, JS + HTML) ────────────────
# The projectBrain-only ratchet above missed real leaks in OTHER
# namespaces (swarm.autoContinue never localized to zh; a stray
# data-i18n-html="browser.lnaPathHint" clobbered JS-rendered content
# with the raw key on language switch — both found 2026-07-04). This
# widens the same guard to every namespace and to the HTML data-i18n*
# surface.

# ANY defined key:  'foo.bar': { zh: ..., en: ... }
_ANY_KEY_DEF_RE = re.compile(r"""(?m)^\s*['"]([A-Za-z0-9_.\-]+)['"]\s*:\s*\{""")

# A t('key') / _t('key') / t(`key`) call — the leading (?<!...) ensures we
# match ONLY a bare `t(` or `_t(`, never the tail of another identifier
# (format(, .at(, assert(, split( …). Group 2 = char after the closing quote
# (concat detect). Backticks are included so a fully-STATIC template literal
# is policed too; a DYNAMIC one — t(`ns.${x}`) — naturally does not match
# because `$`/`{` are not in the key char class, so the required closing
# quote is never reached.
_ANY_T_CALL_RE = re.compile(
    r"""(?<![A-Za-z0-9_$.])_?t\(\s*['"`]([A-Za-z0-9_.\-]+)['"`]\s*(.?)""")

# data-i18n / data-i18n-html / -placeholder / -title attribute value.
_ANY_ATTR_RE = re.compile(
    r"""data-i18n(?:-[a-z]+)?\s*=\s*['"]([A-Za-z0-9_.\-]+)['"]""")

# Extra HTML surfaces that carry data-i18n attributes.
_HTML_FILES = (
    os.path.normpath(os.path.join(HERE, '..', 'index.html')),
    os.path.normpath(os.path.join(HERE, '..', 'static', 'admin.html')),
)


def _all_defined_keys() -> set[str]:
    with open(I18N_FILE, 'r', encoding='utf-8') as f:
        return set(_ANY_KEY_DEF_RE.findall(f.read()))


def _scan_all_refs() -> dict[str, set[str]]:
    """relpath → set of COMPLETE literal i18n keys referenced via t()/_t()
    or a data-i18n* attribute, across all source JS + index/admin HTML.
    Dynamic concatenation prefixes are excluded."""
    refs: dict[str, set[str]] = {}

    def _collect(path: str, text: str, rel: str) -> None:
        found: set[str] = set()
        for m in _ANY_T_CALL_RE.finditer(text):
            key, nxt = m.group(1), m.group(2)
            if _is_dynamic_prefix(key, nxt):
                continue
            found.add(key)
        for m in _ANY_ATTR_RE.finditer(text):
            found.add(m.group(1))
        if found:
            refs[rel] = found

    for root, dirs, files in os.walk(JS_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(('.', '__'))]
        for name in sorted(files):
            if not name.endswith('.js') or _is_generated(name):
                continue
            path = os.path.join(root, name)
            if name == 'i18n.js':
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    text = f.read()
            except OSError:
                continue
            _collect(path, text, os.path.relpath(path, JS_DIR).replace(os.sep, '/'))

    for path in _HTML_FILES:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read()
        except OSError:
            continue
        _collect(path, text, os.path.basename(path))

    return refs


# ── Tests ────────────────────────────────────────────────────────────
def test_i18n_file_exists():
    assert os.path.isfile(I18N_FILE), 'static/js/i18n.js is missing.'


def test_every_referenced_projectbrain_key_is_defined():
    """Every complete literal projectBrain.* key used in source JS must be
    defined in i18n.js — otherwise t() renders the raw key string."""
    defined = _defined_keys()
    refs = _scan_source_refs()
    missing: list[tuple[str, str]] = []
    for rel, keys in refs.items():
        for key in sorted(keys):
            if key not in defined:
                missing.append((rel, key))
    if missing:
        details = '\n'.join(f'  {rel}: {key}' for rel, key in sorted(missing))
        pytest.fail(
            'Source JS references projectBrain.* i18n keys that are NOT '
            'defined in static/js/i18n.js. t() would render the raw key '
            'string in the UI. Add each key to i18n.js (zh + en):\n'
            + details
        )


def test_scanner_sees_the_known_keys():
    """Sanity floor: the scanner must actually find the board-action keys
    that regressed on 2026-07-04, proving it isn't silently matching
    nothing (which would make the ratchet vacuously green)."""
    refs = _scan_source_refs()
    all_keys = set().union(*refs.values()) if refs else set()
    # Canary keys: complete literal projectBrain.* keys the board-action code
    # actually references today (project-brain.js _boardActionBtn calls). The
    # scanner MUST find these — proving it isn't vacuously matching nothing.
    # (The earlier canaries actResume/actDefer/deferReasonPrompt/laneDeferred
    # were removed along with the resume/defer board actions; using live keys
    # keeps the sanity floor meaningful.)
    for key in ('projectBrain.actBlock', 'projectBrain.actReopen'):
        assert key in all_keys, (
            f'{key} not discovered by the scanner — the extraction regex or '
            'the dynamic-prefix filter is too aggressive.'
        )


def test_dynamic_prefixes_are_excluded():
    """The runtime-built concatenation prefixes must NOT be treated as
    complete keys (they can never match an i18n entry verbatim)."""
    sample = (
        "_t('projectBrain.kind.' + kind, kind);\n"
        "_t(\"projectBrain.boardVerb.\" + tr.verb, tr.verb);\n"
        "_t(\"projectBrain.lane\" + status.charAt(0), status);\n"
        "_t('projectBrain.' + labelKey, fallbackLabel);\n"
        "_t('projectBrain.actResume', 'Resume');\n"  # the one COMPLETE key
    )
    keys = _extract_referenced_keys(sample)
    assert keys == {'projectBrain.actResume'}, (
        f'dynamic-prefix filter wrong; extracted {sorted(keys)}'
    )


def test_ratchet_would_catch_an_unguarded_newcomer():
    """Negative control: a synthetic reference to an undefined key must be
    flagged by the same check. Proves the ratchet is load-bearing, not a
    check that can only ever pass."""
    defined = _defined_keys()
    fake = 'projectBrain.__nonexistent_ratchet_probe__'
    assert fake not in defined
    synthetic = f"_t('{fake}', 'x');"
    extracted = _extract_referenced_keys(synthetic)
    assert fake in extracted, 'scanner failed to extract the synthetic key'
    # The same membership test the real test uses would fail on this key:
    assert fake not in defined, 'probe key must be undefined for the control'


def test_every_referenced_key_is_defined_all_namespaces():
    """Widened ratchet: EVERY complete literal i18n key referenced anywhere
    in source JS or index/admin HTML (t()/_t()/data-i18n*) must be defined
    in i18n.js. Catches leaks outside the projectBrain namespace — e.g.
    swarm.autoContinue (never localized) and the stray data-i18n-html
    browser.lnaPathHint that clobbered JS output with the raw key."""
    defined = _all_defined_keys()
    refs = _scan_all_refs()
    missing: list[tuple[str, str]] = []
    for rel, keys in refs.items():
        for key in sorted(keys):
            if key not in defined:
                missing.append((rel, key))
    if missing:
        details = '\n'.join(f'  {rel}: {key}' for rel, key in sorted(missing))
        pytest.fail(
            'Source references i18n keys NOT defined in static/js/i18n.js '
            '(t() renders the raw key; a data-i18n attr overwrites JS output '
            'with the raw key on language switch). Add each (zh + en):\n'
            + details
        )


def test_html_data_i18n_keys_are_defined():
    """The index.html / admin.html data-i18n* surface specifically — the
    stray browser.lnaPathHint lived here and only leaked on _applyI18n()."""
    defined = _all_defined_keys()
    missing: list[tuple[str, str]] = []
    for path in _HTML_FILES:
        if not os.path.isfile(path):
            continue
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
        for m in _ANY_ATTR_RE.finditer(text):
            key = m.group(1)
            if key not in defined:
                missing.append((os.path.basename(path), key))
    assert not missing, (
        'HTML data-i18n* attributes reference undefined i18n keys: '
        + ', '.join(f'{f}:{k}' for f, k in missing)
    )


def test_widened_scanner_would_catch_an_undefined_key_anywhere():
    """Negative control for the widened ratchet: a synthetic reference in a
    non-projectBrain namespace must be extracted (so it WOULD be flagged),
    proving the broadened check is load-bearing, not vacuously green."""
    defined = _all_defined_keys()
    fake = 'swarm.__nonexistent_ratchet_probe__'
    fake_bt = 'swarm.__backtick_ratchet_probe__'
    assert fake not in defined and fake_bt not in defined
    text = (
        f"const x = t('{fake}');\n"
        f"const y = t(`{fake_bt}`);\n"          # STATIC backtick — must be caught
        f"const z = t(`swarm.${{dyn}}`);\n"     # DYNAMIC template literal — must NOT
        f"<span data-i18n=\"{fake}\"></span>"
    )
    found: set[str] = set()
    for m in _ANY_T_CALL_RE.finditer(text):
        if not _is_dynamic_prefix(m.group(1), m.group(2)):
            found.add(m.group(1))
    for m in _ANY_ATTR_RE.finditer(text):
        found.add(m.group(1))
    assert fake in found, 'widened scanner failed to extract the synthetic key'
    assert fake_bt in found, 'widened scanner failed to extract a STATIC backtick key'
    # The dynamic template literal must contribute nothing (no complete literal).
    assert not any(k.startswith('swarm.$') or '{' in k for k in found), (
        f'dynamic template literal leaked into policed keys: {sorted(found)}'
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
