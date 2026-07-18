# HOT_PATH
"""Phase C — aggressively strip cold images.

Faithful extraction of the historical ``micro_compact`` Phase C body,
re-expressed against :class:`CompactionContext` and registered as the
``strip_cold_images`` step.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import CompactionContext, register_step
from lib.tasks_pkg.compaction._tokens import _human_size
from lib.tasks_pkg.compaction._builtin_steps._shared import _log_id

logger = get_logger(__name__)


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
