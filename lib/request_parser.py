"""Unified request body parsing with typed extraction.

Replaces 91 ad-hoc ``data = request.get_json(silent=True) or {}`` +
manual ``data.get('field') or default`` patterns across routes/.

Public API
----------
  parse_body(force=False)                              → dict (always)
  require_str(body, field, *, strip=True, max_len=None, allow_empty=False)
  optional_str(body, field, *, default='', strip=True, max_len=None)
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

from typing import Any, Optional


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
    Production routes run under the Flask→Quart shim from ``server.py``
    which patches ``request.get_json()`` to be sync-safe. This function
    invokes it directly. In test environments, install the shim before
    importing routes (see ``tests/test_request_parser.py`` for the
    pattern).
    """
    from flask import request
    from lib.log import get_logger
    try:
        data = request.get_json(force=force, silent=True)
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
    'BadRequest', 'parse_body',
    'require_str', 'optional_str',
    'require_int', 'optional_int',
    'require_bool', 'optional_bool',
    'require_list', 'optional_list',
    'require_dict', 'optional_dict',
]
