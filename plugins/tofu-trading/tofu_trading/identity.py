"""tofu_trading/identity.py — the single source of truth for "whose data is this?".

Every trading query MUST be scoped by the value this module returns. The old
module had **zero** occurrences of ``user_id``: every table was global and
``DELETE /holdings/all`` wiped the whole table for every user at once. That is
the root cause this module exists to close.

Design
------
The host resolves the request-thread user via ``routes.common._request_user_id``
(login-bound ``AuthContext.user_id``, falling back to ``DEFAULT_USER_ID = 1``).
We do NOT re-implement that resolution — a second implementation would drift
from the host's and silently re-open the leak. We import it and fail CLOSED.

"Fail closed" here means: if the host helper cannot be reached, we raise rather
than defaulting to a user id. A wrong-but-plausible id is precisely how one
user's portfolio gets written into another's row, and it produces no error
signal at the time of the write.
"""

from lib.log import get_logger
from tofu_trading.storage_schema import OWNER_SCOPED_TABLES

logger = get_logger(__name__)

__all__ = ['current_user_id', 'DEFAULT_OWNER_ID', 'SCOPED_TABLES']


# Owner assumed by BACKGROUND workers (autopilot scheduler, intel crawler) that
# run outside any request and therefore cannot resolve a per-request user.
# Matches the host's DEFAULT_USER_ID so a single-user install behaves normally.
#
# This is deliberately a NAMED constant rather than a bare `1` at each call
# site: it makes every "runs on behalf of the default owner" decision greppable,
# and gives per-user scheduling (P1) a single place to replace.
DEFAULT_OWNER_ID = 1


# The storage schema is the single source of truth. Several owner domains
# (simulator sessions, strategy learning, background tasks) intentionally have
# no legacy ``user_id`` column; their sidecar document key still carries the
# explicit owner and makes cross-owner lookup impossible.
SCOPED_TABLES = tuple(sorted(OWNER_SCOPED_TABLES))


def current_user_id() -> int:
    """Resolve the effective user id for the current request thread.

    Returns:
        The host-resolved user id.

    Raises:
        RuntimeError: if the host identity helper is unavailable. Raising is
            deliberate — see the module docstring on failing closed.
    """
    try:
        from routes.common import _request_user_id
    except Exception as e:
        logger.error('[Trading] Host identity helper unavailable: %s', e,
                     exc_info=True)
        raise RuntimeError(
            'tofu_trading requires the host identity helper '
            '(routes.common._request_user_id); refusing to run unscoped'
        ) from e

    uid = _request_user_id()
    if uid is None:
        logger.error('[Trading] Host resolved user_id=None — refusing unscoped query')
        raise RuntimeError('user_id resolved to None; refusing to run unscoped')
    return int(uid)
