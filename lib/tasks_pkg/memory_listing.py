"""Start an accepted attempt's memory-corpus listing before startup needs it.

Responsibility: memory prefetch ranks the eligible metadata corpus against the
attempt's (translated) query inside ``startup_memory``, but listing that corpus
depends only on the attempt's roots. An accepted ordinary attempt whose
configuration already opens the prefetch gate therefore starts the listing at
acceptance, next to its input translation, so it overlaps context assembly,
registration, executor start, worker entry and the translation join.

Entry points: ``start_early_memory_listing`` (``lib.conversation_sync.task_start``)
and ``release_early_memory_listing``. The task carries the listing under
``EARLY_MEMORY_LISTING_KEY``; ``maybe_run_memory_prefetch`` consumes or releases
it; orchestration exit and terminal heavy-state release drop one nobody
consumed. Flow and project-dispatch roots never start one: a Flow root never
prefetches and a dispatch worker resolves its tools and workspace at worker
entry.

Dependencies: the gate is ``_memory_prefetch.accepted_memory_prefetch_roots``,
the listing ``lib.memory.prefetch.PrefetchCorpusListing`` on the shared
context-provider demand executor. Decision record: docs/DEFENSIVE_PATTERNS.md,
"An accepted attempt lists its memory corpus at acceptance".
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

EARLY_MEMORY_LISTING_KEY = '_earlyMemoryListing'


def start_early_memory_listing(config: dict[str, Any]):
    """Start the attempt's prefetch corpus listing, or return ``None``.

    Never raises: the listing is an optimization, so a failure to start it
    leaves ``startup_memory`` listing inline with today's failure handling.
    """
    try:
        return _start(config)
    except Exception:
        logger.warning('[MemoryListing] early corpus listing not started; '
                       'startup lists inline', exc_info=True)
        return None


def _start(config):
    from lib.orchestration.chat_flow_selection import (
        CHAT_FLOW_ENTRY_NONE,
        select_chat_flow_entry,
    )
    from lib.project_dispatch.admission import KEY as DISPATCH_ADMISSION_KEY

    if (select_chat_flow_entry(config) != CHAT_FLOW_ENTRY_NONE
            or DISPATCH_ADMISSION_KEY in config):
        return None
    from lib.tasks_pkg.orchestrator._memory_prefetch import (
        accepted_memory_prefetch_roots,
    )
    roots = accepted_memory_prefetch_roots(config)
    if roots is None:
        return None
    from lib.memory.prefetch import PrefetchCorpusListing
    from lib.tasks_pkg.context_composer._provider_executor import (
        context_provider_executor,
    )
    return PrefetchCorpusListing(*roots, submit=context_provider_executor.submit)


def release_early_memory_listing(task: dict[str, Any]) -> None:
    """Release a listing the task will not consume; absent is a no-op."""
    listing = task.pop(EARLY_MEMORY_LISTING_KEY, None)
    if listing is not None:
        listing.release()


__all__ = [
    'EARLY_MEMORY_LISTING_KEY',
    'release_early_memory_listing',
    'start_early_memory_listing',
]
