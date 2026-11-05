"""Backend-independent bounds for serialized model continuation projections.

Storage admission and compaction restore consume the same limits. This module
has no storage dependencies so transient/core compositions can validate context.
"""

MAX_ENTRY_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 16384
# Bounds only legacy per-Turn witness lists (checkpoints without historyProof).
# A proof checkpoint carries one terminal witness whatever the lane length.
MAX_TURN_WITNESSES = 4096
# Largest integer every JSON peer represents exactly (RFC 7493, I-JSON). A
# history-proof count is a plain counter; this representation limit is its
# only bound, mirrored by Tofu-DB's model_continuation::MAX_JSON_SAFE_INTEGER.
MAX_JSON_SAFE_INTEGER = 2**53 - 1
