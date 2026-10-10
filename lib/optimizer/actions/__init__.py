"""lib/optimizer/actions — Whitelisted action handlers.

Each handler module exports:
    ACTION = {
        'name': 'block_search_domain',
        'auto_apply': True,
        'description': 'Add a domain to server_config.search.skip_domains for N days',
        'apply': apply_fn,      # (args: dict) -> dict  (raises on failure)
        'revert': revert_fn,    # (args: dict) -> None   (must be idempotent)
    }

``ACTION_REGISTRY`` is the canonical whitelist consulted by the applier
AND by the LLM prompt.  Actions missing ``auto_apply=True`` are always
stored as ``pending_review`` by the applier.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from runtime_guards import resolve_deployment_mode

from . import block_search_domain as _block_search_domain

logger = get_logger(__name__)


# Suggest-only action types: no apply/revert handler, always land in
# ``pending_review``.  Per CLAUDE.md §10 (hyperparameters, model routing,
# DB schema, security), these require an explicit human approve step.
_SUGGEST_ONLY: dict[str, str] = {
    'adjust_fetch_timeout':
        'Adjust FETCH_TIMEOUT based on observed fetch failures.',
    'adjust_fetch_top_n':
        'Adjust FETCH_TOP_N based on citation vs fetch ratio.',
    'toggle_llm_content_filter':
        'Enable/disable the LLM content filter depending on IRRELEVANT drop rate.',
    'suggest_model_fallback':
        'Switch FALLBACK_MODEL when the primary model is degrading.',
    'extend_cache_ttl':
        'Flip CACHE_EXTENDED_TTL on/off based on prefix reuse patterns.',
    'disable_failing_scheduled_task':
        'Disable a scheduled task whose fail_count keeps growing.',
    'relax_proactive_poll_schedule':
        'Widen a proactive agent schedule that never decides "act".',
    'tighten_compaction_threshold':
        'Tune compaction thresholds when turns repeatedly hit the context ceiling.',
    'promote_memory_note':
        'Promote a recurring answer/note into project or global memory.',
    'raise_rate_limit_cooldown':
        'Lengthen cooldown for a key/model pair that keeps hitting 429.',
    'flag_expensive_model_pair':
        'Flag outlier (conversation, model) cost pairs for review.',
    'other':
        'Free-form improvement idea that does not fit any other category.',
}


def action_available_in_this_deployment(entry: dict[str, Any]) -> bool:
    """Fail closed when an action only has a personal persistent-state owner."""
    allowed_modes = entry.get('deployment_modes')
    if not isinstance(allowed_modes, (set, frozenset, tuple, list)):
        return False
    return resolve_deployment_mode() in allowed_modes

ACTION_REGISTRY: dict[str, dict[str, Any]] = {
    _block_search_domain.ACTION['name']: _block_search_domain.ACTION,
}
for _name, _desc in _SUGGEST_ONLY.items():
    ACTION_REGISTRY[_name] = {
        'name': _name,
        'auto_apply': False,
        'description': _desc + ' (pending_review — human must approve)',
        'apply': None,
        'revert': None,
        'deployment_modes': frozenset({'personal', 'distributed'}),
    }


def get_action(name: str) -> dict | None:
    return ACTION_REGISTRY.get(name)


__all__ = [
    'ACTION_REGISTRY',
    'action_available_in_this_deployment',
    'get_action',
]
