"""Lightweight, self-contained citation parsers.

Two entry points, both dependency-free (stdlib only):

- :func:`parse_bibtex` — tolerant ``.bib`` parser (the main path for the
  Overleaf product surface). Handles nested braces, quoted values, and the
  ``archivePrefix``/``eprint`` arXiv convention.
- :func:`parse_references` — best-effort splitter for a freeform reference
  blob (e.g. the *References* section extracted from a PDF in paper mode);
  reliably pulls DOI/arXiv identifiers, title best-effort.

Identifier extraction reuses the SAME regexes the vertical-search package
uses (:mod:`tofu_search.search.vertical.doi` / ``arxiv``) so there is a single
source of truth for "what a DOI / arXiv id looks like".
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from tofu_search.log import get_logger
from tofu_search.search.vertical import arxiv as _arxiv_v
from tofu_search.search.vertical import doi as _doi_v

logger = get_logger(__name__)

__all__ = ['Citation', 'parse_bibtex', 'parse_references', 'extract_identifiers',
           'extract_citations_from_text']


@dataclass
class Citation:
    """A structured citation to be verified.

    Only ``raw`` is always meaningful; every other field is best-effort and
    may be empty. The verifier picks the strongest available signal
    (DOI → arXiv id → title).
    """

    key: str = ''
    title: str = ''
    authors: list = field(default_factory=list)
    year: str = ''
    doi: str = ''
    arxiv_id: str = ''
    entry_type: str = ''
    raw: str = ''

    def to_dict(self) -> dict:
        return asdict(self)


def extract_identifiers(raw: str) -> tuple[str, str]:
    """Return ``(doi, arxiv_id)`` found anywhere in *raw* (either may be '')."""
    if not raw:
        return ('', '')
    d = _doi_v.detect(raw)
    a = _arxiv_v.detect(raw)
    return (d[1] if d else '', a[1] if a else '')


# ── BibTeX ──────────────────────────────────────────────────────────────────

_ENTRY_START = re.compile(r'@(\w+)\s*\{', re.IGNORECASE)
_NON_CITE_TYPES = {'comment', 'preamble', 'string'}


def _extract_braced_body(text: str, open_idx: int) -> str | None:
    """``text[open_idx] == '{'`` → inner body up to the matching close."""
    if open_idx >= len(text) or text[open_idx] != '{':
        return None
    depth = 1
    i = open_idx + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '\\' and i + 1 < n:
            i += 2
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
        i += 1
    return None


def _read_delimited(s: str, start: int, open_ch: str, close_ch: str) -> tuple[str, int]:
    """Read a balanced ``{...}`` or quoted run starting at ``s[start]``.

    Returns ``(inner_value, index_after_closer)``.
    """
    depth = 0
    i = start
    n = len(s)
    buf = []
    while i < n:
        ch = s[i]
        if ch == '\\' and i + 1 < n:
            buf.append(s[i:i + 2])
            i += 2
            continue
        if ch == open_ch:
            depth += 1
            if depth == 1 and open_ch != close_ch:
                i += 1
                continue
        if ch == close_ch:
            depth -= 1
            if depth == 0:
                return (''.join(buf), i + 1)
        buf.append(ch)
        i += 1
    return (''.join(buf), i)


_WS_RE = re.compile(r'\s+')


def _clean_value(v: str) -> str:
    """Strip stray braces and collapse whitespace in a field value."""
    v = v.replace('{', '').replace('}', '').replace('\\&', '&')
    return _WS_RE.sub(' ', v).strip().strip(',').strip()


def _parse_fields(body: str) -> dict:
    """Parse ``name = value`` pairs from a BibTeX entry body (sans key)."""
    fields: dict[str, str] = {}
    i = 0
    n = len(body)
    while i < n:
        while i < n and body[i] in ' \t\r\n,':
            i += 1
        j = i
        while j < n and (body[j].isalnum() or body[j] in '_-'):
            j += 1
        name = body[i:j].strip().lower()
        k = j
        while k < n and body[k] in ' \t\r\n':
            k += 1
        if k >= n or body[k] != '=':
            break
        k += 1
        while k < n and body[k] in ' \t\r\n':
            k += 1
        if k >= n:
            break
        if body[k] == '{':
            val, after = _read_delimited(body, k, '{', '}')
        elif body[k] == '"':
            val, after = _read_delimited(body, k, '"', '"')
        else:
            e = k
            while e < n and body[e] != ',':
                e += 1
            val, after = body[k:e], e
        if name:
            fields[name] = _clean_value(val)
        i = after
    return fields


def _arxiv_from_fields(fields: dict) -> str:
    if fields.get('archiveprefix', '').lower() == 'arxiv' and fields.get('eprint'):
        return fields['eprint'].strip()
    ep = fields.get('eprint', '').strip()
    if ep and re.match(r'\d{4}\.\d{4,5}', ep):
        return ep
    haystack = ' '.join(fields.get(k, '') for k in ('url', 'note', 'journal', 'howpublished'))
    return extract_identifiers(haystack)[1]


def _doi_from_fields(fields: dict) -> str:
    if fields.get('doi'):
        return _doi_v.strip_doi(fields['doi'].strip())
    haystack = ' '.join(fields.get(k, '') for k in ('url', 'note'))
    return extract_identifiers(haystack)[0]


def _citation_from_fields(etype: str, key: str, fields: dict, raw: str) -> Citation:
    authors_raw = fields.get('author', '') or fields.get('editor', '')
    authors = [a.strip() for a in re.split(r'\s+and\s+', authors_raw) if a.strip()] if authors_raw else []
    year = fields.get('year', '')
    if not year:
        m = re.search(r'\b(19|20)\d{2}\b', fields.get('date', ''))
        year = m.group(0) if m else ''
    return Citation(
        key=key,
        title=fields.get('title', ''),
        authors=authors,
        year=year,
        doi=_doi_from_fields(fields),
        arxiv_id=_arxiv_from_fields(fields),
        entry_type=etype,
        raw=raw,
    )


def parse_bibtex(text: str) -> list[Citation]:
    """Parse a BibTeX string into a list of :class:`Citation`.

    Tolerant: malformed entries are skipped (logged at debug), the rest still
    parse. ``@comment``/``@preamble``/``@string`` are ignored.
    """
    if not text:
        return []
    out: list[Citation] = []
    for m in _ENTRY_START.finditer(text):
        etype = m.group(1).lower()
        if etype in _NON_CITE_TYPES:
            continue
        body = _extract_braced_body(text, m.end() - 1)
        if body is None:
            logger.debug('[verify] unbalanced bibtex entry near %d, skipped', m.start())
            continue
        key, _, rest = body.partition(',')
        try:
            cit = _citation_from_fields(etype, key.strip(), _parse_fields(rest),
                                        text[m.start():m.end() - 1] + '{' + body + '}')
        except Exception as e:
            logger.debug('[verify] failed to parse bibtex entry %r: %s', key.strip(), e)
            continue
        out.append(cit)
    return out


# ── Freeform references (best-effort) ─────────────────────────────────────────

_NUMBERED_RE = re.compile(r'(?m)^\s*\[\d+\]\s+|\s*^\s*\d+\.\s+')
_QUOTED_TITLE_RE = re.compile(r'[“"]([^”"]{6,})[”"]')


def _split_reference_blob(text: str) -> list[str]:
    """Split a *References* blob into individual entries (best-effort)."""
    if _NUMBERED_RE.search(text):
        parts = _NUMBERED_RE.split(text)
        return [p.strip() for p in parts if p and p.strip()]
    # Fall back to blank-line separation.
    parts = re.split(r'\n\s*\n', text)
    return [p.strip() for p in parts if p.strip()]


def _guess_title(entry: str) -> str:
    """Best-effort title from a freeform reference (quoted span, else '')."""
    m = _QUOTED_TITLE_RE.search(entry)
    return m.group(1).strip() if m else ''


def parse_references(text: str) -> list[Citation]:
    """Best-effort parse of a freeform reference list.

    Reliably extracts DOI/arXiv identifiers per entry; title is best-effort
    (only when quoted). Intended for PDF-extracted reference sections where
    no structured ``.bib`` is available — the verifier still gets Tier-1
    identifiers to check, and falls to ``unverifiable`` when neither id nor a
    usable title is present (never a false ``suspicious``).
    """
    if not text:
        return []
    out: list[Citation] = []
    for entry in _split_reference_blob(text):
        doi, arxiv_id = extract_identifiers(entry)
        ym = re.search(r'\b(19|20)\d{2}\b', entry)
        out.append(Citation(
            title=_guess_title(entry),
            year=ym.group(0) if ym else '',
            doi=doi,
            arxiv_id=arxiv_id,
            raw=entry,
        ))
    return out


def extract_citations_from_text(text: str) -> list[Citation]:
    """Harvest EVERY distinct DOI / arXiv identifier mentioned in *text*.

    Unlike :func:`parse_references` (which assumes a delimited *References*
    blob), this scans arbitrary prose — e.g. an LLM-generated report that
    cites ``arXiv:1706.03762`` and ``10.1234/foo`` inline throughout its body.
    Each unique identifier becomes one :class:`Citation` (id-only; title left
    blank so the verifier takes the deterministic Tier-1 path). Identifiers
    are deduped case-insensitively, preserving first-seen order. ``raw`` holds
    a short window around the first mention for evidence display.

    This intentionally extracts ONLY concrete identifiers, not prose titles:
    a model-emitted DOI/arXiv id that fails to resolve is a high-confidence
    (deterministic) hallucination signal, whereas scraping free-text titles
    would be both fragile and false-positive-prone — exactly what the
    fail-open discipline forbids.
    """
    if not text:
        return []
    out: list[Citation] = []
    seen: set[str] = set()

    def _window(start: int, end: int) -> str:
        a = max(0, start - 40)
        b = min(len(text), end + 40)
        return _WS_RE.sub(' ', text[a:b]).strip()

    for m in _doi_v._DOI_RE.finditer(text):
        ident = _doi_v.strip_doi(m.group(1))
        key = f'doi:{ident.lower()}'
        if key in seen:
            continue
        seen.add(key)
        out.append(Citation(doi=ident, raw=_window(m.start(), m.end())))

    for rx in (_arxiv_v._MODERN_RE, _arxiv_v._LEGACY_RE):
        for m in rx.finditer(text):
            ident = m.group(1)
            key = f'arxiv:{ident.lower()}'
            if key in seen:
                continue
            seen.add(key)
            out.append(Citation(arxiv_id=ident, raw=_window(m.start(), m.end())))

    return out
