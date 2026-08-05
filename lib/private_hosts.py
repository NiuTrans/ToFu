"""lib/private_hosts.py — Internal-host SSRF allowlist store.

The fetch pipeline blocks any URL whose host resolves to a private / loopback /
link-local / reserved address (tofu-search's ``block_private_addresses``). That
default is correct — it is what stops SSRF to cloud metadata and localhost — but
it also makes a deliberately-wanted internal host unreachable. This module is
the persistence + lookup layer for the *explicit* exemption: the operator names
the hosts they mean to fetch, and only those bypass the guard.

Why hostnames, never IPs
------------------------
The judgement is anchored on the HOSTNAME. An internal load balancer rotates its
address between lookups — one host was observed answering as both
``10.176.18.71`` and ``10.192.19.176`` minutes apart — so an IP allowlist rots
silently while the hostname stays true. A bare-IP entry is therefore REFUSED at
the store boundary (:func:`normalize_host`), not merely discouraged: allowing one
would re-introduce the exact fragility this design rejects, and would also hand
back a way to name ``169.254.169.254`` directly.

Two gates, deliberately separate
--------------------------------
This store governs REACHABILITY only. It grants no credentials, and it is not
consulted for them. Its sibling :mod:`lib.auth_sources` governs IDENTITY
(cookies for login-walled sites) and grants no SSRF exemption. Connecting an
account must never silently widen the network boundary, and allowlisting a host
must never imply a login — a permission travelling on the wrong noun is how the
implicit-bypass defect happened in the first place. Keep them apart.

Persistence
-----------
``data/config/private_hosts.json`` via :mod:`lib.json_store` (atomic, locked)::

    {
      "version": 1,
      "hosts": [
        {"host": "example-corp.com", "label": "YourProvider internal",
         "enabled": true, "updated_at": 1701000000.0}
      ]
    }

Like every other file under ``data/config/``, this one is excluded from
``export.py`` exports. That is CORRECT: the allowlist is per-install operator
intent, so a fresh destination starts closed (empty = everything blocked) and
the operator re-states what they mean to reach. The CODE, by contrast, must
survive export — a guard asserts that.

Public API
----------
  list_hosts()                  → list[dict]   (all rows, for the Settings UI)
  enabled_hosts()               → set[str]     (the set handed to tofu-search)
  get_host(host)                → dict | None
  upsert_host(host, **fields)   → dict
  set_enabled(host, enabled)    → bool
  delete_host(host)             → bool
  normalize_host(value)         → str          (raises ValueError when unusable)
"""

from __future__ import annotations

import ipaddress
import threading
import time
from typing import Optional
from urllib.parse import urlparse

from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

__all__ = [
    'list_hosts',
    'enabled_hosts',
    'get_host',
    'upsert_host',
    'set_enabled',
    'delete_host',
    'normalize_host',
]

_STORE_PATH = config_path('private_hosts.json')
_STORE_VERSION = 1
_MAX_HOSTS = 64

_lock = threading.RLock()
_cache: list[dict] = []
_cache_loaded = False


def normalize_host(value: str) -> str:
    """Reduce user input to a bare lowercase hostname.

    Accepts what a user actually pastes — ``https://api.openai.com/ml/x?a=1``,
    ``AIGC.Sankuai.COM:443``, a stray trailing dot — and returns
    ``your-llm-gateway.example.com``. Unlike :func:`lib.auth_sources.normalize_domain` this
    does NOT strip a leading ``www.``: here the string is a network identity to
    be matched literally, not a site's registrable name.

    Raises:
        ValueError: when the input is blank, or is a bare IP address. A bare IP
            is refused on purpose — see the module docstring: internal LBs
            rotate addresses, and an IP entry would also be a direct route to
            naming a metadata endpoint.
    """
    raw = str(value or '').strip()
    if not raw:
        raise ValueError('host is required')
    if '://' in raw:
        raw = urlparse(raw).netloc or ''
    raw = raw.split('/')[0].split('@')[-1].strip()
    # Bracketed IPv6 literal, e.g. [::1]:443
    if raw.startswith('['):
        raw = raw[1:].split(']')[0]
    else:
        raw = raw.split(':')[0]
    raw = raw.strip().rstrip('.').lower()
    if not raw:
        raise ValueError('host is required')
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        # Not an IP literal — a hostname, which is what we want.
        logger.debug('[PrivHosts] %r is not an IP literal — treating as hostname', raw)
    else:
        raise ValueError(
            'bare IP addresses are not accepted — allowlist the HOSTNAME '
            'instead. Internal load balancers rotate their address between '
            'lookups, so an IP entry silently stops matching.')
    if '.' not in raw and raw != 'localhost':
        # A single label ('intranet') is almost always a typo or an
        # over-broad entry; require a dotted name so the intent is explicit.
        raise ValueError(f'{raw!r} is not a qualified hostname (expected e.g. host.example.com)')
    return raw


def _ensure_loaded() -> None:
    global _cache_loaded
    if _cache_loaded:
        return
    with _lock:
        if _cache_loaded:
            return
        store = read_json(_STORE_PATH, default=None)
        rows: list[dict] = []
        if isinstance(store, dict) and isinstance(store.get('hosts'), list):
            for r in store['hosts']:
                if not isinstance(r, dict) or not r.get('host'):
                    continue
                try:
                    host = normalize_host(r['host'])
                except ValueError as e:
                    # A hand-edited or legacy row we refuse to honour. Drop it
                    # rather than feed an unusable entry to the fetch guard.
                    logger.warning('[PrivHosts] dropping unusable stored host %r: %s',
                                   r.get('host'), e)
                    continue
                rows.append({
                    'host': host,
                    'label': str(r.get('label') or host)[:80],
                    'enabled': bool(r.get('enabled', True)),
                    'updated_at': float(r.get('updated_at') or 0.0),
                })
        _cache.clear()
        _cache.extend(rows)
        _cache_loaded = True
        logger.info('[PrivHosts] loaded %d host(s) from %s', len(_cache), _STORE_PATH)


def _persist() -> None:
    payload = {'version': _STORE_VERSION, 'hosts': list(_cache)}
    update_json_atomic(_STORE_PATH, lambda _: payload, default=payload)


def list_hosts() -> list[dict]:
    """All configured hosts, enabled first then alphabetical.

    Nothing is redacted — a hostname is not a secret. This is the deliberate
    contrast with :func:`lib.auth_sources.list_sources`, which must never echo
    a cookie value.
    """
    _ensure_loaded()
    with _lock:
        rows = [dict(r) for r in _cache]
    rows.sort(key=lambda r: (not r.get('enabled'), r.get('host', '')))
    return rows


def enabled_hosts() -> set[str]:
    """The set of enabled hostnames — what gets handed to tofu-search."""
    _ensure_loaded()
    with _lock:
        return {r['host'] for r in _cache if r.get('enabled') and r.get('host')}


def get_host(host: str) -> Optional[dict]:
    """Look up one row by host (normalized), or None."""
    try:
        h = normalize_host(host)
    except ValueError as e:
        logger.debug('[PrivHosts] get_host rejected %r: %s', host, e)
        return None
    _ensure_loaded()
    with _lock:
        for r in _cache:
            if r.get('host') == h:
                return dict(r)
    return None


def upsert_host(host: str, *, label: Optional[str] = None,
                enabled: Optional[bool] = None) -> dict:
    """Create or update an allowlist entry. Returns the stored row.

    Only the fields you pass are touched (None = leave unchanged). A NEW entry
    defaults to ``enabled=True`` — the user adding a host is stating intent, so
    making them flip a second switch would be busywork.

    Raises:
        ValueError: on an unusable host (blank / bare IP / single label), or
            when the entry cap is reached.
    """
    h = normalize_host(host)
    _ensure_loaded()
    with _lock:
        row = None
        for r in _cache:
            if r.get('host') == h:
                row = r
                break
        if row is None:
            if len(_cache) >= _MAX_HOSTS:
                raise ValueError(f'host quota reached (max {_MAX_HOSTS})')
            row = {'host': h, 'label': h, 'enabled': True, 'updated_at': 0.0}
            _cache.append(row)
        if label is not None:
            row['label'] = str(label).strip()[:80] or h
        if enabled is not None:
            row['enabled'] = bool(enabled)
        row['updated_at'] = time.time()
        _persist()
        snapshot = dict(row)

    audit_log('private_host_upsert', host=h, enabled=snapshot.get('enabled'))
    logger.info('[PrivHosts] upsert host=%s enabled=%s', h, snapshot.get('enabled'))
    return snapshot


def set_enabled(host: str, enabled: bool) -> bool:
    """Toggle one entry. Returns True iff it exists."""
    try:
        h = normalize_host(host)
    except ValueError as e:
        logger.debug('[PrivHosts] set_enabled rejected %r: %s', host, e)
        return False
    _ensure_loaded()
    with _lock:
        for r in _cache:
            if r.get('host') == h:
                r['enabled'] = bool(enabled)
                r['updated_at'] = time.time()
                _persist()
                audit_log('private_host_toggle', host=h, enabled=bool(enabled))
                logger.info('[PrivHosts] toggle host=%s enabled=%s', h, bool(enabled))
                return True
    return False


def delete_host(host: str) -> bool:
    """Remove an entry entirely. Idempotent; returns True iff it existed.

    Unlike auth-sources there is no default catalog to reset to — an internal
    hostname is site-specific, so nothing ships pre-listed and a deletion is a
    plain removal.
    """
    try:
        h = normalize_host(host)
    except ValueError as e:
        logger.debug('[PrivHosts] delete rejected %r: %s', host, e)
        return False
    _ensure_loaded()
    with _lock:
        for i, r in enumerate(_cache):
            if r.get('host') == h:
                _cache.pop(i)
                _persist()
                audit_log('private_host_delete', host=h)
                logger.info('[PrivHosts] delete host=%s', h)
                return True
    return False
