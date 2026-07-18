"""lib/memory/prefetch/_inject.py — Injection stage.

Splice the selected memories (full body) into the last user message as a
``<relevant_memories>`` block wrapped in ``<system-reminder>``.
"""
from __future__ import annotations

from lib.log import get_logger

from lib.memory.prefetch._config import PREFETCH_MAX_BYTES

logger = get_logger(__name__)


_RELEVANT_MEMORIES_TAG = '<relevant_memories>'


def _render_relevant_memories_block(selected_memories: list[dict]) -> str:
    """Render the injection block, enforcing PREFETCH_MAX_BYTES."""
    header = (
        'The following memories were pre-selected as likely relevant to '
        "what you're doing in this turn. Read them BEFORE taking action — "
        'they may warn you about traps you previously hit or remind you of '
        'project conventions you previously established. If a memory turns '
        'out not to apply, just ignore it.'
    )
    chunks: list[str] = []
    total = len(header) + len(_RELEVANT_MEMORIES_TAG) * 2 + 200
    for m in selected_memories:
        name = m.get('name', '')
        desc = m.get('description', '')
        body = (m.get('body') or '').strip()
        scope = m.get('scope', 'project')
        fp = m.get('filepath', '')
        chunk = (
            f'### memory: {name}\n'
            f'- scope: {scope}\n'
            f'- description: {desc}\n'
            f'- path: {fp}\n\n'
            f'{body}'
        )
        if total + len(chunk) > PREFETCH_MAX_BYTES:
            # Budget exhausted — truncate remaining bodies to titles + descs
            chunk_short = (
                f'### memory: {name}\n- description: {desc}\n'
                f'- path: {fp}  (body omitted — read with read_files if needed)'
            )
            if total + len(chunk_short) > PREFETCH_MAX_BYTES:
                break
            chunks.append(chunk_short)
            total += len(chunk_short)
            continue
        chunks.append(chunk)
        total += len(chunk)

    body = '\n\n'.join(chunks)
    return (
        f'{_RELEVANT_MEMORIES_TAG}\n'
        f'{header}\n\n{body}\n'
        f'</relevant_memories>'
    )


def inject_relevant_memories(messages: list,
                             selected_memories: list[dict],
                             conv_id: str | None = None) -> None:
    """Inject a <relevant_memories> block into the last user message.

    Wrapped in <system-reminder> so the model knows it's an authoritative
    out-of-band hint, not something the user said.  If the message content
    is already a list-of-blocks, we append a new text block; otherwise we
    convert the string content to a 2-element list for clean cache
    segmentation.

    Args:
        messages: Message list; last user message is mutated in place.
        selected_memories: Memory dicts to inject.
        conv_id: If provided, notify cache_tracking that we legitimately
            mutated the last user message. Without this, the next
            detect_cache_break() call treats the mutation as a
            'PREFIX MUTATION DETECTED' false positive.
    """
    if not selected_memories:
        return
    block = _render_relevant_memories_block(selected_memories)
    reminder = f'<system-reminder>\n{block}\n</system-reminder>'

    injected = False
    # Find last user message
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') != 'user':
            continue
        content = messages[i].get('content', '')
        if isinstance(content, str):
            messages[i]['content'] = [
                {'type': 'text', 'text': content},
                {'type': 'text', 'text': reminder},
            ]
        elif isinstance(content, list):
            messages[i]['content'] = list(content) + [
                {'type': 'text', 'text': reminder},
            ]
        else:
            messages[i]['content'] = [{'type': 'text', 'text': reminder}]
        injected = True
        break

    if injected and conv_id:
        # Tell cache_tracking this mutation is expected so it does NOT
        # false-positive as 'PREFIX MUTATION DETECTED' on the next call.
        try:
            from lib.tasks_pkg.cache_tracking import notify_compaction
            notify_compaction(conv_id)
        except Exception as e:
            logger.debug('[MemPrefetch] notify_compaction unavailable: %s', e)
