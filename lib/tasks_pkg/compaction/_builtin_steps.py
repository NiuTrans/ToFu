# HOT_PATH
"""Built-in Layer-1 compaction steps (the former Phase A–D of micro_compact).

Each function here is a faithful extraction of one phase of the historical
``micro_compact`` body, re-expressed against :class:`CompactionContext`.
They are registered by name so they can be ordered / ablated / replaced
purely by configuration (see ``_steps.py``).

Invariants preserved from the monolith:
  * Cache-aware: every step skips indices inside the prompt-cache prefix
    via ``ctx.is_in_cache_prefix(idx)``.
  * Durable placeholders: tool-result compaction calls ``ctx.stamp(...)``
    which wraps the ``toolContent`` write-back + ``tool_compacted`` SSE
    emit owned by the ``micro_compact`` shell.
  * Zero LLM cost — none of these call the model (enforced by
    ``tests/test_compaction_invariants.py::test_layer1_does_not_invoke_dispatch_chat``).

Step order in the default L1 pass (must match the old phase order for
byte-identical output):
    strip_thinking → compact_tool_results → [fold_paired_interstitial]
    → strip_cold_images → [compact_cold_assistant]
The two bracketed steps are gated (paired / assistant compaction) and are
only included in the step list when their respective flags are set.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import CompactionContext, register_step
from lib.tasks_pkg.compaction._tokens import _human_size

logger = get_logger(__name__)


def _log_id(conv_id: str) -> str:
    return conv_id[:8] if conv_id else '?'


# ═══════════════════════════════════════════════════════════════════════════════
#  Phase A — strip cold thinking / reasoning_content
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
#  Phase B — compress cold tool results
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
#  Phase B2 — co-compact paired interstitial assistant commentary (gated)
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
#  Phase C — aggressively strip cold images
# ═══════════════════════════════════════════════════════════════════════════════

@register_step('strip_cold_images')
def strip_cold_images(ctx: CompactionContext) -> int:
    """Strip cold image tool results, keeping only the 2 most recent.
    Images cost massive context (base64 data URLs) and can't be
    re-searched, so a tight hot tail is safe."""
    _c = ctx.constants
    messages = ctx.messages

    _IMAGE_HOT_TAIL = 2
    image_tool_indices = [
        i for i, m in enumerate(messages)
        if m.get('role') == 'tool'
        and isinstance(m.get('content'), list)
        and any(
            isinstance(b, dict) and b.get('type') == 'image_url'
            for b in m['content']
        )
    ]
    images_stripped = 0
    image_tokens_saved = 0
    image_chars = 0

    if len(image_tool_indices) > _IMAGE_HOT_TAIL:
        cold_image_indices = image_tool_indices[:-_IMAGE_HOT_TAIL]
        for idx in cold_image_indices:
            if ctx.is_in_cache_prefix(idx):
                continue
            msg = messages[idx]
            content = msg['content']
            tool_name = msg.get('name', 'tool')

            image_count = 0
            image_chars = 0
            text_parts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'image_url':
                    image_count += 1
                    image_chars += len(b.get('image_url', {}).get('url', ''))
                elif b.get('type') == 'text':
                    text_parts.append(b.get('text', ''))

            if image_count == 0:
                continue

            text_preview = ' '.join(text_parts).strip()[:200]
            msg['content'] = (
                f'[{tool_name} image compacted — had {image_count} '
                f'image(s) ({_human_size(image_chars)} base64) — '
                f're-call tool if image needed]\n'
                f'Text was: {text_preview}'
            )
            image_tokens_saved += image_count * _c._IMAGE_TOKENS_DEFAULT
            images_stripped += 1

    if images_stripped > 0:
        logger.info('[L1-img] conv=%s  stripped %d cold image tool results '
                    '(~%d vision tokens, %s base64 data freed)',
                    _log_id(ctx.conv_id), images_stripped, image_tokens_saved,
                    _human_size(image_chars))
    return image_tokens_saved


# ═══════════════════════════════════════════════════════════════════════════════
#  Phase D — compact cold assistant message content (gated)
# ═══════════════════════════════════════════════════════════════════════════════

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


# Default ungated L1 step order (matches the historical phase order).
DEFAULT_L1_STEPS = ['strip_thinking', 'compact_tool_results', 'strip_cold_images']
