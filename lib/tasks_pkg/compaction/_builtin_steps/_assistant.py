# HOT_PATH
"""Phase D — compact cold assistant message content (gated).

Faithful extraction of the historical ``micro_compact`` Phase D body,
re-expressed against :class:`CompactionContext` and registered as the
``compact_cold_assistant`` step.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import CompactionContext, register_step
from lib.tasks_pkg.compaction._builtin_steps._shared import _log_id

logger = get_logger(__name__)


@register_step('compact_cold_assistant')
def compact_cold_assistant(ctx: CompactionContext) -> int:
    """Compact verbose cold assistant message content.  Off by default —
    A/B testing (2026-04-06) showed it busts the prompt cache (+57% cost)
    during normal runs; only enabled when a cache rebuild is already
    expected (force_compact / reactive)."""
    messages = ctx.messages

    _ASSISTANT_HOT_TAIL = 6
    _ASSISTANT_COMPACT_THRESHOLD = 800

    all_assistant_indices = [
        i for i, m in enumerate(messages) if m.get('role') == 'assistant'
    ]

    assistant_compacted = 0
    assistant_tokens_saved = 0

    if len(all_assistant_indices) > _ASSISTANT_HOT_TAIL:
        cold_assistant = all_assistant_indices[:-_ASSISTANT_HOT_TAIL]
        for idx in cold_assistant:
            msg = messages[idx]
            content = msg.get('content', '')

            if not content:
                continue

            if isinstance(content, str):
                if len(content) <= _ASSISTANT_COMPACT_THRESHOLD:
                    continue
                if content.startswith('[Assistant response compacted'):
                    continue
                old_len = len(content)
                preview = content[:200].rstrip()
                if not preview.endswith('…') and len(content) > 200:
                    preview += '…'
                msg['content'] = (
                    f'[Assistant response compacted — was {old_len:,} chars]\n'
                    f'{preview}'
                )
                saved = (old_len - len(msg['content'])) // 4
                assistant_tokens_saved += saved
                assistant_compacted += 1

            elif isinstance(content, list):
                total_text_len = 0
                for blk in content:
                    if isinstance(blk, dict) and blk.get('type') == 'text':
                        total_text_len += len(blk.get('text', ''))
                if total_text_len <= _ASSISTANT_COMPACT_THRESHOLD:
                    continue
                text_parts = []
                for blk in content:
                    if isinstance(blk, dict) and blk.get('type') == 'text':
                        text_parts.append(blk.get('text', ''))
                full_text = '\n'.join(text_parts)
                if full_text.startswith('[Assistant response compacted'):
                    continue
                preview = full_text[:200].rstrip()
                if not preview.endswith('…') and len(full_text) > 200:
                    preview += '…'
                msg['content'] = (
                    f'[Assistant response compacted — was {total_text_len:,} chars]\n'
                    f'{preview}'
                )
                saved = (total_text_len - len(msg['content'])) // 4
                assistant_tokens_saved += saved
                assistant_compacted += 1

    if assistant_compacted > 0:
        logger.info('[L1-asst] conv=%s  compacted %d cold assistant messages '
                    '(~%d tokens saved)',
                    _log_id(ctx.conv_id), assistant_compacted, assistant_tokens_saved)
    return assistant_tokens_saved
