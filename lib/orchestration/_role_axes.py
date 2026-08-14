"""Pure role execution-axis vocabulary and fallback resolution."""

from __future__ import annotations


#: Role names understood by the current executor/authoring catalogue. Unknown
#: roles remain a validation warning so rolling or user-defined roles survive.
KNOWN_ROLES = frozenset({
    'researcher', 'coder', 'analyst', 'browser', 'reviewer', 'writer', 'general',
    'planner', 'worker', 'critic', 'synthesizer', 'router', 'virtual_user',
})

# Stable presentation order for every execution-axis option exposed to clients.
EXECUTION_OPTION_ORDER = {
    'tiers': ('light', 'standard', 'heavy'),
    'isolation': ('fresh-context', 'shared-context'),
    'emits': ('assistant', 'user'),
    # Authoring defaults new Groups to isolated; absent legacy values stay inline.
    'scopes': ('isolated', 'inline'),
}

VALID_EMITS = frozenset(EXECUTION_OPTION_ORDER['emits'])
VALID_TIERS = frozenset(EXECUTION_OPTION_ORDER['tiers'])
VALID_ISOLATION = frozenset(EXECUTION_OPTION_ORDER['isolation'])
VALID_SCOPES = frozenset(EXECUTION_OPTION_ORDER['scopes'])

DEFAULT_ROLE_TIER = 'standard'
DEFAULT_ROLE_ISOLATION = 'fresh-context'

# Shared vocabulary for default direction, feedback routing and diagnostics.
VERIFIER_ROLES = frozenset({'critic', 'reviewer', 'virtual_user'})

# Compatibility alias retained for consumers predating the public name.
_USER_EMIT_ROLES = VERIFIER_ROLES


def _node_and_params(node: dict) -> tuple[dict, dict]:
    """Normalize persisted compatibility input at the policy boundary."""
    normalized = node if isinstance(node, dict) else {}
    params = normalized.get('params') or {}
    return normalized, params if isinstance(params, dict) else {}


def resolve_emits(node: dict) -> str:
    """Resolve a node's message side, falling back to its role semantics."""
    node, params = _node_and_params(node)
    explicit = params.get('emits')
    if explicit in VALID_EMITS:
        return explicit
    return 'user' if (node.get('role') or '') in VERIFIER_ROLES else 'assistant'


def resolve_tier(node: dict) -> str:
    """Resolve a role's model tier through the shared compatibility policy."""
    _, params = _node_and_params(node)
    tier = params.get('tier')
    return tier if tier in VALID_TIERS else DEFAULT_ROLE_TIER


def resolve_isolation(node: dict) -> str:
    """Resolve a role's context isolation through the shared policy."""
    _, params = _node_and_params(node)
    isolation = params.get('isolation')
    return (
        isolation
        if isolation in VALID_ISOLATION
        else DEFAULT_ROLE_ISOLATION
    )


def resolve_scope(node: dict) -> str:
    """Resolve subflow scope; missing/invalid legacy values remain inline."""
    _, params = _node_and_params(node)
    scope = params.get('scope')
    return scope if scope in VALID_SCOPES else 'inline'
