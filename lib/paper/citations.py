"""lib/paper/citations.py — PID-anchored citation stack (manubot model).

Responsibility: search REAL literature (OpenAlex), fetch REAL BibTeX (Crossref
content negotiation, with a minimal OpenAlex-record synthesis as fallback),
verify existing .bib entries against OpenAlex, and mutate project .bib/.tex
files exclusively through the journaled project write tools so every change
is undoable. Citations are only ever inserted from a persistent identifier
(DOI / OpenAlex ID / arXiv ID) resolved against a public scholarly index, so a
hallucinated reference cannot enter the manuscript through this path.

Entry points
------------
- ``search_works(query, *, limit=8)``      — OpenAlex search → normalized dicts
- ``fetch_bibtex(work_or_doi)``            — PID → (bibtex_text, cite_key)
- ``collect_entries(bib_text)``            — parse_entries keys + field values
- ``verify_entries(entries)``              — OpenAlex resolution → status rows
- ``append_bib_entries(base, ...)``        — dedup .bib append via write tools
- ``insert_cite(base, ...)``               — anchor ``\\cite`` via write tools
- ``default_bib_path(base, main_file)``    — pick/create the project .bib
- ``bibliography_status(base, bib_rel)``   — is the .bib wired into the TeX?
- ``audit_document(base, ...)``            — one-pass local + remote audit
- ``rewrite_bibtex_key`` / ``validate_key`` — key helpers

Dependencies: ``lib.http_client`` (proxy-aware pooled HTTP; the only network
path), ``lib.paper.bib.parse_entries`` (key index reuse),
``lib.project_mod.write_tools`` (journaled mutations),
``lib.paper.builds.errors.BuildError`` (typed failures). No Flask, no handler
imports — this module stays pure core logic so handlers/tests drive it.

Bounds: one HTTP timeout per call (20s), search ≤25 rows, verification ≤8
entries and ≤25s total, .bib ≤4096 entries / 4 MiB, source walks ≤512 files /
≤8 MiB read, anchors 4..300 chars, keys ``^[A-Za-z0-9:_./-]{1,64}$``.
"""
from __future__ import annotations

import difflib
import os
import re
import time
import unicodedata
from functools import lru_cache
from urllib.parse import quote

from lib.http_client import http_get
from lib.log import get_logger
from lib.paper.bib import parse_entries
from lib.paper.builds.errors import BuildError

logger = get_logger(__name__)

# ── External endpoints / budgets ─────────────────────────────────────
_OPENALEX_API = 'https://api.openalex.org'
_CROSSREF_API = 'https://api.crossref.org'
_DOI_ORG = 'https://doi.org'
_MAILTO = 'tofu@localhost'
_HTTP_TIMEOUT_SECONDS = 20.0
_MAX_SEARCH_LIMIT = 25
_MAX_QUERY_CHARS = 300
_MAX_AUTHORS = 8

VERIFY_MAX_ENTRIES = 8
VERIFY_BUDGET_SECONDS = 25.0
_VERIFY_POLITE_DELAY_SECONDS = 0.15
_SIMILARITY_THRESHOLD = 0.85
_YEAR_TOLERANCE = 1

MAX_BIB_ENTRIES = 4096
MAX_BIB_BYTES = 4 * 1024 * 1024
_MAX_SOURCE_FILES = 512
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_WALK_DEPTH = 5
_MAX_TEX_BYTES = 2 * 1024 * 1024
_MAX_ANCHOR_CHARS = 300
_MIN_ANCHOR_CHARS = 4

_KEY_RE = re.compile(r'^[A-Za-z0-9:_./-]{1,64}$')
_ARXIV_RE = re.compile(r'^(?:arxiv:)?(\d{4}\.\d{4,5})(v\d+)?$', re.IGNORECASE)
_OPENALEX_ID_RE = re.compile(r'^(?:https?://openalex\.org/)?(W\d{1,12})$', re.IGNORECASE)
_BIB_HEAD = re.compile(r'@\s*([A-Za-z]+)\s*[{(]\s*([^,\s}]{1,256})')
_ENTRY_HEAD = re.compile(r'@([A-Za-z]+)\s*[{(]')
_FIELD_HEAD = re.compile(
    r'(?i)\b(doi|title|author|year)\s*=\s*([{"\']?)')
_STRIP_COMMENTS = re.compile(r'(?<!\\)%[^\n]*')
_CITE = re.compile(
    r'\\(?:cite|citep|citet|citealt|citealp|citeauthor|citeyear|citeyearpar'
    r'|parencite|textcite|autocite|footcite|smartcite|supercite|nocite)'
    r'\s*(?:\[[^\]]{0,200}\])*\s*\{([^}]{1,2000})\}')
_BIBLIOGRAPHY = re.compile(r'\\bibliography\s*\{([^}]{1,512})\}')
_ADDBIBRESOURCE = re.compile(
    r'\\addbibresource(?:\[[^\]]{0,200}\])?\s*\{([^}]{1,512})\}')

_STOP_WORDS = frozenset({
    'a', 'an', 'the', 'of', 'on', 'for', 'and', 'in', 'with', 'to', 'via',
    'by', 'from', 'at', 'is', 'are'})

__all__ = [
    'search_works', 'fetch_bibtex', 'collect_entries', 'verify_entries',
    'append_bib_entries', 'insert_cite', 'default_bib_path',
    'bibliography_status', 'audit_document', 'rewrite_bibtex_key',
    'validate_key', 'propose_key',
]


# ── Text / key helpers ───────────────────────────────────────────────

_LIGATURES = str.maketrans({
    'œ': 'oe', 'Œ': 'Oe', 'æ': 'ae', 'Æ': 'Ae', 'ß': 'ss', 'ø': 'o',
    'Ø': 'O', 'đ': 'd', 'Đ': 'D', 'ł': 'l', 'Ł': 'L', 'ð': 'd', 'þ': 'th'})


def _ascii(text: str) -> str:
    return unicodedata.normalize('NFKD', (text or '').translate(_LIGATURES)
                                 ).encode('ascii', 'ignore').decode('ascii')


def _normalize_title(text: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ',
                                      _ascii(text).lower())).strip()


def _title_similarity(a: str, b: str) -> float:
    na, nb = _normalize_title(a), _normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _surname(display_name: str) -> str:
    name = _ascii(display_name).strip()
    if not name:
        return ''
    if ',' in name:  # "Vaswani, Ashish"
        name = name.split(',', 1)[0]
    token = re.sub(r'[^A-Za-z0-9-]', '', name.split()[-1] if name.split() else '')
    return token.lower()


def validate_key(key: str) -> str:
    """Return the key or raise BuildError('invalid_key')."""
    key = str(key or '').strip()
    if not _KEY_RE.match(key):
        raise BuildError(
            'invalid_key',
            "Cite key %r is invalid; use 1-64 chars of A-Za-z0-9:_./-" % key[:80])
    return key


def propose_key(work: dict) -> str:
    """firstauthorSurnameYear + first significant title word, ascii-only."""
    authors = work.get('authors') or []
    surname = _surname(authors[0]) if authors else 'work'
    year = str(work.get('year') or '')
    word = ''
    for token in re.findall(r'[A-Za-z0-9]+', _ascii(work.get('title') or '').lower()):
        if len(token) >= 3 and token not in _STOP_WORDS:
            word = token
            break
    key = ('%s%s%s' % (surname, year, word)).strip() or 'work'
    # Guaranteed to match _KEY_RE: all parts are ascii alnum/dash.
    return key[:64]


def rewrite_bibtex_key(bibtex: str, new_key: str) -> str:
    """Swap the entry-head key, keeping the rest of the entry byte-identical."""
    match = _BIB_HEAD.search(bibtex or '')
    if not match:
        raise BuildError('bibtex_invalid', 'Fetched BibTeX has no entry head')
    return bibtex[:match.start(2)] + new_key + bibtex[match.end(2):]


def _bibtex_key_of(bibtex: str) -> str:
    match = _BIB_HEAD.search(bibtex or '')
    return match.group(2) if match else ''


# ── HTTP boundary (the only network path) ────────────────────────────

def _get(url: str, *, params: dict | None = None, headers: dict | None = None,
         allow_404: bool = False):
    """Bounded GET through the pooled proxy-aware client; typed failures only."""
    try:
        resp = http_get(url, params=params, headers=headers,
                        timeout=_HTTP_TIMEOUT_SECONDS)
    except Exception as exc:
        raise BuildError(
            'citation_backend',
            'Literature backend unreachable (%s): %s'
            % (url.split('/')[2], exc.__class__.__name__)) from exc
    if resp.status_code == 404 and allow_404:
        return None
    if resp.status_code != 200:
        raise BuildError(
            'citation_backend',
            'Literature backend %s returned HTTP %d'
            % (url.split('/')[2], resp.status_code))
    return resp


def _get_json(url: str, *, params: dict | None = None,
              allow_404: bool = False) -> dict | None:
    resp = _get(url, params=params, allow_404=allow_404)
    if resp is None:
        return None
    try:
        data = resp.json()
    except ValueError as exc:
        raise BuildError('citation_backend',
                         'Literature backend returned non-JSON payload') from exc
    return data if isinstance(data, dict) else {}


# TODO(enterprise): this per-process cache is global; per-owner caches with
# explicit TTLs should replace it when multi-tenant state lands. Bounded at
# 256 entries so memory stays flat regardless of workload.
@lru_cache(maxsize=256)
def _cached_openalex_work(path: str) -> dict | None:
    return _get_json('%s/works/%s' % (_OPENALEX_API, path),
                     params={'mailto': _MAILTO}, allow_404=True)


def _openalex_work_by_doi(doi: str) -> dict | None:
    return _cached_openalex_work('doi:%s' % quote(doi.lower(), safe=''))


def _openalex_work_by_id(openalex_id: str) -> dict | None:
    return _cached_openalex_work(quote(openalex_id, safe=''))


# ── Normalization ────────────────────────────────────────────────────

def _raw_authors(work: dict) -> list[str]:
    authors = []
    for authorship in (work.get('authorships') or [])[:_MAX_AUTHORS]:
        name = ((authorship or {}).get('author') or {}).get('display_name')
        if name:
            authors.append(str(name))
    return authors


def _raw_venue(work: dict) -> str:
    location = work.get('primary_location') or {}
    source = location.get('source') or {}
    venue = source.get('display_name')
    if not venue:
        venue = (work.get('host_venue') or {}).get('display_name')
    return str(venue or '')


def _raw_doi(work: dict) -> str:
    doi = str(work.get('doi') or '')
    prefix = 'https://doi.org/'
    return doi[len(prefix):] if doi.startswith(prefix) else doi


def _normalize_work(work: dict) -> dict:
    openalex_url = str(work.get('id') or '')
    best_oa = work.get('best_oa_location') or {}
    open_access = work.get('open_access') or {}
    normalized = {
        'openalex_id': openalex_url.rsplit('/', 1)[-1],
        'doi': _raw_doi(work),
        'title': str(work.get('display_name') or ''),
        'authors': _raw_authors(work),
        'year': work.get('publication_year') or '',
        'venue': _raw_venue(work),
        'cited_by_count': int(work.get('cited_by_count') or 0),
        'is_retracted': bool(work.get('is_retracted')),
        'oa_pdf_url': str(best_oa.get('pdf_url')
                          or open_access.get('oa_url') or ''),
    }
    normalized['bibtex_key'] = propose_key(normalized)
    return normalized


def search_works(query: str, *, limit: int = 8) -> list[dict]:
    """Search OpenAlex (250M works); returns normalized candidate dicts.

    Raises BuildError('citation_backend') on any network/backend failure and
    BuildError('citation_query') on an empty query — never raw exceptions.
    """
    query = str(query or '').strip()[:_MAX_QUERY_CHARS]
    if not query:
        raise BuildError('citation_query', 'Search query is empty')
    try:
        limit = max(1, min(int(limit), _MAX_SEARCH_LIMIT))
    except (TypeError, ValueError):
        limit = 8
    data = _get_json('%s/works' % _OPENALEX_API, params={
        'search': query, 'per-page': limit, 'mailto': _MAILTO})
    results = (data or {}).get('results') or []
    works = []
    for raw in results[:limit]:
        if isinstance(raw, dict):
            works.append(_normalize_work(raw))
    return works


# ── BibTeX fetch (Crossref negotiation → OpenAlex synthesis) ─────────

def _identifier_kind(identifier: str) -> tuple[str, str]:
    """Classify one PID string → (kind, value); kinds: doi/openalex/arxiv."""
    ident = str(identifier or '').strip()
    for prefix in ('https://doi.org/', 'http://doi.org/', 'doi:'):
        if ident.lower().startswith(prefix):
            ident = ident[len(prefix):]
            break
    match = _OPENALEX_ID_RE.match(ident)
    if match:
        return 'openalex', match.group(1).upper()
    match = _ARXIV_RE.match(ident)
    if match:
        # DataCite registers one DOI per arXiv ID; OpenAlex indexes it.
        return 'doi', '10.48550/arXiv.%s' % match.group(1)
    if '/' in ident and re.match(r'^10\.\d{4,9}/', ident):
        return 'doi', ident
    raise BuildError(
        'citation_query',
        'Cannot resolve identifier %r: pass a DOI, OpenAlex ID (W…), '
        'or arXiv ID (NNNN.NNNNN)' % ident[:120])


def _crossref_bibtex(doi: str) -> str | None:
    resp = _get('%s/works/%s/transform/application/x-bibtex'
                % (_CROSSREF_API, quote(doi, safe='')),
                allow_404=True)
    if resp is None:
        return None
    text = (resp.text or '').strip()
    return text if _BIB_HEAD.search(text) else None


def _doi_org_bibtex(doi: str) -> str | None:
    resp = _get('%s/%s' % (_DOI_ORG, quote(doi, safe='')),
                headers={'Accept': 'application/x-bibtex'}, allow_404=True)
    if resp is None:
        return None
    text = (resp.text or '').strip()
    return text if _BIB_HEAD.search(text) else None


def _escape_field(value: str) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).replace('{', '').replace(
        '}', '').strip()


def _synthesize_bibtex(work: dict, raw_type: str = '') -> tuple[str, str]:
    """Minimal entry from a normalized OpenAlex record (article/book/misc)."""
    raw_type = str(raw_type or '').lower()
    if raw_type in ('book', 'monograph', 'reference-book'):
        entry_type = 'book'
    elif raw_type in ('article', 'preprint', 'proceedings-article',
                      'journal-article', 'book-chapter', ''):
        entry_type = 'article'
    else:
        entry_type = 'misc'
    fields = []
    if work.get('authors'):
        fields.append(('author', ' and '.join(
            _escape_field(name) for name in work['authors'])))
    if work.get('title'):
        fields.append(('title', _escape_field(work['title'])))
    if work.get('year'):
        fields.append(('year', str(work['year'])))
    if work.get('venue'):
        fields.append(('booktitle' if entry_type == 'book' else 'journal',
                       _escape_field(work['venue'])))
    if work.get('doi'):
        fields.append(('doi', _escape_field(work['doi'])))
    if work.get('oa_pdf_url'):
        fields.append(('url', _escape_field(work['oa_pdf_url'])))
    key = propose_key(work)
    body = ',\n'.join('  %s = {%s}' % (name, value) for name, value in fields)
    return '@%s{%s,\n%s\n}' % (entry_type, key, body), key


def fetch_bibtex(work_or_doi) -> tuple[str, str]:
    """Resolve one PID to (bibtex_text, cite_key).

    Accepts a DOI string, OpenAlex ID, arXiv ID, or a normalized work dict
    from ``search_works``. Crossref content negotiation is tried first (then
    doi.org negotiation for DataCite DOIs); if neither yields BibTeX, a
    minimal entry is synthesized from the OpenAlex record. Raises BuildError
    ('citation_backend'/'citation_not_found'/'citation_query') — never raw
    exceptions.
    """
    record = None
    raw_type = ''
    if isinstance(work_or_doi, dict):
        record = dict(work_or_doi)
        doi = str(record.get('doi') or '').strip()
        if not doi and record.get('openalex_id'):
            raw = _openalex_work_by_id(str(record['openalex_id']))
            if raw:
                raw_type = str(raw.get('type') or '')
                record = _normalize_work(raw)
                doi = record.get('doi') or ''
        if not doi:
            return _synthesize_bibtex(record, raw_type)
    else:
        kind, value = _identifier_kind(work_or_doi)
        if kind == 'openalex':
            raw = _openalex_work_by_id(value)
            if raw is None:
                raise BuildError('citation_not_found',
                                 'OpenAlex has no work %s' % value)
            raw_type = str(raw.get('type') or '')
            record = _normalize_work(raw)
            doi = record.get('doi') or ''
        else:
            doi = value
    if doi:
        backend_error = None
        for fetcher in (_crossref_bibtex, _doi_org_bibtex):
            try:
                text = fetcher(doi)
            except BuildError as exc:
                backend_error = exc
                continue
            if text:
                return text, _bibtex_key_of(text)
        if record is None:
            raw = _openalex_work_by_doi(doi)
            if raw is not None:
                raw_type = str(raw.get('type') or '')
                record = _normalize_work(raw)
        if record is None:
            if backend_error is not None:
                raise backend_error
            raise BuildError('citation_not_found',
                             'No BibTeX source knows DOI %s' % doi[:160])
        return _synthesize_bibtex(record, raw_type)
    raise BuildError('citation_not_found',
                     'Work has no DOI and no usable OpenAlex record')


# ── .bib field collection (reuses parse_entries for the key index) ───

def _balanced_end(text: str, start: int, cap: int, opener: str) -> int:
    """Index just past the closer matching ``text[start - 1] == opener``."""
    closer = '}' if opener == '{' else ')'
    depth = 1
    i = start
    while i < len(text) and i < cap:
        ch = text[i]
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return min(len(text), cap)


def _field_value(body: str, match: re.Match) -> str:
    i = match.end()  # body[i - 1] is the opener when group(2) matched
    if i > len(body):
        return ''
    token = match.group(2)
    if token == '{':
        end = _balanced_end(body, i, min(len(body), i + 4096), '{')
        return body[i:max(i, end - 1)][:2000]
    if token == '"':
        end = body.find('"', i)
        return body[i:end if end >= 0 else i][:2000]
    end = i
    while end < len(body) and body[end] not in ',\n}':
        end += 1
    return body[i:end].strip()[:64]


def collect_entries(bib_text: str, *, source_file: str = '') -> list[dict]:
    """Per-entry dicts {key, entry_type, line, doi, title, author, year}.

    The key/line index comes from ``lib.paper.bib.parse_entries``; field
    values are extracted from each entry's brace-balanced body. Scanning is
    bounded and never raises on malformed input.
    """
    bib_text = bib_text or ''
    heads, _duplicates = parse_entries(bib_text)
    wanted = {entry.key for entry in heads}
    found: dict[str, dict] = {}
    for match in _ENTRY_HEAD.finditer(bib_text or ''):
        entry_type = match.group(1).lower()
        if entry_type in ('comment', 'preamble', 'string'):
            continue
        comma = bib_text.find(',', match.end(), match.end() + 300)
        if comma < 0:
            continue
        key = bib_text[match.end():comma].strip()
        if key not in wanted or key in found:
            continue
        end = _balanced_end(bib_text, match.end(),
                            min(len(bib_text), match.end() + 65536),
                            bib_text[match.end() - 1])
        body = bib_text[comma + 1:end]
        fields: dict[str, str] = {}
        for field in _FIELD_HEAD.finditer(body):
            name = field.group(1).lower()
            if name not in fields:
                fields[name] = _field_value(body, field)
        authors = [part.strip() for part in
                   re.split(r'\s+and\s+', fields.get('author', '')) if part.strip()]
        found[key] = {
            'key': key,
            'entry_type': entry_type,
            'line': next((e.line for e in heads if e.key == key), 0),
            'source_file': source_file,
            'doi': fields.get('doi', '').strip(),
            'title': fields.get('title', '').strip(),
            'author': authors[0] if authors else '',
            'year': fields.get('year', '').strip(),
        }
        if len(found) >= len(wanted):
            break
    return [found[key] for key in (entry.key for entry in heads) if key in found]


# ── Verification against OpenAlex ────────────────────────────────────

def _classify(entry: dict, work: dict | None, similarity: float) -> dict:
    evidence = {'matched_title': '', 'matched_year': '', 'matched_venue': '',
                'doi': entry.get('doi') or '', 'openalex_id': '',
                'similarity': round(similarity, 3)}
    if work is None:
        return {'key': entry.get('key') or '', 'status': 'not_found',
                'evidence': evidence}
    normalized = _normalize_work(work)
    evidence.update({
        'matched_title': normalized['title'][:300],
        'matched_year': normalized['year'],
        'matched_venue': normalized['venue'][:200],
        'doi': normalized['doi'] or (entry.get('doi') or ''),
        'openalex_id': normalized['openalex_id'],
        'similarity': round(similarity, 3)})
    if normalized['is_retracted']:
        status = 'retracted'
    else:
        title_ok = (not entry.get('title')
                    or similarity >= _SIMILARITY_THRESHOLD)
        year_ok = True
        try:
            if entry.get('year') and normalized['year']:
                year_ok = abs(int(str(entry['year'])[:4])
                              - int(normalized['year'])) <= _YEAR_TOLERANCE
        except (TypeError, ValueError):
            year_ok = False
        author_ok = (not entry.get('author') or not normalized['authors']
                     or _surname(entry['author'])
                     == _surname(normalized['authors'][0]))
        status = 'verified' if (title_ok and year_ok and author_ok) else 'mismatch'
    return {'key': entry.get('key') or '', 'status': status,
            'evidence': evidence}


def _verify_one(entry: dict) -> dict:
    doi = str(entry.get('doi') or '').strip()
    if doi:
        work = _openalex_work_by_doi(doi.lower())
        similarity = 0.0
        if work and entry.get('title'):
            similarity = _title_similarity(
                str(entry['title']), str(work.get('display_name') or ''))
        return _classify(entry, work, similarity)
    title = str(entry.get('title') or '').strip()
    if not title:
        return {'key': entry.get('key') or '', 'status': 'skipped',
                'evidence': {'matched_title': '', 'matched_year': '',
                             'matched_venue': '', 'doi': '', 'openalex_id': '',
                             'similarity': 0.0},
                'note': 'entry has neither DOI nor title'}
    data = _get_json('%s/works' % _OPENALEX_API, params={
        'search': title[:_MAX_QUERY_CHARS], 'per-page': 5, 'mailto': _MAILTO})
    best, best_similarity = None, 0.0
    for raw in ((data or {}).get('results') or [])[:5]:
        if not isinstance(raw, dict):
            continue
        similarity = _title_similarity(title, str(raw.get('display_name') or ''))
        if similarity > best_similarity:
            best, best_similarity = raw, similarity
    if best is None or best_similarity < _SIMILARITY_THRESHOLD:
        return _classify(entry, None, best_similarity)
    return _classify(entry, best, best_similarity)


def verify_entries(entries: list[dict], *,
                   max_entries: int = VERIFY_MAX_ENTRIES,
                   budget_seconds: float = VERIFY_BUDGET_SECONDS) -> list[dict]:
    """Resolve each parsed entry against OpenAlex; never raises.

    Per entry: {key, status: verified|mismatch|not_found|retracted|skipped,
    evidence: {...}}. DOI is preferred; otherwise a title search with a 0.85
    similarity floor. Sequential and polite; entries past ``max_entries`` or
    the time budget are reported as 'skipped', as is any entry when the
    backend is unreachable.
    """
    rows: list[dict] = []
    deadline = time.monotonic() + max(1.0, float(budget_seconds))
    for index, entry in enumerate(list(entries or [])):
        key = str((entry or {}).get('key') or '')
        if index >= max_entries:
            rows.append({'key': key, 'status': 'skipped', 'evidence': {},
                         'note': 'beyond the %d-entry verification budget'
                         % max_entries})
            continue
        if time.monotonic() >= deadline:
            rows.append({'key': key, 'status': 'skipped', 'evidence': {},
                         'note': 'verification time budget exhausted'})
            continue
        try:
            rows.append(_verify_one(entry or {}))
        except Exception as exc:  # backend down/odd payload — never crash
            logger.info('[citations] verify skipped for %s: %s', key, exc)
            rows.append({'key': key, 'status': 'skipped', 'evidence': {},
                         'note': 'backend unavailable: %s'
                         % exc.__class__.__name__})
        if index + 1 < min(len(entries), max_entries):
            time.sleep(_VERIFY_POLITE_DELAY_SECONDS)
    return rows


# ── Bounded source walking / reading ─────────────────────────────────

def _safe_join(base: str, rel_path: str) -> str:
    rel = str(rel_path or '').replace('\\', '/').lstrip('/')
    if not rel or rel.startswith('../') or '/../' in rel or rel == '..':
        raise BuildError('source_path',
                         'Path %r escapes the document root' % rel_path[:160])
    target = os.path.realpath(os.path.join(base, rel))
    root = os.path.realpath(base)
    if target != root and not target.startswith(root + os.sep):
        raise BuildError('source_path',
                         'Path %r escapes the document root' % rel_path[:160])
    return target


def _read_text(base: str, rel_path: str, cap: int) -> str:
    target = _safe_join(base, rel_path)
    with open(target, 'rb') as handle:
        data = handle.read(cap + 1)
    if len(data) > cap:
        raise BuildError('source_limit', '%s exceeds the %d-byte read budget'
                         % (rel_path, cap))
    return data.decode('utf-8', errors='replace')


def _walk_sources(base: str, suffixes: tuple[str, ...]) -> list[str]:
    """Relative source paths under base; skips hidden/vendor dirs, bounded."""
    root = os.path.realpath(base)
    found: list[str] = []
    try:
        for current, dirs, files in os.walk(root):
            rel_dir = os.path.relpath(current, root)
            depth = 0 if rel_dir == '.' else rel_dir.count(os.sep) + 1
            dirs[:] = sorted(
                d for d in dirs
                if not d.startswith('.')
                and d not in ('node_modules', '__pycache__', 'venv', '.venv')
                and depth < _MAX_WALK_DEPTH)
            for name in sorted(files):
                if name.lower().endswith(suffixes):
                    rel = name if rel_dir == '.' else '%s/%s' % (
                        rel_dir.replace(os.sep, '/'), name)
                    found.append(rel)
                    if len(found) >= _MAX_SOURCE_FILES:
                        return found
    except OSError as exc:
        raise BuildError('source_path', 'Cannot walk document root: %s' % exc)
    return found


# ── Journaled mutations (write tools only) ───────────────────────────

def append_bib_entries(base: str, rel_bib: str, entries: list[tuple[str, str]],
                       *, conv_id=None, task_id=None) -> dict:
    """Append deduplicated entries to a .bib under the document root.

    ``entries`` is a list of (bibtex_text, cite_key). Deduplication is by key
    AND by DOI. Mutations go through ``tool_write_file(append=True)`` so the
    append is journaled/undoable. Caps: 4096 entries / 4 MiB per .bib.
    """
    from lib.project_mod.write_tools import tool_write_file
    rel = str(rel_bib or '').replace('\\', '/').lstrip('/')
    if not rel.lower().endswith('.bib'):
        raise BuildError('source_path',
                         'Bibliography target %r is not a .bib file' % rel[:160])
    target = _safe_join(base, rel)
    existing_text = ''
    existed = os.path.isfile(target)
    if existed:
        existing_text = _read_text(base, rel, MAX_BIB_BYTES)
    existing_keys = {entry.key for entry in parse_entries(existing_text)[0]}
    existing_dois = {row['doi'].lower() for row in collect_entries(existing_text)
                     if row.get('doi')}
    added, skipped, blocks = [], [], []
    total_entries = len(existing_keys)
    for bibtex, key in list(entries or []):
        validate_key(key)
        doi = ''
        for field in _FIELD_HEAD.finditer(bibtex or ''):
            if field.group(1).lower() == 'doi':
                doi = _field_value(bibtex, field).strip().lower()
                break
        if key in existing_keys or (doi and doi in existing_dois):
            skipped.append(key)
            continue
        total_entries += 1
        if total_entries > MAX_BIB_ENTRIES:
            raise BuildError('bib_limit',
                             'Appending would exceed the %d-entry .bib cap'
                             % MAX_BIB_ENTRIES)
        block = (bibtex or '').strip()
        if not _BIB_HEAD.search(block):
            raise BuildError('bibtex_invalid',
                             'Entry %s has no parseable BibTeX head' % key)
        blocks.append(block)
        existing_keys.add(key)
        if doi:
            existing_dois.add(doi)
        added.append(key)
    if not added:
        return {'ok': True, 'path': rel, 'added': [], 'skipped': skipped,
                'created': False}
    prefix = ''
    if existing_text and not existing_text.endswith('\n'):
        prefix = '\n'
    addition = prefix + '\n' + '\n\n'.join(blocks) + '\n'
    if len(existing_text.encode('utf-8')) + len(addition.encode('utf-8')) > MAX_BIB_BYTES:
        raise BuildError('bib_limit',
                         'Appending would exceed the 4 MiB .bib cap')
    result = tool_write_file(
        base, rel, addition, append=True, conv_id=conv_id, task_id=task_id,
        description='Append %d verified BibTeX entr%s'
        % (len(added), 'y' if len(added) == 1 else 'ies'))
    if not result.get('ok'):
        raise BuildError('write_failed', 'Journal write to %s failed: %s'
                         % (rel, str(result.get('error'))[:300]))
    return {'ok': True, 'path': rel, 'added': added, 'skipped': skipped,
            'created': not existed}


def insert_cite(base: str, tex_path: str, anchor: str, key: str,
                *, position: str = 'after', conv_id=None, task_id=None) -> dict:
    """Insert ``\\cite{key}`` next to a unique anchor in a .tex file.

    The edit goes through ``tool_apply_diff`` (journaled/undoable). The
    anchor must occur EXACTLY once in the file; an already-present
    ``\\cite{key}`` right at the anchor makes the call a no-op.
    """
    from lib.project_mod.write_tools import tool_apply_diff
    validate_key(key)
    rel = str(tex_path or '').replace('\\', '/').lstrip('/')
    if not rel.lower().endswith(('.tex', '.ltx')):
        raise BuildError('source_path',
                         'Citation target %r is not a .tex file' % rel[:160])
    anchor = str(anchor or '')
    if not (_MIN_ANCHOR_CHARS <= len(anchor) <= _MAX_ANCHOR_CHARS):
        raise BuildError('anchor_not_found',
                         'Anchor must be %d-%d characters'
                         % (_MIN_ANCHOR_CHARS, _MAX_ANCHOR_CHARS))
    text = _read_text(base, rel, _MAX_TEX_BYTES)
    occurrences = text.count(anchor)
    if occurrences == 0:
        raise BuildError('anchor_not_found',
                         'Anchor text does not appear in %s' % rel)
    if occurrences > 1:
        raise BuildError('anchor_ambiguous',
                         'Anchor text appears %d times in %s; pass a longer, '
                         'unique anchor' % (occurrences, rel))
    position = 'before' if str(position).lower() == 'before' else 'after'
    at = text.find(anchor)
    neighborhood = (text[at:at + len(anchor) + 160] if position == 'after'
                    else text[max(0, at - 160):at + len(anchor)])
    if re.search(r'\\cite\w*\s*(?:\[[^\]]*\])*\s*\{[^}]*\b%s\b'
                 % re.escape(key), neighborhood):
        return {'ok': True, 'path': rel, 'key': key, 'inserted': False,
                'reason': 'already cited at the anchor'}
    citation = '\\cite{%s}' % key
    replace = anchor + citation if position == 'after' else citation + anchor
    result = tool_apply_diff(
        base, rel, anchor, replace, conv_id=conv_id, task_id=task_id,
        description='Insert \\cite{%s} after anchor' % key)
    if not result.get('ok'):
        raise BuildError('write_failed', 'Journal edit to %s failed: %s'
                         % (rel, str(result.get('error'))[:300]))
    return {'ok': True, 'path': rel, 'key': key, 'inserted': True}


# ── Document-level helpers ───────────────────────────────────────────

def default_bib_path(base: str, main_file: str = '') -> str:
    """First existing .bib (preferring the main file's directory), else refs.bib."""
    bibs = _walk_sources(base, ('.bib',))
    main_dir = ''
    if main_file and '/' in main_file:
        main_dir = main_file.rsplit('/', 1)[0] + '/'
    for rel in bibs:
        if main_dir and rel.startswith(main_dir):
            return rel
    if bibs:
        return bibs[0]
    return (main_dir or '') + 'refs.bib'


def bibliography_status(base: str, bib_rel: str) -> dict:
    """Whether any .tex source wires this .bib in (\\bibliography/\\addbibresource)."""
    stem = bib_rel[:-4] if bib_rel.lower().endswith('.bib') else bib_rel
    basename = stem.rsplit('/', 1)[-1]
    filename = bib_rel.rsplit('/', 1)[-1]
    for tex_rel in _walk_sources(base, ('.tex', '.ltx'))[:64]:
        try:
            text = _STRIP_COMMENTS.sub('', _read_text(base, tex_rel, _MAX_TEX_BYTES))
        except (BuildError, OSError):
            continue
        for match in _BIBLIOGRAPHY.finditer(text):
            stems = [part.strip() for part in match.group(1).split(',')]
            if stem in stems or basename in stems:
                return {'referenced': True, 'via': 'bibliography',
                        'file': tex_rel, 'bib': bib_rel}
        for match in _ADDBIBRESOURCE.finditer(text):
            names = [part.strip() for part in match.group(1).split(',')]
            if bib_rel in names or filename in names:
                return {'referenced': True, 'via': 'addbibresource',
                            'file': tex_rel, 'bib': bib_rel}
    return {'referenced': False, 'via': None, 'file': '', 'bib': bib_rel}


def audit_document(base: str, *, main_file: str = '',
                   max_entries: int = VERIFY_MAX_ENTRIES) -> dict:
    """One-pass audit: OpenAlex verification + local cross-reference checks.

    Returns {bib_files, tex_files, entries_total, verifications, duplicates,
    undefined, unused, bibliography}. Network failures degrade verifications
    to 'skipped' rows; local findings are always reported.
    """
    tex_files = _walk_sources(base, ('.tex', '.ltx'))
    bib_files = _walk_sources(base, ('.bib',))
    entries: list[dict] = []
    duplicates: list[str] = []
    bib_keys: set[str] = set()
    total_bytes = 0
    for bib_rel in bib_files:
        try:
            text = _read_text(base, bib_rel, MAX_BIB_BYTES)
        except (BuildError, OSError):
            continue
        total_bytes += len(text.encode('utf-8'))
        if total_bytes > _MAX_SOURCE_BYTES:
            break
        heads, repeated = parse_entries(text)
        for entry in heads:
            if entry.key in bib_keys and entry.key not in duplicates:
                duplicates.append(entry.key)
            bib_keys.add(entry.key)
        for dup in repeated:
            if dup not in duplicates:
                duplicates.append(dup)
        entries.extend(collect_entries(text, source_file=bib_rel))
    cites: set[str] = set()
    for tex_rel in tex_files:
        try:
            text = _STRIP_COMMENTS.sub('', _read_text(base, tex_rel, _MAX_TEX_BYTES))
        except (BuildError, OSError):
            continue
        for match in _CITE.finditer(text):
            for key in match.group(1).split(','):
                key = key.strip()
                if key and len(key) <= 256:
                    cites.add(key)
    undefined = sorted(key for key in cites
                       if key not in bib_keys and key != '*')
    unused = [] if '*' in cites else sorted(bib_keys - cites)
    verifications = verify_entries(entries, max_entries=max_entries)
    bibliography = (bibliography_status(base, bib_files[0]) if bib_files
                    else {'referenced': False, 'via': None, 'file': '',
                          'bib': ''})
    return {'bib_files': bib_files, 'tex_files': tex_files,
            'entries_total': len(entries), 'verifications': verifications,
            'duplicates': sorted(duplicates), 'undefined': undefined,
            'unused': unused, 'bibliography': bibliography}
