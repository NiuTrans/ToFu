"""Post-stream analysis — premature-close detection and loop-exit decisions.

Extracted from the inner loop of ``orchestrator.run_task`` to isolate the
logic that inspects each LLM round's result and decides whether to retry
(premature close), break (normal finish / error / abort), or continue to
tool execution.

This package preserves the original ``lib.tasks_pkg.stream_handler`` import
surface: it is a pure re-export facade.  All implementations live in the
sub-modules:

  * ``_audit``   — audit-once guard (``_AUDIT_LOCK`` / ``_AUDIT_LOGGED`` /
    ``_maybe_audit_phase_scope``).
  * ``_budget``  — retry-budget caps, backoff schedule, paced-sleep helpers.
  * ``_analyse`` — the ``analyse_stream_result`` classifier entrypoint.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ── Audit-once guard ──
from lib.tasks_pkg.stream_handler._audit import (  # noqa: E402,F401
    _AUDIT_LOCK,
    _AUDIT_LOGGED,
    _maybe_audit_phase_scope,
)


# ── Retry-budget caps + backoff + paced sleep ──
from lib.tasks_pkg.stream_handler._budget import (  # noqa: E402,F401
    _CANNED_GREETING_RETRY_MAX,
    _EMPTY_STOP_RETRY_MAX,
    _PREMATURE_RETRY_MAX_CLASSIC,
    _PREMATURE_RETRY_MAX_ZERO_BYTE,
    _TODO_CONTINUATION_MAX_DEFAULT,
    _TOOL_CALLS_NO_PAYLOAD_RETRY_MAX,
    _ZERO_BYTE_BACKOFF_BASE_S,
    _ZERO_BYTE_BACKOFF_MAX_S,
    _interruptible_sleep,
    _todo_continuation_max,
    _zero_byte_backoff_seconds,
)


# ── Canned-greeting upstream-artifact detector ──
from lib.tasks_pkg.stream_handler._canned_greeting import (  # noqa: E402,F401
    is_canned_greeting_reply,
    last_user_is_smalltalk,
)


# ── Main classifier entrypoint ──
from lib.tasks_pkg.stream_handler._analyse import (  # noqa: E402,F401
    analyse_stream_result,
)
