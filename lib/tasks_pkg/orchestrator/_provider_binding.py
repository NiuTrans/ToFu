"""Provider binding — hard provider pin + conversation-sticky routing.

Extracted 2026-08-01 ( slice 31) from ``run_task``'s pre-stream
prep. Runs ONCE per run_task invocation, right before Section 1 (config
resolution). Both binds are THREAD-LOCAL and cleared in run_task's finally
block (``_teardown.finalize_task_lane``) because worker threads are pooled
and reused.

Two bindings, one branch:

1. **Hard route-group pin (multi-tenant isolation).** When this task owns a
   request-scoped model-routing v2 candidate group, bind THIS worker thread
   to that group so every LLM
   dispatch on it (main solve, L2/advanced compaction summaries, Flow role
   replan turns) can only pick that group's slots — never silently cross
   into another request or operator credential. See
   ``lib/llm_dispatch/provider_pin.py``. A root task without its own group
   clears any pin on the caller's context: it dispatches through its
   configured pool, never through a route group it does not own. A child
   without its own group keeps its parent's pin.

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
    """Bind the task's own route pin and conversation affinity.

    A root without its own route group clears the caller's pin; a child
    without one keeps its parent's. The affinity is always rebound.

    Args:
        task: Live task dict; reads ``_pinned_provider_id`` and ``convId``.
        tid: 8-char task id prefix for log correlation.
    """
    # ── Hard provider pin (multi-tenant isolation) ──
    from lib.llm_dispatch.provider_pin import set_pinned_provider
    from lib.tasks_pkg.invocation import is_child_invocation
    _pinned_provider_id = task.get('_pinned_provider_id') or ''
    if _pinned_provider_id:
        set_pinned_provider(_pinned_provider_id)
        logger.info('[Task %s] Provider-pinned to %s (hard isolation)',
                    tid, _pinned_provider_id)
    elif not is_child_invocation(task):
        # A retired or foreign group left on the caller's context would
        # confine this root's dispatch to slots it does not own (no_slot).
        set_pinned_provider(None)

    # ── Conversation-sticky routing (UNCONDITIONAL — an empty convId
    #    clears the pooled thread's stale affinity from its previous
    #    task, which would otherwise bias key selection toward a key
    #    that served an UNRELATED conversation) ──
    from lib.llm_dispatch.conv_affinity import set_conv_affinity
    from lib.tasks_pkg.invocation import invocation_affinity_id
    set_conv_affinity(invocation_affinity_id(task))
