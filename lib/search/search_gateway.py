"""lib/search/search_gateway.py — Search-gateway settings: catalog, persistence, key.

A search gateway is one keyed endpoint fronting several commercial search APIs
(tofu-search ``search/engines/gateway.py`` speaks its wire protocol). This
module owns the host side of that capability:

* **Catalog** — gateway descriptions (endpoint, key label, selectable sources)
  merged by ``id`` from ``bundles/*/search_gateways/*.json`` and then
  ``<config_path>/search_gateways/*.json``. Company endpoints live only in
  release-tiered bundles, so the core tree ships no gateway at all.
* **Persisted shape** — ``server_config.json`` ``search.gateway`` holds
  ``{enabled, id, endpoint?, sources, full_content, free_engines,
  credential_vault}`` and never a plaintext key.
* **Key** — the credentials vault entry ``search_gateway_key``; the Settings UI
  sees only ``has_key`` and a hint.
* **Engine projection** — :func:`engine_settings` produces the tofu-search
  ``configure()`` fields; a disabled or keyless gateway yields an inactive one.

Alternatives rejected: reusing the gateway's model-provider key from
model-routing v2 (that aggregate releases plaintext only for one outbound
model-credential resolution, so search would cross its authority boundary),
and a plaintext key in ``server_config.json`` (exports and backups carry it).

TODO(enterprise): the gateway key and settings are deployment-wide, like the
rest of ``server_config.json`` ``search``; per-tenant billing needs an
owner-scoped credential passed per request instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from lib.config_dir import config_path
from lib.error_envelope import BadRequest
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'FREE_ENGINE_POLICIES',
    'SEARCH_GATEWAY_VAULT_ENTRY',
    'catalog_payload',
    'engine_settings',
    'load_gateway_catalog',
    'prepare_gateway_update',
    'public_gateway_settings',
    'store_gateway_key',
]

SEARCH_GATEWAY_VAULT_ENTRY = 'search_gateway_key'
FREE_ENGINE_POLICIES = ('fallback', 'parallel', 'off')
_MAX_SOURCES = 32
_MAX_KEY_BYTES = 8192  # the credentials vault's value bound
_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,63}$')
_SOURCE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$')
_BUNDLES_ROOT = Path(__file__).resolve().parents[2] / 'bundles'


def _valid_endpoint(value) -> str:
    endpoint = str(value or '').strip()
    parsed = urlparse(endpoint)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password or len(endpoint) > 2048):
        raise ValueError('endpoint must be an http(s) URL without credentials')
    return endpoint


def _normalize_catalog_entry(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError('gateway description must be an object')
    gateway_id = str(raw.get('id') or '').strip()
    if not _ID_RE.match(gateway_id):
        raise ValueError('gateway id must be a lowercase identifier')
    sources = []
    for source in raw.get('sources') or []:
        if not isinstance(source, dict) or not _SOURCE_RE.match(str(source.get('id') or '')):
            raise ValueError('every source needs a valid id')
        sources.append({
            'id': source['id'],
            'name': str(source.get('name') or source['id'])[:40],
            'default': bool(source.get('default', False)),
            'note': str(source.get('note') or '')[:200],
        })
    if not sources:
        raise ValueError('gateway declares no sources')
    return {
        'id': gateway_id,
        'name': str(raw.get('name') or gateway_id)[:80],
        'endpoint': _valid_endpoint(raw.get('endpoint')),
        'key_label': str(raw.get('key_label') or 'API Key')[:40],
        'key_hint': str(raw.get('key_hint') or '')[:200],
        'docs_url': str(raw.get('docs_url') or '')[:2048],
        'sources': sources,
    }


def _catalog_directories() -> list[Path]:
    directories = []
    if _BUNDLES_ROOT.is_dir():
        directories.extend(bundle / 'search_gateways' for bundle in sorted(_BUNDLES_ROOT.iterdir())
                           if (bundle / 'search_gateways').is_dir())
    directories.append(Path(config_path('search_gateways')))
    return directories


def load_gateway_catalog() -> dict[str, dict]:
    """Return gateway descriptions by id; later layers win, bad files are skipped."""
    catalog: dict[str, dict] = {}
    for directory in _catalog_directories():
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob('*.json')):
            try:
                entry = _normalize_catalog_entry(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, ValueError) as exc:
                logger.warning('[SearchGateway] skipping gateway description %s: %s',
                               path.name, exc)
                continue
            catalog[entry['id']] = entry
    return catalog


def catalog_payload() -> list[dict]:
    """Catalog entries for the Settings UI, ordered by id."""
    catalog = load_gateway_catalog()
    return [catalog[key] for key in sorted(catalog)]


def _saved_gateway(saved_search) -> dict:
    gateway = saved_search.get('gateway') if isinstance(saved_search, dict) else None
    return gateway if isinstance(gateway, dict) else {}


def _vault_key() -> str:
    from lib.credentials_vault import get_entry
    return get_entry(SEARCH_GATEWAY_VAULT_ENTRY) or ''


def _vault_hint() -> str:
    from lib.credentials_vault import list_entries
    for entry in list_entries():
        if entry.get('name') == SEARCH_GATEWAY_VAULT_ENTRY:
            return str(entry.get('hint') or '')
    return ''


def prepare_gateway_update(incoming) -> tuple[dict, str]:
    """Validate a Settings payload; return ``(persisted_shape, new_plaintext_key)``.

    The plaintext key (``api_key``) is returned separately for the caller to
    store in the vault; an empty key keeps the stored one.

    Raises:
        BadRequest: the payload is malformed.
    """
    if not isinstance(incoming, dict):
        raise BadRequest('must be an object')
    gateway_id = str(incoming.get('id') or '').strip()
    if gateway_id and not _ID_RE.match(gateway_id):
        raise BadRequest('id must be a lowercase identifier', field='id')
    raw_sources = incoming.get('sources') or []
    if not isinstance(raw_sources, list):
        raise BadRequest('sources must be a list', field='sources')
    sources = list(dict.fromkeys(str(s).strip() for s in raw_sources if str(s).strip()))
    if len(sources) > _MAX_SOURCES or any(not _SOURCE_RE.match(s) for s in sources):
        raise BadRequest('sources must be at most %d valid source ids' % _MAX_SOURCES,
                         field='sources')
    free_engines = str(incoming.get('free_engines') or 'fallback').strip()
    if free_engines not in FREE_ENGINE_POLICIES:
        raise BadRequest('free_engines must be one of %s' % ', '.join(FREE_ENGINE_POLICIES),
                         field='free_engines')
    for flag in ('enabled', 'full_content'):
        if not isinstance(incoming.get(flag, False), bool):
            raise BadRequest('%s must be a boolean' % flag, field=flag)
    persisted = {
        'enabled': incoming.get('enabled', False),
        'id': gateway_id,
        'sources': sources,
        'full_content': incoming.get('full_content', False),
        'free_engines': free_engines,
        'credential_vault': SEARCH_GATEWAY_VAULT_ENTRY,
    }
    # A gateway this build has no catalog entry for (an export imported from
    # another deployment) is kept; without an endpoint it simply stays inactive.
    if gateway_id not in load_gateway_catalog() and incoming.get('endpoint'):
        try:
            persisted['endpoint'] = _valid_endpoint(incoming.get('endpoint'))
        except ValueError as exc:
            raise BadRequest(str(exc), field='endpoint') from exc
    key = incoming.get('api_key') or ''
    if not isinstance(key, str):
        raise BadRequest('api_key must be a string', field='api_key')
    if len(key.strip().encode('utf-8')) > _MAX_KEY_BYTES:
        raise BadRequest('api_key exceeds %d bytes' % _MAX_KEY_BYTES, field='api_key')
    return persisted, key.strip()


def store_gateway_key(key: str) -> None:
    """Encrypt the gateway key into the credentials vault.

    Raises:
        BadRequest: the vault rejects the value.
    """
    from lib.credentials_vault import set_entry
    set_entry(SEARCH_GATEWAY_VAULT_ENTRY, key, note='search gateway API key')


def _endpoint_for(gateway: dict) -> str:
    entry = load_gateway_catalog().get(str(gateway.get('id') or ''))
    if entry:
        return entry['endpoint']
    try:
        return _valid_endpoint(gateway.get('endpoint'))
    except ValueError:
        return ''


def public_gateway_settings(saved_search) -> dict:
    """The Settings UI projection: saved choices plus key presence, never the key."""
    gateway = _saved_gateway(saved_search)
    hint = _vault_hint()
    return {
        'enabled': bool(gateway.get('enabled', False)),
        'id': str(gateway.get('id') or ''),
        'endpoint': str(gateway.get('endpoint') or ''),
        'sources': [str(s) for s in gateway.get('sources') or []],
        'full_content': bool(gateway.get('full_content', False)),
        'free_engines': (gateway.get('free_engines')
                         if gateway.get('free_engines') in FREE_ENGINE_POLICIES else 'fallback'),
        'has_key': bool(hint),
        'key_hint': hint,
    }


def engine_settings(saved_search) -> dict:
    """tofu-search ``configure()`` fields for the saved gateway (inactive when unusable)."""
    gateway = _saved_gateway(saved_search)
    policy = gateway.get('free_engines')
    settings = {
        'gateway_url': '',
        'gateway_api_key': '',
        'gateway_sources': [],
        'gateway_full_content': bool(gateway.get('full_content', False)),
        'free_engines': policy if policy in FREE_ENGINE_POLICIES else 'fallback',
    }
    sources = [str(s) for s in gateway.get('sources') or [] if _SOURCE_RE.match(str(s))]
    if not gateway.get('enabled') or not sources:
        return settings
    endpoint = _endpoint_for(gateway)
    key = _vault_key()
    if not endpoint or not key:
        logger.warning('[SearchGateway] enabled gateway %r is inactive: %s',
                       gateway.get('id') or 'custom',
                       'no endpoint' if not endpoint else 'no key in the credential vault')
        return settings
    settings.update(gateway_url=endpoint, gateway_api_key=key, gateway_sources=sources)
    return settings
