"""Search observation quality, separate from fetch admission and ranking.

Entry points enforce explicit site constraints and annotate lexical evidence.
No network, model, mutable global state or host dependencies are used. A lexical
miss is uncertainty, never a semantic rejection or proof of nonexistence.
"""
import re
from urllib.parse import urlsplit

from tofu_search.lexical import matching_text
from tofu_search.search.prefetch_gate import query_subject, query_terms
from tofu_search.search.prefetch_gate import result_terms as result_terms
from tofu_search.search.rerank import _tokenize

_SITE = re.compile(r"(?<![\w:])(?P<excluded>-)?site:(?P<scope>[^\s()]+)", re.I)


def site_scopes(query):
    """Only unambiguous standalone site operators constrain result URLs.

    Positive scopes form a union (supports OR); exclusions always apply.
    Query groups with different site/term branches remain engine-owned.
    """
    text = query or ''
    matches = [m for m in _SITE.finditer(text) if text[:m.start()].count('"') % 2 == 0]
    if re.search(r'\bOR\b', text):
        # A site operator outside a parenthesized disjunction is global.
        prefix = text.split('(', 1)[0] if '(' in text else ''
        global_scope = bool(_SITE.search(prefix))
        branches = re.split(r'\bOR\b', text)
        if not global_scope and any(not _SITE.search(branch) for branch in branches):
            return []  # ambiguous branch-local scopes remain engine-owned
    return [(bool(m['excluded']), m['scope'].strip('"\'')) for m in matches]


def matches_site_constraints(url, scopes):
    if not scopes:
        return True
    try:
        target = urlsplit(url)
        host = (target.hostname or '').lower().rstrip('.')
        positive, negative = [], []
        for excluded, scope in scopes:
            parsed = urlsplit(scope if '://' in scope else 'https://' + scope)
            domain = (parsed.hostname or '').lower().rstrip('.')
            if not domain:
                continue
            matches = (host == domain or host.endswith('.' + domain))
            path = parsed.path.rstrip('/')
            if path:
                matches = matches and (target.path == path or target.path.startswith(path + '/'))
            (negative if excluded else positive).append(matches)
        return not any(negative) and (not positive or any(positive))
    except ValueError:
        return False


_GENERIC_TERMS = frozenset({
    'github', 'search', 'results', 'result', 'server', 'summary', 'documentation',
    'docs', 'homepage', 'website', 'official', 'page', 'mcp',
})
_LINKS_HEADER = '--- Page Links ---'
_IDENTIFIER = re.compile(r'\b(?:[A-Za-z0-9]+[-_][A-Za-z0-9_-]+|'
                         r'[A-Za-z]+\d[A-Za-z0-9]*|[A-Z]{2,8})\b')


def fetched_body(result):
    """Full fetched BODY, excluding the extractor's appended link inventory.

    No passage/rerank truncation: evidence late in a document still counts.
    This cannot identify arbitrary footer prose; only explicit link inventories
    have a reliable extraction boundary.
    """
    return (result.get('full_content') or '').split(_LINKS_HEADER, 1)[0].strip()


def _normalized(text):
    return matching_text(text)


def _contains_subject(subject, text):
    normalized = _normalized(subject)
    return bool(normalized and f' {normalized} ' in f' {_normalized(text)} ')


def _quoted_subjects(query):
    return [m.group()[1:-1] for m in re.finditer(
        r'(?:[+-]?[A-Za-z]+:|[+-])?"[^"]*"', query or '')
        if m.group().startswith('"')]


def _subject_anchors(query):
    # Preserve quoted phrases before syntax removal; operator operands never
    # become identifiers. Hyphen/underscore/space variants share normalization.
    phrases = _quoted_subjects(query)
    identifiers = _IDENTIFIER.findall(query_subject(query))
    return [s for s in phrases + identifiers if query_terms('"' + s + '"')]


def _classify_text(query, text):
    terms = query_terms(query)
    matched = terms & set(_tokenize(text))
    subjects = _subject_anchors(query)
    anchors = [s for s in subjects if _contains_subject(s, text)]
    identifier_subjects = [s for s in subjects if any(c.isdigit() for c in s)]
    identifier_present = not identifier_subjects or any(s in anchors for s in identifier_subjects)
    quoted = set(_quoted_subjects(query))
    # Bare all-caps generic acronyms (API, CPU, ...) cannot independently
    # establish a substantive multi-term subject. Concrete model IDs, quoted
    # subjects and composite identifiers remain useful anchors.
    subject_present = any(query_terms(s) - _GENERIC_TERMS and (
        s in quoted or any(c.isdigit() for c in s) or '-' in s or '_' in s
        or len(terms) <= 2) for s in anchors)
    coverage = len(matched) / len(terms) if terms else 0.0
    # Short subjects need no universal exact-AND. Longer queries need either
    # concrete subject evidence or multiple terms with meaningful coverage.
    strong = identifier_present and (subject_present or bool(matched and (
        (len(terms) <= 2 and not _quoted_subjects(query)) or
        (len(matched) >= 2 and coverage >= 0.4 and
         (len(terms) < 4 or matched - _GENERIC_TERMS)))))
    return {
        'status': 'lexical_match' if strong else 'weak_match' if matched else 'unverified',
        'matched_terms': sorted(matched),
        'query_terms': sorted(terms),
        'coverage': round(coverage, 4),
        'matched_subjects': sorted(set(anchors)),
    }


def _classify_source(query, text):
    evidence = _classify_text(query, text)
    # Simple disjunctions are alternatives, not an exact-AND requirement.
    # Never split OR inside quotes or parenthesized mixed-scope expressions.
    if '(' not in (query or '') and ')' not in (query or ''):
        branches = re.split(r'\s+OR\s+(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)', query or '')
        if len(branches) > 1:
            statuses = [_classify_text(branch, text)['status'] for branch in branches]
            if 'lexical_match' in statuses:
                evidence['status'] = 'lexical_match'
    return evidence


def _script_families(text):
    families = set()
    for pattern, family in (
        (r'[A-Za-z]', 'latin'),
        (r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]', 'east_asian'),
        (r'[\u0400-\u052f]', 'cyrillic'),
        (r'[\u0600-\u06ff]', 'arabic'),
        (r'[\u0900-\u097f]', 'devanagari'),
    ):
        if re.search(pattern, text or ''):
            families.add(family)
    return families


_COLLECTION_ACTIONS = frozenset({'watch', 'browse', 'explore', 'discover', 'search'})
_COLLECTION_KINDS = frozenset({'reels', 'videos', 'posts', 'photos', 'images', 'results', 'clips'})
_COLLECTION_WORDS = _COLLECTION_ACTIONS | _COLLECTION_KINDS | frozenset({
    'a', 'an', 'and', 'about', 'around', 'for', 'from', 'in', 'of', 'on', 'the',
    'to', 'with', 'people', 'world', 'related', 'popular', 'trending', 'short', 'more',
})


def _query_only_collection(result, query):
    """Recognize query repetition plus English collection boilerplate only.

    Require complete subject coverage in both fields and a collection action
    and kind beyond the query itself. Keep raw normalized words for the novelty
    check: stop-word removal would erase new negations or spelled-out numbers.
    Any additional name, value, qualification or unsupported wording fails open.
    """
    terms = query_terms(query)
    if len(terms) < 3:
        return False
    title = str(result.get('title') or '')
    snippet = str(result.get('snippet') or '')
    if not (terms <= set(_tokenize(title)) and terms <= set(_tokenize(snippet))):
        return False
    subject_words = set(_normalized(query_subject(query)).split()) | terms
    title_extra = set(_normalized(title).split()) - subject_words
    snippet_extra = set(_normalized(snippet).split()) - subject_words
    return bool(snippet_extra & _COLLECTION_ACTIONS and snippet_extra & _COLLECTION_KINDS
                and not (title_extra | snippet_extra) - _COLLECTION_WORDS)


def classify_relevance(result, query):
    """Return explainable lexical observations, never semantic judgments.

    SERP and body evidence are independent; Page Links are diagnostic only.
    A fetch success alone cannot upgrade relevance. A miss (including a
    cross-language miss) remains unverified, not rejected. URLs do not count
    as subject evidence. The returned mapping is JSON-serializable.
    """
    serp = _classify_source(query, f"{result.get('title') or ''}\n{result.get('snippet') or ''}")
    if _query_only_collection(result, query):
        serp['status'] = 'unverified'
        serp['query_echo_only'] = True
    body = _classify_source(query, fetched_body(result))
    content = result.get('full_content') or ''
    links = content.split(_LINKS_HEADER, 1)[1] if _LINKS_HEADER in content else ''
    link_evidence = _classify_source(query, links)
    sources = {'serp': serp, 'body': body}
    status = next((label for label in ('lexical_match', 'weak_match', 'unverified')
                   if any(e['status'] == label for e in sources.values())))
    terms = query_terms(query)
    matched = set(serp['matched_terms']) | set(body['matched_terms'])
    return {
        'status': status,
        'query_terms': sorted(terms),
        'matched_terms': sorted(matched),
        'coverage': round(len(matched) / len(terms), 4) if terms else 0.0,
        'sources': sources,
        'links': link_evidence,
        'body_present': bool(fetched_body(result)),

        # Different-script text is explicitly uncertain, not automatically
        # relevant. Keep it accessible without penalizing a partial overlap.
        'cross_language': bool(_script_families(fetched_body(result))
                               - _script_families(query_subject(query))),
    }


def annotate_relevance(result, query):
    """Mutate and return the same result; safe to repeat after fetching.

    ``relevance`` retains its string API, adding ``weak_match`` for partial
    evidence. ``relevance_evidence`` explains concrete terms and source coverage.
    """
    evidence = classify_relevance(result, query)
    result['relevance'] = evidence['status']
    result['relevance_evidence'] = evidence
    return result
