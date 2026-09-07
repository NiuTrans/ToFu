"""Provider-native context-compaction capability resolution.

Responsibility
--------------
Keep wire/provisioning facts out of the provider-neutral compaction engine.
The request boundary uses :func:`native_compaction_mode_for_route` for the
exact selected slot; the task orchestrator uses
:func:`resolve_task_native_compaction_mode` before dispatch and only trusts a
native-first decision when every eligible slot agrees.

Subscription OAuth is deliberately excluded.  Codex and Claude Code tokens
authenticate different product backends; public API beta fields are not a
portable capability of those credentials.
"""

from __future__ import annotations

from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)

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
) -> str:
    """Return the native compaction dialect for one concrete wire route.

    An empty string means local compaction remains authoritative.  The checks
    are intentionally strict: an OpenAI-compatible or Anthropic-compatible
    gateway does not inherit a vendor beta merely by copying the core wire.
    """
    wire = str(protocol or 'openai').strip().lower()
    oauth_kind = str(oauth or '').strip().lower()
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


def resolve_task_native_compaction_mode(
    task: dict | None,
    *,
    model: str,
) -> str:
    """Resolve a safe pre-dispatch native-first decision for one task.

    Before a request is dispatched there may be several eligible provider
    slots.  We defer local L2 only when all matching slots expose the same
    native dialect.  Mixed or unknown pools keep the lossless local fallback;
    the exact selected route can still enable its native field at the wire
    boundary.
    """
    if not model:
        return ''
    task = task or {}
    config = task.get('config') or {}
    provider_id = str(
        task.get('provider_id')
        or task.get('_pinned_provider_id')
        or config.get('providerId')
        or config.get('provider_id')
        or ''
    )

    try:
        from lib.llm_dispatch.factory import get_dispatcher

        dispatcher = get_dispatcher()
        initialize = getattr(dispatcher, 'initialize', None)
        if callable(initialize):
            initialize()
        candidates = []
        for slot in list(dispatcher.slots):
            if provider_id and str(slot.provider_id or '') != provider_id:
                continue
            if model not in (str(slot.logical_model or ''), str(slot.model or '')):
                continue
            candidates.append(slot)
    except Exception as exc:
        logger.debug('[CompactionPolicy] slot resolution failed: %s', exc)
        return ''

    if not candidates:
        return ''
    modes = {
        native_compaction_mode_for_route(
            protocol=slot.protocol or 'openai',
            model=slot.model or model,
            responses_profile=slot.responses_profile or '',
            base_url=slot.base_url or '',
            oauth=slot.oauth or '',
        )
        for slot in candidates
    }
    if len(modes) != 1:
        return ''
    return next(iter(modes))


__all__ = [
    'ANTHROPIC_MESSAGES_COMPACTION',
    'OPENAI_RESPONSES_COMPACTION',
    'native_compaction_mode_for_route',
    'resolve_task_native_compaction_mode',
]
