"""Protocol-extension bindings for a selected route's declared capabilities.

This module grants wire support only, never tool, credential, or owner authority.
Profiles are closed-world except for the installed Codex stateful wire bindings;
explicit unknown/malformed declarations still deny support. ``None`` marks a
legacy caller without a route contract. Core protocol support grants no extensions.
"""
from __future__ import annotations

from collections.abc import Mapping

# Feature names are stable contract keys, not provider or model names. A new
# dialect adds an installed binding here and a focused serializer, not a regex.
_FEATURE_PROTOCOLS = {
    'responses_tool_search': 'responses',
    'responses_programmatic': 'responses',
    'responses_multi_agent': 'responses',
    'responses_compaction': 'responses',
    'responses_websocket': 'responses',
    'responses_prompt_cache': 'responses',

    'responses_cache_breakpoints': 'responses',
    'responses_safety_identifier': 'responses',
    'responses_reasoning_replay': 'responses',

    'responses_reasoning_summary': 'responses',
    'responses_reasoning_mode': 'responses',
    'responses_text_verbosity': 'responses',
    'responses_image_detail': 'responses',
    'anthropic_tool_search': 'anthropic',
    'anthropic_compaction': 'anthropic',
}
_SUBSCRIPTION_FEATURES = frozenset({
    'responses_reasoning_replay', 'responses_prompt_cache',
    'responses_reasoning_summary',
})


def route_feature(profile, feature: str, *, protocol: str, oauth: str = '') -> bool | None:
    """Return a bounded declared binding; None means legacy/no profile only.

    Subscription public-API extensions remain disabled even when misdeclared.
    Raw authorization or probing results cannot widen the installed adapters.
    """
    if profile is None:
        return None
    if not isinstance(profile, Mapping):
        return False
    wire = 'responses' if protocol == 'openai_responses' else protocol
    if _FEATURE_PROTOCOLS.get(feature) != wire:
        return False
    if oauth and feature not in _SUBSCRIPTION_FEATURES:
        return False
    features = profile.get('features')
    if isinstance(features, Mapping) and feature in features:
        return features[feature] == 'supported'
    # Codex's installed Responses dialect requires replay of its encrypted
    # reasoning items with store=false, supports prompt-cache affinity and
    # readable reasoning summaries. These are independent wire capabilities.
    # Catalog reasoning/sampling metadata is not a complete wire-feature list:
    # treating an omitted feature as unsupported makes a successful tool round
    # impossible to continue as soon as the backend emits opaque reasoning.
    # This binding is exclusive to the authenticated Codex dialect; explicit
    # unsupported/unknown declarations still narrow it, and other subscription
    # or keyed Responses routes remain closed-world.
    if oauth == 'codex' and feature in _SUBSCRIPTION_FEATURES:
        return True
    return False


def body_route_profile(body: Mapping):
    """Read the selected-route sidecar, including its rollout-era spelling."""
    return body.get('_route_inference_profile', body.get('_inference_profile'))
