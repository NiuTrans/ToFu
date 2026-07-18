# HOT_PATH
"""Phase B — compress cold tool results.

Faithful extraction of the historical ``micro_compact`` Phase B body,
re-expressed against :class:`CompactionContext` and registered as the
``compact_tool_results`` step.  Records paired-assistant indices in
``ctx.scratch['paired_assistant_indices']`` so a later
``fold_paired_interstitial`` step can co-compact them.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import CompactionContext, register_step
from lib.tasks_pkg.compaction._tokens import _human_size
from lib.tasks_pkg.compaction._builtin_steps._shared import _log_id

logger = get_logger(__name__)


def _find_paired_assistant(messages: list, tool_idx: int) -> int | None:
    """Walk backward from a tool index to its paired assistant(tool_calls)
    message.  Returns None if a user/system boundary is crossed first."""
    for j in range(tool_idx - 1, -1, -1):
        role_j = messages[j].get('role')
        if role_j == 'assistant':
            return j
        if role_j in ('user', 'system'):
            return None
    return None


@register_step('compact_tool_results')
def compact_tool_results(ctx: CompactionContext) -> int:
    """Compress cold tool results outside the hot tail.  Records the
    paired-assistant indices in ``ctx.scratch['paired_assistant_indices']``
    so a later ``fold_paired_interstitial`` step can co-compact them."""
    _c = ctx.constants
    messages = ctx.messages
    conv_id = ctx.conv_id

    paired_assistant_indices: set[int] = ctx.scratch.setdefault(
        'paired_assistant_indices', set())

    tool_indices = [i for i, m in enumerate(messages) if m.get('role') == 'tool']

    cold_indices = []
    if len(tool_indices) <= _c.MICRO_HOT_TAIL:
        logger.debug('[L1] %d tool results ≤ hot-tail size %d, '
                     'skipping Phase B (Phase C image strip may still run)',
                     len(tool_indices), _c.MICRO_HOT_TAIL)
    else:
        cold_indices = tool_indices[:-_c.MICRO_HOT_TAIL]

    compacted_count = 0
    skipped_short = 0
    skipped_already = 0
    tool_tokens_saved = 0

    for idx in cold_indices:
        if ctx.is_in_cache_prefix(idx):
            skipped_already += 1
            continue

        msg = messages[idx]
        content = msg.get('content', '')
        tool_name = msg.get('name', 'tool')
        mutated = False

        # ── Multimodal content (list of content blocks) ──
        if isinstance(content, list):
            text_parts = []
            image_count = 0
            image_chars = 0
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'text':
                    text_parts.append(b.get('text', ''))
                elif b.get('type') == 'image_url':
                    image_count += 1
                    image_chars += len(b.get('image_url', {}).get('url', ''))

            text_len = sum(len(t) for t in text_parts)

            if image_count > 0:
                _before_chars = text_len + image_chars
                text_preview = ' '.join(text_parts).strip()[:200]
                msg['content'] = (
                    f'[{tool_name} result compacted — had {image_count} '
                    f'image(s) ({_human_size(image_chars)} base64) + '
                    f'{text_len:,} chars text — re-call tool if needed]\n'
                    f'Text was: {text_preview}'
                )
                tool_tokens_saved += text_len // 4 + image_count * _c._IMAGE_TOKENS_DEFAULT
                compacted_count += 1
                mutated = True
                ctx.stamp(msg, _before_chars, len(msg['content']))
            elif text_len <= _c.MICRO_COMPACT_THRESHOLD:
                skipped_short += 1
            else:
                _before_chars = text_len
                msg['content'] = (
                    f'[{tool_name} result compacted — was {text_len:,} chars'
                    f' — re-call tool if full content needed]'
                )
                tool_tokens_saved += text_len // 4
                compacted_count += 1
                mutated = True
                ctx.stamp(msg, _before_chars, len(msg['content']))

        # ── Plain-string content ──
        elif isinstance(content, str):
            if content.startswith('[') and 'compacted' in content[:80]:
                skipped_already += 1
            elif content.startswith('[Persisted to:'):
                skipped_already += 1
            elif len(content) <= _c.MICRO_COMPACT_THRESHOLD:
                skipped_short += 1
            else:
                old_len = len(content)
                first_two = '\n'.join(content.split('\n')[:2])
                if len(first_two) > 120:
                    first_two = first_two[:120] + '…'
                placeholder = (
                    f'[{tool_name} result compacted — was {old_len:,} chars]\n'
                    f'Preview: {first_two}\n'
                    f'[Re-call tool if full content needed]'
                )
                msg['content'] = placeholder
                tool_tokens_saved += (old_len - len(placeholder)) // 4
                compacted_count += 1
                mutated = True
                ctx.stamp(msg, old_len, len(placeholder))

        if mutated:
            paired_idx = _find_paired_assistant(messages, idx)
            if paired_idx is not None and not ctx.is_in_cache_prefix(paired_idx):
                paired_assistant_indices.add(paired_idx)

    logger.info('[L1] conv=%s  cold=%d  compacted=%d  '
                'skipped_short=%d  skipped_already=%d  '
                '~%d tokens saved',
                _log_id(conv_id),
                len(cold_indices), compacted_count,
                skipped_short, skipped_already, tool_tokens_saved)
    return tool_tokens_saved
