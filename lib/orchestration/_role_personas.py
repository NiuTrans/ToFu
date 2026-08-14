"""Read-only projection of runtime role persona designs."""

from __future__ import annotations


def role_persona(role: str | None = None):
    """Return one role persona, or the complete runtime persona catalogue.

    The registry import stays lazy so the lightweight orchestration contract
    does not eagerly load the swarm agent stack. Returned dictionaries are
    projections; authoring clients cannot mutate the runtime prompt registry.
    """
    from lib.swarm.registry import AGENT_ROLES, get_role_model_hint

    def _one(role_name: str) -> dict:
        resolved_role = role_name if role_name in AGENT_ROLES else 'general'
        config = AGENT_ROLES.get(resolved_role) or {}
        return {
            'prompt': (config.get('system_prompt_suffix') or '').strip(),
            'whenToUse': (config.get('when_to_use') or '').strip(),
            'tier': get_role_model_hint(resolved_role),
        }

    if role is not None:
        return _one(role)
    return {role_name: _one(role_name) for role_name in AGENT_ROLES}
