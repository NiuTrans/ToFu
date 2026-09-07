"""tests/_source_scan.py — shared source-text scanning primitives for guards.

WHY THIS EXISTS (the rule, learned three times the hard way)
-----------------------------------------------------------
**Any guard that scans SOURCE TEXT must strip comments first.** This is a fixed
precondition for writing that kind of guard, not a patch to apply after it
misfires. Measured history, three separate incidents of the same shape:

  1. The QR batch: a test harness faked ``t()`` with ``(k, d) => (d || k)``,
     inverting the real semantics, so 11 render guards passed on a defect that
     shipped a literal ``project.qrScan`` to users.
  2. The ``--only-shell`` ratchet in ``test_install_uv_fastpath.py``: a comment
     documenting the recovery command ``python -m playwright install chromium``
     was matched as a real invocation, turning a correct tree red.
  3. ``test_chromium_binary_resolution.py`` (the SAME ratchet in a second
     implementation, which is the actual root cause): incident 2 was fixed in
     one file only, so the duplicate kept the bug and went red on the next
     comment that mentioned the command.

Incident 3 is the reason this module exists rather than a second local fix: two
copies of "what counts as a real invocation" will always drift. Both guards now
call :func:`strip_comments` / :func:`playwright_install_invocations`, so the
definition lives once.

A comment must never be able to SATISFY a guard, and must never be able to
VIOLATE one either. Both directions matter: the first makes a guard vacuous, the
second makes it a false alarm that trains people to ignore it.
"""

import re

__all__ = [
    'strip_comments',
    'supported_langs',
    'js_function_body',
    'python_block',
    'brace_block',
    'playwright_install_invocations',
]

#: Line-comment markers per language family.
#:
#: The ``js`` family also lists ``*`` so that the CONTINUATION lines of a
#: ``/* … */`` banner (conventionally ``   * …``) are blanked as whole-line
#: comments; the block delimiters themselves are handled by the block pass in
#: :func:`strip_comments`.
#:
#: ``css`` is a FIRST-CLASS language, not an alias. Before it existed, the dozen
#: guards that scan ``static/styles.css`` passed ``lang='css'`` and silently
#: fell through to the shell default (``#`` only) — which strips essentially
#: nothing from CSS. That is the worst kind of failure for a guard primitive: it
#: looks like a successful strip and quietly removes the guard's teeth. CSS has
#: no line-comment syntax at all (``//`` is invalid CSS), hence the empty tuple;
#: its ``/* … */`` blocks are handled by the block pass.
_LINE_COMMENT_PREFIXES = {
    'shell': ('#',),
    'python': ('#',),
    'js': ('//', '*'),
    'css': (),
}

#: Accepted spellings that map onto a canonical language above.
#:
#: ``py`` is honoured because two call sites in
#: ``tests/test_chat_active_consumer_census.py`` already pass it. That worked
#: only by accident — it fell through to the shell default, which happens to
#: share ``#`` with Python — so registering it turns a silent coincidence into
#: a declared equivalence.
_LANG_ALIASES = {
    'py': 'python',
    'sh': 'shell',
    'bash': 'shell',
    'javascript': 'js',
    'mjs': 'js',
}

#: Languages whose ``/* … */`` block comments :func:`strip_comments` removes.
#:
#: Opt-in per language. By DEFAULT the block pass is WHOLE-LINE only (see the
#: function's docstring); ``inline=True`` opts into the stricter pass that also
#: handles a block opening after code.
_BLOCK_COMMENT_LANGS = frozenset({'js', 'css'})


def supported_langs():
    """Return the set of language names :func:`strip_comments` accepts.

    Exposed so a guard can assert its language is genuinely supported rather
    than discovering at runtime that it stripped nothing.
    """
    return frozenset(_LINE_COMMENT_PREFIXES) | frozenset(_LANG_ALIASES)


def _resolve_lang(lang):
    """Canonicalise ``lang``, FAILING LOUDLY when it is unknown.

    A silent fall-through is how ``lang='css'`` came to strip nothing for a
    dozen guards: the call succeeded, returned almost-unchanged text, and every
    assertion downstream ran against source that still had its comments. An
    unknown language is a programming error in the guard, so it must raise —
    the same reasoning as refusing to return a partial function body.
    """
    key = (lang or '').lower()
    key = _LANG_ALIASES.get(key, key)
    if key not in _LINE_COMMENT_PREFIXES:
        raise ValueError(
            'unsupported lang %r for strip_comments — known: %s. Silently '
            'falling back would strip almost nothing and make the calling '
            'guard vacuous, so this is a hard error.'
            % (lang, ', '.join(sorted(supported_langs()))))
    return key


def _strip_blocks_inline(text, strings=False):
    """Blank ``/* … */`` blocks INCLUDING ones opening after code.

    Line count is preserved (callers report source line numbers), and the code
    BEFORE an opener plus the code AFTER a terminator both survive — the
    property a naive ``re.sub(r'/\\*.*?\\*/', '', s, flags=re.S)`` gets right
    and a whole-line pass cannot.

    String/template literals are respected, which is exactly what the module
    docstring says a half-correct parser must not get wrong: a ``/*`` inside a
    quoted string is DATA, and dropping from there would delete real code.

    ``strings=True`` additionally EMPTIES each literal — the delimiters are kept
    so the expression still parses to a reader, but the contents are dropped, so
    an identifier-level scan cannot be satisfied (or violated) by a word that
    only ever appears inside a message string. This reuses the quote tracking
    already needed above rather than adding a second literal parser, which is
    the whole point of it living here (charter #24).
    """
    out = []
    in_block = False
    quote = None
    for line in text.splitlines():
        buf = []
        i = 0
        while i < len(line):
            two = line[i:i + 2]
            if in_block:
                if two == '*/':
                    in_block = False
                    i += 2
                    continue
                i += 1
                continue
            if quote:
                # Inside a literal. With strings=True we emit nothing until the
                # closing delimiter, so the literal collapses to '' / "" / ``.
                if line[i] == '\\':
                    if not strings:
                        buf.append(line[i])
                        if i + 1 < len(line):
                            buf.append(line[i + 1])
                    i += 2
                    continue
                if line[i] == quote:
                    buf.append(line[i])          # closing delimiter
                    quote = None
                    i += 1
                    continue
                if not strings:
                    buf.append(line[i])
                i += 1
                continue
            if line[i] in '"\'`':
                quote = line[i]
                buf.append(line[i])              # opening delimiter
                i += 1
                continue
            if two == '//':
                break                      # rest of the line is a comment
            if two == '/*':
                in_block = True
                i += 2
                continue
            buf.append(line[i])
            i += 1
        # An unterminated template literal legitimately spans lines; a plain
        # quote does not, so reset it per line to avoid swallowing the file.
        if quote in ('"', "'"):
            quote = None
        out.append(''.join(buf).rstrip())
    # ``splitlines()`` on text ending in "\n" yields N entries, but joining N
    # entries with "\n" produces text whose own ``splitlines()`` is N — the
    # terminator is lost, and every caller that maps an output index back onto a
    # source line number is off by one at the tail. Measured on
    # static/js/core/escape_html.js: 20 source lines came back as 19, with lines
    # 0-18 aligned and only the final empty line gone.
    #
    # Appending "\n" to the joined string does NOT fix it (that text still
    # splitlines() to N). The input's trailing newline has to come back as an
    # extra EMPTY ELEMENT before the join.
    if text.endswith('\n'):
        out.append('')
    return '\n'.join(out)


def strip_comments(text, lang='shell', inline=False, strings=False):
    """Return ``text`` with comments removed, line count preserved.

    Lines are blanked rather than deleted so that any line-number arithmetic a
    caller does afterwards still lines up with the original file.

    Only WHOLE-LINE comments are stripped BY DEFAULT — a trailing ``# …`` after
    real code is left alone, because in shell a ``#`` can appear inside a quoted
    string and this module refuses to half-parse quoting. Guards that need to
    ignore trailing comments should match a more specific command shape instead.

    For ``lang='js'`` / ``lang='css'`` a ``/* … */`` BLOCK pass runs first,
    again whole-line only by default: a line is blanked while it sits inside a
    block that OPENED on its own line.

    ``inline=True`` opts into the stricter block pass that ALSO strips a block
    opening after code on the same line, while respecting string/template
    literals. That is what the CSS guards need and why they each grew a local
    copy: a comment containing literal braces corrupts a brace-based rule
    splitter, so leaving inline comments in place is not an option for them.
    Measured on the real 22k-line ``static/styles.css``: the default pass leaves
    20 ``/*`` markers behind, ``inline=True`` leaves 0.

    ``strings=True`` (implies the inline pass) additionally EMPTIES string and
    template literals, keeping their delimiters. An identifier-level scan then
    cannot be satisfied — or falsely tripped — by a word that only ever appears
    inside a message string, which is the same class of false signal a comment
    causes. Needed by guards that assert "this symbol does not appear in CODE",
    where a user-facing string legitimately mentioning it must not count.

    Args:
        text: Full source text.
        lang: ``'shell'`` / ``'python'`` (both ``#``), ``'js'`` (``//``, ``*``
            continuations, ``/* … */``), or ``'css'`` (``/* … */`` only).
            Aliases: ``py``, ``sh``, ``bash``, ``javascript``, ``mjs``.
            An unknown language RAISES rather than silently under-stripping.
        inline: Also strip block comments that open after code (JS/CSS only).
        strings: Also empty string/template literals (JS/CSS only; implies
            ``inline``).

    Returns:
        The text with comments removed, line count preserved.

    Raises:
        ValueError: if ``lang`` is not supported.
    """
    lang = _resolve_lang(lang)
    prefixes = _LINE_COMMENT_PREFIXES[lang]
    strip_blocks = lang in _BLOCK_COMMENT_LANGS

    if (inline or strings) and strip_blocks:
        text = _strip_blocks_inline(text, strings=strings)
        if not prefixes:
            return text
        # Same trailing-line trap as in _strip_blocks_inline: this second pass
        # must not undo the terminator the first one just preserved. CSS has no
        # line-comment prefixes so it returns above and was unaffected, which is
        # why the bug showed up only on JS (measured: 72 of 173 files off by one
        # until this join was fixed too).
        lines = [
            '' if line.lstrip().startswith(prefixes) else line
            for line in text.splitlines()
        ]
        if text.endswith('\n'):
            lines.append('')
        return '\n'.join(lines)

    out = []
    in_block = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if strip_blocks:
            if in_block:
                # Still inside a block comment: blank the line, and close the
                # block when this line carries the terminator.
                if '*/' in line:
                    in_block = False
                out.append('')
                continue
            if stripped.startswith('/*'):
                # A one-line ``/* … */`` closes immediately; otherwise the
                # block stays open for the lines that follow.
                if '*/' not in stripped[2:]:
                    in_block = True
                out.append('')
                continue
        out.append('' if (prefixes and stripped.startswith(prefixes)) else line)
    # Same trailing-line trap as the inline path above, and PRE-EXISTING here
    # (present in HEAD before the inline/strings modes were added — verified by
    # running HEAD's copy: escape_html.js came back 20 -> 19). The module
    # docstring promises "line count preserved", so this is the promise being
    # kept rather than a behaviour change: a caller mapping an output index onto
    # a source line number was silently off by one at the tail.
    if text.endswith('\n'):
        out.append('')
    return '\n'.join(out)


def js_function_body(text, name, lang='js'):
    """Return the FULL body of JS ``function <name>`` by brace matching.

    WHY THIS EXISTS (incident 4 of the family in the module docstring)
    -----------------------------------------------------------------
    Two guards in ``tests/test_conv_state_p6_verdict.py`` extracted a function
    body as a FIXED ``src[start:start + 4000]`` byte slice. That is wrong in
    both directions and silently so:

      * OVERSHOOT — measured on the live tree, the 4000-byte window past
        ``_reconcileStuckActiveTaskPins`` (real body 2999 B) swallowed 1001
        bytes of ``_reconcileIntervalMs`` + ``_crossDeviceReconcile``, and the
        window past ``applyConvStateSnapshot`` (real body 3391 B) swallowed 609
        bytes of the next declaration. A guard asserting "token X must NOT
        appear in this function" therefore fails on a NEIGHBOUR's use of X —
        an accusation pointed at innocent code.
      * TRUNCATION — the mirror failure once a function grows past the window:
        the tail stops being scanned, so a real violation added at the end of a
        long function is silently not seen. That direction is worse, because it
        is invisible: the guard just goes quiet.

    Brace matching removes the constant. Comments and string literals are
    stripped/neutralised BEFORE counting so a ``{`` inside a comment or a
    quoted string cannot unbalance the scan — the same discipline
    :func:`strip_comments` exists to enforce, applied to a different question.

    Args:
        text: Full source text of the file.
        name: Function name, as it appears after the ``function`` keyword.
        lang: Comment family for the pre-strip (default ``'js'``).

    Returns:
        Source of the function from the ``function`` keyword through its
        matching closing brace, comments blanked (line count preserved).

    Raises:
        AssertionError: if the function is absent, or its braces never balance
            (a caller asserting on a body must never silently receive a
            partial one).
    """
    live = strip_comments(text, lang=lang)
    needle = 'function ' + name
    start = live.find(needle)
    assert start != -1, (
        'function %s not found in the scanned source (it may have been '
        'renamed or deleted — a guard must not silently scan nothing)' % name)

    open_at = live.find('{', start)
    assert open_at != -1, 'no opening brace after function %s' % name

    # Neutralise string/template literals so braces inside them do not count.
    # Comments are already blanked by strip_comments above.
    depth = 0
    i = open_at
    quote = None
    while i < len(live):
        ch = live[i]
        if quote:
            if ch == '\\':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in '"\'`':
            quote = ch
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return live[start:i + 1]
        i += 1
    raise AssertionError(
        'braces never balanced for function %s — refusing to return a partial '
        'body, which would make any "token not in body" assertion vacuous'
        % name)


def python_block(text, anchor, lang='python'):
    """Return the full INDENTED block introduced by the line containing ``anchor``.

    The Python counterpart of :func:`js_function_body`, and the primitive the
    fixed-byte-slice sites actually needed: three of the four measured sites
    slice a Python ``if``/``async def`` body, which has no braces to match.

    WHY (the same incident family, truncation direction)
    ---------------------------------------------------
    Measured on the live tree, every Python slice site TRUNCATES rather than
    overshoots — the real constructs are 6424 B, 16684 B and 3692 B against
    800/600/800-byte windows. All three carry POSITIVE assertions ("this token
    must be present"), and a positive assertion over a truncated window passes
    today only because the token happens to sit early in the block. Move the
    code, or add a branch above it, and the guard goes quietly green while the
    property it protects is gone. That silence is the failure mode: unlike an
    overshoot it never produces a red light to investigate.

    Block end = the first non-blank line indented at or below the anchor line's
    own indentation. Comments are stripped first so a dedented comment cannot
    terminate the block early.

    Args:
        text: Full source text.
        anchor: Substring identifying the introducing line (e.g.
            ``"if field_type == 'type':"``). Must appear exactly once, so a
            guard cannot silently bind to the wrong occurrence.
        lang: Comment family (default ``'python'``).

    Returns:
        The introducing line plus its indented body.

    Raises:
        AssertionError: if ``anchor`` is missing or ambiguous.
    """
    live = strip_comments(text, lang=lang)
    hits = live.count(anchor)
    assert hits != 0, (
        'anchor %r not found — a guard must never silently scan nothing'
        % (anchor,))
    assert hits == 1, (
        'anchor %r appears %d times; refusing to guess which block was meant '
        '(make the anchor more specific)' % (anchor, hits))

    start = live.index(anchor)
    line_start = live.rfind('\n', 0, start) + 1
    lines = live[line_start:].splitlines()
    base = len(lines[0]) - len(lines[0].lstrip())

    # ── Skip a MULTI-LINE header before measuring the body ──────────────
    # A ``def f(\n  a, b\n) -> T:`` signature closes its paren at the SAME
    # indentation as ``def``, and its continuation lines are indented deeper.
    # Naively taking the line after ``lines[0]`` as the body start therefore
    # ended the block at the closing ``)`` — measured: 164 B returned for a
    # function whose real body is 6.4 KB, i.e. a "precise" extractor that was
    # wronger than the 2000-byte window it replaced. The header runs until the
    # first line whose bracket depth returns to zero AND which ends in ``:``.
    head = 0
    depth = 0
    for idx, ln in enumerate(lines):
        depth += ln.count('(') + ln.count('[') + ln.count('{')
        depth -= ln.count(')') + ln.count(']') + ln.count('}')
        if depth <= 0 and ln.rstrip().endswith(':'):
            head = idx
            break

    out = lines[:head + 1]
    for ln in lines[head + 1:]:
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= base:
            break
        out.append(ln)
    return '\n'.join(out)


def brace_block(text, anchor, lang='js'):
    """Return the ``{ … }`` block that ENCLOSES the line containing ``anchor``.

    For a brace-language region that is not a whole named function — e.g. the
    ``catch`` body of an inline arrow chain, which :func:`js_function_body`
    cannot address because there is no ``function NAME`` to anchor on.

    The anchor names a line INSIDE the block; the enclosing ``{`` is found by
    scanning BACKWARDS. Scanning forwards for the next ``{`` was the obvious
    implementation and it is wrong: measured, it latched onto an inner object
    literal (``handle({ nested: true })``) and returned a fragment that stopped
    before the token being asserted on. An extractor that silently returns the
    wrong region is the same failure class as the fixed-byte window it replaces.

    Same guarantees as :func:`js_function_body`: comments blanked and string /
    template literals neutralised before counting, and a REFUSAL rather than a
    partial block when the braces never balance.

    Args:
        text: Full source text.
        anchor: Substring on a line INSIDE the wanted block.
        lang: Comment family (default ``'js'``).

    Returns:
        Source from the enclosing ``{`` through its matching ``}``.

    Raises:
        AssertionError: if the anchor is missing or no enclosing block balances.
    """
    live = strip_comments(text, lang=lang)
    at = live.find(anchor)
    assert at != -1, (
        'anchor %r not found — a guard must never silently scan nothing'
        % (anchor,))

    # Walk backwards to the innermost unclosed '{' at or before the anchor.
    depth = 0
    open_at = -1
    i = at
    while i >= 0:
        ch = live[i]
        if ch == '}':
            depth += 1
        elif ch == '{':
            if depth == 0:
                open_at = i
                break
            depth -= 1
        i -= 1
    assert open_at != -1, (
        'no enclosing "{" found before anchor %r — the anchor is not inside a '
        'brace block' % (anchor,))

    depth = 0
    i = open_at
    quote = None
    while i < len(live):
        ch = live[i]
        if quote:
            if ch == '\\':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in '"\'`':
            quote = ch
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return live[open_at:i + 1]
        i += 1
    raise AssertionError(
        'braces never balanced from anchor %r — refusing to return a partial '
        'block, which would make any assertion over it unreliable' % (anchor,))


#: A real ``playwright install`` browser-download invocation.
#:
#: ``install-deps`` is a DIFFERENT subcommand (it apt-installs system libs and
#: accepts no ``--only-shell``), so it is excluded. Requiring the browser name
#: after optional flags is what separates a command from prose such as the WARN
#: line "playwright install failed; JS-page fetch will degrade".
_PLAYWRIGHT_INSTALL_RE = re.compile(
    r'playwright install(?!-deps)((?:\s+-[^\s|&>]+)*)\s+chromium\b')


def playwright_install_invocations(text, lang='shell'):
    """Return every REAL ``playwright install … chromium`` invocation in ``text``.

    Comments are stripped first (see the module docstring). This is the single
    definition of "a real invocation" shared by the ``--only-shell`` ratchets in
    tests/test_install_uv_fastpath.py and
    tests/test_chromium_binary_resolution.py — previously two hand-written
    copies that drifted, which is how a comment-induced false alarm survived a
    fix applied to only one of them.

    Returns:
        List of matched invocation strings, whitespace-trimmed.
    """
    live = strip_comments(text, lang=lang)
    return [m.group(0).strip() for m in _PLAYWRIGHT_INSTALL_RE.finditer(live)]
