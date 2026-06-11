"""Translation engine + runtime constants."""

DEFAULT_USER_ID = 1

# Async task TTL (seconds) — also passed into TaskRuntime.
_TRANSLATE_TASK_TTL = 1800

# Max chars allowed for synchronous /api/translate (non-task path).
_SYNC_TRANSLATE_MAX_CHARS = 20000

# Free-text (chat / assistant message) translation chunking.
# A single cheap-tier LLM call silently stops early on long inputs (the model
# emits only the first portion, finish_reason!='length'), leaving a truncated
# translation that the 20%-ratio guard happily accepts.  Split inputs longer
# than the threshold on paragraph boundaries and translate each piece in its
# own call so every chunk stays well inside the model's reliable output range.
_FREETEXT_CHUNK_THRESHOLD = 4000
_FREETEXT_CHUNK_SIZE = 3000
