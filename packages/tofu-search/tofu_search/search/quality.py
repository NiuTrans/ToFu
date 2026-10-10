"""Search observation quality, separate from fetch admission and ranking.

Entry points enforce explicit site constraints and annotate lexical evidence.
No network, model, mutable global state or host dependencies are used. A lexical
miss is uncertainty, never a semantic rejection or proof of nonexistence.
"""

import re
from urllib.parse import urlsplit

from tofu_search.search.prefetch_gate import query_terms, result_terms

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


def annotate_relevance(result, query):
    # Site operators are constraints, not words proving subject relevance.
    terms = query_terms(_SITE.sub('', query or ''))
    overlap = bool(terms & result_terms(result)) if terms else None
    result['relevance'] = ('lexical_match' if overlap else 'unverified')
    return result
