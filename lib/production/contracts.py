"""Media-neutral content contracts for production recipes.

Decks, motion videos and future deliverables may render very differently, but
their narrative units still need the same small set of trustworthy fields:
why the unit exists, which source cards support it, what real assets it needs,
and how a quality finding is shaped. This module normalises only that common
vocabulary; capability-specific layout and timing semantics stay outside.
"""

from __future__ import annotations

import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'CREATIVE_MODES', 'DEFAULT_CREATIVE_MODE', 'FINDING_SEVERITIES',
    'MEDIA_QUERY_KINDS', 'normalise_asset_briefs', 'normalise_creative_mode',
    'normalise_findings', 'normalise_media_queries',
    'normalise_narrative_core', 'normalise_source_ids',
]

FINDING_SEVERITIES = ('blocker', 'major', 'minor')
CREATIVE_MODES = ('standard', 'director')
DEFAULT_CREATIVE_MODE = 'director'
MEDIA_QUERY_KINDS = ('image', 'video', 'gif', 'webpage')
_SOURCE_ID_RE = re.compile(r'^S\d+$')


def _compact(value, *, limit: int) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def normalise_creative_mode(value, *, default: str = DEFAULT_CREATIVE_MODE) -> str:
    """Return the bounded production strategy used in checkpoint identities.

    ``standard`` performs one planning call. ``director`` drafts contrasting
    candidates and asks an independent critic to choose. Unknown values fall
    back to the caller-selected default so old manifests and API clients stay
    resumable while the public surface remains finite.
    """
    fallback = str(default or DEFAULT_CREATIVE_MODE).strip().lower()
    if fallback not in CREATIVE_MODES:
        fallback = DEFAULT_CREATIVE_MODE
    mode = str(value or '').strip().lower()
    return mode if mode in CREATIVE_MODES else fallback


def normalise_source_ids(raw, *, valid_ids=(), limit: int = 6) -> list[str]:
    """Return unique ``S#`` ids, optionally restricted to an evidence set."""
    values = raw if isinstance(raw, (list, tuple, set)) else []
    allowed = {str(value) for value in valid_ids or ()}
    cap = max(0, limit)
    if cap == 0:
        return []
    out: list[str] = []
    for value in values:
        sid = str(value or '').strip().upper()
        if not _SOURCE_ID_RE.fullmatch(sid):
            continue
        if allowed and sid not in allowed:
            continue
        if sid not in out:
            out.append(sid)
        if len(out) >= cap:
            break
    return out


def normalise_narrative_core(unit: dict, *, allowed_roles,
                             fallback_role: str,
                             fallback_why: str) -> dict:
    """Fill the common narrative role/reason fields of one media unit."""
    roles = tuple(str(role) for role in allowed_roles)
    role = str(unit.get('narrative_role') or '').strip().lower()
    if role not in roles:
        role = fallback_role if fallback_role in roles else (roles[0] if roles else '')
    why = _compact(unit.get('narrative_why'), limit=280)
    unit['narrative_role'] = role
    unit['narrative_why'] = why or _compact(fallback_why, limit=280)
    return unit


def normalise_asset_briefs(raw, *, allowed_roles,
                           fallback_role: str = '', max_items: int = 3,
                           prompt_limit: int = 1200,
                           log_prefix: str = '[ProductionContract]') -> list[dict]:
    """Validate generic ``{'role', 'prompt'}`` asset obligations.

    An unknown role degrades to the caller-selected fallback so a typo cannot
    silently invent a new hard obligation in a renderer.
    """
    roles = tuple(str(role) for role in allowed_roles)
    fallback = fallback_role if fallback_role in roles else (roles[-1]
                                                              if roles else '')
    out: list[dict] = []
    cap = max(0, max_items)
    if not isinstance(raw, list) or cap == 0:
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        prompt = _compact(item.get('prompt'), limit=prompt_limit)
        if not prompt:
            continue
        role = str(item.get('role') or '').strip().lower()
        if role not in roles:
            logger.info('%s asset role %r is not one of %s; using %r',
                        log_prefix, role, roles, fallback)
            role = fallback
        brief = {'role': role, 'prompt': prompt}
        semantic_target = _compact(
            item.get('semantic_target') or item.get('supports'), limit=280)
        if semantic_target:
            brief['semantic_target'] = semantic_target
        out.append(brief)
        if len(out) >= cap:
            break
    return out


def normalise_media_queries(raw, *, max_items: int = 4) -> list[dict]:
    """Validate renderer-neutral requests for real image/video/web evidence.

    This is deliberately a request contract, not a downloader. Capabilities
    may satisfy it with a stock provider, supplied media, a browser capture,
    or a generated still while preserving the semantic target in checkpoints.
    """
    out: list[dict] = []
    cap = max(0, max_items)
    if not isinstance(raw, list) or cap == 0:
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        query = _compact(item.get('query'), limit=500)
        semantic_target = _compact(
            item.get('semantic_target') or item.get('must_show'), limit=280)
        if not query or not semantic_target:
            continue
        kind = str(item.get('kind') or 'image').strip().lower()
        if kind not in MEDIA_QUERY_KINDS:
            kind = 'image'
        request = {
            'kind': kind,
            'query': query,
            'semantic_target': semantic_target,
        }
        source_url = _compact(item.get('source_url'), limit=4096)
        if source_url.startswith(('https://', 'http://')):
            request['source_url'] = source_url
        license_hint = _compact(item.get('license_hint'), limit=160)
        if license_hint:
            request['license_hint'] = license_hint
        out.append(request)
        if len(out) >= cap:
            break
    return out


def normalise_findings(raw, *, valid_checks=()) -> list[dict]:
    """Return the shared actionable quality-finding shape."""
    allowed = {str(check) for check in valid_checks or ()}
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        issue = _compact(item.get('issue'), limit=400)
        if not issue:
            continue
        check = str(item.get('check') or '')
        if allowed and check not in allowed:
            check = ''
        severity = str(item.get('severity') or 'minor').strip().lower()
        if severity not in FINDING_SEVERITIES:
            severity = 'minor'
        out.append({
            'check': check,
            'element': _compact(item.get('element'), limit=200),
            'issue': issue,
            'severity': severity,
            'fix': _compact(item.get('fix'), limit=400),
        })
    return out
