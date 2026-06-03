"""lib/byo_providers.py — Per-API-key BYO (Bring-Your-Own) provider store.

External callers register their own LLM endpoints — typically internal
inference servers (vLLM / SGLang / Ollama) or OpenAI-compatible
proxies — and get back an opaque ``prov_id`` they can use as a model
suffix (``"deepseek-v4-pro@prov_a3f2"``) to pin runs to that endpoint.

Why a separate store from ``server_config.json``?
=================================================
Server-config providers are operator-curated and visible to every
caller in the process. BYO providers are caller-scoped:

* Created by Bearer-key holders via ``POST /api/v1/providers``.
* Indexed by the owning ``key_id`` so caller A never sees caller B's
  endpoint or, more importantly, caller B's secret.
* The api_key is held in plaintext on disk (it has to be — we proxy
  requests with it) but the file lives at
  ``data/config/byo_providers.json`` with the rest of the config and
  the API never echoes the raw key back. The list endpoint returns a
  redacted ``key_hint`` only.

Persistence
-----------
``data/config/byo_providers.json`` via :mod:`lib.json_store` (atomic,
locked)::

    {
      "version": 1,
      "providers": [
        {
          "id":              "prov_a3f2c1",
          "owner_key_id":    "k_a3f2",
          "name":            "deepseek-cluster-A",
          "base_url":        "http://10.0.0.5:8080/v1",
          "api_key":         "sk-…",            # plaintext (proxy needs it)
          "models":          [{"model_id": "deepseek-v4-pro", ...}],
          "extra_headers":   {},
          "thinking_format": "chat_template_kwargs",
          "created_at":      1701000000.0,
          "last_used_at":    null,
          "disabled":        false
        }
      ]
    }

Public API
----------
  list_providers(key_id)              → list[dict]   (no api_key field)
  get_provider(prov_id, key_id)       → dict | None  (with api_key)
  get_public(prov_id, key_id)         → dict | None  (no api_key)
  create_provider(...)                → dict
  update_provider(prov_id, key_id, **fields) → bool
  delete_provider(prov_id, key_id)    → bool
  touch_provider(prov_id)             → None         (records last_used_at)
  resolve_model_string(model_string, key_id) → ResolvedModel | None
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional

from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

__all__ = [
    'ResolvedModel',
    'list_providers',
    'get_provider',
    'get_public',
    'create_provider',
    'update_provider',
    'delete_provider',
    'touch_provider',
    'resolve_model_string',
    'redact',
]


_STORE_PATH = config_path('byo_providers.json')
_STORE_VERSION = 1
_MAX_PROVIDERS_PER_KEY = 32
_MAX_MODELS_PER_PROVIDER = 64

_lock = threading.RLock()
_cache: list[dict] = []
_cache_loaded = False


@dataclass
class ResolvedModel:
    """Output of :func:`resolve_model_string`.

    ``provider`` is None when the model string did not include a
    ``@prov_…`` suffix — callers fall back to the global slot pool.
    """
    model_id: str
    provider: Optional[dict]  # full row (incl. api_key) or None


def _ensure_loaded() -> None:
    global _cache_loaded
    if _cache_loaded:
        return
    with _lock:
        if _cache_loaded:
            return
        store = read_json(_STORE_PATH, default=None)
        rows: list[dict] = []
        if isinstance(store, dict) and isinstance(store.get('providers'), list):
            rows = [r for r in store['providers'] if isinstance(r, dict)]
        _cache.clear()
        _cache.extend(rows)
        _cache_loaded = True
        logger.info('[BYOProv] loaded %d row(s) from %s', len(_cache), _STORE_PATH)


def _persist() -> None:
    payload = {'version': _STORE_VERSION, 'providers': list(_cache)}
    update_json_atomic(_STORE_PATH, lambda _: payload, default=payload)


def redact(row: dict) -> dict:
    """Return a copy with the secret api_key replaced by a hint."""
    out = dict(row)
    raw = out.pop('api_key', '') or ''
    if raw:
        out['key_hint'] = (raw[:6] + '…' + raw[-2:]) if len(raw) > 12 else '••••'
    else:
        out['key_hint'] = ''
    return out


def list_providers(owner_key_id: str) -> list[dict]:
    """All providers registered by this Bearer key, newest first."""
    _ensure_loaded()
    with _lock:
        rows = [r for r in _cache if r.get('owner_key_id') == owner_key_id]
    rows.sort(key=lambda r: r.get('created_at', 0), reverse=True)
    return [redact(r) for r in rows]


def get_provider(prov_id: str, owner_key_id: str) -> Optional[dict]:
    """Internal lookup — returns the FULL row including api_key.

    Use :func:`get_public` for any response the user sees.
    """
    _ensure_loaded()
    with _lock:
        for r in _cache:
            if r.get('id') == prov_id and r.get('owner_key_id') == owner_key_id:
                return dict(r)
    return None


def get_public(prov_id: str, owner_key_id: str) -> Optional[dict]:
    """User-facing lookup — api_key replaced with key_hint."""
    row = get_provider(prov_id, owner_key_id)
    return redact(row) if row else None


def _validate_models(models) -> list[dict]:
    if not isinstance(models, list):
        raise ValueError('models must be a list')
    if len(models) > _MAX_MODELS_PER_PROVIDER:
        raise ValueError(f'too many models (max {_MAX_MODELS_PER_PROVIDER})')
    out = []
    for i, m in enumerate(models):
        if not isinstance(m, dict):
            raise ValueError(f'models[{i}] must be an object')
        mid = (m.get('model_id') or m.get('id') or '').strip()
        if not mid:
            raise ValueError(f'models[{i}].model_id is required')
        entry = {'model_id': mid}
        caps = m.get('capabilities')
        if isinstance(caps, list):
            entry['capabilities'] = [str(c) for c in caps if c]
        for opt_key in ('rpm', 'cost', 'latency'):
            if opt_key in m:
                try:
                    entry[opt_key] = float(m[opt_key])
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        f'models[{i}].{opt_key} must be numeric: {e}') from e
        out.append(entry)
    return out


def _validate_base_url(url: str) -> str:
    url = (url or '').strip().rstrip('/')
    if not url:
        raise ValueError('base_url is required')
    if not (url.startswith('http://') or url.startswith('https://')):
        raise ValueError('base_url must start with http:// or https://')
    return url


def _validate_thinking_format(value) -> str:
    """Normalise + validate a ``thinking_format`` field.

    Accepts None / missing as ''. Anything else MUST be one of the
    values in :data:`lib.llm_dispatch.slot.THINKING_FORMATS`.
    """
    if value is None or value == '':
        return ''
    if not isinstance(value, str):
        raise ValueError('thinking_format must be a string')
    from lib.llm_dispatch.provider_registry import is_valid_thinking_format
    from lib.llm_dispatch.slot import THINKING_FORMATS
    norm = value.strip()
    if not is_valid_thinking_format(norm):
        raise ValueError(
            'thinking_format=%r is not one of %s (nor a registered '
            'tofu.providers plugin dialect)' % (
                value, sorted(THINKING_FORMATS)))
    return norm


def create_provider(*, owner_key_id: str, name: str, base_url: str,
                    api_key: str, models: list,
                    extra_headers: Optional[dict] = None,
                    thinking_format: str = '') -> dict:
    """Register a new provider for this Bearer key.

    Returns the FULL row (callers that want to display it should pass
    it through :func:`redact`). Raises ``ValueError`` on validation
    failure and ``RuntimeError`` when the per-key cap is reached.

    ``thinking_format`` pins the body-shape dialect this engine speaks.
    Empty string (default) means auto-detect from the model name.
    Operators usually let :func:`probe_provider` populate this from
    ``/v1/models``'s ``owned_by`` field, so manual config is the
    exception, not the rule.
    """
    if not owner_key_id:
        raise ValueError('owner_key_id is required')
    name = (name or '').strip()[:80]
    if not name:
        raise ValueError('name is required')
    base_url = _validate_base_url(base_url)
    api_key = (api_key or '').strip()
    models_clean = _validate_models(models)
    headers_clean = dict(extra_headers or {})
    thinking_format = _validate_thinking_format(thinking_format)

    _ensure_loaded()
    with _lock:
        owned = [r for r in _cache if r.get('owner_key_id') == owner_key_id]
        if len(owned) >= _MAX_PROVIDERS_PER_KEY:
            raise RuntimeError(
                f'provider quota reached '
                f'({_MAX_PROVIDERS_PER_KEY} per API key)')
        prov_id = 'prov_' + secrets.token_hex(4)
        row = {
            'id': prov_id,
            'owner_key_id': owner_key_id,
            'name': name,
            'base_url': base_url,
            'api_key': api_key,
            'models': models_clean,
            'extra_headers': headers_clean,
            'thinking_format': thinking_format,
            'created_at': time.time(),
            'last_used_at': None,
            'disabled': False,
        }
        _cache.append(row)
        _persist()
    audit_log('byo_provider_created', prov_id=prov_id,
              owner_key_id=owner_key_id, base_url=base_url,
              n_models=len(models_clean))
    logger.info('[BYOProv] created %s owner=%s url=%s n_models=%d',
                prov_id, owner_key_id, base_url, len(models_clean))
    return dict(row)


_UPDATABLE = frozenset({
    'name', 'base_url', 'api_key', 'models', 'extra_headers',
    'thinking_format', 'disabled',
})


def update_provider(prov_id: str, owner_key_id: str, **fields) -> bool:
    """Mutate a provider row. Returns True iff the row exists and is owned.

    Unknown fields are silently dropped (forward-compat with future
    columns).
    """
    _ensure_loaded()
    with _lock:
        for row in _cache:
            if (row.get('id') != prov_id
                    or row.get('owner_key_id') != owner_key_id):
                continue
            changed = {}
            for k, v in fields.items():
                if k not in _UPDATABLE:
                    continue
                if k == 'name':
                    v = (v or '').strip()[:80]
                    if not v:
                        continue
                elif k == 'base_url':
                    v = _validate_base_url(v)
                elif k == 'api_key':
                    v = (v or '').strip()
                elif k == 'models':
                    v = _validate_models(v)
                elif k == 'extra_headers':
                    if not isinstance(v, dict):
                        continue
                    v = dict(v)
                elif k == 'thinking_format':
                    v = _validate_thinking_format(v)
                elif k == 'disabled':
                    v = bool(v)
                if row.get(k) != v:
                    row[k] = v
                    changed[k] = (k if k == 'api_key' else v)  # don't audit secrets
            if changed:
                _persist()
                audit_log('byo_provider_updated', prov_id=prov_id,
                          owner_key_id=owner_key_id,
                          fields=list(changed.keys()))
                logger.info('[BYOProv] updated %s fields=%s', prov_id,
                            list(changed.keys()))
            return True
    return False


def delete_provider(prov_id: str, owner_key_id: str) -> bool:
    """Remove a provider. Idempotent — returns True iff a row was removed."""
    _ensure_loaded()
    with _lock:
        for i, row in enumerate(_cache):
            if (row.get('id') == prov_id
                    and row.get('owner_key_id') == owner_key_id):
                _cache.pop(i)
                _persist()
                audit_log('byo_provider_deleted', prov_id=prov_id,
                          owner_key_id=owner_key_id)
                logger.info('[BYOProv] deleted %s owner=%s', prov_id,
                            owner_key_id)
                return True
    return False


def touch_provider(prov_id: str) -> None:
    """Best-effort update of ``last_used_at``. No fsync."""
    if not prov_id:
        return
    _ensure_loaded()
    with _lock:
        for row in _cache:
            if row.get('id') == prov_id:
                row['last_used_at'] = time.time()
                # Persistence happens lazily on the next mutation; the
                # last_used_at field is for UI display only.
                return


def resolve_model_string(model: str, owner_key_id: str) -> Optional[ResolvedModel]:
    """Parse a model string of the form ``"name"`` or ``"name@prov_xxx"``.

    Returns:
        :class:`ResolvedModel` with ``provider=None`` for plain
        ``"name"`` strings (caller falls back to the global slot pool).
        Returns ``None`` when the ``@prov_xxx`` suffix is present but
        the provider doesn't exist or isn't owned by ``owner_key_id``
        — the caller should map this to a 404.
    """
    if not model or '@' not in model:
        return ResolvedModel(model_id=(model or '').strip(), provider=None)
    name, _, suffix = model.rpartition('@')
    name = name.strip()
    suffix = suffix.strip()
    if not suffix.startswith('prov_'):
        # ``foo@1.0`` style version tag — not our suffix; pass through
        # untouched so the dispatcher's own model-aliasing handles it.
        return ResolvedModel(model_id=model.strip(), provider=None)
    if not name:
        return None
    row = get_provider(suffix, owner_key_id)
    if row is None or row.get('disabled'):
        return None
    return ResolvedModel(model_id=name, provider=row)
