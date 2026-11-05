"""Server-side execution profiles that narrow a task below its owner's account.

Responsibility: the one restricted profile, ``THIRD_PARTY_TEXT``, for input a
third party sends through an owner's integration (Feishu default-owner
senders, docs/modules/integrations_api.md "Feishu bridge authority"): a plain
model reply. No tools, no owner context (memory, My Context, skills,
credentials, project, environment) and no owner-data writes.

Only the trusted task constructor attaches a profile
(``create_task(execution_profile=...)``, reached through ``run_task_sync``);
it is never read from request configuration, so no config key can widen it.
Enforcement, each keyed on ``is_third_party_text(task)``: ``create_task``
replaces the request config with ``restricted_task_config``; tool assembly
(``orchestrator._tool_assembly_prep.assemble_round_tools``) admits no tool;
the Context Composer renders only ``THIRD_PARTY_CONTEXT_BLOCKS``; the turn is
never eligible for My Context consolidation (``orchestrator._memory_prefetch``).

Entry points: ``THIRD_PARTY_TEXT``, ``validate_execution_profile``,
``restricted_task_config``, ``mark_task``, ``is_third_party_text``,
``THIRD_PARTY_CONTEXT_BLOCKS``. Dependencies: ``lib.agent_core.personal_scope``.
"""
from __future__ import annotations

from typing import Any

THIRD_PARTY_TEXT = 'third_party_text'
EXECUTION_PROFILES = frozenset({THIRD_PARTY_TEXT})

# Context Composer registry IDs this profile renders: the platform contract
# and the date. Every other source is owner context or tool guidance.
THIRD_PARTY_CONTEXT_BLOCKS = frozenset({'platform_static', 'current_date'})

_TASK_FIELD = '_executionProfile'
_PASSTHROUGH_CONFIG = ('model', 'stream')


def validate_execution_profile(profile: Any) -> str | None:
    if profile is not None and profile not in EXECUTION_PROFILES:
        raise ValueError(f'unknown execution profile {profile!r}')
    return profile


def restricted_task_config(config: dict) -> dict:
    """The complete config of a third-party task, rebuilt rather than merged.

    Only the model selection passes through; every capability is pinned off,
    so no request key can bring back a tool, a project or a personal source.
    """
    from lib.agent_core.personal_scope import apply_headless_personal_defaults
    restricted = {key: config[key] for key in _PASSTHROUGH_CONFIG if key in config}
    restricted.update({
        'searchMode': 'off', 'fetchEnabled': False, 'codeExecEnabled': False,
        'browserEnabled': False, 'desktopEnabled': False, 'imageGenEnabled': False,
        'schedulerEnabled': False, 'humanGuidanceEnabled': False, 'plugins': [],
    })
    return apply_headless_personal_defaults(restricted)


def mark_task(task: dict, profile: str | None) -> None:
    if profile is not None:
        task[_TASK_FIELD] = validate_execution_profile(profile)


def is_third_party_text(task: Any) -> bool:
    return isinstance(task, dict) and task.get(_TASK_FIELD) == THIRD_PARTY_TEXT


__all__ = ['EXECUTION_PROFILES', 'THIRD_PARTY_CONTEXT_BLOCKS', 'THIRD_PARTY_TEXT',
           'is_third_party_text', 'mark_task', 'restricted_task_config',
           'validate_execution_profile']
