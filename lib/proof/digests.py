"""Canonical content addressing for proof bundles.

One canonical form: orjson with sorted object keys (UTF-8, compact). Every
digest in a bundle is ``sha256:<hex>`` over those bytes. Assembly and
verification share this module, so a digest is always exactly recomputable
on either side.
"""
from __future__ import annotations

import hashlib

import orjson

_DIGEST_PREFIX = 'sha256:'


def canonical_bytes(value) -> bytes:
    """Serialize one JSON-pure value to the canonical digest input."""
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def sha256_digest(value) -> str:
    """Content address of one JSON-pure value."""
    return _DIGEST_PREFIX + hashlib.sha256(canonical_bytes(value)).hexdigest()


def bundle_id(bundle_without_id: dict) -> str:
    """Content address of a whole bundle.

    The caller passes the bundle WITHOUT its ``bundleId`` field; verification
    strips the field the same way, so the address covers every other byte.
    """
    return sha256_digest(bundle_without_id)
