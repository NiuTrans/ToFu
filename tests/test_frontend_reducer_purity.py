#!/usr/bin/env python3
"""RENDER_CONTRACT Phase 3 — stream_reducer.js PURITY guard.

The whole value of the reducer is that it is a PURE function: every apply path
(live/warm/cold/poll) can fold through it and reach the same fixed point ONLY if
it has no side effects and reads no ambient state. This static guard fails if
the module references any of the forbidden side-effect / global symbols, so a
future edit can't quietly reintroduce a `twUpdate(convId)` or a `document.…`
read that would make the projection path-dependent again.

Pure text scan of static/js/ui/stream_reducer.js. Standalone + pytest.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

REDUCER_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'static', 'js', 'ui', 'stream_reducer.js')

# Symbols that would make the reducer impure / DOM-coupled / global-coupled.
# Word-boundary matched so a substring in a comment word (e.g. "document" inside
# "documented") doesn't false-positive — we match actual identifier uses.
FORBIDDEN = [
    r'\btwUpdate\b',
    r'\bdocument\b',
    r'\bwindow\b',
    r'\bconsole\b',
    r'\bApi\b',
    r'\bfetch\b',
    r'\bsetTimeout\b',
    r'\bsetInterval\b',
    r'\brenderChat\b',
    r'\bsaveConversations\b',
    r'\bconversations\b',       # the global conv array — reducer must be passed state
    r'\blocalStorage\b',
    r'\bsessionStorage\b',
]


def _strip_comments_and_strings(src: str) -> str:
    """Remove // line comments, /* */ block comments, and string literals so the
    scan only sees CODE identifiers (a forbidden word inside a comment or a
    string message is fine).

    Delegates to the SINGLE shared implementation (charter #24) via the
    ``strings=True`` option, which was added for exactly this caller: the shared
    module already tracked quote state for its inline block pass, so emptying
    literals reuses that scanner instead of introducing a second literal parser.

    Equivalence MEASURED on the real file this guard scans
    (``static/js/ui/stream_reducer.js``), at the level the guard actually works
    at: the set of CODE IDENTIFIERS surviving the strip is IDENTICAL under both
    implementations (zero identifiers unique to either side). Comparing raw text
    would have been the wrong test — the two legitimately differ in whitespace,
    because the shared pass preserves line count while the local regexes
    collapsed each comment and literal to a single space.
    """
    from tests._source_scan import strip_comments
    return strip_comments(src, lang='js', strings=True)


def test_reducer_is_pure_no_side_effect_symbols():
    """stream_reducer.js CODE must not reference any DOM/global/side-effect
    symbol. The one allowed ambient reference is the `module`/`module.exports`
    test hook (Node harness), which is explicitly whitelisted."""
    with open(REDUCER_JS, encoding='utf-8') as f:
        raw = f.read()
    code = _strip_comments_and_strings(raw)

    # Whitelist the CommonMJS test-export hook lines.
    code = code.replace('module.exports', '').replace('typeof module', '')

    hits = []
    for pat in FORBIDDEN:
        for m in re.finditer(pat, code):
            hits.append((pat, m.start()))
    assert not hits, (
        'PURITY VIOLATION: stream_reducer.js references forbidden side-effect / '
        f'global symbols in code (not comments/strings): '
        f'{sorted(set(p for p, _ in hits))}. The reducer must be a pure '
        '(state, event) → state function — pass state in, return it, no ambient '
        'reads, no DOM, no twUpdate/console/Api.')


def test_reducer_exposes_pure_projection_api():
    """The three public entry points must be defined as plain functions."""
    with open(REDUCER_JS, encoding='utf-8') as f:
        raw = f.read()
    for fn in ('reduceStreamState', 'projectStreamEvents', 'projectColdSnapshot', 'locateRound'):
        assert re.search(r'\bfunction\s+' + fn + r'\s*\(', raw), (
            f'{fn} must be defined as a top-level function in stream_reducer.js')


def test_NC_forbidden_scan_catches_injected_side_effect():
    """NEUTER: prove the scan is load-bearing — inject a twUpdate() call into a
    copy of the source and confirm the scan flags it."""
    with open(REDUCER_JS, encoding='utf-8') as f:
        raw = f.read()
    poisoned = raw.replace('return state;', 'twUpdate(convId); return state;', 1)
    code = _strip_comments_and_strings(poisoned).replace('module.exports', '').replace('typeof module', '')
    hit = bool(re.search(r'\btwUpdate\b', code))
    assert hit, 'NEUTER FAILED: the purity scan did not catch an injected twUpdate() call'


def test_NC_forbidden_word_inside_a_string_is_not_a_violation():
    """NEUTER, the other direction — pins the ``strings=True`` half.

    The test above injects twUpdate as CODE, which ANY stripper catches, so it
    cannot tell a literal-emptying stripper from a comments-only one. This one
    can: it puts forbidden words where they are legal — inside a comment and
    inside a string message — and requires the scan to stay silent.

    Worth pinning explicitly because the real ``stream_reducer.js`` currently
    contains ZERO occurrences of any FORBIDDEN symbol even with literals kept
    (measured), so the production scan alone exercises neither half. Drop
    ``strings=True`` and the fixture below starts failing; without this test
    that regression is invisible until someone writes an error message
    mentioning ``localStorage`` and the guard fails on prose.
    """
    fixture = (
        '// this reducer must never call saveConversations directly\n'
        'function reduce(state, ev) {\n'
        '  if (!ev) throw new Error("cannot use localStorage in a reducer");\n'
        '  const hint = `pass state in, do not read window here`;\n'
        '  return state;\n'
        '}\n'
    )
    code = _strip_comments_and_strings(fixture)
    hits = sorted({p for p in FORBIDDEN if re.search(p, code)})
    assert not hits, (
        'forbidden symbols were counted while appearing ONLY in a comment or a '
        f'string literal: {hits}. The scan must see CODE identifiers only — a '
        'user-facing message naming localStorage is not an impure read.')
    # Complement: the same words AS CODE in the same shapes must still be caught,
    # so this is not merely "the stripper deletes everything".
    as_code = (
        'function reduce(state, ev) {\n'
        '  saveConversations();\n'
        '  localStorage.getItem("x");\n'
        '  return state;\n'
        '}\n'
    )
    code2 = _strip_comments_and_strings(as_code)
    assert re.search(r'\bsaveConversations\b', code2), (
        'a real saveConversations() call was destroyed by the stripper')
    assert re.search(r'\blocalStorage\b', code2), (
        'a real localStorage read was destroyed by the stripper')


def _run(fn):
    try:
        fn(); print('  \033[32m✓\033[0m', fn.__name__); return True
    except AssertionError as e:
        print('  \033[31m✗\033[0m', f'{fn.__name__}: {e}'); return False


def main():
    print('\n\033[36m═══ Phase-3 stream_reducer purity guard ═══\033[0m\n')
    ok = all(_run(f) for f in (test_reducer_is_pure_no_side_effect_symbols,
                               test_reducer_exposes_pure_projection_api,
                               test_NC_forbidden_scan_catches_injected_side_effect))
    print('\n' + ('\033[32mALL PURE\033[0m' if ok else '\033[31mIMPURE\033[0m') + '\n')


if __name__ == '__main__':
    main()
