"""Backend-independent bounds for serialized model continuation projections.

Storage admission and compaction restore consume the same limits. This module
has no storage dependencies so transient/core compositions can validate context.
"""

MAX_ENTRY_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 16384
MAX_TURN_WITNESSES = 4096
