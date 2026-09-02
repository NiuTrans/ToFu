"""Unified request body parsing with typed extraction.

Replaces 91 ad-hoc ``data = request.get_json(silent=True) or {}`` +
manual ``data.get('field') or default`` patterns across routes/.

Public API
----------
  parse_body(force=False)                              → dict (always)
  require_str(body, field, *, strip=True, max_len=None, allow_empty=False)
  optional_str(body, field, *, default='', strip=True, max_len=None)
  query_str(args, field, *, default='', strip=True)
  require_int(body, field, *, min=None, max=None)
  optional_int(body, field, *, default=None, min=None, max=None)
  require_bool(body, field)
  optional_bool(body, field, *, default=False)
  require_list(body, field, *, item_type=None, max_len=None)
  optional_list(body, field, *, default=None, item_type=None, max_len=None)
  require_dict(body, field)
  optional_dict(body, field, *, default=None)

All ``require_*`` raise ``BadRequest('field required')`` on missing.
``optional_*`` return ``default`` on missing. All raise ``BadRequest``
on type-mismatch (``"field must be a string"``, etc.).

The route layer's ``@safe_route`` decorator (``lib.api_response``) and
the global ``BadRequest`` errorhandler in ``server.py`` convert
``BadRequest`` into a 400 response automatically.

Design philosophy
-----------------
This is intentionally NOT a schema validation library (no pydantic,
marshmallow, etc.). Routes that need full schema validation can keep
using their own ad-hoc checks; this module covers the 90% case of
"give me this field as a string with a default" cleanly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Optional

from werkzeug.exceptions import HTTPException

# Encoded tokens that betray a still-percent-encoded value: a path separator
# (%2f '/', %5c '\') or a bare percent (%25) that a proxy left behind.
_ENCODED_MARKERS = ('%2f', '%5c', '%25')
_MAX_REDECODE_PASSES = 3


def decode_proxy_path_arg(name: str = 'path', *,
                          default: str = '') -> str:
    """Read a filesystem-path query arg, defensively undoing DOUBLE-encoding.

    A reverse proxy in front of the app — notably the VS Code web IDE proxy
    (``/proxy/<port>/``) — can RE-ENCODE an already-percent-encoded query
    value. The frontend Api client encodes a path exactly once
    (``/mnt/x`` → ``%2Fmnt%2Fx``); the proxy turns that into ``%252Fmnt%252Fx``.
    Quart then decodes ONCE, so the route receives the literal string
    ``%2Fmnt%2Fx`` — which matches no DB rows and blanks the Project Brain
    panel even though the data exists. Scalar params (``since=0``, ``limit=20``)
    have no ``%`` so the proxy re-encode is a no-op for them; only path-valued
    args break, which is why this is applied specifically to path args.

    Fix: while the value STILL looks percent-encoded (contains ``%2f`` / ``%5c``
    / ``%25``, case-insensitive) AND does NOT already resolve to an existing
    filesystem path, ``unquote`` it again — bounded to a few passes.

    Edge case (documented tradeoff): a real directory whose name contains a
    literal ``%2f`` / ``%25`` substring would be over-decoded by a naive
    heuristic. The ``os.path.exists`` short-circuit fixes the common form of
    that — if the current value is already a real path we stop immediately,
    even when it contains an encoded marker. The residual risk (a
    NON-existent target path that legitimately contains ``%2f``, e.g. a
    board/charter key for a project dir that isn't mounted on THIS host) is
    accepted as vanishingly rare: absolute POSIX/Windows paths do not contain
    literal ``%2f`` sequences in practice, and the loop is hard-bounded to
    ``_MAX_REDECODE_PASSES`` so it can never spin.
    """
    from urllib.parse import unquote

    from quart import request
    raw = (request.args.get(name) or '').strip()
    if not raw:
        return default
    for _ in range(_MAX_REDECODE_PASSES):
        low = raw.lower()
        if not any(m in low for m in _ENCODED_MARKERS):
            break
        # Already a real path? Then the encoded marker is a genuine part of the
        # name — stop, don't corrupt it.
        try:
            if os.path.exists(raw):
                break
        except (OSError, ValueError) as _e:
            from lib.log import get_logger
            get_logger(__name__).debug(
                '[request_parser] decode_proxy_path_arg os.path.exists(%r) raised %s: %s',
                raw, type(_e).__name__, _e)
            pass  # unusual path value; fall through to decode attempt
        decoded = unquote(raw)
        if decoded == raw:
            break
        raw = decoded.strip()
    return raw


class BadRequest(ValueError):
    """Raised by request_parser when a field is missing/wrong type.

    Carries a ``.field`` attribute (empty string when not field-scoped)
    that ``@safe_route`` and ``api_response._normalize_error`` both read
    when converting the exception to a 400 response.
    """
    def __init__(self, message: str, *, field: str = ''):
        super().__init__(message)
        self.field = field

    def to_envelope(self) -> dict:
        env = {'kind': 'bad_request', 'detail': str(self)}
        if self.field:
            env['field'] = self.field
        return env


# ── Body parsing ───────────────────────────────────────────────

def parse_body(*, force: bool = False) -> dict:
    """Parse the current request's JSON body into a dict.

    Always returns a dict. Empty body → empty dict. Non-dict body
    (top-level JSON list/string) → raises ``BadRequest``.

    Parameters
    ----------
    force : bool
        If True, parse even when ``Content-Type`` is not ``application/json``.

    Notes
    -----
    Legacy synchronous routes run in Quart's executor and cross the request
    body boundary through :mod:`lib.quart_sync`. New async routes should use
    :func:`async_parse_body`.
    """
    from lib.quart_sync import request_json
    from lib.log import get_logger
    try:
        data = request_json(force=force, silent=True)
    except HTTPException:
        # Size/time policy exceptions are HTTP boundary decisions, not
        # malformed optional JSON.  Swallowing them turns an intentional 413
        # into a misleading empty-body 400 and lets callers retry harder.
        raise
    except Exception as e:
        # Outside-request-context, malformed Content-Type, etc. Don't
        # silently swallow — log so misuse is debuggable. We still return
        # {} because the contract is "empty body → empty dict".
        get_logger(__name__).debug(
            '[request_parser] parse_body get_json raised %s: %s',
            type(e).__name__, e)
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise BadRequest('Request body must be a JSON object')
    return data


async def async_parse_body(*, force: bool = False) -> dict:
    """Async-native version of :func:`parse_body` for ``async def`` handlers.

    A native-async handler runs ON the event loop, where the sync
    ``parse_body`` shim's ``asyncio.run()`` fallback would raise
    ``RuntimeError: asyncio.run() cannot be called from a running event
    loop``. This awaits Quart's genuinely-async ``request.get_json()`` directly.

    Same contract as ``parse_body``: always returns a dict; empty body → {};
    top-level non-dict JSON → raises ``BadRequest``.
    """
    from quart import request
    from lib.log import get_logger
    try:
        data = await request.get_json(force=force, silent=True)
    except HTTPException:
        raise
    except Exception as e:
        get_logger(__name__).debug(
            '[request_parser] async_parse_body get_json raised %s: %s',
            type(e).__name__, e)
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise BadRequest('Request body must be a JSON object')
    return data


# ── String accessors ───────────────────────────────────────────

def require_str(body: dict, field: str, *,
                strip: bool = True, max_len: Optional[int] = None,
                allow_empty: bool = False) -> str:
    """Extract a required string field. Raises BadRequest if missing/empty."""
    if field not in body:
        raise BadRequest(f'{field} is required', field=field)
    val = body[field]
    if val is None:
        raise BadRequest(f'{field} is required', field=field)
    if not isinstance(val, str):
        raise BadRequest(f'{field} must be a string', field=field)
    if strip:
        val = val.strip()
    if not val and not allow_empty:
        raise BadRequest(f'{field} is required', field=field)
    if max_len is not None and len(val) > max_len:
        raise BadRequest(f'{field} too long (max {max_len} chars)',
                          field=field)
    return val


def optional_str(body: dict, field: str, *,
                 default: str = '', strip: bool = True,
                 max_len: Optional[int] = None) -> str:
    """Extract an optional string field. Returns default if missing/None."""
    val = body.get(field)
    if val is None:
        return default
    if not isinstance(val, str):
        raise BadRequest(f'{field} must be a string', field=field)
    if strip:
        val = val.strip()
    if max_len is not None and len(val) > max_len:
        raise BadRequest(f'{field} too long (max {max_len} chars)',
                          field=field)
    return val


def query_str(args: Mapping[str, Any], field: str, *,
              default: str = '', strip: bool = True) -> str:
    """Read one optional query-string value from an injected mapping.

    Flask/Quart query values are strings, but focused adapters and tests may
    supply generic mappings. Non-string values therefore use ``default``
    instead of leaking coercion policy into each route module.
    """
    val = args.get(field)
    if not isinstance(val, str):
        return default
    return val.strip() if strip else val


# ── Integer accessors ──────────────────────────────────────────

def _coerce_int(val: Any, field: str) -> int:
    if isinstance(val, bool):  # bool is subclass of int — reject explicitly
        raise BadRequest(f'{field} must be an integer', field=field)
    if isinstance(val, int):
        return val
    if isinstance(val, float) and val.is_integer():
        return int(val)
    if isinstance(val, str):
        try:
            return int(val.strip())
        except (ValueError, TypeError):
            raise BadRequest(f'{field} must be an integer',
                              field=field) from None
    raise BadRequest(f'{field} must be an integer', field=field)


def require_int(body: dict, field: str, *,
                min: Optional[int] = None,
                max: Optional[int] = None) -> int:
    """Extract a required int field. Coerces stringified ints."""
    if field not in body or body[field] is None:
        raise BadRequest(f'{field} is required', field=field)
    n = _coerce_int(body[field], field)
    if min is not None and n < min:
        raise BadRequest(f'{field} must be >= {min}', field=field)
    if max is not None and n > max:
        raise BadRequest(f'{field} must be <= {max}', field=field)
    return n


def optional_int(body: dict, field: str, *,
                 default: Optional[int] = None,
                 min: Optional[int] = None,
                 max: Optional[int] = None) -> Optional[int]:
    """Extract an optional int field. Returns default on missing/None."""
    val = body.get(field)
    if val is None:
        return default
    n = _coerce_int(val, field)
    if min is not None and n < min:
        raise BadRequest(f'{field} must be >= {min}', field=field)
    if max is not None and n > max:
        raise BadRequest(f'{field} must be <= {max}', field=field)
    return n


# ── Boolean accessors ──────────────────────────────────────────

def _coerce_bool(val: Any, field: str) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ('true', '1', 'yes', 'on'):
            return True
        if v in ('false', '0', 'no', 'off', ''):
            return False
        raise BadRequest(f'{field} must be a boolean', field=field)
    raise BadRequest(f'{field} must be a boolean', field=field)


def require_bool(body: dict, field: str) -> bool:
    if field not in body or body[field] is None:
        raise BadRequest(f'{field} is required', field=field)
    return _coerce_bool(body[field], field)


def optional_bool(body: dict, field: str, *, default: bool = False) -> bool:
    val = body.get(field)
    if val is None:
        return default
    return _coerce_bool(val, field)


# ── List accessors ─────────────────────────────────────────────

def _check_list(val: Any, field: str, *, item_type: Optional[type],
                 max_len: Optional[int]) -> list:
    if not isinstance(val, list):
        raise BadRequest(f'{field} must be a list', field=field)
    if max_len is not None and len(val) > max_len:
        raise BadRequest(f'{field} too long (max {max_len} items)',
                          field=field)
    if item_type is not None:
        for i, item in enumerate(val):
            if not isinstance(item, item_type):
                raise BadRequest(
                    f'{field}[{i}] must be a {item_type.__name__}',
                    field=field)
    return val


def require_list(body: dict, field: str, *,
                 item_type: Optional[type] = None,
                 max_len: Optional[int] = None) -> list:
    if field not in body or body[field] is None:
        raise BadRequest(f'{field} is required', field=field)
    return _check_list(body[field], field,
                        item_type=item_type, max_len=max_len)


def optional_list(body: dict, field: str, *,
                  default: Optional[list] = None,
                  item_type: Optional[type] = None,
                  max_len: Optional[int] = None) -> list:
    val = body.get(field)
    if val is None:
        return default if default is not None else []
    return _check_list(val, field, item_type=item_type, max_len=max_len)


# ── Dict accessors ─────────────────────────────────────────────

def require_dict(body: dict, field: str) -> dict:
    if field not in body or body[field] is None:
        raise BadRequest(f'{field} is required', field=field)
    val = body[field]
    if not isinstance(val, dict):
        raise BadRequest(f'{field} must be an object', field=field)
    return val


def optional_dict(body: dict, field: str, *,
                  default: Optional[dict] = None) -> dict:
    val = body.get(field)
    if val is None:
        return default if default is not None else {}
    if not isinstance(val, dict):
        raise BadRequest(f'{field} must be an object', field=field)
    return val


__all__ = [
    'BadRequest', 'parse_body', 'async_parse_body', 'decode_proxy_path_arg',
    'require_str', 'optional_str', 'query_str',
    'require_int', 'optional_int',
    'require_bool', 'optional_bool',
    'require_list', 'optional_list',
    'require_dict', 'optional_dict',
]
