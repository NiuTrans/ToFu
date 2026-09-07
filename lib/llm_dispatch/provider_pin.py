"""lib/llm_dispatch/provider_pin.py — Execution-context provider binding.

Why this exists
===============
Model-routing v2 mints a bounded group of request-scoped ephemeral Slots and
appends them to the **single process-global** dispatcher pool. Their logical
model commonly collides with slots belonging to another request or to the
operator. Without a hard group pin, score-based selection could silently move
the task onto a credential outside its authorized candidate set.

That override was **advisory** (a hint in the request body), not a hard
scope. This module makes it a hard execution-context binding:

    once a task is created with route group P, every LLM dispatch on that
    task execution context (main solve, compaction summary, endpoint replan, …)
    may ONLY pick a slot whose dispatcher ``provider_id == P``. If no such slot is
    available the picker returns None — it NEVER falls back to a
    different provider's key.

Mechanism
---------
A :class:`contextvars.ContextVar`. ``run_task`` (the orchestrator's per-task
worker thread) sets the pin from ``task['_pinned_provider_id']`` and clears it
on exit; ``_pick`` / ``has_capable_slots`` read it. Synchronous auxiliary calls
inherit it on that thread, while asyncio Tasks receive isolated context copies
instead of sharing one thread-local value on the event loop. Swarm sub-agents
run on their own threads, so the master forwards the pin and each
:class:`SubAgent` re-enters :func:`provider_pin` at the top of its run loop.

The pin is identified by the request's unique v2 route-group ID, assigned to
every candidate Slot's dispatcher ``provider_id``. This internal value is not
the public Provider resource ID. Two concurrent requests by the same owner
therefore cannot select one another's candidate slots.

This is deliberately a no-op when nothing is pinned (the default for the
operator's own UI traffic), so the shared multi-key load balancer is
completely unaffected.
"""

from __future__ import annotations

import contextlib
import contextvars

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'get_pinned_provider',
    'set_pinned_provider',
    'clear_pinned_provider',
    'provider_pin',
]

_provider_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    'tofu_provider_pin', default=None,
)


def get_pinned_provider() -> str | None:
    """Return the provider_id pinned in the current execution context."""
    return _provider_id.get()


def set_pinned_provider(provider_id: str | None) -> None:
    """Bind the current thread to ``provider_id`` (None / '' clears it).

    Idempotent. Used by ``run_task`` which cannot wrap its ~900-line body
    in a ``with`` block; it pairs this with :func:`clear_pinned_provider`
    in its ``finally``.
    """
    _provider_id.set(provider_id or None)


def clear_pinned_provider() -> None:
    """Remove any provider pin on the current thread.

    Critical: worker threads are pooled and reused, so a pin left behind
    would bleed into the NEXT unrelated task that lands on this thread.
    """
    _provider_id.set(None)


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
    token = _provider_id.set(provider_id)
    try:
        yield
    finally:
        _provider_id.reset(token)
