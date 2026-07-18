"""lib/log_clean — Pure-function log noise detection (facade package).

This is a server-side port of ``static/js/log-clean.js``. It exists for
two reasons:

1. **Frontend/backend boundary** (CLAUDE.md §16): policy that decides
   what counts as "noise" is server-side; the UI is just a renderer
   over the structured detection result.
2. **Headless API parity**: SDK callers and CI pipelines need access
   to the same heuristic that the UI shows in its banner. They get it
   via ``POST /api/v1/logs/clean`` (same shape, same passes).

The function is pure: ``detect_log_noise(text) → CleaningResult | None``.
No I/O, no state, no logger spam on the hot path. Suitable for batch
calls (translate-mode, paper-mode, evaluation harnesses).

Parity with the JS version
--------------------------
* Same regex patterns, same pass ordering, same threshold constants
  (``min_lines=5``, ``saved_pct >= 8``, ``saved_chars >= 80``).
* Same operation labels (Chinese), same banner-tag shape.
* Same output keys so ``static/js/log-clean.js`` becomes a thin
  fetch + render wrapper without changing its UI contract.

Tests in ``tests/test_log_clean.py`` exercise the same fixtures the
existing JS tests use, so any divergence shows up immediately.

Package layout
--------------
* ``_patterns`` — compiled regex tables + threshold constants
* ``_types``    — CleaningOp / CleaningResult dataclasses
* ``_helpers``  — pure line-level helpers
* ``_collapse`` — collapse/dedup pass implementations
* ``_detect``   — public ``detect_log_noise`` orchestrator

This file is a pure re-export facade so every historical
``from lib.log_clean import X`` keeps working byte-identically.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# ── Output dataclasses ─────────────────────────────────────────────
from lib.log_clean._types import (  # noqa: E402,F401
    CleaningOp,
    CleaningResult,
)

# ── Regex tables & threshold constants ─────────────────────────────
from lib.log_clean._patterns import (  # noqa: E402,F401
    _LOG_PREFIX_PATTERNS,
    _NOISE_LINE_PATTERNS,
    _POINTER_LINE_RE,
    _LONG_PATH_RE,
    _TQDM_BAR_RE,
    _TQDM_RATE_TAIL_RE,
    _HEX_ADDR_RE,
    _UUID_RE,
    _IP_RE,
    _DEVICE_ID_RE,
    _LONG_DIGIT_RE,
    _DEVICE_ID_PATTERNS,
    _WORKER_TAG_RE,
)

# ── Pure line-level helpers ────────────────────────────────────────
from lib.log_clean._helpers import (  # noqa: E402,F401
    _shorten_paths,
    _is_tqdm_line,
    _extract_tqdm_pct,
    _extract_device_ids,
    _format_device_range,
    _fingerprint,
)

# ── Collapse/dedup pass implementations ────────────────────────────
from lib.log_clean._collapse import (  # noqa: E402,F401
    _deduplicate_worker_blocks,
    _collapse_progress_bars,
    _collapse_similar_lines,
    _collapse_blank_lines,
)

# ── Public entrypoint ──────────────────────────────────────────────
from lib.log_clean._detect import detect_log_noise  # noqa: E402,F401


__all__ = ['detect_log_noise', 'CleaningResult', 'CleaningOp']
