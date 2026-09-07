"""Leaf helpers for Layer-0 disk persistence — id/size/label formatting,
filename sanitization, the head+tail truncation fallback, and the two
module-level regexes shared by the splitters.

Imports nothing from sibling compaction sub-modules (a strict leaf, so the
persist package stays acyclic).
"""

import re
import uuid

from lib.log import get_logger

logger = get_logger(__name__)


# Matches a vertical-search block emitted by
# ``handlers.search._vertical_header_for_llm`` and prepended to web_search
# tool content: the ``═══ Vertical Search … ═══`` header, its body, and the
# closing ``═══ Web Search Results ═══`` marker — optionally preceded by the
# ``=== Search: <q> ===`` batch-query header. Relocated into the persist index
# by ``_persist_web_search_split`` because the ``═══`` header is NOT a ``════``
# per-result boundary: left in place, the split would glue the block onto
# result-1's file and it would drop out of the model's immediate context.
# See JOURNAL 2026-07-06 (vertical-search debug-panel gap).
_VERT_BLOCK_RE = re.compile(
    r'(?:^=== Search:[^\n]*\n)?'
    r'^═══ Vertical Search.*?'
    r'^═══ Web Search Results ═══[^\n]*\n?',
    re.MULTILINE | re.DOTALL,
)


def _short_id(tool_use_id: str) -> str:
    """Derive a short, unique filename id fragment from a tool-use id.

    The live caller always passes a provider tool-use id (``toolu_<24 random>``
    for Anthropic, ``call_<24>`` for OpenAI, etc.).  The entropy lives in the
    tail, while the ``toolu_`` / ``call_`` prefix is constant and repeated once
    per split file in the persist index the model reads back — pure waste.
    Strip the constant prefix and keep the entropy-bearing tail, capped so a
    persisted filename never balloons.  Falls back to a fresh uuid fragment
    when no tool-use id is supplied.
    """
    raw = (tool_use_id or uuid.uuid4().hex[:12]).replace('/', '_')
    raw = re.sub(r'^(toolu_|call_|fc_)', '', raw)
    return raw[-16:] or 'id'


def _human_size(byte_count: int) -> str:
    """Format a byte/char count as a human-readable string.

    Local copy so ``_persist`` stays a strict leaf-of-``_constants``.
    """
    if byte_count < 1024:
        return f'{byte_count}B'
    elif byte_count < 1024 * 1024:
        return f'{byte_count / 1024:.1f}KB'
    else:
        return f'{byte_count / (1024 * 1024):.1f}MB'


# Lines that carry no human meaning as a result description: decorative
# rules (═══ / ─── / ═══ boundaries, ==== markers) and blank lines. Used to
# skip past the leading separator a formatted tool result (e.g.
# ``get_conversation``) opens with, so the persisted-result label reads
# ``past conversation — "Referenced Conversation: …"`` instead of a wall of
# box-drawing characters.
_DECORATIVE_LINE_RE = re.compile(r'^[\s═─—\-=_*#·•]+$')


def _first_meaningful_line(content: str) -> str:
    """Return the first non-decorative, non-blank line of ``content``.

    Falls back to the first line when every scanned line is decorative, so a
    description is always produced.
    """
    first = ''
    for line in content.lstrip().split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        if not first:
            first = stripped
        if not _DECORATIVE_LINE_RE.match(stripped):
            return stripped
    return first


def _sanitize_filename(s: str, max_len: int = 60) -> str:
    """Convert a string to a safe, short filename fragment."""
    s = re.sub(r'[^\w\-]', '_', s)
    s = re.sub(r'_+', '_', s).strip('_')
    return s[:max_len] if s else 'item'


def _truncate_head_tail(content: str, tool_name: str, max_chars: int) -> str:
    """Legacy head+tail truncation fallback.

    Used only when disk persistence fails (e.g. permission errors).
    """
    original_len = len(content)
    head_budget = int(max_chars * 0.70)
    tail_budget = int(max_chars * 0.25)

    head = content[:head_budget]
    tail = content[-tail_budget:]

    truncation_note = (
        f'\n\n... [{original_len - head_budget - tail_budget:,} chars truncated — '
        f'result was {original_len:,} chars, budget is {max_chars:,}] ...\n\n'
    )

    logger.info('[Budget] %s result truncated (fallback): %s → %s (budget %s)',
                tool_name, _human_size(original_len),
                _human_size(head_budget + tail_budget),
                _human_size(max_chars))

    return head + truncation_note + tail
