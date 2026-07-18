"""Phase 0 / 0.5 payload-shrinking helpers for reactive compaction.

  * ``_strip_images_aggressive`` — Phase 0 OOM protection (memory:
    ``micro-compact-image-strip-bug-fix``): drop all but the most-recent
    ``keep_tail`` ``image_url`` blocks once a 413 has already fired.
  * ``_truncate_largest_message`` — Phase 0.5 in-place head+tail truncation
    of the single largest tool-result text (memory: conv mqgfkmxy fix).
"""

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import _WIRE_IMAGE_KEEP_TAIL
from lib.tasks_pkg.compaction._tokens import _human_size

logger = get_logger(__name__)


def _strip_images_aggressive(messages: list,
                             keep_tail: int = _WIRE_IMAGE_KEEP_TAIL,
                             ) -> tuple[int, int]:
    """Strip all ``image_url`` blocks except the most-recent ``keep_tail``.

    Used by reactive_compact when a 413 has already fired — at that point
    the normal hot-tail protection is overridden because the gateway has
    proven the payload is too big. Each stripped image is replaced with a
    short textual placeholder so the model knows something was there.

    Returns (stripped_count, bytes_freed_estimate).
    """
    image_positions: list[tuple[int, int]] = []
    for mi, msg in enumerate(messages):
        content = msg.get('content')
        if not isinstance(content, list):
            continue
        for bi, blk in enumerate(content):
            if isinstance(blk, dict) and blk.get('type') == 'image_url':
                image_positions.append((mi, bi))

    if len(image_positions) <= keep_tail:
        return 0, 0

    to_strip = image_positions[:-keep_tail] if keep_tail > 0 else image_positions
    stripped = 0
    bytes_freed = 0

    by_msg: dict[int, list[int]] = {}
    for mi, bi in to_strip:
        by_msg.setdefault(mi, []).append(bi)

    for mi, bi_list in by_msg.items():
        content = messages[mi].get('content')
        if not isinstance(content, list):
            continue
        for bi in sorted(bi_list, reverse=True):
            if bi >= len(content):
                continue
            blk = content[bi]
            if not (isinstance(blk, dict) and blk.get('type') == 'image_url'):
                continue
            url = blk.get('image_url', {}).get('url', '')
            bytes_freed += len(url)
            content[bi] = {
                'type': 'text',
                'text': '[image removed during emergency compaction — ask again if needed]',
            }
            stripped += 1

    return stripped, bytes_freed


def _truncate_largest_message(messages: list, *, ceiling_chars: int) -> tuple[int, int]:
    """Head+tail-truncate the single largest tool-result text in place.

    Layer-4 remediation for the failure mode that made conv mqgfkmxy fatal:
    when ONE tool message carries the entire overflow (e.g. 1.7MB of binary
    decoded as text), dropping whole messages (``_head_truncate``) can't help
    because the offending message is in the protected tail AND the byte
    target is reached before it's dropped. This shrinks *within* the worst
    message so the overflow is actually removed.

    Only string tool/user/assistant content is touched; ``image_url`` /
    multimodal list content is left to ``_strip_images_aggressive``.

    Args:
        messages:      Live message list (mutated in place).
        ceiling_chars: Clamp the worst message's text to roughly this size.

    Returns:
        ``(truncated_index, chars_freed)`` — ``(-1, 0)`` if nothing exceeded
        the ceiling.
    """
    worst_idx = -1
    worst_len = ceiling_chars
    for i, msg in enumerate(messages):
        if msg.get('role') == 'system':
            continue
        content = msg.get('content')
        if isinstance(content, str) and len(content) > worst_len:
            worst_len = len(content)
            worst_idx = i

    if worst_idx < 0:
        return -1, 0

    original = messages[worst_idx]['content']
    head_budget = int(ceiling_chars * 0.70)
    tail_budget = int(ceiling_chars * 0.25)
    elided = len(original) - head_budget - tail_budget
    clamped = (
        original[:head_budget]
        + (f'\n\n... [⚠ {elided:,} chars elided by emergency reactive '
           f'truncation — this single message was {len(original):,} chars, '
           f'likely binary/base64 decoded as text] ...\n\n')
        + original[-tail_budget:]
    )
    messages[worst_idx]['content'] = clamped
    freed = len(original) - len(clamped)
    logger.warning('[ReactiveCompact] In-place truncated largest message '
                   '(idx=%d role=%s) %s → %s (~%d chars freed)',
                   worst_idx, messages[worst_idx].get('role', '?'),
                   _human_size(len(original)), _human_size(len(clamped)), freed)
    return worst_idx, freed
