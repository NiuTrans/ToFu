# HOT_PATH
"""Phase A — strip cold thinking / reasoning_content.

Faithful extraction of the historical ``micro_compact`` Phase A body,
re-expressed against :class:`CompactionContext` and registered as the
``strip_thinking`` step.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import CompactionContext, register_step
from lib.tasks_pkg.compaction._builtin_steps._shared import _log_id

logger = get_logger(__name__)


@register_step('strip_thinking')
def strip_thinking(ctx: CompactionContext) -> int:
    """Strip reasoning_content from cold assistant messages, keeping the
    most recent ``_THINKING_HOT_TAIL`` intact."""
    _c = ctx.constants
    messages = ctx.messages
    tokens_saved = 0

    # ── DeepSeek-V4 thinking mode guard ──
    # DeepSeek V4 (pro/flash) in thinking mode rejects an assistant turn
    # whose reasoning_content was emptied with HTTP 400 ("The
    # reasoning_content in the thinking mode must be passed back to the
    # API."). Blanking it here would make the very next round 400 and force
    # an off-DeepSeek fallback. Skip the strip for these models.
    _task = ctx.task or {}
    _model = _task.get('model') or (_task.get('config') or {}).get('model') or ''
    if _model:
        from lib.model_info import model_requires_reasoning_content_replay
        if model_requires_reasoning_content_replay(_model):
            logger.debug('[L1-think] conv=%s  skipping reasoning_content strip '
                         '(model=%s requires reasoning replay in thinking mode)',
                         _log_id(ctx.conv_id), _model)
            return 0

    assistant_indices = [
        i for i, m in enumerate(messages)
        if m.get('role') == 'assistant' and m.get('reasoning_content')
    ]
    thinking_stripped = 0
    if len(assistant_indices) > _c._THINKING_HOT_TAIL:
        cold_thinking = assistant_indices[:-_c._THINKING_HOT_TAIL]
        for idx in cold_thinking:
            if ctx.is_in_cache_prefix(idx):
                continue
            msg = messages[idx]
            rc = msg.get('reasoning_content', '')
            if not rc:
                continue
            rc_len = len(rc) if isinstance(rc, str) else 0
            if rc_len > 0:
                tokens_saved += rc_len // 4
                msg['reasoning_content'] = ''
                thinking_stripped += 1

    if thinking_stripped > 0:
        logger.info('[L1-think] conv=%s  stripped reasoning_content from %d '
                    'cold assistant messages (~%d tokens saved)',
                    _log_id(ctx.conv_id), thinking_stripped, tokens_saved)
    return tokens_saved
