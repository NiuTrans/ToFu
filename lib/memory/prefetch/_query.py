"""lib/memory/prefetch/_query.py — Query construction.

Strip tools, thinking, system-reminder blocks; keep last K user+assistant
turns as a compact plain-text transcript that anchors both the BM25 coarse
stage and the cheap-LLM rerank.
"""
from __future__ import annotations

import re

from lib.log import get_logger

from lib.memory.prefetch._config import _MAX_QUERY_CHARS, PREFETCH_RECENT_TURNS_K

logger = get_logger(__name__)


def _msg_plain_text(msg: dict) -> str:
    """Return a message's user-visible text, stripping tool/image blocks.

    Also strips any ``<system-reminder>...</system-reminder>`` blocks —
    those are out-of-band injections (CLAUDE.md context, prior prefetch,
    cache hints) that shouldn't drive the relevance query.
    """
    content = msg.get('content', '')
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get('type') or ''
            if btype in ('text', 'output_text'):
                parts.append(block.get('text', '') or '')
            # Skip image / tool_use / tool_result / thinking / input_json / …
        text = '\n'.join(p for p in parts if p)
    else:
        return ''
    # Strip <system-reminder>...</system-reminder> blocks (DOTALL).
    text = re.sub(r'<system-reminder>.*?</system-reminder>', '',
                  text, flags=re.DOTALL)
    return text


def _extract_current_user_request(messages: list,
                                  cap: int = _MAX_QUERY_CHARS // 2) -> str:
    """Return ONLY the last user message's plain text (no role prefix).

    Used to anchor the cheap-LLM filter on "what the user just asked",
    distinct from the prior conversational background.
    """
    for msg in reversed(messages):
        if msg.get('role') != 'user':
            continue
        text = _msg_plain_text(msg).strip()
        if not text:
            continue
        if len(text) > cap:
            return text[:cap] + '…'
        return text
    return ''


def _build_recent_turns_text(messages: list, k: int = PREFETCH_RECENT_TURNS_K,
                             exclude_last_user: bool = False) -> str:
    """Collect up to K most recent user+assistant turns as plain text.

    Excludes system messages, tool messages, tool calls, thinking blocks,
    and image attachments.  Produces a compact ``[role] text`` transcript
    capped at _MAX_QUERY_CHARS total.

    Args:
        exclude_last_user: When True, the most recent user message is
            skipped — used by the cheap-LLM rerank step where the last
            user request is shown in its own dedicated section so the
            model can anchor on it rather than blend it into history.
    """
    pairs: list[tuple[str, str]] = []
    skipped_last_user = not exclude_last_user
    # Walk newest-first; collect text for 'user' and 'assistant' roles.
    for msg in reversed(messages):
        role = msg.get('role', '')
        if role not in ('user', 'assistant'):
            continue
        text = _msg_plain_text(msg).strip()
        if not text:
            continue
        if not skipped_last_user and role == 'user':
            skipped_last_user = True
            continue
        pairs.append((role, text))
        # Roughly: 2 messages = 1 "round", so stop after 2*K entries.
        if len(pairs) >= 2 * k:
            break
    pairs.reverse()

    buf: list[str] = []
    total = 0
    for role, text in pairs:
        line = f'[{role}] {text}'
        if total + len(line) > _MAX_QUERY_CHARS:
            remain = _MAX_QUERY_CHARS - total
            if remain > 100:
                buf.append(line[:remain] + '…')
            break
        buf.append(line)
        total += len(line) + 1
    return '\n\n'.join(buf)
