"""lib/optimizer/actions/block_search_domain.py — Add/remove a domain in
server_config.json::search.fetch_skip_domains.

This is the ONE auto-apply action in v1.  It:
  * Loads data/config/server_config.json
  * Adds (or removes) the target domain under ``search.fetch_skip_domains``:
    a fetch-quality skip (tofu-search spends no anonymous fetch on it), never
    an operator ban — search listings, the browser tools and ``run_command``
    still reach the domain (``lib.search.saved_domain_lists``)
  * Persists the file atomically
  * Calls ``lib.reload_config()`` so ``lib.FETCH_SKIP_DOMAINS`` updates live
  * Emits ``audit_log('optimizer_action', ...)``

All mutations are reversible — ``revert()`` removes the domain again.
"""

from __future__ import annotations

import lib as _lib
from lib.config_dir import config_path as _config_path
from lib.identity import require_user_id
from lib.json_store import JsonStoreReadError, update_json_atomic
from lib.log import audit_log, get_logger
from lib.search.saved_domain_lists import (
    saved_fetch_skip_domains,
    saved_skip_domains,
    store_skip_domains,
)
from lib.search.search_settings import normalise_domain as _ss_normalise_domain

logger = get_logger(__name__)


_CONFIG_FILE = _config_path('server_config.json')


def _normalise_domain(domain: str) -> str:
    # Canonical implementation lives in lib.search.search_settings.normalise_domain
    # (shared with the update_search_settings agent tool); the alias name is
    # kept because tests patch/assert it here.
    return _ss_normalise_domain(domain)


# ══════════════════════════════════════════════════════════
#  Public apply / revert
# ══════════════════════════════════════════════════════════

def apply(args: dict, *, owner_user_id: int) -> dict:
    """Add the target domain to ``search.fetch_skip_domains``.

    Args:
        args: ``{"domain": "<host>", "ttl_days": <int>}``

    Returns:
        Dict with ``domain`` and ``fetch_skip_domains_size_after`` for the action log.

    Raises:
        ValueError on invalid args; OSError on file write failure.
    """
    owner_id = require_user_id(
        owner_user_id, context='optimizer action owner')
    domain = _normalise_domain(str(args.get('domain') or ''))
    if not domain or '.' not in domain:
        raise ValueError(f'invalid domain: {args.get("domain")!r}')
    ttl_days = int(args.get('ttl_days') or 7)

    outcome = {'size': 0}

    def _mutate(data):
        if not isinstance(data, dict):
            raise JsonStoreReadError(
                'server_config.json is not a JSON object')
        search_cfg = data.get('search')
        if not isinstance(search_cfg, dict):
            search_cfg = {}
            data['search'] = search_cfg
        current = saved_fetch_skip_domains(search_cfg)
        if domain in current:
            logger.info(
                '[Optimizer.block_search_domain] %s already present — no-op',
                domain)
        search_cfg['fetch_skip_domains'] = sorted(current | {domain})
        outcome['size'] = len(search_cfg['fetch_skip_domains'])
        return data

    update_json_atomic(
        _CONFIG_FILE, _mutate, default={}, strict=True, indent=2)

    # Hot-reload so lib.FETCH_SKIP_DOMAINS updates without restart
    try:
        _lib.reload_config()
    except Exception as e:
        logger.error('[Optimizer.block_search_domain] reload_config failed: %s',
                     e, exc_info=True)
        raise

    audit_log(
        'optimizer_action',
        user_id=owner_id,
        action='block_search_domain',
        domain=domain,
        ttl_days=ttl_days,
        fetch_skip_domains_size_after=outcome['size'],
    )
    logger.info('[Optimizer.block_search_domain] applied domain=%s ttl_days=%d '
                'fetch_skip_domains=%d',
                domain, ttl_days, outcome['size'])

    return {
        'domain': domain,
        'ttl_days': ttl_days,
        'fetch_skip_domains_size_after': outcome['size'],
    }


def revert(args: dict, *, owner_user_id: int) -> dict:
    """Remove the target domain from ``search.fetch_skip_domains``."""
    owner_id = require_user_id(
        owner_user_id, context='optimizer action owner')
    domain = _normalise_domain(str(args.get('domain') or ''))
    if not domain:
        raise ValueError(f'invalid domain for revert: {args.get("domain")!r}')

    outcome = {'reverted': False, 'size': 0}

    def _mutate(data):
        if not isinstance(data, dict):
            raise JsonStoreReadError(
                'server_config.json is not a JSON object')
        search_cfg = data.get('search')
        if not isinstance(search_cfg, dict):
            return None
        skips = saved_fetch_skip_domains(search_cfg)
        # An action applied before the ban/fetch-skip split wrote the ban list;
        # its expiry must still lift it there.
        bans = saved_skip_domains(search_cfg)
        if domain not in skips and domain not in bans:
            return None
        if domain in skips:
            search_cfg['fetch_skip_domains'] = sorted(skips - {domain})
        if domain in bans:
            store_skip_domains(search_cfg, bans - {domain})
        outcome['reverted'] = True
        outcome['size'] = len(search_cfg.get('fetch_skip_domains') or ())
        return data

    update_json_atomic(
        _CONFIG_FILE, _mutate, default={}, strict=True, indent=2)
    if outcome['reverted']:
        try:
            _lib.reload_config()
        except Exception as e:
            logger.error('[Optimizer.block_search_domain] reload_config on revert '
                         'failed: %s', e, exc_info=True)
            raise
        audit_log('optimizer_revert',
                  user_id=owner_id,
                  action='block_search_domain', domain=domain,
                  fetch_skip_domains_size_after=outcome['size'])
        logger.info('[Optimizer.block_search_domain] reverted domain=%s '
                    'fetch_skip_domains=%d',
                    domain, outcome['size'])
        return {'domain': domain, 'reverted': True,
                'fetch_skip_domains_size_after': outcome['size']}

    logger.info('[Optimizer.block_search_domain] revert no-op: %s not present',
                domain)
    audit_log('optimizer_revert',
              user_id=owner_id,
              action='block_search_domain', domain=domain, noop=True)
    return {'domain': domain, 'reverted': False, 'reason': 'not_present'}


ACTION = {
    'name': 'block_search_domain',
    'auto_apply': True,
    'description': ('Stop fetching a domain\'s pages (server_config.search.'
                    'fetch_skip_domains) for ttl_days (default 7); auto-reverts '
                    'on expiry. Search listings and other tools still reach it.'),
    'apply': apply,
    'revert': revert,
    # server_config.json is a personal-computer authority. Distributed mode
    # must not let one owner mutate a deployment-global file.
    'deployment_modes': frozenset({'personal'}),
}
