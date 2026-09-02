"""Translation engine + runtime constants."""

# Async task TTL (seconds) — also passed into TaskRuntime.
_TRANSLATE_TASK_TTL = 1800

# Max chars allowed for synchronous /api/translate (non-task path).
_SYNC_TRANSLATE_MAX_CHARS = 20000
