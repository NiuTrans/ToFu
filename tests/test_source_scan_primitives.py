"""tests/test_source_scan_primitives.py — the shared scanner has no guard of its own.

WHY THIS FILE EXISTS (pt_b95c6d396edd467d)
------------------------------------------
``tests/_source_scan.py`` is charter #24's single source of truth for "strip
comments before scanning source text", and 12+ guards now import it. It had
**zero tests**. Every consumer's correctness rests on it, so a silent
regression here turns a whole family of guards vacuous at once — the exact
failure mode charter #24 exists to prevent, one level up.

WHAT THE EPIC GOT WRONG, AND WHY THAT MATTERS MORE THAN THE HEADCOUNT
--------------------------------------------------------------------
The ticket said: 20 test files each carry a hand-written ``_strip_comments``,
migrate them all to the shared module. Measured, that instruction is unsafe for
17 of the 20:

  * **12 of them consume CSS**, and the shared module had **no ``css`` language
    at all**. ``strip_comments(css, lang='css')`` silently fell through to the
    shell default (``#`` line comments) and stripped essentially nothing — a
    no-op that would have looked like a successful migration.
  * **17 of them strip INLINE ``/* … */``** (a block opening after code on the
    same line). The shared implementation deliberately refuses to, documenting
    that a half-correct block parser could drop real code. That refusal is
    right for its original shell/JS callers and WRONG for these: their
    docstrings record the concrete trap they exist to stop — a comment
    containing literal braces corrupting a brace-based CSS rule splitter.
    Measured on the real 22k-line ``static/styles.css``: the local
    inline-aware strip leaves **0** ``/*`` behind, the shared whole-line strip
    leaves **20**.

So a blind migration would have QUIETLY WEAKENED 17 guards while reporting
success — a worse outcome than the duplication it set out to remove. The
correct move is to make the shared module able to express what those callers
genuinely need (a real ``css`` language, and an opt-in inline mode), then
migrate only what is provably equivalent.

This suite pins the primitives so that becomes safe:

  1. ``css`` is a REAL language, not a silent fall-through to shell.
  2. An unknown ``lang`` FAILS LOUDLY instead of silently under-stripping —
     the property whose absence made the CSS no-op invisible.
  3. ``inline=True`` strips a block comment that opens after code; the default
     still does not (no behaviour change for existing callers).
  4. Inline mode preserves line count, so callers doing line arithmetic keep
     working.
  5. Comments can neither SATISFY nor VIOLATE a scan (charter #24, both
     directions), for every supported language.
  6. String/template literals are not mistaken for comments — for BOTH markers
     (``/*`` and ``//``). The ``//`` half is what a real guard got wrong: a
     ``'http://'`` in a string was truncated as if it started a comment.
  7. ``js_function_body`` refuses partial bodies and ignores braces inside
     comments and strings.
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, HERE)

from _source_scan import (brace_block, js_function_body,  # noqa: E402
                          python_block, strip_comments,
                          supported_langs)


# ═══════════════════════════════════════════════════════════════════
# 1-2. CSS must be a real language, and an unknown one must fail loudly
# ═══════════════════════════════════════════════════════════════════

def test_css_is_a_supported_language():
    """Face 1 — 12 guards consume CSS; the module must actually know it.

    Before this, ``lang='css'`` fell through to the shell default and stripped
    ``#`` line comments, i.e. essentially nothing in a CSS file. A migration
    onto that would have passed its own tests while removing the guard's teeth.
    """
    assert 'css' in supported_langs(), (
        'css must be a first-class language: a dozen guards scan styles.css, '
        'and a silent fall-through to the shell default strips nothing')

    css = '.a{color:red}\n/* whole line */\n.b{color:blue}\n'
    out = strip_comments(css, lang='css')
    assert '/* whole line */' not in out, 'css block comments must be stripped'
    assert '.a{color:red}' in out and '.b{color:blue}' in out, (
        'real declarations must survive')


def test_unknown_lang_fails_loudly():
    """Face 2 — the property whose absence hid the CSS no-op.

    A typo or an unsupported language must not degrade to "strip almost
    nothing" and let the caller believe it scanned clean text. This is the
    same class as charter #29's "structural guards must assert reachability":
    a silent partial success is indistinguishable from a real one.
    """
    with pytest.raises((ValueError, AssertionError, KeyError)):
        strip_comments('irrelevant', lang='rustacean')


# ═══════════════════════════════════════════════════════════════════
# 3-4. Inline mode — opt-in, line-count preserving
# ═══════════════════════════════════════════════════════════════════

_INLINE_PROBE_JS = (
    'const a = 1; /* inline */ const b = 2;\n'
    'real();\n'
    '/* whole\n'
    ' * line\n'
    ' */\n'
    'code2();\n'
)


def test_inline_mode_strips_a_block_that_opens_after_code():
    """Face 3 — what 17 of the 20 local copies actually need."""
    default = strip_comments(_INLINE_PROBE_JS, lang='js')
    assert '/* inline */' in default, (
        'the DEFAULT must stay whole-line-only — existing callers depend on it '
        'and the module documents why a half-parse is refused')

    inline = strip_comments(_INLINE_PROBE_JS, lang='js', inline=True)
    assert '/* inline */' not in inline, (
        'inline=True must strip a block comment opening after code')
    assert 'const a = 1;' in inline and 'const b = 2;' in inline, (
        'the CODE around an inline comment must survive intact')


def test_inline_mode_preserves_line_count():
    """Face 4 — callers doing line arithmetic must keep working.

    ``test_frontend_lazy_sentinel_anchor`` and ``test_i18n_pack_boot_floor``
    both report source line numbers, so a stripper that deletes lines instead
    of blanking them makes every reported location wrong.
    """
    for lang in ('js', 'css'):
        src = 'a\n/* x */\nb /* y */ c\nd\n'
        out = strip_comments(src, lang=lang, inline=True)
        assert len(out.splitlines()) == len(src.splitlines()), (
            'lang=%s inline=True changed the line count (%d -> %d)'
            % (lang, len(src.splitlines()), len(out.splitlines())))


def test_inline_mode_handles_a_multiline_block_opening_after_code():
    """A block that opens after code and closes lines later.

    The hard case for a line-oriented stripper: the opening line has real code
    before ``/*``, so it cannot simply be blanked, but the following lines must
    be.
    """
    src = 'keep1(); /* open\n middle\n close */ keep2();\nkeep3();\n'
    out = strip_comments(src, lang='js', inline=True)
    assert 'keep1();' in out, 'code before the opener must survive'
    assert 'keep3();' in out, 'code after the block must survive'
    assert 'middle' not in out, 'the block interior must be gone'
    assert len(out.splitlines()) == len(src.splitlines())


# ═══════════════════════════════════════════════════════════════════
# 5. Charter #24 both directions, for every language
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('lang,comment,code', [
    ('js', '// forbiddenToken()', 'realCall();'),
    ('js', '/* forbiddenToken() */', 'realCall();'),
    ('css', '/* .forbidden{x:1} */', '.real{y:2}'),
    ('python', '# forbidden_token()', 'real_call()'),
    ('shell', '# forbidden_token', 'real_call'),
])
def test_a_comment_can_neither_satisfy_nor_violate(lang, comment, code):
    """Face 5 — the charter #24 invariant itself, parametrised.

    SATISFY direction: a guard asserting "token must be PRESENT" must not be
    fooled by the token appearing only in prose.
    VIOLATE direction: a guard asserting "token must be ABSENT" must not go red
    because prose mentions it — the failure that made
    test_conv_state_p6_verdict red on a byte-clean tree.
    """
    token = 'forbiddenToken' if lang in ('js', 'css') else 'forbidden_token'

    comment_only = comment + '\n'
    out = strip_comments(comment_only, lang=lang, inline=True)
    assert token not in out, (
        'lang=%s: a comment-only line still exposes %r, so a comment can '
        'both satisfy and violate a scan' % (lang, token))

    with_code = comment + '\n' + code + '\n'
    out2 = strip_comments(with_code, lang=lang, inline=True)
    assert token not in out2, 'lang=%s: comment survived beside code' % lang
    assert code.split('(')[0].split('{')[0] in out2, (
        'lang=%s: real code was destroyed along with the comment' % lang)


def test_string_literals_are_not_mistaken_for_comments():
    """Face 6 — the reason the module refuses a naive block parser.

    A ``/*`` inside a string literal is DATA, not a comment. Dropping from
    there would silently delete real code, which the module docstring calls a
    worse failure than leaving a comment in.
    """
    src = 'const s = "not /* a comment */ here";\nreal();\n'
    out = strip_comments(src, lang='js', inline=True)
    assert 'real();' in out, 'code after the string must survive'
    assert 'const s =' in out, 'the assignment must survive'


def test_a_line_comment_marker_inside_a_literal_is_data():
    """★ The ``//`` half of face 6 — the defect that made a guard blind.

    The block-comment case above was pinned; this one was not, and it is the
    one that actually bit. ``test_frontend_api_isolation`` carried
    ``re.sub(r'//[^\\n]*', '', s)``, so this real line from ``static/js/api.js``::

        if (path.startsWith('http://') || path.startsWith('https://')) return path;

    was truncated to ``if (path.startsWith('http:`` — everything from the first
    ``//`` onward deleted BEFORE the scan ran. A URL scheme inside a string is
    DATA, exactly like ``/*`` is.

    Why it is worth its own test rather than folding into the parametrised case:
    the failure is SILENT and one-directional. Code vanishing early cannot make
    a "must be absent" guard red, so nothing goes wrong visibly — the guard just
    stops seeing part of the file and keeps reporting green. Measured across the
    frontend tree, 27 of 171 JS files differ between the two strippers, and in
    every one the shared tokenizer is the one that PRESERVES code (zero cases of
    the reverse), so this property is what those 27 files' guards rest on.
    """
    src = ("if (path.startsWith('http://') || path.startsWith('https://')) "
           "return path;\nreal();\n")
    out = strip_comments(src, lang='js', inline=True)
    assert 'https://' in out, (
        "a '//' inside a string literal is a URL scheme, not a comment — "
        'dropping from there deletes real code before the scan sees it')
    assert 'return path;' in out, 'the statement after the literal must survive'
    assert 'real();' in out, 'the following line must survive'

    # Complement: a genuine trailing line comment on a line that ALSO contains a
    # literal with '//' must still go, so this is not just "never strip //".
    mixed = "const u = 'https://x'; // drop THIS_TOKEN\nkeep();\n"
    out2 = strip_comments(mixed, lang='js', inline=True)
    assert 'THIS_TOKEN' not in out2, 'a real line comment must still be stripped'
    assert "'https://x'" in out2, 'the literal on the same line must survive'
    assert 'keep();' in out2


# ═══════════════════════════════════════════════════════════════════
# 7. js_function_body invariants
# ═══════════════════════════════════════════════════════════════════

def test_python_block_spans_a_multiline_signature():
    """★ A "precise" extractor can be WRONGER than the window it replaces.

    Measured during this migration: ``python_block`` first returned **164 B**
    for ``lib/browser/advanced.py::fill_form_sequential``, whose real body is
    ~6.4 KB. The cause is that a wrapped signature closes its paren at the SAME
    column as ``def``::

        def f(
            a, b,
        ) -> dict:          # <- column 0, looks like "block ended"
            body()

    so an indent-only rule stopped at the closing paren. The old fixed 2000-byte
    window happened to cover the tokens, so replacing it with the precise
    extractor turned a passing guard RED — a false alarm caused by the fix, not
    by the product. The header must therefore be consumed up to the first line
    whose bracket depth returns to zero and which ends in ``:``.
    """
    src = (
        'def wrapped(\n'
        '    a, b,\n'
        '    c=1,\n'
        ') -> dict:\n'
        '    TOKEN_IN_BODY = 1\n'
        '    return a\n'
        '\n'
        'def neighbour():\n'
        '    FORBIDDEN\n'
    )
    body = python_block(src, 'def wrapped')
    assert 'TOKEN_IN_BODY' in body, (
        'a wrapped signature must not terminate the block at its closing paren '
        '— that returns a stub and makes every assertion over it vacuous')
    assert 'return a' in body
    assert 'FORBIDDEN' not in body, "the neighbour's body must stay out"


def test_python_block_stops_at_the_next_sibling():
    src = (
        'def target():\n'
        '    inner()\n'
        '    if x:\n'
        '        deeper()\n'
        'def neighbour():\n'
        '    FORBIDDEN\n'
    )
    body = python_block(src, 'def target')
    assert 'deeper()' in body, 'nested deeper lines belong to the block'
    assert 'FORBIDDEN' not in body


def test_python_block_refuses_a_missing_or_ambiguous_anchor():
    """Silence and guesswork are both worse than a raise."""
    with pytest.raises(AssertionError):
        python_block('def a():\n    pass\n', 'def nope')
    with pytest.raises(AssertionError):
        python_block('if q:\n    x()\nif q:\n    y()\n', 'if q:')


def test_python_block_ignores_a_dedented_comment():
    """A comment at column 0 must not look like the end of the block."""
    src = (
        'def target():\n'
        '    first()\n'
        '# a dedented comment in the middle\n'
        '    TOKEN_LATE = 1\n'
        'def neighbour():\n'
        '    FORBIDDEN\n'
    )
    body = python_block(src, 'def target')
    assert 'TOKEN_LATE' in body, (
        'comments are stripped first precisely so a dedented one cannot '
        'truncate the block')
    assert 'FORBIDDEN' not in body


def test_brace_block_handles_a_non_function_region():
    """The JS sub-region case ``js_function_body`` cannot address."""
    src = (
        'promise.catch((err) => {\n'
        '  const isAbort = err.name === "AbortError";\n'
        '  handle({ nested: true });\n'
        '  TOKEN_IN_CATCH;\n'
        '});\n'
        'function neighbour() { FORBIDDEN; }\n'
    )
    body = brace_block(src, 'const isAbort')
    assert 'TOKEN_IN_CATCH' in body, 'the whole catch body must be captured'
    assert 'FORBIDDEN' not in body
    with pytest.raises(AssertionError):
        brace_block('nothing here', 'absent-anchor')


def test_trailing_newline_never_costs_a_line():
    """★ A file ending in a newline must not come back one line shorter.

    ``splitlines()`` on text ending in ``"\\n"`` yields N entries, and joining N
    entries with ``"\\n"`` produces text whose own ``splitlines()`` is also N —
    the terminator is silently gone. Every caller that maps an output index back
    onto a SOURCE LINE NUMBER is then off by one at the tail.

    Found while migrating the two line-number-reporting guards
    (``test_frontend_lazy_sentinel_anchor``, ``test_i18n_pack_boot_floor``):
    measured on ``static/js/core/escape_html.js``, 20 source lines came back as
    19, with lines 0-18 aligned and only the final empty line missing. It was
    PRE-EXISTING in the default whole-line path (reproduced against HEAD's copy),
    and the module docstring promises "line count preserved" — so this pins that
    promise rather than a new behaviour.

    Note the fix is NOT appending ``"\\n"`` to the joined string: that text still
    ``splitlines()`` to N. The input's terminator has to return as an extra
    EMPTY ELEMENT before the join. Both the block loop and the line-prefix pass
    needed it — CSS returns before the second one, which is why the bug showed
    up on JS only (72 of 173 files off by one until both were fixed).
    """
    for lang, sample in (
        ('js', 'const a = 1;\n// c\nconst b = 2;\n'),
        ('css', '.a{x:1}\n/* c */\n.b{y:2}\n'),
        ('python', 'a = 1\n# c\nb = 2\n'),
        ('shell', 'echo a\n# c\necho b\n'),
    ):
        modes = [{}]
        if lang in ('js', 'css'):
            modes += [{'inline': True}, {'strings': True}]
        for kw in modes:
            out = strip_comments(sample, lang=lang, **kw)
            assert len(out.splitlines()) == len(sample.splitlines()), (
                'lang=%s %r: %d source lines came back as %d — a caller mapping '
                'an index onto a source line number is off by one at the tail'
                % (lang, kw, len(sample.splitlines()), len(out.splitlines())))
            assert out.endswith('\n') == sample.endswith('\n'), (
                'lang=%s %r: trailing-newline presence changed' % (lang, kw))

    # Complement: a file with NO trailing newline must not gain one.
    for lang in ('js', 'css', 'python'):
        sample = ('x = 1' if lang == 'python'
                  else '.a{x:1}' if lang == 'css' else 'const a = 1;')
        out = strip_comments(sample, lang=lang)
        assert not out.endswith('\n'), (
            'lang=%s: a file without a trailing newline must not gain one' % lang)


def test_strings_mode_empties_literals_but_keeps_code():
    """``strings=True`` — added for test_frontend_reducer_purity.

    That guard asserts a reducer contains no side-effect SYMBOLS, so a forbidden
    word appearing inside a user-facing message string must not count. Emptying
    literals reuses the quote tracking the inline pass already needs, rather
    than adding a second literal parser (charter #24).
    """
    src = ('const msg = "do not call saveConversations here";\n'
           'const t = `also localStorage in a template`;\n'
           'realCall();\n')

    default = strip_comments(src, lang='js', inline=True)
    assert 'saveConversations' in default, (
        'the DEFAULT pass must leave literal contents alone — existing callers '
        'depend on seeing class names and messages')

    stripped = strip_comments(src, lang='js', strings=True)
    assert 'saveConversations' not in stripped, (
        'strings=True must empty literal contents so an identifier scan cannot '
        'be tripped by a message string')
    assert 'localStorage' not in stripped, 'template literals too'
    assert 'realCall' in stripped, 'real code must survive'
    assert 'const msg' in stripped and 'const t' in stripped, (
        'the assignments themselves must survive — only the contents go')
    assert len(stripped.splitlines()) == len(src.splitlines()), (
        'strings=True must preserve line count like every other mode')


def test_js_function_body_is_brace_matched_not_byte_sliced():
    src = (
        'function target() {\n'
        '  if (x) { inner(); }\n'
        '  return 1;\n'
        '}\n'
        'function neighbour() {\n'
        '  FORBIDDEN_TOKEN;\n'
        '}\n'
    )
    body = js_function_body(src, 'target')
    assert 'return 1;' in body, 'the whole body must be captured'
    assert 'FORBIDDEN_TOKEN' not in body, (
        "a neighbour's code must never be attributed to this function")


def test_js_function_body_refuses_a_partial_body():
    """Never let an assertion run against a truncated body — that is vacuous."""
    with pytest.raises(AssertionError):
        js_function_body('function f() {\n  if (x) {\n', 'f')
    with pytest.raises(AssertionError):
        js_function_body('var y = 1;', 'absent')


def test_js_function_body_ignores_braces_in_comments_and_strings():
    src = (
        'function target() {\n'
        '  /* } this brace is prose } */\n'
        '  const s = "} also not real";\n'
        '  return 2;\n'
        '}\n'
        'function neighbour() { FORBIDDEN; }\n'
    )
    body = js_function_body(src, 'target')
    assert 'return 2;' in body, (
        'a brace inside a comment or string must not close the function early')
    assert 'FORBIDDEN' not in body
