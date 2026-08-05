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

#: Fields a pinned entry carries; anything else in the file is preserved
#: (forward-compatible with later schema additions, e.g. detail knowledge).
_ENTRY_FIELDS = ('wait_selector', 'extractor_js', 'scrolls', 'version',
                 'verified_at', 'verified_by', 'evidence', 'notes')


def _store_path() -> str:
    return _STORE_PATH


def get_knowledge(domain: str) -> dict | None:
    """Return the pinned knowledge entry for ``domain``, or None.

    The returned dict is a defensive copy shaped for the engine:
    ``{wait_selector, extractor_js, scrolls, version, ...}``.
    """
    if not domain:
        return None
    data = read_json(_store_path(), default={})
    entry = (data or {}).get(domain)
    if not isinstance(entry, dict) or not entry.get('extractor_js'):
        return None
    return {k: entry[k] for k in _ENTRY_FIELDS if k in entry}


def pin_knowledge(domain: str, *, extractor_js: str, wait_selector: str = '',
                  scrolls: int = 2, verified_by: str = 'site-doctor',
                  evidence: dict | None = None, notes: str = '') -> dict:
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

    def _mut(data):
        data = data if isinstance(data, dict) else {}
        prev = data.get(domain) or {}
        entry = {
            'wait_selector': wait_selector or '',
            'extractor_js': extractor_js,
            'scrolls': max(0, int(scrolls or 0)),
            'version': int(prev.get('version') or 0) + 1,
            'verified_at': time.time(),
            'verified_by': verified_by,
            'evidence': dict(evidence or {}),
            'notes': notes or '',
        }
        data[domain] = entry
        return data

    update_json_atomic(_store_path(), _mut, default={})
    entry = get_knowledge(domain) or {}
    logger.info('[SiteKnowledge] pinned %s v%s by %s (extractor %d chars)',
                domain, entry.get('version'), verified_by, len(extractor_js))
    return entry


def clear_knowledge(domain: str) -> bool:
    """Remove the pinned entry for ``domain`` (rollback to built-ins)."""
    removed = {'ok': False}

    def _mut(data):
        data = data if isinstance(data, dict) else {}
        removed['ok'] = domain in data
        data.pop(domain, None)
        return data

    update_json_atomic(_store_path(), _mut, default={})
    if removed['ok']:
        logger.info('[SiteKnowledge] cleared %s (engines fall back to '
                    'built-in constants)', domain)
    return removed['ok']


def list_knowledge() -> dict:
    """All pinned entries, keyed by domain (defensive copy)."""
    data = read_json(_store_path(), default={})
    if not isinstance(data, dict):
        return {}
    return {d: {k: e[k] for k in _ENTRY_FIELDS if k in e}
            for d, e in data.items() if isinstance(e, dict)}
