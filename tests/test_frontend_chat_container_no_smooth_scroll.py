"""Chat-container smooth-scroll drift guard.

BACKGROUND (2026-07-24 fix)
---------------------------
The tofu theme (the DEFAULT theme) used to set
    [data-theme="tofu"] .chat-container { scroll-behavior:smooth }
which turned the every-rAF `scrollTop = scrollHeight` write in
`scrollToBottom()` into an animation. During streaming, scrollHeight is still
growing, so the smooth animator perpetually chases a moving target — visible
as the reader drifting off the bottom then snapping back ("generation makes
the page scroll up and down on its own"). Compounded by
`#chatContainer{overflow-anchor:auto}`, which briefly pins content above the
viewport and fights the "stick to bottom" write.

The fix flipped the rule to `scroll-behavior:auto`. This test is the drift
guard: any sibling that later adds `scroll-behavior:smooth` on the
chat-container selectors under ANY theme block (dark/light/tofu/any new
`[data-theme="…"]`, or the unthemed base) fails here — because a memory-only
warning is only visible in sessions where memory is enabled AND prefetch
happens to surface it, whereas the test is a hard collect-gate signal every
sibling sees.

INVARIANT
---------
No CSS rule whose selector list references any of
    `.chat-container`, `#chatContainer`, `#chatInner`
may include `scroll-behavior: smooth` in its declaration block. Absence or
`scroll-behavior: auto` is fine.

Per-call `scrollIntoView({behavior:'smooth'})` from JS is a per-invocation
option and is NOT inherited from a container's `scroll-behavior` CSS rule, so
that smoothed jump-to-target behavior (branch panel, turn nav) is unaffected.
Rules on OTHER scrollables (e.g. `.paper-qa-messages`, `.branch-messages`)
are unrelated and legal.

PARSING
-------
The test parses CSS BLOCKS, not raw substrings. This is load-bearing: the
fix-side comment above the corrected rule contains the word `smooth` several
times, and a naive `substring "smooth"` search would false-positive on it.
Comments are stripped first; then leaf `{ ... }` blocks are enumerated with
their preceding selector text; a match on our selectors is combined with a
declaration-level `scroll-behavior:` lookup.

MANUAL NEUTER-PROOF (run once at authoring; not automated)
----------------------------------------------------------
Temporarily edit static/styles.css from `scroll-behavior:auto` back to
`scroll-behavior:smooth` on the `[data-theme="tofu"] .chat-container` rule
(styles.css ~L12748). Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \
        tests/test_frontend_chat_container_no_smooth_scroll.py
The `test_no_smooth_scroll_on_chat_container_selectors` test MUST fail. Restore
and confirm the whole file goes green again. Result recorded in JOURNAL.md.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS_PATH = os.path.join(ROOT, 'static', 'styles.css')


# Selectors whose scroll behavior governs the message-list container. Any leaf
# CSS rule whose selector list references ONE of these — under any theme —
# participates in the invariant.
_CHAT_SELECTOR_TOKENS = ('.chat-container', '#chatContainer', '#chatInner')


def _read_css() -> str:
    with open(CSS_PATH, encoding='utf-8') as f:
        return f.read()


def _strip_comments(css: str) -> str:
    """Remove /* ... */ CSS comments (non-greedy, spans newlines).

    Load-bearing for this test: the fix-side commentary above the target rule
    contains the word `smooth` and would false-positive a naive substring
    check. After this pass the source is comment-free so brace-block parsing
    is unambiguous.

    Delegates to the SINGLE shared implementation (charter #24).

    EQUIVALENCE, MEASURED on the real 22k-line static/styles.css rather than
    assumed: the local ``re.sub(r'/\\*.*?\\*/', '', css, flags=re.DOTALL)`` this
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


# A LEAF `{ ... }` block: selector text (no braces) followed by a body (no
# braces). @media / @supports / @keyframes WRAPPERS also match `{...}` but
# their bodies contain further `{`, so `[^{}]*` won't match — this regex
# naturally isolates the innermost rules. That is EXACTLY the scope of the
# invariant (declarations only live in leaf rules), so no explicit @media
# unwrapping is needed.
_LEAF_BLOCK = re.compile(r'([^{}]+)\{([^{}]*)\}', re.DOTALL)


def _iter_leaf_rules():
    """Yield `(selector_text, declarations_text, offset)` tuples for every
    innermost CSS rule in styles.css. `offset` is the byte index of the
    opening `{` in the ORIGINAL (comment-stripped) source, used to compute a
    friendly source-line number for the error message."""
    src = _strip_comments(_read_css())
    for m in _LEAF_BLOCK.finditer(src):
        selector = m.group(1)
        decls = m.group(2)
        # Compute line number of the `{`.
        line = src[:m.end(1)].count('\n') + 1
        yield selector, decls, line


def _selector_touches_chat_container(selector_text: str) -> bool:
    """True iff any comma-separated selector in the group references one of
    the chat-container-family tokens.

    A CSS selector list separates alternatives by commas (at bracket-depth 0).
    Our selectors of interest are simple id / class references, so a shallow
    split-by-`,` is sufficient; we then check for token-boundary presence.
    """
    # Extremely light tokenizer: split by commas at zero paren/bracket depth.
    parts: list[str] = []
    depth = 0
    buf = []
    for ch in selector_text:
        if ch in '([':
            depth += 1
            buf.append(ch)
        elif ch in ')]':
            depth -= 1
            buf.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append(''.join(buf))

    for part in parts:
        s = part.strip()
        if not s:
            continue
        # Token-boundary match: the id/class must be a full token (followed by
        # whitespace, EOL, `:`, `.`, `#`, `[`, `,`, `>`, `+`, `~`, `{`) so we
        # don't match a substring inside a longer class name.
        for tok in _CHAT_SELECTOR_TOKENS:
            idx = 0
            while True:
                pos = s.find(tok, idx)
                if pos < 0:
                    break
                end = pos + len(tok)
                after = s[end:end + 1]
                # OK if end-of-string OR next char is not a class/id name
                # continuation character.
                if not after or not (after.isalnum() or after in '-_'):
                    return True
                idx = end
    return False


# `scroll-behavior:` declaration extractor. Property is case-insensitive per
# CSS; value follows a colon and terminates at `;` or end-of-block.
_SB_DECL = re.compile(
    r'(?:^|[;\{\s])\s*scroll-behavior\s*:\s*([a-zA-Z]+)',
    re.IGNORECASE,
)


def _scroll_behavior_values(decls: str) -> list[str]:
    return [m.group(1).strip().lower() for m in _SB_DECL.finditer(decls)]


# ─────────────────────────── the invariant ───────────────────────────


def test_no_smooth_scroll_on_chat_container_selectors():
    """No leaf CSS rule targeting `.chat-container` / `#chatContainer` /
    `#chatInner` may set `scroll-behavior: smooth`. See module docstring
    for the failure mode this drift guard prevents.
    """
    offenders: list[str] = []
    for selector, decls, line in _iter_leaf_rules():
        if not _selector_touches_chat_container(selector):
            continue
        for value in _scroll_behavior_values(decls):
            if value == 'smooth':
                # Emit a friendly one-line locator.
                offenders.append(
                    f'styles.css:{line}: selector `{selector.strip()[:80]}` '
                    f'sets scroll-behavior:smooth (declarations: '
                    f'`{decls.strip()[:120]}`)'
                )
    assert not offenders, (
        'chat-container smooth-scroll drift detected — `scroll-behavior:smooth` '
        'on the message-list container turns every per-frame `scrollTop = '
        'scrollHeight` write into an animation that chases a moving target '
        'during streaming, producing visible up/down jitter. Use '
        '`scroll-behavior:auto` (or omit the property), and reach for a per-call '
        '`scrollIntoView({behavior:"smooth"})` when you need a smoothed jump to '
        'a specific target.\n\n' + '\n'.join(offenders)
    )


# ─────────────────────── sanity checks on the parser ───────────────────────

def test_parser_finds_the_current_auto_rule():
    """Sanity: the parser locates the CURRENT (post-fix) tofu chat-container
    rule and reports scroll-behavior:auto. If this regresses the invariant
    test above could go trivially green because the parser stopped finding the
    rule — this ratchet catches that class of drift too."""
    found_auto = False
    for selector, decls, _line in _iter_leaf_rules():
        if '[data-theme="tofu"]' not in selector:
            continue
        if '.chat-container' not in selector:
            continue
        values = _scroll_behavior_values(decls)
        if values and values[0] == 'auto':
            found_auto = True
            break
    assert found_auto, (
        "parser did not locate the [data-theme='tofu'] .chat-container rule "
        "with scroll-behavior:auto — either the rule moved / was deleted, or "
        "the CSS block parser broke. Investigate before trusting the smooth "
        "invariant test."
    )


def test_parser_does_not_confuse_commented_smooth_word():
    """Comment stripping must precede block parsing — otherwise the fix-side
    commentary above the tofu rule (which mentions `smooth` several times)
    would leak into the declaration body of a nearby rule. Verify by grepping
    the raw source for `smooth` inside a `/* … */` block and asserting the
    stripped source does NOT contain that comment's payload."""
    raw = _read_css()
    stripped = _strip_comments(raw)
    # The fix comment contains a distinctive phrase; presence in raw but
    # absence in stripped proves the strip pass ran.
    marker = 'perpetually chases a moving target'
    assert marker in raw, 'fix-side comment was removed — update this marker'
    assert marker not in stripped, (
        'comment stripper failed — the fix-side commentary is still present in '
        'the parsed source, which would cause false positives in the smooth '
        'invariant test.'
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
