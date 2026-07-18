# HOT_PATH
"""Phase B2 — co-compact paired interstitial assistant commentary (gated).

Faithful extraction of the historical ``micro_compact`` Phase B2 body,
re-expressed against :class:`CompactionContext` and registered as the
``fold_paired_interstitial`` step.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import CompactionContext, register_step
from lib.tasks_pkg.compaction._builtin_steps._shared import _log_id

logger = get_logger(__name__)


@register_step('fold_paired_interstitial')
def fold_paired_interstitial(ctx: CompactionContext) -> int:
    """Compact the interstitial ``content`` on assistant(tool_calls)
    messages whose paired tool result was compacted by
    ``compact_tool_results``.  A/B-verified -1.4% cache writes vs B-only
    (2026-04-27, debug/test_paired_compact_live.py)."""
    messages = ctx.messages
    paired_assistant_indices = ctx.scratch.get('paired_assistant_indices') or set()
    if not paired_assistant_indices:
        return 0

    _PAIRED_COMMENTARY_THRESHOLD = 200
    _PAIRED_PREVIEW_LEN = 100

    paired_assistants_compacted = 0
    paired_assistants_tokens_saved = 0

    for idx in sorted(paired_assistant_indices):
        msg = messages[idx]
        content = msg.get('content', '')

        if isinstance(content, str) and content:
            if content.startswith('[Interstitial compacted'):
                continue
            if len(content) <= _PAIRED_COMMENTARY_THRESHOLD:
                continue
            old_len = len(content)
            preview = content[:_PAIRED_PREVIEW_LEN].rstrip()
            if len(content) > _PAIRED_PREVIEW_LEN:
                preview += '…'
            new_content = (
                f'[Interstitial compacted — was {old_len:,} chars] {preview}'
            )
            msg['content'] = new_content
            paired_assistants_tokens_saved += (old_len - len(new_content)) // 4
            paired_assistants_compacted += 1

        elif isinstance(content, list):
            text_blocks = [
                (i, b) for i, b in enumerate(content)
                if isinstance(b, dict) and b.get('type') == 'text'
            ]
            total_text = sum(len(b.get('text', '')) for _, b in text_blocks)
            if total_text <= _PAIRED_COMMENTARY_THRESHOLD:
                continue
            if text_blocks and text_blocks[0][1].get('text', '').startswith(
                    '[Interstitial compacted'):
                continue
            combined = ''.join(b.get('text', '') for _, b in text_blocks)
            preview = combined[:_PAIRED_PREVIEW_LEN].rstrip()
            if len(combined) > _PAIRED_PREVIEW_LEN:
                preview += '…'
            new_text = (
                f'[Interstitial compacted — was {total_text:,} chars] {preview}'
            )
            new_content = []
            text_replaced = False
            for b in content:
                if isinstance(b, dict) and b.get('type') == 'text':
                    if not text_replaced:
                        new_content.append({'type': 'text', 'text': new_text})
                        text_replaced = True
                else:
                    new_content.append(b)
            msg['content'] = new_content
            paired_assistants_tokens_saved += (total_text - len(new_text)) // 4
            paired_assistants_compacted += 1

    if paired_assistants_compacted > 0:
        logger.info(
            '[L1-pair] conv=%s  co-compacted %d paired assistant interstitials '
            '(~%d tokens saved; A/B-verified -1.4%% cache writes vs B-only)',
            _log_id(ctx.conv_id),
            paired_assistants_compacted, paired_assistants_tokens_saved,
        )
    return paired_assistants_tokens_saved
