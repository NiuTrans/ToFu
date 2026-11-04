"""Legacy native-compaction dialect capability facts, not policy selection.

The local compactor owns all new rewrites. Wire adapters retain recognition of
existing provider compaction items; declaring support never enables a second
summarization policy. Subscription backends do not inherit public API fields.
"""

from __future__ import annotations

from urllib.parse import urlparse

OPENAI_RESPONSES_COMPACTION = 'openai_responses'
ANTHROPIC_MESSAGES_COMPACTION = 'anthropic_messages'


def _is_official_anthropic_api(base_url: str) -> bool:
    try:
        return (urlparse(str(base_url or '')).hostname or '').lower() == (
            'api.anthropic.com')
    except (TypeError, ValueError):
        return False


def _supports_anthropic_compaction_model(model: str) -> bool:
    """Match the model families in Anthropic's compaction compatibility row."""
    normalized = str(model or '').lower().replace('.', '-')
    supported_markers = (
        'fable-5',
        'mythos-5',
        'opus-5',
        'sonnet-5',
        'opus-4-6',
        'opus-4-7',
        'opus-4-8',
        'sonnet-4-6',
    )
    return any(marker in normalized for marker in supported_markers)


def native_compaction_mode_for_route(
    *,
    protocol: str,
    model: str,
    responses_profile: str = '',
    base_url: str = '',
    oauth: str = '',
    inference_profile=None,
) -> str:
    """Return the native compaction dialect for one concrete wire route.

    An empty string means local compaction remains authoritative.  The checks
    are intentionally strict: an OpenAI-compatible or Anthropic-compatible
    gateway does not inherit a vendor beta merely by copying the core wire.
    """
    wire = str(protocol or 'openai').strip().lower()
    oauth_kind = str(oauth or '').strip().lower()

    if inference_profile is not None:
        from lib.llm.route_features import route_feature
        if route_feature(inference_profile, 'responses_compaction',
                         protocol=wire, oauth=oauth_kind):
            return OPENAI_RESPONSES_COMPACTION
        if route_feature(inference_profile, 'anthropic_compaction',
                         protocol=wire, oauth=oauth_kind):
            return ANTHROPIC_MESSAGES_COMPACTION
        return ''
    if oauth_kind:
        return ''

    if wire == 'responses':
        from lib.model_info._openai_gpt56 import is_official_gpt56_model

        if (str(responses_profile or '').strip().lower() == 'openai'
                and is_official_gpt56_model(str(model or '').lower())):
            return OPENAI_RESPONSES_COMPACTION
        return ''

    if (wire == 'anthropic'
            and _is_official_anthropic_api(base_url)
            and _supports_anthropic_compaction_model(model)):
        return ANTHROPIC_MESSAGES_COMPACTION
    return ''


__all__ = [
    'ANTHROPIC_MESSAGES_COMPACTION',
    'OPENAI_RESPONSES_COMPACTION',
    'native_compaction_mode_for_route',
]
