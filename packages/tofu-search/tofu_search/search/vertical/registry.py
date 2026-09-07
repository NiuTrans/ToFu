"""Vertical registry — detection chain, handler tables, and dispatch.

Each vertical module exposes ``TYPE``, ``DOMAIN``, ``search(identifier, params)``
and (for the ones that participate in auto-detect) ``detect(query)``. This
module wires them into the ordered detection chain and the type/domain tables,
and provides the three public entry points used by the package façade.
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from tofu_search.log import get_logger
from tofu_search.search.vertical import (
    arxiv,
    cve,
    doi,
    github,
    hf_papers,
    ip,
    npm,
    pypi,
    semantic_scholar,
    stock,
    travel_flight,
    travel_hotel,
)

logger = get_logger(__name__)

# All vertical modules.
_MODULES = [cve, arxiv, doi, pypi, npm, github, stock, ip, hf_papers,
            semantic_scholar, travel_flight, travel_hotel]

# Type → search handler. Built from each module's TYPE + search().
_VERTICAL_HANDLERS = {m.TYPE: m.search for m in _MODULES}

# Type → module, for the capability/metadata surface.
_TYPE_MODULES = {m.TYPE: m for m in _MODULES}

# Ordered detection chain — ORDER IS SIGNIFICANT (most specific first, stock
# last). Mirrors the original detect_vertical_intent priority:
#   CVE → DOI → arXiv → PyPI → npm → GitHub → IP → HF papers → S2 → stock.
# The travel types match natural language rather than a structured identifier,
# so they sit late — after every identifier vertical, before the (deliberately
# most permissive) stock detector.
_DETECT_CHAIN = [cve, doi, arxiv, pypi, npm, github, ip, hf_papers,
                 semantic_scholar, travel_flight, travel_hotel, stock]

# Domain → list of types that belong to it. Used by the explicit-domain
# parameter path (`vertical='academic'` etc.) — order matters for fan-out.
_DOMAIN_TYPES = {
    'academic': ['arxiv', 'doi', 'hf_papers', 'semantic_scholar'],
    'code':     ['pypi', 'npm', 'github'],
    'finance':  ['stock'],
    'security': ['cve'],
    'network':  ['ip'],
    'travel':   ['flight', 'hotel'],
}

# Domain-level capability metadata. This is the SOURCE OF TRUTH a host renders
# its tool description from — adding a vertical must not require editing prose
# in a downstream repo.
DOMAIN_META = {
    'academic': {
        'purpose': 'Papers, citations and related work.',
        'when_to_use': 'Any research/paper query. Works with FREE-TEXT topics: '
                       'an arXiv ID → paper metadata + Semantic Scholar '
                       'citations; a DOI → CrossRef; "trending/daily" phrasing '
                       '→ Hugging Face Papers; otherwise free text → HF keyword '
                       'search + S2 related work in parallel.',
        'examples': ['2301.07041', 'papers related to Mamba', 'hf daily papers'],
    },
    'code': {
        'purpose': 'Package and repository metadata.',
        'when_to_use': 'Needs a package/repo IDENTIFIER, not a free-text '
                       'concept (tries PyPI + npm + GitHub for that exact '
                       'name; "best react libraries" returns nothing).',
        'examples': ['pypi:requests', 'npm:express', 'github:facebook/react'],
    },
    'finance': {
        'purpose': 'Live quote and recent price history.',
        'when_to_use': 'Needs a ticker symbol.',
        'examples': ['AAPL', '$TSLA'],
    },
    'security': {
        'purpose': 'Vulnerability details from NVD/NIST.',
        'when_to_use': 'Needs a CVE ID.',
        'examples': ['CVE-2024-1234'],
    },
    'network': {
        'purpose': 'IP geolocation and owning organisation.',
        'when_to_use': 'Needs an IP address.',
        'examples': ['8.8.8.8'],
    },
    'travel': {
        'purpose': 'Real bookable travel inventory — flights and hotels with '
                   'live prices.',
        'when_to_use': 'A trip query naming places and dates. Flights need an '
                       'origin, a destination and a date; hotels need a place '
                       'and a check-in date. Dates in the past are rejected '
                       'without a request.',
        'examples': ['8月3日北京到上海的机票', '三亚 8/3-8/5 酒店',
                     'flights from Hangzhou to Chengdu next Tuesday'],
    },
}


def detect_vertical_intent(query):
    """Detect if a query matches a vertical domain pattern.

    Returns:
        (type, identifier, params) tuple, or None.
    """
    q = query.strip()
    if not q or len(q) > 200:
        return None
    for mod in _DETECT_CHAIN:
        hit = mod.detect(q)
        if hit:
            return hit
    return None


def _type_is_available(type_name):
    """True when a vertical TYPE can serve a request right now.

    A module opts into runtime gating by exposing ``is_available()``; a module
    without one is unconditionally available (every keyless public-API
    vertical). Availability is a per-TYPE property on purpose: the travel
    domain's two types have different credential requirements, and binding the
    anonymous-capable one to the credentialed one's key would silently throw
    away half the capability.
    """
    mod = _TYPE_MODULES.get(type_name)
    if mod is None:
        return False
    checker = getattr(mod, 'is_available', None)
    if checker is None:
        return True
    try:
        return bool(checker())
    except Exception as e:
        logger.warning('[Vertical] is_available() failed for %s: %s', type_name, e)
        return False


def available_types(domain):
    """Return the currently-usable types of ``domain`` (order preserved)."""
    return [t for t in _DOMAIN_TYPES.get(domain, []) if _type_is_available(t)]


def list_domains(available_only=True):
    """Return the public list of supported vertical domains.

    With ``available_only`` (the default) a domain is listed only when at least
    ONE of its types can currently serve a request, so a host never advertises
    a domain the model is guaranteed to get nothing from.
    """
    if not available_only:
        return list(_DOMAIN_TYPES.keys())
    return [d for d in _DOMAIN_TYPES if available_types(d)]


def describe_domains(available_only=True):
    """Return structured capability metadata for each vertical domain.

    This is the machine-readable surface a host builds its LLM tool description
    from. Each entry::

        {
          'domain': 'travel',
          'purpose': '...',
          'when_to_use': '...',
          'examples': [...],
          'types': ['flight', 'hotel'],            # declared
          'available_types': ['flight'],           # usable right now
          'requires_credential': True,             # any type needs one
          'credential_env': 'ROLLINGGO_API_KEY',
          'unavailable_types': [                   # why the gap exists
              {'type': 'hotel', 'credential_env': 'ROLLINGGO_API_KEY'},
          ],
        }

    ``available_types`` is what makes a partially-available domain honest: a
    model told only that ``travel`` exists would ask it for hotels and come
    back empty-handed when no key is configured.
    """
    out = []
    for domain, types in _DOMAIN_TYPES.items():
        usable = available_types(domain)
        if available_only and not usable:
            continue
        meta = dict(DOMAIN_META.get(domain, {}))
        creds = set()
        requires = False
        unavailable = []
        for t in types:
            tmeta = getattr(_TYPE_MODULES.get(t), 'META', None) or {}
            env = tmeta.get('credential_env') or ''
            if tmeta.get('requires_credential'):
                requires = True
                if env:
                    creds.add(env)
            if t not in usable:
                unavailable.append({'type': t, 'credential_env': env})
        meta.update({
            'domain': domain,
            'types': list(types),
            'available_types': usable,
            'requires_credential': requires,
            'credential_env': sorted(creds)[0] if creds else '',
            'unavailable_types': unavailable,
        })
        out.append(meta)
    return out


def search_vertical(domain_or_type, identifier, params=None):
    """Execute a vertical lookup.

    Backwards-compatible: ``domain_or_type`` may be a low-level type name
    (``'arxiv'``, ``'cve'``, …) — the legacy auto-detect path uses this.
    For explicit domain-level fan-out (``'academic'`` etc.), call
    :func:`search_vertical_domain` instead.

    Returns:
        Dict with keys (domain, type, identifier, content, source) or None.
    """
    handler = _VERTICAL_HANDLERS.get(domain_or_type)
    if not handler:
        logger.warning('[Vertical] Unknown type: %s', domain_or_type)
        return None

    t0 = time.time()
    result = handler(identifier, params or {})
    elapsed = time.time() - t0

    if result:
        logger.info('[Vertical] %s/%s OK in %.1fs (%d chars)',
                    domain_or_type, identifier, elapsed, len(result.get('content', '')))
    else:
        logger.info('[Vertical] %s/%s no data in %.1fs', domain_or_type, identifier, elapsed)
    return result


def _structured_items_from_record(record):
    """Best-effort extraction of frontend-friendly items from a handler record.

    Returns a list of dicts with at least ``title`` + ``url`` (when known)
    suitable for rendering as compact rows in the vertical card. Falls back
    to a single ``content``-bearing item so we never drop data.
    """
    if not isinstance(record, dict):
        return []
    items = record.get('items')
    if isinstance(items, list) and items:
        return items
    # Fallback: synthesize a single item from the record header.
    head = record.get('content', '').splitlines()[:1]
    title = head[0].lstrip('# ').strip() if head else (record.get('source') or record.get('type') or 'Result')
    return [{
        'title': title,
        'snippet': '',
        'url': '',
        'type': record.get('type') or '',
    }]


def _academic_subtypes_for(query):
    """Pick which academic sub-handlers to fan out for an explicit query.

    Strategy:
      - arXiv id present → ('arxiv', id) AND semantic_scholar citations
      - DOI present → ('doi', doi)
      - 'related to / similar to / citing X' phrase → semantic_scholar
      - 'trending / daily' phrase → hf_papers
      - free-text → hf_papers (keyword) AND semantic_scholar (related)
    """
    plans = []
    arxiv_m = re.search(r'\b(\d{4}\.\d{4,5}(?:v\d+)?)\b', query)
    if arxiv_m:
        plans.append(('arxiv', arxiv_m.group(1), {}))
        plans.append(('semantic_scholar', arxiv_m.group(1), {'mode': 'citations'}))
        return plans
    doi_m = re.search(r'(10\.\d{4,}/\S+)', query)
    if doi_m:
        plans.append(('doi', doi.strip_doi(doi_m.group(1)), {}))
        return plans

    ss_intent = semantic_scholar.detect(query)
    if ss_intent:
        plans.append(ss_intent)

    hf_intent = hf_papers.detect(query)
    if hf_intent:
        plans.append(hf_intent)

    if not plans:
        # Free-text academic query: fan out HF keyword + S2 related.
        plans.append(('hf_papers', query, {'period': 'day'}))
        plans.append(('semantic_scholar', query, {'mode': 'related'}))
    return plans


def _travel_subtypes_for(domain, query):
    """Plan a travel fan-out from the query's own intent.

    Firing both sub-handlers unconditionally would send a hotel query to the
    flight endpoint (and vice versa) — two guaranteed-empty round trips on a
    live inventory API. Intent cues pick the right one; a query that mentions
    both (a trip plan) legitimately fans out to both.
    """
    from tofu_search.search.vertical import travel_slots

    wants_flight = travel_slots.looks_like_flight(query)
    wants_hotel = travel_slots.looks_like_hotel(query)
    if not wants_flight and not wants_hotel:
        wants_flight = wants_hotel = True
    plans = []
    if wants_flight:
        plans.append(('flight', query, {}))
    if wants_hotel:
        plans.append(('hotel', query, {}))
    return plans


def _simple_subtypes_for(domain, query):
    """Default plan for non-academic domains: try every type in the domain."""
    return [(t, query, {}) for t in _DOMAIN_TYPES.get(domain, [])]


_DOMAIN_PLANNERS = {
    'academic': _academic_subtypes_for,
    'travel': _travel_subtypes_for,
}


def search_vertical_domain(domain, query):
    """Run an explicit, domain-level vertical search.

    Fans out to one or more sub-handlers in parallel, merges their records,
    and returns a single dict::

        {
          'domain': 'academic',
          'sources': [{'type': 'hf_papers', 'source': 'Hugging Face Papers', ...}, ...],
          'items': [...],          # flat, frontend-renderable rows
          'content': '...',         # concatenated markdown for the LLM
        }

    or ``None`` if nothing useful came back. Safe against PDF parsing —
    every sub-handler returns JSON metadata only.
    """
    if domain not in _DOMAIN_TYPES:
        logger.warning('[Vertical] Unknown domain for explicit search: %s', domain)
        return None

    planner = _DOMAIN_PLANNERS.get(domain, _simple_subtypes_for)
    plans = planner(query) if planner is _academic_subtypes_for else planner(domain, query)
    # Never dispatch to a type that cannot serve right now (missing credential,
    # or an endpoint that already proved it needs one).
    plans = [p for p in plans if _type_is_available(p[0])]
    if not plans:
        logger.info('[Vertical] domain=%s has no available types for query=%r',
                    domain, query[:80])
        return None

    t0 = time.time()
    sources = []
    with ThreadPoolExecutor(max_workers=min(4, len(plans))) as pool:
        futs = {pool.submit(search_vertical, t, ident, params): (t, ident)
                for (t, ident, params) in plans}
        for fut in as_completed(futs):
            tname, ident = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:
                logger.warning('[Vertical] domain=%s sub=%s failed: %s', domain, tname, e)
                continue
            if rec:
                sources.append(rec)

    if not sources:
        logger.info('[Vertical] domain=%s no data in %.1fs (query=%r)',
                    domain, time.time() - t0, query[:80])
        return None

    # Merge: items per source + concatenated content for the LLM.
    items = []
    content_parts = []
    for rec in sources:
        sub_items = _structured_items_from_record(rec)
        for it in sub_items:
            it = dict(it)
            it.setdefault('type', rec.get('type', ''))
            it.setdefault('source', rec.get('source', ''))
            items.append(it)
        if rec.get('content'):
            content_parts.append(f"## {rec.get('source', rec.get('type', ''))}\n\n{rec['content']}")

    elapsed = time.time() - t0
    logger.info('[Vertical] domain=%s OK in %.1fs (%d sources, %d items)',
                domain, elapsed, len(sources), len(items))
    return {
        'domain': domain,
        'sources': [{'type': r.get('type'), 'source': r.get('source'),
                     'identifier': r.get('identifier')}
                    for r in sources],
        'items': items,
        'content': '\n\n'.join(content_parts),
    }
