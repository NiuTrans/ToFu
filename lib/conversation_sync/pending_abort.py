"""Owner-scoped cancellation fence for a command still entering the executor.

The fence lives in the declared runtime-state store, so every replica observes
the same short-lived stop request. It never owns transcript state; accepted
attempts are cancelled through the durable attempt API instead.
"""

from __future__ import annotations

import json
import time
from typing import Any

from lib.runtime_state_store import get_store


_STORE_KIND = "conversation_pending_abort"
_FENCE_TTL_SECONDS = 300.0


def _owner_key(conversation_id: str, user_id: Any) -> str:
    return json.dumps(
        [str(user_id if user_id is not None else ""), str(conversation_id or "")],
        separators=(",", ":"),
        ensure_ascii=True,
    )


def mark_pending_abort(
    conversation_id: str,
    user_id: Any,
    *,
    occurred_at: float | None = None,
) -> float | None:
    """Publish a bounded cancellation fence and return its server timestamp."""
    if not conversation_id:
        return None
    timestamp = time.time() if occurred_at is None else float(occurred_at)
    get_store().set_value(
        _STORE_KIND,
        _owner_key(conversation_id, user_id),
        repr(timestamp),
        _FENCE_TTL_SECONDS,
    )
    return timestamp


def was_pending_abort_after(
    conversation_id: str,
    user_id: Any,
    request_started_at: float | None,
) -> bool:
    """Return whether this owner stopped the conversation after request start."""
    if not conversation_id or request_started_at is None:
        return False
    raw = get_store().get_value(_STORE_KIND, _owner_key(conversation_id, user_id))
    if raw is None:
        return False
    try:
        return float(raw) >= float(request_started_at)
    except (TypeError, ValueError):
        return False


__all__ = ["mark_pending_abort", "was_pending_abort_after"]
