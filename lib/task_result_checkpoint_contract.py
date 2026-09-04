"""Shared wire constants for guarded task-result checkpoints.

The task manager and Storage Sidecar deploy independently during rolling
upgrades.  This explicit response echo lets a new manager stop issuing legacy
preflight reads only after the peer proves it owns the parent/owner/status
fences atomically inside the checkpoint transaction.  The independent cache
settings echo likewise lets cache accounting stop its legacy settings RMW only
after the peer proves those two bounded facts joined the same transaction.
"""

TASK_RESULT_CHECKPOINT_GUARD_CONTRACT = (
    "tofu.task-results.checkpoint.guard/v1"
)
TASK_RESULT_CACHE_SETTINGS_CONTRACT = (
    "tofu.task-results.checkpoint.cache-settings/v1"
)

TASK_CACHE_PREFIX_HWM_CANDIDATE_FIELD = "_cachePrefixHWMCandidate"
TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD = (
    "_lastTurnCacheReadCandidate"
)
TASK_RESULT_CACHE_PREFIX_HWM_FIELD = "cache_prefix_hwm"
TASK_RESULT_LAST_TURN_CACHE_READ_FIELD = "last_turn_cache_read"

CONVERSATION_CACHE_PREFIX_HWM_SETTING = "cachePrefixHWM"
CONVERSATION_LAST_TURN_CACHE_READ_SETTING = "lastTurnCacheRead"
TASK_RESULT_CACHE_FACT_MAXIMUM = 2_147_483_647


__all__ = [
    "CONVERSATION_CACHE_PREFIX_HWM_SETTING",
    "CONVERSATION_LAST_TURN_CACHE_READ_SETTING",
    "TASK_CACHE_PREFIX_HWM_CANDIDATE_FIELD",
    "TASK_LAST_TURN_CACHE_READ_CANDIDATE_FIELD",
    "TASK_RESULT_CACHE_PREFIX_HWM_FIELD",
    "TASK_RESULT_CACHE_FACT_MAXIMUM",
    "TASK_RESULT_CACHE_SETTINGS_CONTRACT",
    "TASK_RESULT_CHECKPOINT_GUARD_CONTRACT",
    "TASK_RESULT_LAST_TURN_CACHE_READ_FIELD",
]
