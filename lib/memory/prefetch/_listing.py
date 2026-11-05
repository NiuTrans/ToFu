"""Prefetch corpus listing, run inline or started before its ranking.

Responsibility: ``list_prefetch_corpus`` is the one definition of the corpus
the high-confidence prefetch ranks: eligible, metadata-only retrieval rows of
the attempt's memory roots. The listing does not depend on the query, so
``PrefetchCorpusListing`` runs it on a caller-supplied executor before the
query exists and answers ``run_memory_prefetch`` for the same roots.

Freshness: the listing goes through ``list_all_memories``, whose coalescer
shares only scans that started after the call, so a listing started at attempt
acceptance reflects the corpus at or after acceptance (docs/DEFENSIVE_PATTERNS.md,
"An accepted attempt lists its memory corpus at acceptance").

Entry points: ``list_prefetch_corpus``, ``PrefetchCorpusListing``.
Dependencies: ``lib.memory.storage`` only; the executor is the caller's.
"""

from __future__ import annotations

import time


def list_prefetch_corpus(project_path, extra_paths):
    """Eligible metadata-only retrieval rows the prefetch ranks."""
    from lib.memory.storage import get_eligible_memories

    return get_eligible_memories(
        project_path,
        extra_paths=extra_paths,
        include_body=False,
        record_view='retrieval',
    )


class PrefetchCorpusListing:
    """A corpus listing started before its prefetch ranks it.

    ``submit`` schedules a zero-argument callable and returns its Future.
    ``roots`` is the exact ``(project_path, extra_paths)`` request the listing
    answers; ``listing_ms`` is the listing's own duration once it finished.
    """

    def __init__(self, project_path, extra_paths, *, submit):
        self.roots = _roots(project_path, extra_paths)
        self.listing_ms = None
        self._ran = False
        self._future = submit(self._list)

    def _list(self):
        self._ran = True
        started = time.monotonic()
        try:
            return list_prefetch_corpus(self.roots[0], list(self.roots[1]))
        finally:
            self.listing_ms = (time.monotonic() - started) * 1000

    def take(self, project_path, extra_paths):
        """Return the rows for these roots, joining a listing still running.

        A listing for other roots, or one that never ran (scheduling failed or
        it was released), answers ``None`` so the caller lists inline. A listing
        that ran and failed raises its own error, exactly as an inline listing.
        """
        if self.roots != _roots(project_path, extra_paths):
            return None
        try:
            return self._future.result()
        except Exception:
            if self._ran:
                raise
            return None

    def release(self):
        """Withdraw an unconsumed listing; a scan already running ends unused."""
        self._future.cancel()


def _roots(project_path, extra_paths):
    return project_path, tuple(extra_paths or ())


__all__ = ['PrefetchCorpusListing', 'list_prefetch_corpus']
