"""Typed error envelope for backend → frontend error reporting.

Every error surfaced to the user (in `task['error']`, in the `error`
field of SSE `done` / `state` events, and in persisted
`task_results.error` / `assistantMsg.error`) is a dict with
this shape::

    {
      'kind':      <enum>,           # machine-classifiable error category
      'severity':  'warning'|'error',# UI severity (color, icon)
      'retryable': bool,             # is "try again" likely to help?
      'message':   str,              # short bilingual title (one line)
      'hint':      str,              # bilingual recovery hint (multi-line)
      'detail':    str,              # technical detail (truncated raw text)
      'model':     str,              # model under which the error fired
      'context':   str,              # short tag, e.g. 'fallback', 'task-fatal'
      'source':    str,              # component that minted it
      'raw':       str,              # raw exception text (≤300 chars)
      'titleKey':  str,   # OPTIONAL — i18n key (``err.k.<kind>.title``) the
                          # frontend resolves in the CURRENT UI language.
                          # Absent when the caller supplied a custom
                          # ``message`` (rendered verbatim by all clients).
      'hintKey':   str,   # OPTIONAL — i18n key (``err.k.<kind>.hint``) for
                          # the hint block; absent for custom hints without
                          # an explicit key.
    }

``message`` / ``hint`` are ALWAYS populated byte-identically (bilingual) for
headless clients and as the fallback when a frontend bundle predates the
keyed surface (2026-07-25).

The `kind` enum is closed — callers must pick one of these values:

  - ``quota``               persistent billing / balance exhaustion
  - ``ratelimit``           transient 429 / TPM-RPM throttle
  - ``permission``          401 / 403, key invalid or revoked
  - ``no_slot``             dispatch layer found zero usable slots
  - ``dispatch_exhausted``  every slot for this capability has been tried
  - ``timeout``             upstream / network read timeout
  - ``network``             connection error, DNS, proxy reset
  - ``endpoint_unreachable`` model endpoint host down / not accepting connections
  - ``content_filter``      provider safety filter (HTTP 450, etc.)
  - ``invalid_image``       image content rejected
  - ``prompt_too_long``     context window overflow (after auto-compact)
  - ``stream_only``         model rejects non-streaming
  - ``model_limit``         max_tokens exceeds learned model cap
  - ``tool_rounds_exhausted`` legacy persisted turns from the retired cap
  - ``tool_timeout``        repeated tool-execution timeouts
  - ``tool_loop``           byte-identical tool call+result repeated (degenerate loop)
  - ``premature_close``     SSE stream cut off (retries exhausted)
  - ``abnormal_stop``       missing finish marker, partial reply
  - ``aborted``             user clicked Stop (rare in error path)
  - ``server_offline``      frontend lost contact with the server
  - ``server_busy``         backend admission capacity is temporarily full
  - ``task_start_failed``   durable turn exists but its executor did not start
  - ``internal``            backend bug / unhandled exception
  - ``generic``             unrecognized — last-resort fallback
  - ``bad_request``         deterministic HTTP 400 payload rejection
  - ``not_found``           HTTP 404 — model/route missing on the upstream
  - ``tool_not_available``  requested tool is absent from this turn
  - ``tool_call_rejected``  tool was blocked before execution

Backwards-compat note (2026-05-22): there is none.  The string form of
``task['error']`` was retired in favour of this dict.  Persistence
serializes the dict as JSON; the SSE / poll payloads carry the dict
verbatim; the frontend reducer reads ``error.message`` for display and
``error.kind`` / ``error.severity`` for classification.

This module is a pure re-export facade — all implementations live in the
sub-modules (``_constants``, ``_classify``, ``_build``, ``_serde``).  The
``from lib.error_envelope import X`` surface is preserved byte-identically.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# ── Constants (closed enum + classification tables) ───────────────────
from lib.error_envelope._constants import (  # noqa: E402,F401
    KINDS,
    _WARNING_KINDS,
    _RETRYABLE_KINDS,
    _TITLES,
    _SETTINGS_HINT_CN,
    _SETTINGS_HINT_EN,
    _PERMISSION_HINT_CN,
    _PERMISSION_HINT_EN,
    _TIMEOUT_HINT_CN,
    _TIMEOUT_HINT_EN,
    _NETWORK_HINT_CN,
    _NETWORK_HINT_EN,
    _UNREACHABLE_HINT_CN,
    _UNREACHABLE_HINT_EN,
)

# ── Exception classification ──────────────────────────────────────────
from lib.error_envelope._classify import _classify_exception  # noqa: E402,F401

# ── Envelope builders ─────────────────────────────────────────────────
from lib.error_envelope._build import (  # noqa: E402,F401
    make_envelope,
    from_exception,
    normalize_envelope,
)

# ── Persistence helpers ───────────────────────────────────────────────
from lib.error_envelope._serde import (  # noqa: E402,F401
    to_json,
    from_json,
    is_envelope,
)

# ── Wire-contract schema ──────────────────────────────────────────────
from lib.error_envelope._schema import (  # noqa: E402,F401
    typed_error_envelope_schema,
)


__all__ = [
    'KINDS',
    'make_envelope',
    'from_exception',
    'normalize_envelope',
    'to_json',
    'from_json',
    'is_envelope',
    'typed_error_envelope_schema',
    '_classify_exception',
]
