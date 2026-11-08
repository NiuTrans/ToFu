"""lib/search/search_settings.py — Owner of the saved search setting: the fetch blocklist.

Search and fetch limits (results per search, page sizes, timeouts, download
size) are fixed tofu-search defaults owned by ``tofu_search.config``; there is
no host tuning layer. The only saved, user-adjustable search behaviour is
``skip_domains``: URLs the assistant's web tools never visit. Entries are
hosts (``example.com``, including subdomains) or host+path prefixes
(``example.com/private``); a ``*`` path segment matches one segment. Matching
is owned by :mod:`lib.search.url_blocklist`. Its writers share this module
so "what counts as the same entry" has one answer:

  * **The ``update_search_settings`` agent tool** blocks/unblocks a domain
    through :func:`apply_updates` — validated, persisted, hot-reloaded and
    audit-logged; called with no changes it is a pure read.
  * **The Daily Optimizer** (``block_search_domain`` action) reuses
    :func:`normalise_domain`.
  * **The Settings UI** saves the same ``search.skip_domains`` list through
    ``routes/config.py``.
"""

from __future__ import annotations

import threading

import lib as _lib
from lib.config_dir import config_path as _config_path
from lib.json_store import JsonStoreReadError, update_json_atomic
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

_CONFIG_FILE = _config_path('server_config.json')
_APPLY_LOCK = threading.Lock()

ADJUSTABLE_KEYS = ('block_domain', 'unblock_domain')


def normalise_domain(domain: str) -> str:
    """Canonical blocklist-entry normaliser (scheme/www stripped, lowercased).

    Shared by the optimizer's block_search_domain action and the
    update_search_settings tool so "what counts as the same entry" has one
    answer across every writer of ``skip_domains``.

    An entry is either a bare host (``example.com`` — port stripped, since
    host rules are port-independent) or a host plus URL path
    (``example.com/private``). The path is PRESERVED: stripping it would
    silently widen a request to block one repository or directory into
    blocking the whole host. ``*`` stays intact as a one-segment wildcard
    (``github.com/*/terminal-bench``). Matching semantics live in
    :mod:`lib.search.url_blocklist`.
    """
    dom = (domain or '').strip().lower()
    if dom.startswith('http://') or dom.startswith('https://'):
        dom = dom.split('://', 1)[1]
    if dom.startswith('www.'):
        dom = dom[4:]
    dom = dom.split('?', 1)[0].split('#', 1)[0]
    if '/' in dom:
        host, _, path = dom.partition('/')
        host = host.split(':', 1)[0]
        return host + '/' + path.strip('/')
    if ':' in dom:
        dom = dom.split(':', 1)[0]
    return dom


def read_effective() -> dict:
    """The blocklist the fetch pipeline will actually use RIGHT NOW."""
    return {'skip_domains': sorted(getattr(_lib, 'SKIP_DOMAINS', set()))}


def apply_updates(changes: dict) -> dict:
    """Validate, persist, and hot-apply blocklist changes.

    Args:
        changes: ``block_domain`` / ``unblock_domain`` → one host or a list of
          hosts, normalised via :func:`normalise_domain`. Any other key is
          rejected: search limits are fixed defaults, not settings.

    Returns:
        ``{'ok', 'applied', 'errors', 'effective'}`` where ``applied`` maps
        each alias to the stored domains and ``effective`` is the post-change
        :func:`read_effective` snapshot. With NO changes the function is a
        pure read (nothing written, no reload, no audit).
    """
    with _APPLY_LOCK:
        return _apply_updates_locked(changes)


def _apply_updates_locked(changes: dict) -> dict:
    """Persist and hot-apply one blocklist update in commit order."""
    changes = dict(changes or {})
    result = {'ok': True, 'applied': {}, 'errors': {},
              'effective': read_effective()}

    domains_to_block: list[str] = []
    domains_to_unblock: list[str] = []
    for alias, bucket in (('block_domain', domains_to_block),
                          ('unblock_domain', domains_to_unblock)):
        raw = changes.pop(alias, None)
        if raw is None:
            continue
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            dom = normalise_domain(str(item or ''))
            if dom and '.' in dom:
                bucket.append(dom)
            else:
                result['errors'][alias] = f'invalid domain: {item!r}'

    for key in changes:
        result['errors'][key] = (
            f'{key!r} is not adjustable: search limits are fixed defaults '
            f'(allowed: {", ".join(ADJUSTABLE_KEYS)})')

    if not domains_to_block and not domains_to_unblock:
        if result['errors']:
            result['ok'] = False
            logger.warning('[SearchSettings] apply rejected: %s', result['errors'])
        return result   # pure read when called with no changes at all

    # ── Persist into server_config.json (locked field-level merge) ──
    def _mutate(data):
        if not isinstance(data, dict):
            raise JsonStoreReadError(
                'server_config.json is not a JSON object')
        search_cfg = data.get('search')
        if not isinstance(search_cfg, dict):
            search_cfg = {}
            data['search'] = search_cfg
        current = search_cfg.get('skip_domains')
        if not isinstance(current, list):
            # Seed from in-memory defaults so we never shrink the set.
            current = sorted(_lib.SKIP_DOMAINS)
        current = set(current)
        current |= set(domains_to_block)
        current -= set(domains_to_unblock)
        search_cfg['skip_domains'] = sorted(current)
        if domains_to_block:
            result['applied']['block_domain'] = sorted(set(domains_to_block))
        if domains_to_unblock:
            result['applied']['unblock_domain'] = sorted(set(domains_to_unblock))
        return data

    try:
        update_json_atomic(
            _CONFIG_FILE, _mutate, default={}, strict=True, indent=2)
    except Exception as e:
        logger.error('[SearchSettings] persist failed: %s', e, exc_info=True)
        result['ok'] = False
        result['errors']['persist'] = str(e)
        return result

    # ── Hot-apply (re-reads the file, updates lib.*, re-syncs tofu-search) ──
    try:
        _lib.reload_config()
    except Exception as e:
        logger.error('[SearchSettings] reload_config failed: %s', e, exc_info=True)
        result['ok'] = False
        result['errors']['reload'] = str(e)
        return result

    result['effective'] = read_effective()
    audit_log('search_settings_update',
              applied=result['applied'],
              rejected=result['errors'] or None,
              skip_domains_size=len(result['effective']['skip_domains']))
    logger.info('[SearchSettings] applied=%s errors=%s', result['applied'],
                result['errors'] or 'none')
    return result
