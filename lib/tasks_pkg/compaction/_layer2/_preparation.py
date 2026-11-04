"""Prepare L1's deterministic projection inside the L2 adoption transaction.

The existing step registry owns transforms and preservation. This pass has no
cache fence, persistence, telemetry or model capability. Unchanged messages keep
their original identity for source attribution and managed restoration accounting.
"""
from __future__ import annotations

from copy import deepcopy


def prepare_l2_projection(messages: list, task: dict | None) -> list:
    """Return L1 edits as a detached candidate; L2 alone may adopt them."""
    config = ((task or {}).get('config') or {}).get('compaction') or {}
    if not isinstance(config, dict):
        config = {}
    if config.get('disableDefaultL1') or (task or {}).get('aborted'):
        return messages
    from lib.tasks_pkg.compaction import _constants
    from lib.tasks_pkg.compaction._steps import CompactionContext, make_constants, run_steps
    import lib.tasks_pkg.compaction._builtin_steps  # noqa: F401

    # Only the existing LLM-free builtins participate. Custom/structural
    # experiment steps retain their own host and are not replayed here.
    steps = ['strip_thinking', 'compact_tool_results', 'strip_cold_images']
    if isinstance(config.get('steps'), list):
        steps = [step for step in steps if step in config['steps']]
    if not steps:
        return messages
    candidate = deepcopy(messages)
    run_steps(steps, CompactionContext(
        messages=candidate, task=task,
        constants=make_constants(_constants, config.get('constant_overrides')),
        ignore_cache_prefix=True, dry_run=True))
    if len(candidate) != len(messages):
        raise ValueError('L2 preparation requires content-only L1 transforms')
    return [original if projected == original else projected
            for original, projected in zip(messages, candidate)]
