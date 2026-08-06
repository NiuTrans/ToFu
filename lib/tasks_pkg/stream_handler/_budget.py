"""Retry-budget caps, backoff schedule, and paced-sleep helpers.

Groups the tunable knobs that govern abnormal-stream-termination retries:

  * ``_PREMATURE_RETRY_MAX_CLASSIC`` / ``_PREMATURE_RETRY_MAX_ZERO_BYTE`` /
    ``_EMPTY_STOP_RETRY_MAX`` — the per-signature retry caps.
  * ``_TODO_CONTINUATION_MAX_DEFAULT`` + ``_todo_continuation_max`` — the
    todo-continuation nudge cap (env-overridable, fail-open).
  * ``_ZERO_BYTE_BACKOFF_BASE_S`` / ``_ZERO_BYTE_BACKOFF_MAX_S`` +
    ``_zero_byte_backoff_seconds`` — the exponential-backoff schedule.
  * ``_interruptible_sleep`` — abort-aware backoff sleep.
"""

import random
import time

from lib.log import get_logger

logger = get_logger(__name__)


# Retry caps for abnormal stream termination.
#
# Two qualitatively different failure signatures get different budgets:
#
#   ── Classic premature close ──────────────────────────────────────
#   Model produced substantial thinking (>1000 chars) then the stream
#   was cut off mid-generation. This is a transport-layer hiccup, not
#   the model giving up — so we keep retrying (paced with exponential
#   backoff, same schedule as zero-byte) rather than failing the whole
#   task on a single dropped connection. Each retry re-bills the prompt
#   cache + re-generates thinking, so there's still a finite cap as a
#   runaway guard, but it's generous.
#
#   ── Zero-byte stream anomaly ─────────────────────────────────────
#   Gateway/proxy opens the SSE connection, returns zero tokens, and
#   closes within a few seconds.  No output tokens were generated, but
#   prompt-cache reads ARE billed — for a turn with 30 KB cached, 16
#   back-to-back retries means ~480 KB of cache-read traffic.  Caps are
#   therefore finite, and retries are paced with exponential backoff
#   (see ``_zero_byte_backoff_seconds``) so we don't hammer a poisoned
#   upstream pool that is overwhelmingly likely to zero-byte the next
#   request milliseconds later.  Recurring trigger: ``aws.claude-opus-4.7``
#   via sankuai gateway.  Retries go through append_event 'phase:retrying'
#   so the UI shows a spinner with attempt count.
_PREMATURE_RETRY_MAX_CLASSIC = 16
_PREMATURE_RETRY_MAX_ZERO_BYTE = 16
# Empty-stop retry budget (model emitted thinking / a few chunks but no
# content, then closed cleanly with finish_reason=stop). Observed on
# GLM-5.1, MiniMax M2.5/M2.7, and occasionally Claude.  Each retry is
# moderately expensive (cache reads + new thinking), so the cap is low.
_EMPTY_STOP_RETRY_MAX = 2

# Canned-greeting retry budget (upstream returns a "successful" canned
# greeting incongruent with the conversation tail — see _canned_greeting.py).
# In the 2026-07-28 incident the failure was intermittent (~50% per round),
# so 2 retries recover ~75% of poisoned turns. Shares the per-phase counter
# with the other buckets (same runaway-guard discipline as empty_stop), and
# each retry re-bills a mostly cache-read prompt, so the cap stays low.
_CANNED_GREETING_RETRY_MAX = 2

# Tool-calls-finish-without-payload retry budget (the gateway reports
# finish_reason=tool_calls but the stream carried ZERO tool_call deltas —
# the model's tool calls were lost upstream; 2026-08-06 kimi-k3/sankuai
# incident, conv msh3qeplzneph5 R3). Ending the turn there delivers a
# preamble as if it were the conclusion. The poisoned round DID bill
# prompt + completion tokens (unlike zero-byte), and the failure is
# intermittent gateway flakiness, so the cap matches the other
# "tokens were spent" buckets (empty_stop / canned_greeting).
_TOOL_CALLS_NO_PAYLOAD_RETRY_MAX = 2

# ── Todo-continuation enforcer (OMC/CC backport, Rec 2) ──
# When the model tries to end its turn (finish_reason=stop, no tool calls) but
# its structured checklist (task['_todos'], written via the todo_write tool)
# still has pending / in_progress items, inject a reminder and RE-DRIVE the
# loop instead of breaking — catching a premature stop CHEAPLY (mid-loop) vs.
# a full Critic round. Bounded by a hard nudge cap so a model that refuses to
# either finish or update the checklist can't loop forever (runaway guard,
# same discipline as the retry caps above). Env-overridable, fail-open:
# unset→default 3, 0/<=0→DISABLED (never enforce), garbage→default.
_TODO_CONTINUATION_MAX_DEFAULT = 3


def _todo_continuation_max() -> int:
    """Max todo-continuation nudges per phase (runaway guard). Fail-open."""
    import os
    raw = (os.environ.get('TOFU_TODO_CONTINUATION_MAX') or '').strip()
    if not raw:
        return _TODO_CONTINUATION_MAX_DEFAULT
    try:
        val = int(raw)
    except (ValueError, TypeError) as e:
        logger.debug('[stream_handler] TOFU_TODO_CONTINUATION_MAX=%r not an int '
                     '(%s) — using default %d', raw, e,
                     _TODO_CONTINUATION_MAX_DEFAULT)
        return _TODO_CONTINUATION_MAX_DEFAULT
    return val if val > 0 else 0

# Exponential-backoff schedule for zero-byte retries.
# 0.5s, 1s, 2s, 4s, 8s, 8s, 8s, ...  + uniform jitter [0, 0.5s).
_ZERO_BYTE_BACKOFF_BASE_S = 0.5
_ZERO_BYTE_BACKOFF_MAX_S = 8.0


def _zero_byte_backoff_seconds(attempt: int) -> float:
    """Return the sleep (seconds) before the Nth zero-byte retry.

    ``attempt`` is 1-based: the 1st retry sleeps ~0.5s, 2nd ~1s, 3rd ~2s, etc.
    """
    base = min(
        _ZERO_BYTE_BACKOFF_BASE_S * (2 ** max(0, attempt - 1)),
        _ZERO_BYTE_BACKOFF_MAX_S,
    )
    return base + random.uniform(0.0, 0.5)


def _interruptible_sleep(seconds: float, task) -> None:
    """Sleep up to ``seconds``, polling ``task['aborted']`` every 100 ms.

    Lets a user abort (or task supersession) interrupt the backoff promptly.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if task.get('aborted'):
            return
        time.sleep(min(0.1, remaining))
