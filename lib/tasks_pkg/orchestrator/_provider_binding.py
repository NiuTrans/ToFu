"""Provider binding — hard provider pin + conversation-sticky routing.

Extracted 2026-08-01 ( slice 31) from ``run_task``'s pre-stream
prep. Runs ONCE per run_task invocation, right before Section 1 (config
resolution). Both binds are THREAD-LOCAL and cleared in run_task's finally
block (``_teardown.finalize_task_lane``) because worker threads are pooled
and reused.

Two bindings, one branch:

1. **Hard provider pin (multi-tenant isolation).** When this task was
   created from an inline ``provider`` block or a registered ``@prov_xxx``
   BYO endpoint, bind THIS worker thread to that provider so every LLM
   dispatch on it (main solve, L2/advanced compaction summaries, Flow role
   replan turns) can only pick that provider's slot — never silently fall
   back to an operator key and eat a 429. See
   ``lib/llm_dispatch/provider_pin.py``. BRANCH: only when
   ``task['_pinned_provider_id']`` is truthy.

2. **Conversation-sticky routing.** Bind this worker thread to the
   conversation so every LLM dispatch on it prefers the API key that last
   served this conv — keeping the Anthropic per-key prompt cache warm
   across rounds. Soft preference: the picker still falls back to a
   healthy key when the sticky one is cooled down. See
   ``lib/llm_dispatch/conv_affinity.py``. Called UNCONDITIONALLY — an
   empty convId clears whatever stale affinity the pooled thread carries
   from its previous task.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def bind_provider_and_affinity(task, tid):
    """Apply the hard provider pin (when pinned) + conv affinity (always).

    Args:
        task: Live task dict; reads ``_pinned_provider_id`` and ``convId``.
        tid: 8-char task id prefix for log correlation.
    """
    # ── Hard provider pin (multi-tenant isolation) ──
    from lib.llm_dispatch.provider_pin import set_pinned_provider
    _pinned_provider_id = task.get('_pinned_provider_id') or ''
    if _pinned_provider_id:
        set_pinned_provider(_pinned_provider_id)
        logger.info('[Task %s] Provider-pinned to %s (hard isolation)',
                    tid, _pinned_provider_id)

    # ── Conversation-sticky routing (UNCONDITIONAL — an empty convId
    #    clears the pooled thread's stale affinity from its previous
    #    task, which would otherwise bias key selection toward a key
    #    that served an UNRELATED conversation) ──
    from lib.llm_dispatch.conv_affinity import set_conv_affinity
    set_conv_affinity(task.get('convId') or '')
