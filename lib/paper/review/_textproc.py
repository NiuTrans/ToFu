"""Review Mode — deterministic text-cleaning pipeline.

Backend is the source of truth for a submittable review body: smart quotes,
slop-dash removal, table/emphasis stripping, and scorecard relocation are all
applied HERE on the final review body regardless of what the model emitted.
"""

from lib.log import get_logger

logger = get_logger(__name__)



# ── Typography: straight → smart (curly) quotes ─────────────────────────
# A review MUST render with typographic (smart/curly) quotes regardless of
# what the model emits — asking the model in the prompt is not reliable, so we
# educate the quotes deterministically on the final review body (backend is the
# source of truth). The one hard rule is that this must NOT touch anything where
# a straight quote is SYNTAX rather than punctuation: KaTeX math (``$...$`` /
# ``$$...$$`` — where ``'`` is a derivative prime and ``"`` a double-prime),
# code spans/blocks, and URLs (in ``](...)`` links or bare ``http(s)://``). Those
# spans are masked out, the gaps are educated, then the spans are restored
# verbatim.
import re as _re

# Protected spans, matched in priority order. Whichever alternative wins at a
# position is emitted UNCHANGED; only the text between matches is educated.
_PROTECT_RE = _re.compile(
    r'```.*?```'                 # fenced code block (multiline)
    r'|~~~.*?~~~'                # fenced code block (tilde form)
    r'|`[^`\n]+`'                # inline code span
    r'|\$\$.*?\$\$'              # display math
    r'|\$[^$\n]+\$'              # inline math (no newline — a lone $ is left alone)
    r'|\]\([^)]*\)'              # markdown link / image target: ](URL "title")
    r'|<https?://[^>\s]+>'       # autolink URL
    r'|https?://\S+',            # bare URL
    _re.DOTALL,
)

# Characters that legitimately precede an OPENING quote (start-of-word context).
_OPEN_BEFORE = ' \t\r\n([{<\u2014\u2013\u201c\u2018/*_~'


def _educate_segment(text: str) -> str:
    """Convert straight quotes to curly quotes in a plain-text segment."""
    if '"' not in text and "'" not in text:
        return text
    # ── Double quotes ──
    # Opening: at segment start or after an opening-context char.
    text = _re.sub(r'(^|[' + _re.escape(_OPEN_BEFORE) + r'])"',
                   lambda m: m.group(1) + '\u201c', text)
    # Any remaining double quote closes.
    text = text.replace('"', '\u201d')
    # ── Single quotes / apostrophes ──
    # Apostrophe inside/after a word (contraction it's, possessive authors',
    # or before a digit like '90s). Handled first so it never becomes an
    # opening curly quote.
    text = _re.sub(r"(?<=[\w\u4e00-\u9fff])'", '\u2019', text)
    text = _re.sub(r"'(?=\d)", '\u2019', text)
    # Opening single quote: start/opening-context followed by a word char.
    text = _re.sub(r'(^|[' + _re.escape(_OPEN_BEFORE) + r"])'(?=[^\W\d]|[\u4e00-\u9fff])",
                   lambda m: m.group(1) + '\u2018', text)
    # Anything left closes.
    text = text.replace("'", '\u2019')
    return text


def smarten_quotes(text: str) -> str:
    """Educate straight quotes to smart quotes, preserving math/code/URLs.

    Straight ``"`` → ``\u201c``/``\u201d`` and straight ``'`` → ``\u2018``/``\u2019``
    (apostrophes → ``\u2019``), applied ONLY to prose. Spans where a straight quote
    is syntax — KaTeX math (``$...$`` primes), inline/fenced code, and URLs — are
    matched and passed through verbatim, so ``$f'(x)$`` and
    ``](https://a.com/x'y)`` are never corrupted.

    Args:
        text: The Markdown review body.

    Returns:
        The same text with prose quotes curled; protected spans unchanged.
    """
    if not text or ('"' not in text and "'" not in text):
        return text or ''
    out = []
    last = 0
    for m in _PROTECT_RE.finditer(text):
        out.append(_educate_segment(text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_educate_segment(text[last:]))
    return ''.join(out)


# ── Typography: remove slop dashes ──────────────────────────────────────
# The em-dash used as a sentence separator (``novel — it improves X``) is the
# single most recognizable LLM-slop tell. A review must not use it; we rewrite
# it deterministically to a comma on the final body, the same way quotes are
# educated. Which dashes are "slop" vs. legitimate typography:
#   • em-dash U+2014 / horizontal-bar U+2015 / double em-dash ``——`` — ALWAYS a
#     prose separator → comma (fullwidth ``，`` in a CJK context, ASCII ``, ``
#     otherwise).
#   • en-dash U+2013 — ONLY slop when it is NOT a numeric range. ``1–10`` /
#     ``2–4`` are real ranges (kept); ``method – result`` (letters/space around
#     it) is a separator (→ comma).
#   • ASCII hyphen-minus ``-`` is NEVER touched: it is a hyphen (``well-motivated``),
#     a markdown bullet (``- x``), or a horizontal rule (``---``).
# Protected spans (math/code/URLs) are masked exactly as smarten_quotes does.
_CJK_RE = _re.compile(r'[\u3000-\u9fff\uff00-\uffef]')

# An em-dash (optionally doubled) with optional surrounding whitespace.
_EMDASH_RE = _re.compile(r'\s*[\u2014\u2015]+\s*')
# An en-dash that is a SEPARATOR: not flanked on BOTH sides by digits. We match
# an en-dash whose immediate neighbours are not both a digit.
_ENDASH_SEP_RE = _re.compile(r'(?<!\d)\s*\u2013\s*|\s*\u2013\s*(?!\d)')


def _deslop_segment(text: str) -> str:
    """Rewrite slop dashes to commas in a plain-text segment."""
    if '\u2014' not in text and '\u2015' not in text and '\u2013' not in text:
        return text

    def _comma_for(match: '_re.Match') -> str:
        # Fullwidth comma when the character just before the dash run is CJK, so
        # a Chinese review never gets a stray ASCII comma; else ``, ``.
        start = match.start()
        prev = text[start - 1] if start > 0 else ''
        return '\uff0c' if _CJK_RE.match(prev) else ', '

    text = _EMDASH_RE.sub(_comma_for, text)

    # En-dash: only a separator (not a numeric range) becomes a comma. Replace
    # occurrences where at least one side is a non-digit.
    def _endash_sub(m: '_re.Match') -> str:
        s, e = m.start(), m.end()
        before = text[s - 1] if s > 0 else ''
        after = text[e] if e < len(text) else ''
        # Numeric range on BOTH sides → keep verbatim.
        stripped = m.group(0).strip()
        if before.isdigit() and after.isdigit() and stripped == '\u2013':
            return m.group(0)
        return '\uff0c' if _CJK_RE.match(before) else ', '

    text = _re.sub(r'\s*\u2013\s*', _endash_sub, text)
    return text


def strip_slop_dashes(text: str) -> str:
    """Rewrite LLM-slop dashes (em-dash / separator en-dash) to commas.

    Applies ONLY to prose. Numeric en-dash ranges (``1–10``), ASCII hyphens
    (``well-motivated``, markdown ``---`` / ``- ``), and syntax spans — KaTeX
    math, inline/fenced code, URLs — are preserved verbatim.

    Args:
        text: The Markdown review body.

    Returns:
        The same text with prose slop dashes turned into commas; protected
        spans and legitimate ranges/hyphens unchanged.
    """
    if not text or ('\u2014' not in text and '\u2015' not in text
                    and '\u2013' not in text):
        return text or ''
    out = []
    last = 0
    for m in _PROTECT_RE.finditer(text):
        out.append(_deslop_segment(text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_deslop_segment(text[last:]))
    return ''.join(out)


# ── Submittable-copy finalization ──────────────────────────────────────
# A peer review must be pasteable straight into the venue's review box: the
# body is 100% prose, and the venue-calibrated SCORES (entered via the form's
# UI fields, not the free-text box) are moved BELOW an obviously-non-submittable
# separator so the reviewer transcribes them into those fields. This is the
# deterministic "belt" that guarantees a clean copy REGARDLESS of what the model
# emits — the prompt asks for the same shape, but the logged lesson
# (``paper-report-image-injection``: LLMs universally ignore manifest/format
# instructions) is exactly why the guarantee cannot live in the prompt alone.
#
# Three cleanups, all applied to the FINAL review body (backend is the source of
# truth), all skipping protected spans (KaTeX math, inline/fenced code, URLs) so
# a table or ``*`` shown INSIDE code is prose, not a rendered artifact:
#   1. strip Markdown pipe tables + HTML <table>/<figure>-style blocks;
#   2. collapse dangling/unpaired ``*`` emphasis left by degraded image captions
#      (the ``…Howe*`` / ``…discrepancy.*`` artifact);
#   3. relocate the scorecard section (its ``## Quantitative Scores`` / venue
#      heading and everything after) below the separator.

_SCORECARD_SEPARATOR_EN = '--- FOR THE REVIEW FORM (do not paste into the review text) ---'
_SCORECARD_SEPARATOR_ZH = '--- 供评审表单填写（请勿粘贴进评审正文） ---'

# Headings that begin the quantitative-scores block, in either language. The
# scorecards all open with a level-2 heading whose text contains one of these.
_SCORE_HEADING_RE = _re.compile(
    r'^[ \t]{0,3}#{1,6}[ \t]+.*(?:Quantitative Scores|Overall Recommendation'
    r'|量化评分|总体推荐)\b.*$',
    _re.IGNORECASE | _re.MULTILINE)

# A Markdown pipe-table row: a line that both starts and ends (bar trailing
# space) with ``|``, i.e. a genuine table row, not a lone inline ``|``.
_MD_TABLE_ROW_RE = _re.compile(r'^[ \t]{0,3}\|.*\|[ \t]*$')
# A Markdown table delimiter row: ``| --- | :--: |`` etc.
_MD_TABLE_DELIM_RE = _re.compile(r'^[ \t]{0,3}\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$')


def _strip_md_tables(text: str) -> str:
    """Remove Markdown pipe-table blocks + raw HTML table/figure wrappers.

    A run of two or more consecutive pipe rows (a header + delimiter, or any
    multi-row table) is dropped whole; a lone line with a single inline ``|`` is
    left alone. Raw ``<table>…</table>`` and ``<figure>…</figure>`` blocks are
    removed with their inner cells (any prose caption between the tags is kept as
    plain text by the tag neutralizer that follows). Operates on a
    protected-span-masked string, so tables inside code fences are untouched.
    """
    if not text:
        return text or ''
    # 1) HTML table / figure blocks → drop the whole element (keep inner text).
    #    Non-greedy so multiple blocks are handled independently.
    def _drop_html_block(tag):
        nonlocal text
        text = _re.sub(rf'<{tag}\b[^>]*>.*?</{tag}>', '', text,
                       flags=_re.IGNORECASE | _re.DOTALL)
    for _tag in ('table', 'figure'):
        _drop_html_block(_tag)

    # 2) Markdown pipe tables: drop maximal runs of >=2 consecutive pipe rows.
    lines = text.split('\n')
    out, i, n = [], 0, len(lines)
    while i < n:
        if _MD_TABLE_ROW_RE.match(lines[i]) or _MD_TABLE_DELIM_RE.match(lines[i]):
            j = i
            while j < n and (_MD_TABLE_ROW_RE.match(lines[j])
                             or _MD_TABLE_DELIM_RE.match(lines[j])):
                j += 1
            if j - i >= 2:          # a real table (>=2 rows) → drop it
                i = j
                continue
        out.append(lines[i])
        i += 1
    return '\n'.join(out)


def _collapse_dangling_emphasis(text: str) -> str:
    """Remove unpaired ``*`` emphasis markers left by degraded image captions.

    A stripped image embed becomes ``*alt*``; when the alt was truncated
    mid-word the pair breaks and marked.js renders a literal trailing ``*``
    (the ``…Howe*`` / ``…discrepancy.*`` artifact). We drop a ``*`` that is not
    part of a balanced ``*...*`` / ``**...**`` pair on its line, and never touch
    ``**`` bold runs that ARE balanced. Bullet markers (``* `` / ``- `` at line
    start) and ``---`` rules are unaffected (only ``*`` glued to a word is
    considered emphasis). Protected spans are handled by the caller.
    """
    if not text or '*' not in text:
        return text or ''
    result = []
    for line in text.split('\n'):
        # Leave list/HR lines and lines with no word-glued star untouched.
        # An emphasis star touches a non-space on at least one side.
        if '*' not in line:
            result.append(line)
            continue
        # Count emphasis stars that are glued to a word char (skip a leading
        # bullet ``* `` which is separated by a space).
        # If the total run-count of ``*`` is odd, the line has a dangling one;
        # drop trailing/leading stars that are adjacent to punctuation/word and
        # have no partner. Simplest robust rule: if stars are unbalanced, remove
        # any ``*`` immediately preceded by a word char / punctuation and not
        # followed by a word char (a closing marker with no opener on the line).
        stars = line.count('*')
        if stars % 2 == 0:
            result.append(line)
            continue
        # Unbalanced: strip a ``*`` that is glued to the char before it and sits
        # at a word/sentence boundary (``Howe*`` → ``Howe``, ``discrepancy.*`` →
        # ``discrepancy.``), leaving genuine paired emphasis alone.
        fixed = _re.sub(r'(?<=[\w\u4e00-\u9fff.,;:!?)\]])\*(?![\w\u4e00-\u9fff])', '', line)
        result.append(fixed)
    return '\n'.join(result)


def _split_scorecard(text: str):
    """Split a review body into (prose_body, scorecard) at the scores heading.

    Returns ``(body, scorecard)`` where ``scorecard`` is '' when the body has
    no recognizable quantitative-scores section. The scorecard is everything
    from the scores heading to the end of the document.
    """
    m = _SCORE_HEADING_RE.search(text)
    if not m:
        return text, ''
    return text[:m.start()].rstrip(), text[m.start():].strip()


def scorecard_separator(ui_lang: str) -> str:
    """The literal, non-submittable separator that precedes the scorecard.

    Shared by ``finalize_review_body`` (which inserts it) and the prompt (which
    asks the model to emit the scorecard after it), so the two never drift.
    """
    return _SCORECARD_SEPARATOR_ZH if ui_lang == 'zh' else _SCORECARD_SEPARATOR_EN


def finalize_review_body(text: str, ui_lang: str) -> str:
    """Make a review body directly submittable to a venue's review box.

    Deterministically enforces (regardless of what the model emitted):
      • the review body proper is pure prose — no Markdown/HTML tables, no
        dangling ``*`` emphasis from degraded captions;
      • the venue scorecard (entered via the form's UI fields) is relocated
        below an explicit, obviously-non-submittable separator.

    Idempotent: re-running a finalized body is a no-op (the scorecard is already
    past the separator, so it is not re-extracted). Protected spans — KaTeX
    math, inline/fenced code, URLs — are masked before cleanup and restored
    verbatim, so a table or ``*`` shown inside code is preserved as prose.

    Args:
        text: The final review Markdown.
        ui_lang: 'zh' or anything else (→ English separator).

    Returns:
        The finalized review Markdown (body, separator, then the scorecard),
        or the input unchanged when it has no scorecard and no table/star to
        clean. Never raises — a cleanup failure returns the original text.
    """
    if not text:
        return text or ''
    sep = scorecard_separator(ui_lang)
    try:
        # If already finalized, split only the prose part; keep the footer.
        existing_body, _, existing_footer = text.partition(sep)
        target = existing_body if existing_footer else text

        # Mask protected spans so tables/stars inside code/math/URLs are safe.
        protected: list[str] = []

        def _mask(m):
            protected.append(m.group(0))
            return f'\x00{len(protected) - 1}\x00'

        masked = _PROTECT_RE.sub(_mask, target)

        # Peel off the scorecard BEFORE table-strip so a pipe-formatted
        # scorecard is relocated, not deleted.
        #
        # Resolve the two cleanup helpers through the facade package
        # (``lib.paper.review``) rather than the bare module globals, so a
        # caller (or test) that monkeypatches ``lib.paper.review._strip_md_tables``
        # still overrides the step exactly as it did when everything lived in
        # the single ``review.py`` module. Fall back to the local defs.
        try:
            import lib.paper.review as _facade
            _strip = getattr(_facade, '_strip_md_tables', _strip_md_tables)
            _collapse = getattr(_facade, '_collapse_dangling_emphasis',
                                _collapse_dangling_emphasis)
        except Exception as e:
            logger.debug('[Paper:Review] facade resolve failed, using local defs: %s', e)
            _strip, _collapse = _strip_md_tables, _collapse_dangling_emphasis
        body, scorecard = _split_scorecard(masked)
        body = _strip(body)
        body = _collapse(body)

        def _unmask(s):
            return _re.sub(r'\x00(\d+)\x00', lambda m: protected[int(m.group(1))], s)
        body = _unmask(body).rstrip()
        scorecard = _unmask(scorecard).strip()

        if existing_footer:
            # Re-attach the pre-existing footer (plus any newly-found scorecard,
            # though a finalized body should have none above the line).
            footer = (scorecard + '\n\n' if scorecard else '') + existing_footer.strip()
            return f'{body}\n\n{sep}\n\n{footer.strip()}\n'
        if not scorecard:
            return f'{body}\n' if body != target.rstrip() else text
        return f'{body}\n\n{sep}\n\n{scorecard}\n'
    except Exception as e:
        logger.warning('[Paper:Review] finalize_review_body failed (returning original): %s',
                       e, exc_info=True)
        return text



# ── Rebuttal follow-up: reply-body + structured score-decision split ────
# A rebuttal pass emits a reviewer reply (prose the reviewer posts to the
# authors) followed by a machine-parseable decision block after the
# ``<<<SCORE DECISION>>>`` sentinel. Backend is the source of truth: we split
# the reply from the decision here and parse the decision into a structured
# dict the UI highlights (e.g. "OA 4 → 5 ⬆"), regardless of stray formatting the
# model wraps around the fields.

_REBUTTAL_DECISION_MARKER = '<<<SCORE DECISION>>>'

# ``KEY: value`` lines inside the decision block. Tolerant of leading markdown
# bullet/bold/space and a trailing ``**``; matched case-insensitively so
# ``Changed:`` / ``CHANGED:`` both work. Value is the rest of the line.
_DECISION_FIELD_RE = _re.compile(
    r'^[ \t>*_-]*\**[ \t]*'
    r'(ORIGINAL_OVERALL|NEW_OVERALL|ORIGINAL_CONFIDENCE|NEW_CONFIDENCE|CHANGED|REASON)'
    r'\**[ \t]*[:：][ \t]*(.*?)[ \t]*\**[ \t]*$',
    _re.IGNORECASE | _re.MULTILINE)

_TRUE_TOKENS = {'yes', 'true', 'y', '1', 'changed', '是', '改', '变', '有'}
_FALSE_TOKENS = {'no', 'false', 'n', '0', 'unchanged', '否', '不变', '无'}


def rebuttal_decision_marker() -> str:
    """The literal sentinel that precedes the structured score decision."""
    return _REBUTTAL_DECISION_MARKER


def _clean_decision_value(v: str) -> str:
    """Strip surrounding brackets/quotes/backticks a model may wrap a field in."""
    v = (v or '').strip()
    # Drop a leading/trailing angle-bracket placeholder the model copied
    # verbatim (``<your overall score>``) — treated as empty.
    v = v.strip('`').strip()
    if v.startswith('<') and v.endswith('>'):
        return ''
    return v.strip('"\u201c\u201d\u2018\u2019 ').strip()


def parse_rebuttal_decision(text: str) -> dict:
    """Parse the structured score decision from a finished rebuttal body.

    Reads the ``KEY: value`` lines below the ``<<<SCORE DECISION>>>`` sentinel
    into a structured verdict the frontend renders. Never raises — a body with
    no decision block (or an unparseable one) returns ``{'present': False}``.

    The ``changed`` boolean is derived DEFENSIVELY: it trusts an explicit
    ``CHANGED:`` yes/no when present, but a model that writes ``CHANGED: no``
    while giving a different NEW value (or vice-versa) is reconciled toward the
    actual values — the scores are the ground truth, the self-reported flag is a
    hint. When the values differ, ``changed`` is forced True regardless of the
    self-report; when they are identical, ``changed`` is forced False.

    Args:
        text: The full rebuttal Markdown (reply + decision block).

    Returns:
        dict: ``{'present': bool, 'origOverall', 'newOverall', 'origConfidence',
        'newConfidence', 'changed': bool, 'reason'}`` — string fields '' when
        absent. ``present`` is False when no decision block was found.
    """
    if not text:
        return {'present': False}
    idx = text.find(_REBUTTAL_DECISION_MARKER)
    block = text[idx + len(_REBUTTAL_DECISION_MARKER):] if idx >= 0 else text
    fields: dict[str, str] = {}
    for m in _DECISION_FIELD_RE.finditer(block):
        fields[m.group(1).upper()] = _clean_decision_value(m.group(2))
    if not fields:
        return {'present': False}

    orig_oa = fields.get('ORIGINAL_OVERALL', '')
    new_oa = fields.get('NEW_OVERALL', '')
    orig_conf = fields.get('ORIGINAL_CONFIDENCE', '')
    new_conf = fields.get('NEW_CONFIDENCE', '')
    reason = fields.get('REASON', '')

    # Self-reported flag (a hint) — reconcile against the actual values below.
    raw_changed = fields.get('CHANGED', '').strip().lower()
    self_changed = None
    if raw_changed in _TRUE_TOKENS:
        self_changed = True
    elif raw_changed in _FALSE_TOKENS:
        self_changed = False

    def _norm(v: str) -> str:
        return _re.sub(r'\s+', ' ', (v or '')).strip().lower()

    # Values are ground truth in BOTH directions: a difference in EITHER
    # dimension forces changed=True; two present-and-identical dimensions force
    # changed=False regardless of a mis-reported CHANGED flag. Only when a new
    # value is MISSING (can't compare) do we fall back to the self-reported flag.
    oa_diff = bool(new_oa) and _norm(orig_oa) != _norm(new_oa)
    conf_diff = bool(new_conf) and _norm(orig_conf) != _norm(new_conf)
    values_differ = oa_diff or conf_diff
    values_comparable = bool(new_oa) or bool(new_conf)
    if values_differ:
        changed = True
    elif values_comparable:
        # Every present dimension equals its original → no change (ground truth
        # overrides a stray CHANGED: yes).
        changed = False
    elif self_changed is not None:
        changed = self_changed
    else:
        changed = False

    return {
        'present': True,
        'origOverall': orig_oa,
        'newOverall': new_oa or orig_oa,
        'origConfidence': orig_conf,
        'newConfidence': new_conf or orig_conf,
        'overallChanged': oa_diff,
        'confidenceChanged': conf_diff,
        'changed': changed,
        'reason': reason,
    }


def finalize_rebuttal_body(text: str, ui_lang: str) -> str:
    """Make a rebuttal reply pasteable + relocate the score decision block.

    Mirrors :func:`finalize_review_body` for the rebuttal pass: the reply text
    (everything above the ``<<<SCORE DECISION>>>`` sentinel) is de-slopped and
    table-stripped so it is directly pasteable into the discussion box, and the
    decision block is kept verbatim below the sentinel (it is parsed separately
    by :func:`parse_rebuttal_decision`, not pasted). Idempotent; never raises.

    Args:
        text: The final rebuttal Markdown (reply + decision block).
        ui_lang: 'zh' or anything else (unused today; kept parallel to
            ``finalize_review_body`` for a symmetric call site).

    Returns:
        The finalized rebuttal Markdown, or the input unchanged on any failure.
    """
    if not text:
        return text or ''
    try:
        reply, sep, decision = text.partition(_REBUTTAL_DECISION_MARKER)
        # Clean ONLY the reply prose; the decision block is machine-read, so it
        # must survive byte-for-byte (a comma-rewrite there would corrupt a
        # score value). Mask protected spans exactly as the review path does.
        protected: list[str] = []

        def _mask(m):
            protected.append(m.group(0))
            return f'\x00{len(protected) - 1}\x00'

        masked = _PROTECT_RE.sub(_mask, reply)
        try:
            import lib.paper.review as _facade
            _strip = getattr(_facade, '_strip_md_tables', _strip_md_tables)
            _collapse = getattr(_facade, '_collapse_dangling_emphasis',
                                _collapse_dangling_emphasis)
        except Exception as e:
            logger.debug('[Paper:Review] facade resolve failed, using local defs: %s', e)
            _strip, _collapse = _strip_md_tables, _collapse_dangling_emphasis
        cleaned = _collapse(_strip(masked))

        def _unmask(s):
            return _re.sub(r'\x00(\d+)\x00', lambda m: protected[int(m.group(1))], s)
        cleaned = _unmask(cleaned).rstrip()

        if sep:
            return f'{cleaned}\n\n{_REBUTTAL_DECISION_MARKER}{decision.rstrip()}\n'
        return f'{cleaned}\n' if cleaned != reply.rstrip() else text
    except Exception as e:
        logger.warning('[Paper:Review] finalize_rebuttal_body failed (returning original): %s',
                       e, exc_info=True)
        return text

