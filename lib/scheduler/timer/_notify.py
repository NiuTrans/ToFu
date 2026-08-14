"""Best-effort timer-list invalidation over the shared push socket.

The timer panel is a projection of ``timer_watchers``.  Shipping the full row
set in every event would create a second state protocol, so writers publish a
small invalidation only; visible clients then reconcile through the existing
list endpoint.  Failures are deliberately non-fatal: persistence is the
authoritative operation and push always has a polling fallback.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def notify_timer_changed(change: str) -> None:
    """Tell subscribed browsers that the durable timer projection changed."""
    try:
        from lib.agent_core.push import push_event

        # A global invalidation contains no timer/conversation identifier.  It
        # is sufficient for the current personal-server model and avoids
        # turning a future multi-user deployment into an identifier side
        # channel.  The list endpoint remains the authorization boundary.
        push_event('timer', '*', {
            'type': 'timer_changed',
            'change': str(change or 'updated')[:40],
        })
    except Exception as exc:
        logger.debug('[Timer] push invalidation failed (%s): %s', change, exc)
