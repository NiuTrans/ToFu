"""Render selected memory evidence for Context Composer."""
from __future__ import annotations

from lib.log import get_logger

from lib.memory.prefetch._config import PREFETCH_MAX_BYTES, PREFETCH_MAX_TOKENS

logger = get_logger(__name__)


_RELEVANT_MEMORIES_TAG = '<relevant_memories>'


def _render_relevant_memories_block(selected_memories: list[dict]) -> str:
    """Render evidence within both the token and defensive byte budgets."""
    header = (
        'The following memories were pre-selected as likely relevant to '
        "what you're doing in this turn. Read them BEFORE taking action — "
        'they may warn you about traps you previously hit or remind you of '
        'project conventions you previously established. If a memory turns '
        'out not to apply, just ignore it.'
    )
    chunks: list[str] = []
    total = len(header) + len(_RELEVANT_MEMORIES_TAG) * 2 + 200

    def _tokens(text: str) -> int:
        try:
            from lib.token_counter import count_text
            return int(count_text(text))
        except Exception as exc:
            logger.debug('[MemoryPrefetch] token counter fallback: %s', exc)
            return (len(text) + 3) // 4
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
        projected = total + len(chunk)
        if (projected > PREFETCH_MAX_BYTES
                or _tokens('\n\n'.join([header, *chunks, chunk]))
                > PREFETCH_MAX_TOKENS):
            # Budget exhausted — truncate remaining bodies to titles + descs
            chunk_short = (
                f'### memory: {name}\n- description: {desc}\n'
                f'- path: {fp}  (body omitted — read with read_files if needed)'
            )
            if (total + len(chunk_short) > PREFETCH_MAX_BYTES
                    or _tokens('\n\n'.join([header, *chunks, chunk_short]))
                    > PREFETCH_MAX_TOKENS):
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


__all__ = ['_RELEVANT_MEMORIES_TAG', '_render_relevant_memories_block']
