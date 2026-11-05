"""Wire projection and request/error vocabulary shared by both Python transports.

No I/O or server imports. Paths and envelopes follow API v4; callers retain the
original error body while exposing canonical SDK metadata.
"""
from __future__ import annotations

from collections.abc import Mapping
import math
import re
from typing import Any, Optional

_DECIMAL_SEQUENCE = re.compile(r'[+-]?[0-9]+')


class TofuError(RuntimeError):
    """HTTP failure or a typed local transport rejection (see ``body``)."""
    def __init__(self, status: int, body: Any):
        super().__init__(f'Tofu API error {status}: {body!r}')
        self.status = status
        self.body = body
        payload = body if isinstance(body, dict) else {}
        nested = payload.get('error')
        self.kind = str((nested.get('kind') if isinstance(nested, dict) else '')
                        or payload.get('error_kind') or payload.get('code') or '')
        self.retryable = status == 429 or status >= 500
        seconds = _retry_seconds(payload.get('retry_after'))
        self.retry_after = seconds or None


def _retry_seconds(value: Any) -> float:
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


def retry_delay(attempt: int, base: float, retry_after: Any = None, *, cap: float = 30.0) -> float:
    return min(max(_retry_seconds(retry_after), base * (2 ** attempt)), cap)


def task_terminal(payload: dict) -> bool:
    return (payload.get('type') in ('done', 'error', 'aborted')
            or payload.get('event') in ('done', 'error', 'aborted'))


def parse_sequence(value: Any) -> int | None:
    """Accept integer values/decimal IDs, never fabricate replay progress."""
    if isinstance(value, str):
        if _DECIMAL_SEQUENCE.fullmatch(value.strip()):
            try:
                return int(value)
            except ValueError:  # Includes the interpreter's decimal conversion limit.
                pass
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    return None


def error_body(response):
    try:
        return response.json()
    except (ValueError, RecursionError):
        return response.text


def _api_v4_path(path: str) -> str:
    if path.startswith('/api/v1/'):
        return '/api/v4/' + path[len('/api/v1/'):]
    return path

def _unwrap_envelope(body: Any) -> Any:
    if (isinstance(body, dict) and 'data' in body
            and isinstance(body.get('meta'), dict)):
        data = body['data']
        # Preserve the established public client result, not the wire wrapper.
        # An explicit domain flag wins; failures never call this normalizer.
        return {'ok': True, **data} if isinstance(data, dict) else data
    return body

def _native_model_payload(model: Mapping[str, str]) -> dict[str, str]:
    """Copy and shape-check the two native v2 selection forms."""
    if not isinstance(model, Mapping):
        raise TypeError(
            'model must be {creator_id, model_id} or '
            '{provider_id, offering_id}')
    payload = {str(key): '' if value is None else str(value).strip()
               for key, value in model.items()}
    if set(payload) not in (
        {'creator_id', 'model_id'},
        {'provider_id', 'offering_id'},
    ) or not all(payload.values()):
        raise ValueError(
            'model must contain exactly non-empty creator_id+model_id or '
            'provider_id+offering_id')
    return payload

def _native_agent_payload(
    *,
    messages: list,
    model: Mapping[str, str] | None = None,
    routing: Optional[Mapping[str, Any]] = None,
    model_routing: Optional[Mapping[str, Any]] = None,
    config: Optional[dict] = None,
    capabilities: Optional[dict] = None,
    tools: Optional[list] = None,
    trajectory: str = '',
    timeout_s: float = 600.0,
    request_id: str = '',
    **extra,
) -> dict:
    """Build the shared sync/async native agent payload without legacy BYO.

    A standalone runtime may own a configured default and omit ``model``;
    full ChatUI callers provide a structured identity. Secret-bearing inline
    ``provider`` blocks fail locally instead of being silently ignored by a
    newer server.
    """
    if 'provider' in extra:
        raise ValueError(
            'inline provider blocks were removed; configure model_routing v2')
    body = dict(extra)
    body.update({'messages': messages, 'timeout_s': timeout_s})
    if model is not None:
        body['model'] = _native_model_payload(model)
    if routing is not None:
        if not isinstance(routing, Mapping):
            raise TypeError('routing must be an object')
        body['routing'] = dict(routing)
    if model_routing is not None:
        if not isinstance(model_routing, Mapping):
            raise TypeError('model_routing must be an object')
        body['model_routing'] = dict(model_routing)
    for key, value in (
        ('config', config),
        ('capabilities', capabilities),
        ('tools', tools),
        ('trajectory', trajectory),
        ('id', request_id),
    ):
        if value:
            body[key] = value
    return body
