"""Translation engine + runtime constants."""

DEFAULT_USER_ID = 1

# Async task TTL (seconds) — also passed into TaskRuntime.
_TRANSLATE_TASK_TTL = 1800

# Chars before splitting into chunks for translation.
_CHUNK_THRESHOLD = 12000

# Max chars allowed for synchronous /api/translate (non-task path).
_SYNC_TRANSLATE_MAX_CHARS = 20000

# Parallel workers for chunked translation (was 4, raised to 6 to speed
# up agent translation).
_CHUNK_MAX_WORKERS = 6
