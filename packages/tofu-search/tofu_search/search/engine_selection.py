"""Explicit engine selection and native source authority for one search call.

Entry: select_engines. Provider objects and engine callables are borrowed from
the request-bound orchestrator. This owner validates selection before any worker
pool starts; it never broadens an explicit selection to generic fallback.
"""
import re
from dataclasses import dataclass

from tofu_search.log import get_logger
from tofu_search.providers import normalize_site_search_results

logger = get_logger(__name__)


class EngineSelectionError(ValueError):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class EngineSelection:
    specs: tuple
    names: frozenset
    deferred_names: frozenset
    allow_free_recovery: bool


def select_engines(query, engines, freshness, *, web_engines, site_provider,
                   native_search_available, native_search, config, gateway_search,
                   gateway_tag_prefix):
    """Validate source authority and select initial and deferred engine waves."""
    requested = None if engines is None else set(engines)

    native_requested = requested is not None and any(
        tag == 'Xiaohongshu' or str(tag).startswith('Site:') for tag in requested)
    # Native keyword APIs cannot enforce Web operator semantics, even for a
    # matching site domain. Never strip constraints or navigate first.
    if native_requested and (freshness or re.search(
            r'(?i)(?<!\w)(?:site|filetype|ext|inurl|allinurl|intitle|allintitle|'
            r'intext|allintext|before|after|cache|related|source):', query)):
        raise EngineSelectionError(
            'unsupported_site_query',
            'Native site search cannot enforce Web operators or freshness. '
            'Use plain native keywords, or select a Web engine to preserve these constraints.')

    FREE_ENGINE_NAMES = tuple(tag for tag, _fn, _limit in web_engines)
    ALL_ENGINE_NAMES = list(FREE_ENGINE_NAMES)
    gateway_engine_specs = []
    if (str(getattr(config, 'gateway_url', '') or '').strip()
            and str(getattr(config, 'gateway_api_key', '') or '').strip()):
        for source_id in dict.fromkeys(
                str(s).strip() for s in getattr(config, 'gateway_sources', None) or ()):
            if not source_id:
                continue

            def _gateway_search(q, n=10, fresh='', *, _sid=source_id):
                return gateway_search(_sid, q, n, fresh)

            gateway_engine_specs.append((gateway_tag_prefix + source_id, _gateway_search, 10))
            ALL_ENGINE_NAMES.append(gateway_tag_prefix + source_id)
    free_policy = str(getattr(config, 'free_engines', 'fallback') or 'fallback').strip().lower()
    if free_policy not in ('fallback', 'parallel', 'off'):
        logger.warning('[Search] Unknown free_engines=%r; using fallback', free_policy)
        free_policy = 'fallback'
    site_engine_specs = []
    if site_provider is not None and requested and any(
            str(tag).startswith('Site:') for tag in requested):
        try:
            site_sources = site_provider.list_sources() or []
        except Exception as exc:
            logger.warning('[Search] site-search source discovery failed: %s', exc)
            site_sources = []
        for source in site_sources:
            if isinstance(source, str):
                source = {'id': source, 'name': source}
            if not isinstance(source, dict) or not source.get('id'):
                continue
            source_id = str(source['id'])
            # Xiaohongshu has its own paced/backoff engine below.  It calls the
            # same provider internally, so adding it again would double-hit the
            # user's account for one query.
            aliases = {str(a).lower() for a in source.get('aliases', [])}
            if source_id.lower() in ('xiaohongshu', 'xhs') or 'xhs' in aliases:
                continue
            tag = 'Site:' + source_id
            source_name = str(source.get('name') or source_id)

            def _site_search(q, n=10, fresh='', *, _sid=source_id,
                             _name=source_name):
                raw = site_provider.search(_sid, q, max_results=n,
                                           freshness=fresh)
                if raw is None:
                    raise RuntimeError('Native site source unavailable: ' + _sid)
                return normalize_site_search_results(
                    raw, source_id=_sid, source_name=_name, max_results=n)

            site_engine_specs.append((tag, _site_search, 10))
            ALL_ENGINE_NAMES.append(tag)
    _xhs_on = bool(requested and 'Xiaohongshu' in requested and native_search_available())
    if _xhs_on:
        ALL_ENGINE_NAMES.append('Xiaohongshu')
    deferred_engines = set()
    if requested is not None:
        invalid = requested - set(ALL_ENGINE_NAMES)
        if not requested or invalid:
            raise EngineSelectionError(
                'invalid_engines',
                'Explicit engine selection is empty or contains unknown/unavailable engines: '
                + ', '.join(sorted(map(str, invalid or requested))))
        engine_allow = requested
    else:
        engine_allow = set(ALL_ENGINE_NAMES)
        if gateway_engine_specs and free_policy != 'parallel':
            engine_allow -= set(FREE_ENGINE_NAMES)
            if free_policy == 'fallback':
                deferred_engines = set(FREE_ENGINE_NAMES)
    # An explicit selection is an authority boundary, not a fallback hint.
    # Under the 'off' policy the DDG/Brave retry and the browser-search
    # fallback count as free engines too.
    allow_free_recovery = requested is None and not (
        gateway_engine_specs and free_policy == 'off')

    specs = list(web_engines) + gateway_engine_specs + site_engine_specs
    if _xhs_on and 'Xiaohongshu' in engine_allow:
        specs.append(('Xiaohongshu', native_search, 10))
    return EngineSelection(
        tuple(spec for spec in specs if spec[0] in engine_allow | deferred_engines),
        frozenset(engine_allow), frozenset(deferred_engines), allow_free_recovery)
