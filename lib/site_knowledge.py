"""lib/site_knowledge.py — per-site extraction knowledge store.

Site Knowledge Layer (docs/SITE_KNOWLEDGE_LAYER_DESIGN.md): entries are
OVERRIDES on top of tofu-search's built-in engine constants. When a site's
DOM drifts, the site-doctor (lib/site_doctor.py) verifies new selectors
against the LIVE page and pins them here as DATA; tofu-search engines read
them through the SiteKnowledgeProvider seam (wired in lib/search_bridge.py)
and fall back to their built-ins when no entry exists. Re-pinning a site
never needs a library release or a server restart.

Store: data/config/site_knowledge.json (sibling of private_hosts.json),
atomic writes via lib.json_store with per-path locking.
"""

from __future__ import annotations

import os
import time

from lib.json_store import read_json, update_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['get_knowledge', 'pin_knowledge', 'clear_knowledge',
           'list_knowledge']

#: Monkeypatchable for tests (the store path is a module-level single source,
#: same convention as lib.auth_sources' store).
_STORE_PATH = os.path.join('data', 'config', 'site_knowledge.json')

#: Fields a pinned operation entry carries; anything else is preserved.
_ENTRY_FIELDS = ('wait_selector', 'extractor_js', 'scrolls', 'version',
                 'verified_at', 'verified_by', 'evidence', 'notes', 'access')


def _store_path() -> str:
    return _STORE_PATH


def get_knowledge(domain: str, operation: str = 'search') -> dict | None:
    """Return the pinned knowledge for ``domain`` + ``operation``.

    The returned dict is a defensive copy shaped for the engine:
    ``{wait_selector, extractor_js, scrolls, version, ...}``.
    """
    if not domain:
        return None
    data = read_json(_store_path(), default={})
    site = (data or {}).get(domain)
    if isinstance(site, dict) and isinstance(site.get('operations'), dict):
        entry = site['operations'].get(operation)
    elif operation == 'search':
        # Legacy flat entries migrate lazily on the next pin.
        entry = site
    else:
        entry = None
    if not isinstance(entry, dict) or not entry.get('extractor_js'):
        return None
    return {k: entry[k] for k in _ENTRY_FIELDS if k in entry}


def pin_knowledge(domain: str, *, extractor_js: str, wait_selector: str = '',
                  scrolls: int = 2, verified_by: str = 'site-doctor',
                  evidence: dict | None = None, notes: str = '',
                  operation: str = 'search', access: str = 'read') -> dict:
    """Pin (create or replace) the knowledge entry for ``domain``.

    ``version`` increments monotonically from whatever the file holds (never
    resets), so a later reader can tell two pins apart. Returns the entry as
    persisted. Raises ValueError on an empty extractor — pinning nothing
    would black out the site harder than the drift did.
    """
    if not domain or not isinstance(domain, str):
        raise ValueError('domain is required')
    if not isinstance(extractor_js, str) or not extractor_js.strip():
        raise ValueError('extractor_js must be a non-empty JS string')
    if not operation or not isinstance(operation, str):
        raise ValueError('operation is required')
    if access != 'read':
        raise ValueError('automatic extraction knowledge is read-only; '
                         'write operations must fail closed')

    def _mut(data):
        data = data if isinstance(data, dict) else {}
        site = data.get(domain) or {}
        if isinstance(site, dict) and 'operations' not in site \
                and site.get('extractor_js'):
            site = {'operations': {'search': site}}
        if not isinstance(site, dict):
            site = {}
        operations = site.setdefault('operations', {})
        prev = operations.get(operation) or {}
        entry = {
            'wait_selector': wait_selector or '',
            'extractor_js': extractor_js,
            'scrolls': max(0, int(scrolls or 0)),
            'version': int(prev.get('version') or 0) + 1,
            'verified_at': time.time(),
            'verified_by': verified_by,
            'evidence': dict(evidence or {}),
            'notes': notes or '',
            'access': 'read',
        }
        operations[operation] = entry
        data[domain] = site
        return data

    update_json_atomic(_store_path(), _mut, default={})
    entry = get_knowledge(domain, operation) or {}
    logger.info('[SiteKnowledge] pinned %s.%s v%s by %s (extractor %d chars)',
                domain, operation, entry.get('version'), verified_by,
                len(extractor_js))
    return entry


def clear_knowledge(domain: str, operation: str | None = None) -> bool:
    """Remove one operation, or all knowledge for ``domain`` when omitted."""
    removed = {'ok': False}

    def _mut(data):
        data = data if isinstance(data, dict) else {}
        if operation is None:
            removed['ok'] = domain in data
            data.pop(domain, None)
            return data
        site = data.get(domain)
        if isinstance(site, dict) and isinstance(site.get('operations'), dict):
            removed['ok'] = operation in site['operations']
            site['operations'].pop(operation, None)
            if not site['operations']:
                data.pop(domain, None)
        elif operation == 'search' and isinstance(site, dict):
            removed['ok'] = domain in data
            data.pop(domain, None)
        return data

    update_json_atomic(_store_path(), _mut, default={})
    if removed['ok']:
        logger.info('[SiteKnowledge] cleared %s (engines fall back to '
                    'built-in constants)', domain)
    return removed['ok']


def list_knowledge() -> dict:
    """All pinned entries keyed by domain, then operation."""
    data = read_json(_store_path(), default={})
    if not isinstance(data, dict):
        return {}
    out = {}
    for domain, site in data.items():
        if not isinstance(site, dict):
            continue
        operations = site.get('operations') if isinstance(site.get('operations'), dict) \
            else {'search': site}
        out[domain] = {'operations': {
            op: {k: entry[k] for k in _ENTRY_FIELDS if k in entry}
            for op, entry in operations.items() if isinstance(entry, dict)
        }}
    return out
