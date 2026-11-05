"""Billing scalar validation and raw-document decoding.

Shared only by SQL billing implementations; no storage or configuration IO."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.storage.errors import StorageError

import orjson


def _text(payload: Mapping[str, Any], key: str, maximum: int = 512,
          *, required: bool = True) -> str:
    value = payload.get(key, '')
    if not isinstance(value, str) or len(value) > maximum:
        raise StorageError(
            'database_protocol_error', f'Invalid {key} in billing request')
    if required and not value:
        raise StorageError(
            'database_protocol_error', f'Missing {key} in billing request')
    return value


def _integer(payload: Mapping[str, Any], key: str, *, minimum: int | None = None,
             maximum: int = 9_223_372_036_854_775_807) -> int:
    value = payload.get(key)
    if (not isinstance(value, int) or isinstance(value, bool)
            or value > maximum or (minimum is not None and value < minimum)):
        raise StorageError(
            'database_protocol_error', f'Invalid {key} in billing request')
    return value


def _decode_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        decoded = orjson.loads(value or '{}')
    except (TypeError, orjson.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}
