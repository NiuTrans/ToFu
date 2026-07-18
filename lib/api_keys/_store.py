"""lib/api_keys/_store.py — In-memory cache + disk persistence.

Owns the **single** process-wide mutable state for the API-key store:

  * ``_cache``       — the loaded rows (a list, mutated in place)
  * ``_cache_lock``  — the RLock guarding every read/write of ``_cache``

Both are defined here exactly once and re-exported by reference from
``lib.api_keys.__init__`` so ``import lib.api_keys as k; k._cache`` and
``from lib.api_keys._store import _cache`` are the *same object*.

Rebindable settings — ``_STORE_PATH``, ``_STORE_VERSION``,
``_cache_loaded`` and ``_FIRST_RUN_TOKEN_FILE`` — live on the package
facade (``lib.api_keys``). Tests patch or reassign them there
(``patch('lib.api_keys._STORE_PATH', …)``,
``api_keys._cache_loaded = False``). Because a plain ``from … import``
would snapshot the value at import time, the helpers below resolve those
names **through the facade at call time** so a test's patch/assignment is
always honoured.
"""

from __future__ import annotations

import hashlib
import threading

from lib.json_store import read_json, update_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

# ── The single source of truth for mutable cache state ────────────
# These two objects are mutated in place (``.clear()`` / ``.append()`` /
# lock acquire) so a re-export by reference keeps every caller pinned to
# the same instance. Never reassign these at module or facade level.
_cache_lock = threading.RLock()
_cache: list[dict] = []


def _facade():
    """Return the package facade module (``lib.api_keys``).

    Rebindable settings (``_STORE_PATH``, ``_cache_loaded``, …) are read
    from / written to here so test-time patches on the facade take effect.
    Imported lazily to avoid an import cycle at package-init time.
    """
    import lib.api_keys as pkg
    return pkg


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _public_view(row: dict) -> dict:
    """Return a row dict with the secret hash redacted."""
    out = dict(row)
    out.pop('secret_hash', None)
    return out


def _ensure_loaded() -> None:
    pkg = _facade()
    if pkg._cache_loaded:
        return
    with _cache_lock:
        if pkg._cache_loaded:
            return
        store = read_json(pkg._STORE_PATH, default=None)
        keys: list[dict] = []
        if isinstance(store, dict) and isinstance(store.get('keys'), list):
            keys = [k for k in store['keys'] if isinstance(k, dict)]
        _cache.clear()
        _cache.extend(keys)
        pkg._cache_loaded = True
        logger.info('[ApiKeys] loaded %d key(s) from %s', len(_cache),
                    pkg._STORE_PATH)


def _persist() -> None:
    """Write the cache back to disk atomically."""
    pkg = _facade()
    payload = {'version': pkg._STORE_VERSION, 'keys': list(_cache)}
    def _mutator(_):
        return payload
    update_json_atomic(pkg._STORE_PATH, _mutator, default=payload)
