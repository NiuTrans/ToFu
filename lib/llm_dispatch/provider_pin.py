"""lib/llm_dispatch/provider_pin.py — Thread-scoped hard provider binding.

Why this exists
===============
An inline ``provider`` block (or a registered ``@prov_xxx`` BYO provider)
on ``/api/v1/agent/run`` / ``/api/v1/chat/completions`` mints an
*ephemeral* :class:`~lib.llm_dispatch.slot.Slot` and appends it to the
**single process-global** dispatcher pool. The slot keeps the caller's
alias model name (e.g. ``deepseek-v4-pro``), which usually collides with
the operator-curated slots built from ``server_config.json`` for the same
model. The dispatcher's ``_pick`` then chooses the lowest-``score()`` slot
across *all* same-model candidates — so under concurrency, when the
ephemeral slot's ``inflight`` count inflates its score, the picker
silently selects an operator-keyed slot instead. The request leaves on
the operator's shared gateway key, gets 429'd, and the caller sees a
``no-fallback`` error for traffic that was supposed to be isolated to
their own endpoint.

That override was **advisory** (a hint in the request body), not a hard
scope. This module makes it a hard, thread-scoped binding:

    once a task is created with provider P, every LLM dispatch on that
    task's thread (main solve, compaction summary, endpoint replan, …)
    may ONLY pick a slot whose ``provider_id == P``. If no such slot is
    available the picker returns None — it NEVER falls back to a
    different provider's key.

Mechanism
---------
A :class:`threading.local`. ``run_task`` (the orchestrator's per-task
worker thread) sets the pin from ``task['_pinned_provider_id']`` and
clears it on exit; ``_pick`` / ``has_capable_slots`` read it. Aux LLM
calls made synchronously on the same thread (Layer-2 / advanced
compaction summarizers, endpoint Planner/Worker/Critic turns) inherit
it automatically. Swarm sub-agents run on their OWN threads, so the
master orchestrator forwards the pin and each :class:`SubAgent` re-enters
:func:`provider_pin` at the top of its run loop.

The pin is identified by the slot's ``provider_id``. Ephemeral slots are
minted with a unique ``ephemeral:<handle_id>`` id (see
``ephemeral.mint_ephemeral_slot``), so the binding is per-request: two
concurrent inline-provider requests from the same API key still pin to
their own distinct slots.

This is deliberately a no-op when nothing is pinned (the default for the
operator's own UI traffic), so the shared multi-key load balancer is
completely unaffected.
"""

from __future__ import annotations

import contextlib
import threading

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'get_pinned_provider',
    'set_pinned_provider',
    'clear_pinned_provider',
    'provider_pin',
]

_state = threading.local()


def get_pinned_provider() -> str | None:
    """Return the provider_id pinned on the current thread, or None."""
    return getattr(_state, 'provider_id', None)


def set_pinned_provider(provider_id: str | None) -> None:
    """Bind the current thread to ``provider_id`` (None / '' clears it).

    Idempotent. Used by ``run_task`` which cannot wrap its ~900-line body
    in a ``with`` block; it pairs this with :func:`clear_pinned_provider`
    in its ``finally``.
    """
    _state.provider_id = (provider_id or None)


def clear_pinned_provider() -> None:
    """Remove any provider pin on the current thread.

    Critical: worker threads are pooled and reused, so a pin left behind
    would bleed into the NEXT unrelated task that lands on this thread.
    """
    _state.provider_id = None


@contextlib.contextmanager
def provider_pin(provider_id: str | None):
    """Context manager form — pin for the duration of the block.

    Restores the previous pin on exit (supports nesting). When
    ``provider_id`` is falsy this is a transparent no-op so callers can
    wrap unconditionally:

        with provider_pin(task.get('_pinned_provider_id')):
            agent.run()
    """
    if not provider_id:
        yield
        return
    prev = getattr(_state, 'provider_id', None)
    _state.provider_id = provider_id
    try:
        yield
    finally:
        _state.provider_id = prev
