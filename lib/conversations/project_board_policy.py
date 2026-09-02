"""Pure timing and state policy for project-board leases and block cooldowns.

Application orchestration and storage operations import this module so lease
expiry and retry timing have one implementation.
"""

DEFAULT_LEASE_TTL_MS = 30 * 60 * 1000
BLOCK_COOLDOWN_BASE_MS = 60 * 60 * 1000
BLOCK_COOLDOWN_MAX_MS = 24 * 60 * 60 * 1000
SIBLING_BLOCK_COOLDOWN_MS = DEFAULT_LEASE_TTL_MS
SIBLING_BLOCK_TAG = "[sibling]"

_BLOCK_COOLDOWN_FACTOR = 4


def effective_board_status(
    stored_status: str,
    lease_expires_at: int,
    current_time_ms: int,
) -> str:
    """Project an expired advisory claim back to open."""
    if (
        stored_status == "claimed"
        and lease_expires_at
        and lease_expires_at <= current_time_ms
    ):
        return "open"
    return stored_status


def block_cooldown_ms(block_count: int, block_class: str = "human") -> int:
    """Return the bounded retry delay for a newly recorded block."""
    count = int(block_count or 0)
    if count <= 0:
        return 0
    if block_class == "sibling":
        return SIBLING_BLOCK_COOLDOWN_MS
    exponent = min(count - 1, 20)
    return min(
        BLOCK_COOLDOWN_MAX_MS,
        BLOCK_COOLDOWN_BASE_MS * (_BLOCK_COOLDOWN_FACTOR**exponent),
    )
