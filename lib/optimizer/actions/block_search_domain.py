"""lib/optimizer/actions/block_search_domain.py — Add/remove a domain in
server_config.json::search.skip_domains.

This is the ONE auto-apply action in v1.  It:
  * Loads data/config/server_config.json
  * Adds (or removes) the target domain under ``search.skip_domains``
  * Persists the file atomically
  * Calls ``lib.reload_config()`` so ``lib.SKIP_DOMAINS`` updates live
  * Emits ``audit_log('optimizer_action', ...)``

All mutations are reversible — ``revert()`` removes the domain again.
"""

from __future__ import annotations

import lib as _lib
from lib.config_dir import config_path as _config_path
from lib.json_store import read_json, write_json_atomic
from lib.log import audit_log, get_logger
from lib.search_settings import normalise_domain as _ss_normalise_domain

logger = get_logger(__name__)


_CONFIG_FILE = _config_path('server_config.json')


# ══════════════════════════════════════════════════════════
#  File helpers (delegate to lib.json_store)
# ══════════════════════════════════════════════════════════

def _load_config() -> dict:
    """Read server_config.json. Returns {} on missing or unparseable file."""
    data = read_json(_CONFIG_FILE, default={})
    return data if isinstance(data, dict) else {}


def _atomic_write(data: dict) -> None:
    """Atomically persist the config file."""
    write_json_atomic(_CONFIG_FILE, data)


def _normalise_domain(domain: str) -> str:
    # Canonical implementation lives in lib.search_settings.normalise_domain
    # (shared with the update_search_settings agent tool); the alias name is
    # kept because tests patch/assert it here.
    return _ss_normalise_domain(domain)


# ══════════════════════════════════════════════════════════
#  Public apply / revert
# ══════════════════════════════════════════════════════════

def apply(args: dict) -> dict:
    """Add the target domain to ``search.skip_domains``.

    Args:
        args: ``{"domain": "<host>", "ttl_days": <int>}``

    Returns:
        Dict with ``domain`` and ``skip_domains_size_after`` for the action log.

    Raises:
        ValueError on invalid args; OSError on file write failure.
    """
    domain = _normalise_domain(str(args.get('domain') or ''))
    if not domain or '.' not in domain:
        raise ValueError(f'invalid domain: {args.get("domain")!r}')
    ttl_days = int(args.get('ttl_days') or 7)

    data = _load_config()
    search_cfg = data.setdefault('search', {}) if isinstance(data.get('search', {}), dict) else {}
    if not isinstance(search_cfg, dict):
        search_cfg = {}
        data['search'] = search_cfg
    current = search_cfg.get('skip_domains')
    if not isinstance(current, list):
        # Seed with in-memory defaults so we never SHRINK the effective set
        current = sorted(_lib.SKIP_DOMAINS)
    if domain in current:
        logger.info('[Optimizer.block_search_domain] %s already present — no-op',
                    domain)
    else:
        current.append(domain)
    search_cfg['skip_domains'] = sorted(set(current))
    data['search'] = search_cfg

    _atomic_write(data)

    # Hot-reload so lib.SKIP_DOMAINS updates without restart
    try:
        _lib.reload_config()
    except Exception as e:
        logger.error('[Optimizer.block_search_domain] reload_config failed: %s',
                     e, exc_info=True)
        raise

    audit_log(
        'optimizer_action',
        action='block_search_domain',
        domain=domain,
        ttl_days=ttl_days,
        skip_domains_size_after=len(search_cfg['skip_domains']),
    )
    logger.info('[Optimizer.block_search_domain] applied domain=%s ttl_days=%d '
                'skip_domains=%d',
                domain, ttl_days, len(search_cfg['skip_domains']))

    return {
        'domain': domain,
        'ttl_days': ttl_days,
        'skip_domains_size_after': len(search_cfg['skip_domains']),
    }


def revert(args: dict) -> dict:
    """Remove the target domain from ``search.skip_domains``."""
    domain = _normalise_domain(str(args.get('domain') or ''))
    if not domain:
        raise ValueError(f'invalid domain for revert: {args.get("domain")!r}')

    data = _load_config()
    search_cfg = data.setdefault('search', {})
    if not isinstance(search_cfg, dict):
        search_cfg = {}
        data['search'] = search_cfg
    current = search_cfg.get('skip_domains')
    if isinstance(current, list) and domain in current:
        current = [d for d in current if d != domain]
        search_cfg['skip_domains'] = sorted(set(current))
        _atomic_write(data)
        try:
            _lib.reload_config()
        except Exception as e:
            logger.error('[Optimizer.block_search_domain] reload_config on revert '
                         'failed: %s', e, exc_info=True)
            raise
        audit_log('optimizer_revert',
                  action='block_search_domain', domain=domain,
                  skip_domains_size_after=len(search_cfg['skip_domains']))
        logger.info('[Optimizer.block_search_domain] reverted domain=%s '
                    'skip_domains=%d',
                    domain, len(search_cfg['skip_domains']))
        return {'domain': domain, 'reverted': True,
                'skip_domains_size_after': len(search_cfg['skip_domains'])}

    logger.info('[Optimizer.block_search_domain] revert no-op: %s not present',
                domain)
    audit_log('optimizer_revert',
              action='block_search_domain', domain=domain, noop=True)
    return {'domain': domain, 'reverted': False, 'reason': 'not_present'}


ACTION = {
    'name': 'block_search_domain',
    'auto_apply': True,
    'description': ('Add a domain to server_config.search.skip_domains for '
                    'ttl_days (default 7); auto-reverts on expiry.'),
    'apply': apply,
    'revert': revert,
}
