"""Trading request ownership delegates to the host authentication boundary.

`current_user_id` uses the same validated request principal as core routes;
missing ownership fails closed. Background workers retain their explicitly
named legacy owner until their scheduling contract supplies an owner.
"""

from tofu_trading.storage_schema import OWNER_SCOPED_TABLES

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
    """Resolve the authenticated owner through the host's existing policy."""
    from routes.api_v1.auth import request_user_id

    return request_user_id()
