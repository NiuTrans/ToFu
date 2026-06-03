"""lib/artifacts/scanner.py — Producer B.

Detect renderable artifacts emitted INSIDE assistant prose (not via
write_file).  Two detection paths:

  1. Fenced code blocks: ```html, ```markdown / ```md, ```svg above a
     size threshold.  Tiny snippets (single-line inline examples) stay
     as plain code; only "report-shaped" blocks are promoted.
  2. Bare HTML documents: a region beginning with ``<!doctype`` (case-
     insensitive) or ``<html`` and ending with ``</html>`` (or the rest
     of the message when the closing tag is missing in a streamed
     truncation).

The detector is deterministic and stateless — no LLM round-trip, same
philosophy as the ``paper-report-image-injection`` memory: post-process
the assistant output rather than asking the model to emit a special
marker.

Public API:

    scan_message(conv_id, message_text, *, msg_id='', task_id='') -> list[dict]
        Returns the list of created artifact metadata dicts (after dedupe).
        ``content`` is NOT included — callers re-fetch via
        ``get_artifact`` if needed.

The scanner does NOT mutate the assistant message body; it just persists
the detected blocks as artifact rows.  The chat continues to render the
fenced block / inline HTML as code/markdown — the chip is added on top so
the user gets a side-by-side "preview in the panel" affordance.
"""

from __future__ import annotations

import os
import re
from typing import Any

from lib.artifacts.core import (
    ALLOWED_FORMATS,
    ArtifactSizeError,
    create_artifact,
)
from lib.artifacts.events import emit_artifact_event
from lib.log import get_logger

logger = get_logger(__name__)


# ── Thresholds ────────────────────────────────────────────────────────
# Fenced blocks below all of these stay as plain code in the chat.  We
# don't want every ```bash``` snippet to grow a chip.  Tunable via env
# vars so deployments can tighten/loosen without a redeploy.

def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        return int(v) if v else default
    except (ValueError, TypeError) as _e_audit:
        logger.debug('[scanner] _env_int caught %s: %s', type(_e_audit).__name__, _e_audit)
        return default


# Minimum size of a fenced block before it becomes an artifact.
_MIN_FENCE_BYTES = _env_int('TOFU_ARTIFACT_FENCE_MIN_BYTES', 2048)
_MIN_FENCE_LINES = _env_int('TOFU_ARTIFACT_FENCE_MIN_LINES', 80)

# Same thresholds for bare-HTML detection — but lower, because if the
# model bothered to emit `<!doctype html>` at all, it's almost certainly
# a "render this" payload regardless of size.
_MIN_DOC_BYTES = _env_int('TOFU_ARTIFACT_DOC_MIN_BYTES', 200)

# Per-feature ceiling.  Anything larger is left as inline code rather
# than persisted (the lib.artifacts hard cap is 8 MiB).
_MAX_BYTES = _env_int('TOFU_ARTIFACT_SCAN_MAX_BYTES', 1 * 1024 * 1024)


# ── Regexes ───────────────────────────────────────────────────────────
# Fenced code block: opening ``` followed by an optional language tag,
# then content up to the closing ```.  We use a non-greedy match and
# capture both the language and the body.
#
# Backticks at column 0 (or after a newline) are the canonical form — we
# also accept ~~~ which is the alternative CommonMark fence.  Three or
# more backticks/tildes is the spec; we cap at exactly three for
# pragmatic regex simplicity (4-tilde fences are rare in practice).
_FENCE_RE = re.compile(
    r'(?:^|\n)(?P<fence>```|~~~)(?P<lang>[^\n`]*)\n(?P<body>.*?)(?:\n)(?P=fence)(?=\n|$)',
    re.DOTALL,
)

# Inline code span (single backticks).  Matches the smallest plausible
# CommonMark span: one backtick, non-backtick content, one backtick on
# the same line.  Multiline / multi-backtick spans are rare in practice
# and harmless if missed — masking is an over-conservative measure to
# stop ``<html>`` written as inline code from tripping the bare-HTML
# detector.  Anchored by the absence of a backtick inside.
_INLINE_CODE_RE = re.compile(r'`[^`\n]+?`')

# Bare HTML document — picks up `<!doctype html>` / `<!DOCTYPE HTML>` /
# bare `<html>` opening with optional attrs.  We capture from that point
# to the matching `</html>`; if the closing tag is absent (truncated
# stream), we capture to end of string.
_DOCTYPE_RE = re.compile(
    r'<!doctype\s+html[^>]*>.*?</html\s*>|<!doctype\s+html[^>]*>.*?\Z',
    re.IGNORECASE | re.DOTALL,
)
_BARE_HTML_RE = re.compile(
    r'<html(?:\s[^>]*)?>.*?</html\s*>|<html(?:\s[^>]*)?>.*?\Z',
    re.IGNORECASE | re.DOTALL,
)

# Title extraction — first H1 in markdown or <title> in HTML.
_MD_H1_RE = re.compile(r'^\s*#\s+(.+?)\s*$', re.MULTILINE)
_HTML_TITLE_RE = re.compile(
    r'<title[^>]*>\s*(.*?)\s*</title>',
    re.IGNORECASE | re.DOTALL,
)


# ── Language tag → format mapping ─────────────────────────────────────
_LANG_TO_FORMAT = {
    'html':     'html',
    'htm':      'html',
    'xhtml':    'html',
    'markdown': 'markdown',
    'md':       'markdown',
    'mdx':      'markdown',
    'svg':      'svg',
}


def _lang_to_format(lang: str) -> str | None:
    """Translate a fence language tag to one of ALLOWED_FORMATS."""
    if not lang:
        return None
    tag = lang.strip().lower().split()[0] if lang.strip() else ''
    return _LANG_TO_FORMAT.get(tag)


def _extract_title(content: str, fmt: str) -> str:
    """Best-effort title extraction.  Falls back to '' (caller substitutes)."""
    if fmt == 'markdown':
        m = _MD_H1_RE.search(content)
        if m:
            return m.group(1).strip()[:200]
    elif fmt == 'html':
        m = _HTML_TITLE_RE.search(content)
        if m:
            # Decode &amp; etc. is overkill for a title — strip tags only.
            t = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            return t[:200]
    elif fmt == 'svg':
        m = re.search(r'<title[^>]*>(.*?)</title>', content,
                      re.IGNORECASE | re.DOTALL)
        if m:
            return re.sub(r'<[^>]+>', '', m.group(1)).strip()[:200]
    return ''


# ── Detection ─────────────────────────────────────────────────────────

def _detect_fenced_blocks(text: str) -> list[dict[str, Any]]:
    """Find renderable fenced code blocks above the size threshold.

    Returns a list of dicts: {format, content, title, source_ref}.
    """
    out: list[dict[str, Any]] = []
    for i, m in enumerate(_FENCE_RE.finditer(text or '')):
        lang = m.group('lang') or ''
        fmt = _lang_to_format(lang)
        if fmt is None:
            continue
        body = m.group('body') or ''
        if not body.strip():
            continue
        size = len(body.encode('utf-8', errors='replace'))
        line_count = body.count('\n') + 1
        # Either threshold qualifies — markdown reports tend to be
        # line-heavy without being byte-heavy and vice-versa for HTML.
        if size < _MIN_FENCE_BYTES and line_count < _MIN_FENCE_LINES:
            continue
        if size > _MAX_BYTES:
            logger.info(
                '[Artifacts:scan] skip oversize fenced block fmt=%s size=%d cap=%d',
                fmt, size, _MAX_BYTES,
            )
            continue
        out.append({
            'format': fmt,
            'content': body,
            'title': _extract_title(body, fmt) or f'inline.{fmt}',
            'source_ref': {
                'kind': 'fence',
                'language': lang.strip(),
                'fence_index': i,
                'start': m.start('body'),
                'end': m.end('body'),
            },
            'source': 'inline_fence',
        })
    return out


def _all_fence_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) ranges of EVERY fenced code block, regardless
    of language tag.  Used by ``_mask_all_code_regions`` so the
    bare-HTML detector ignores HTML written inside ``` blocks of any
    language (including documentation prose like ``\u200B```python``).
    """
    spans: list[tuple[int, int]] = []
    for m in _FENCE_RE.finditer(text or ''):
        spans.append((m.start(), m.end()))
    return spans


def _all_inline_code_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) ranges of every inline ``` `code` ``` span."""
    return [(m.start(), m.end()) for m in _INLINE_CODE_RE.finditer(text or '')]


def _detect_bare_html_docs(text: str) -> list[dict[str, Any]]:
    """Find inline `<!doctype html>` / `<html>` documents not in a fence.

    To avoid double-counting a fenced HTML block, the caller masks out
    fence regions (see ``scan_message``).
    """
    out: list[dict[str, Any]] = []
    seen_spans: list[tuple[int, int]] = []

    for rx in (_DOCTYPE_RE, _BARE_HTML_RE):
        for m in rx.finditer(text or ''):
            start, end = m.start(), m.end()
            # Dedupe overlapping matches (e.g. doctype+html)
            if any(start < e and end > s for s, e in seen_spans):
                continue
            body = m.group(0)
            size = len(body.encode('utf-8', errors='replace'))
            if size < _MIN_DOC_BYTES or size > _MAX_BYTES:
                continue
            out.append({
                'format': 'html',
                'content': body,
                'title': _extract_title(body, 'html') or 'inline.html',
                'source_ref': {
                    'kind': 'doc',
                    'start': start,
                    'end': end,
                },
                'source': 'inline_doc',
            })
            seen_spans.append((start, end))
    return out


def _mask_fence_regions(text: str, blocks: list[dict[str, Any]]) -> str:
    """Replace fenced regions in *text* with same-length spaces so that
    bare-HTML detection doesn't re-detect them.  Length-preserving so
    indices on the raw text remain valid for downstream callers."""
    if not blocks:
        return text
    out = list(text)
    for b in blocks:
        sr = b.get('source_ref') or {}
        s = sr.get('start')
        e = sr.get('end')
        if s is None or e is None:
            continue
        for i in range(s, min(e, len(out))):
            if out[i] != '\n':
                out[i] = ' '
    return ''.join(out)


def _mask_all_code_regions(text: str) -> str:
    """Wipe ALL code regions (fenced + inline) before bare-HTML scan.

    Why mask ALL fenced blocks (not only renderable-language ones)?
    Documentation prose routinely contains HTML markup inside ``` blocks
    tagged ``python``, ``text``, or no language at all (technical
    explanations).  The bare-HTML detector, finding ``<html>`` inside
    such a block + a stray closing tag elsewhere in prose, would
    promote the spliced span to an artifact — which is exactly what
    happened in the regression that produced this code path (see
    memory ``artifacts-scanner-prose-false-positive``).

    Also masks inline ``` `code` ``` spans so backtick-quoted tags like
    ``\u200B`<html>` `` in technical writing can't anchor a bare-HTML
    capture either.

    Length-preserving so source_ref offsets stay valid downstream.
    """
    if not text:
        return text
    out = list(text)
    spans = _all_fence_spans(text) + _all_inline_code_spans(text)
    for s, e in spans:
        for i in range(s, min(e, len(out))):
            if out[i] != '\n':
                out[i] = ' '
    return ''.join(out)


# ── Public entry point ────────────────────────────────────────────────

def scan_message(
    conv_id: str,
    message_text: str,
    *,
    msg_id: str = '',
    task_id: str = '',
    task: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Scan an assistant message body and persist any artifacts found.

    Args:
        conv_id: target conversation id.
        message_text: assistant content (already finalized).
        msg_id: stable assistant message id (``_msgId``) for chip rehydration.
        task_id: optional task id for audit.
        task: optional live task dict — when provided, an SSE
              ``artifact`` event is appended for each newly-persisted row.

    Returns:
        List of artifact metadata dicts (no content), one per detected
        + persisted block.  Empty list when nothing renderable was found
        or the dedupe layer collapsed everything to existing rows.
    """
    if not conv_id or not message_text:
        return []
    if not isinstance(message_text, str):
        return []

    fence_blocks = _detect_fenced_blocks(message_text)
    # Mask EVERY code region (renderable fences + non-renderable fences
    # + inline backtick spans) before bare-HTML detection.  Without this,
    # technical prose that mentions HTML tags inside ``` blocks gets
    # spliced into a fake artifact — see comment on
    # ``_mask_all_code_regions``.
    masked = _mask_all_code_regions(message_text)
    doc_blocks = _detect_bare_html_docs(masked)

    candidates = fence_blocks + doc_blocks
    if not candidates:
        return []

    created: list[dict[str, Any]] = []
    for c in candidates:
        fmt = c['format']
        if fmt not in ALLOWED_FORMATS:
            continue
        try:
            meta = create_artifact(
                conv_id=conv_id,
                content=c['content'],
                format=fmt,
                source=c['source'],
                source_ref=c['source_ref'],
                task_id=task_id or '',
                msg_id=msg_id or '',
                title=c['title'],
            )
        except ArtifactSizeError as e:
            logger.info('[Artifacts:scan] size cap rejection: %s', e)
            continue
        except Exception as e:
            logger.warning(
                '[Artifacts:scan] create_artifact failed for fmt=%s: %s',
                fmt, e, exc_info=True,
            )
            continue

        created.append(meta)
        if task is not None:
            try:
                emit_artifact_event(task, meta)
            except Exception as e:
                logger.warning(
                    '[Artifacts:scan] emit_artifact_event failed for id=%s: %s',
                    meta.get('id', '?')[:8], e, exc_info=True,
                )

    if created:
        logger.info(
            '[Artifacts:scan] conv=%s msg=%s found=%d (fence=%d doc=%d) created=%d',
            conv_id[:8], (msg_id or '')[:8],
            len(candidates), len(fence_blocks), len(doc_blocks), len(created),
        )
    return created
