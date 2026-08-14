"""Persistence helpers for the error envelope.

``task_results.error`` is ``TEXT``.  Envelopes are stored as a JSON object
string.  Older rows (pre-2026-05-22) may have a plain string — the loader
normalises those into a ``generic`` envelope so the frontend's typed-reducer
never has to handle two shapes.
"""

from __future__ import annotations

import json
from typing import Any

from lib.log import get_logger

from lib.error_envelope._build import make_envelope, normalize_envelope
from lib.error_envelope._constants import KINDS

logger = get_logger(__name__)


def to_json(envelope: dict[str, Any] | str | None) -> str | None:
    """Serialise an envelope (or legacy string) for DB storage."""
    normalized = normalize_envelope(
        envelope, context='error:persistence', source='lib.error_envelope')
    if normalized is None:
        return None
    try:
        return json.dumps(normalized, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.warning('[ErrorEnvelope] to_json failed: %s — falling back to string', e)
        return json.dumps(make_envelope(
            'internal',
            detail=f'Envelope serialise failed: {e}',
            raw=str(normalized)[:200],
        ), ensure_ascii=False)


def from_json(s: str | dict | None) -> dict[str, Any] | None:
    """Deserialise an envelope from DB storage.

    Accepts the two historical shapes:

      • JSON-serialised envelope dict (current format)
      • plain string (legacy ``task['error'] = 'something failed'``)

    Returns ``None`` for ``None`` / empty input.
    """
    if s is None or s == '':
        return None
    if isinstance(s, dict):
        return normalize_envelope(
            s, context='error:persistence', source='lib.error_envelope')
    if not isinstance(s, str):
        logger.debug('[ErrorEnvelope] from_json got unexpected type=%s', type(s).__name__)
        return make_envelope('generic', detail=str(s)[:200], raw=str(s))
    s = s.strip()
    if not s:
        return None
    if s[0] == '{':
        try:
            obj = json.loads(s)
            if isinstance(obj, dict) and 'kind' in obj:
                return normalize_envelope(
                    obj, context='error:persistence',
                    source='lib.error_envelope')
            # JSON object that isn't a typed envelope (e.g. raw error
            # body from some other layer) — wrap.
            return make_envelope('generic', detail=s[:200], raw=s)
        except (json.JSONDecodeError, ValueError) as _e_audit:
            # Looked like JSON but wasn't — fall through to string wrap.
            logger.debug('[error_envelope] from_json caught %s: %s', type(_e_audit).__name__, _e_audit)
            pass
    # Plain legacy string — wrap as generic.
    return make_envelope('generic', detail=s[:200], raw=s)


def is_envelope(obj: Any) -> bool:
    """True iff ``obj`` is valid and renderable as a typed envelope."""
    return (isinstance(obj, dict)
            and isinstance(obj.get('kind'), str)
            and obj.get('kind') in KINDS
            and isinstance(obj.get('message'), str)
            and bool(obj.get('message')))
