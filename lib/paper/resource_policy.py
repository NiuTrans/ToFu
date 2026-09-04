"""Launch-probed resident budgets owned by Paper reading workflows.

Entry points
------------
``paper_qa_source_cache_capacity`` bounds the process-wide reconstructible
source working set used by repeated Q&A starts. Prompt/source semantics remain
in their domain owners; this module only resolves resource policy.
"""

from __future__ import annotations

from collections.abc import Mapping

from runtime_guards import resolve_resource_budget


PAPER_QA_SOURCE_CACHE_HARD_CAPACITY = 32
PAPER_QA_SOURCE_CACHE_TTL_SECONDS = 600


def paper_qa_source_cache_capacity(
    environment: Mapping[str, str] | None = None,
) -> int:
    """Return the finite process-wide active-paper source capacity."""
    return resolve_resource_budget(
        'TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY',
        environment,
        minimum=1,
        maximum=PAPER_QA_SOURCE_CACHE_HARD_CAPACITY,
    )


__all__ = [
    'PAPER_QA_SOURCE_CACHE_HARD_CAPACITY',
    'PAPER_QA_SOURCE_CACHE_TTL_SECONDS',
    'paper_qa_source_cache_capacity',
]
