"""lib.conversations.title_gen — LLM-generated conversation titles.

Replaces the naive "first 60 chars of the first user message" heuristic
with a short, descriptive title produced by a cheap model. The title is
generated from the opening turn (first user message + first assistant
reply, when available) so it captures the *topic*, not just the literal
first sentence.

Used by ``POST /api/v1/conversations/<id>/generate-title``. Falls back to
the truncated-first-message heuristic if the model call fails — the
caller never has to special-case a missing title.
"""

from __future__ import annotations

import re
import time

from lib.log import get_logger

logger = get_logger(__name__)

# Hard cap on the persisted title length (chars). Mirrors the frontend
# substring cap so a generated title never overflows the sidebar row.
TITLE_MAX_CHARS = 60

_SYSTEM_PROMPT_BASE = (
    'You write a concise, information-dense title for a chat conversation, '
    'shown in a narrow sidebar. The reader must grasp what this specific '
    'conversation is about at a glance, without opening it.\n'
    'Rules:\n'
    '- Lead with the concrete subject: the specific topic, task, or '
    'question — name the actual thing involved (the technology, file, '
    'error, concept, or goal), not a vague category.\n'
    '- Be specific enough to tell this conversation apart from similar '
    'ones. Prefer "Fix CORS error in Flask login" over "Debugging help"; '
    'prefer "对比 Rust 与 Go 并发模型" over "技术问题".\n'
    '- Pack in the distinguishing detail, but stay scannable. Omit filler '
    'words, greetings, and pleasantries ("help me", "请问", "如何").\n'
    '{length_rule}\n'
    '{lang_rule}\n'
    '- No trailing punctuation. Do NOT wrap the title in quotes. Do NOT '
    'add a prefix like "Title:". Output ONLY the title text.'
)

# Per-UI-language title rule. When the caller passes an explicit interface
# language we force the title into it; otherwise the title mirrors the
# conversation's own language.
_LANG_RULES = {
    'zh': '- Write the title in Simplified Chinese, regardless of the '
          "conversation's language.",
    'en': '- Write the title in English, regardless of the '
          "conversation's language.",
}
_LANG_RULE_DEFAULT = (
    "- Use the SAME language as the user's message "
    '(Chinese title for a Chinese conversation, English for English).'
)

# Length guidance is language-specific: char budgets read better for CJK,
# word counts for English. Both target a single scannable sidebar line.
_LENGTH_RULES = {
    'zh': '- Keep it to roughly 6-16 Chinese characters — tight but '
          'complete.',
    'en': '- Keep it to roughly 3-7 words — tight but complete.',
}
_LENGTH_RULE_DEFAULT = (
    '- Keep it short: about 3-7 words, or 6-16 characters for CJK.'
)


def _system_prompt(lang: str | None) -> str:
    """Build the title system prompt, forcing output language when given."""
    key = (lang or '').lower()
    rule = _LANG_RULES.get(key, _LANG_RULE_DEFAULT)
    length = _LENGTH_RULES.get(key, _LENGTH_RULE_DEFAULT)
    return _SYSTEM_PROMPT_BASE.format(lang_rule=rule, length_rule=length)


def _msg_text(msg: dict, prefer_original: bool = True) -> str:
    """Return a message's plain user-visible text (no tool / image blocks).

    Args:
        msg: A message dict with ``role`` / ``content``.
        prefer_original: When True (the default) and the message carries an
            ``originalContent`` field — present on auto-translated user
            messages, holding what the user actually typed — use it instead
            of the translated ``content``. This keeps generated titles in the
            user's own language rather than the English the model saw.
    """
    content = msg.get('content', '')
    if prefer_original:
        original = msg.get('originalContent')
        if isinstance(original, str) and original.strip():
            content = original
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get('type') in (
                    'text', 'output_text', None):
                parts.append(block.get('text', '') or '')
        text = '\n'.join(p for p in parts if p)
    else:
        return ''
    # Strip notranslate markers and out-of-band system reminders.
    text = re.sub(r'</?(?:notranslate|nt)>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<system-reminder>.*?</system-reminder>', '', text,
                  flags=re.DOTALL)
    return text.strip()


def _fallback_title(messages: list) -> str:
    """First user message truncated to TITLE_MAX_CHARS — the legacy heuristic."""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get('role') != 'user':
            continue
        text = _msg_text(msg)
        if text:
            return text[:TITLE_MAX_CHARS] + (
                '…' if len(text) > TITLE_MAX_CHARS else '')
    return 'Untitled'


def _clean_title(raw: str) -> str:
    """Normalize the model's output into a single-line, unquoted title."""
    title = (raw or '').strip()
    # Collapse to the first non-empty line.
    for line in title.splitlines():
        if line.strip():
            title = line.strip()
            break
    # Strip a leading "Title:" / "标题：" label if the model added one.
    title = re.sub(r'^\s*(?:title|标题)\s*[:：]\s*', '', title,
                   flags=re.IGNORECASE)
    # Strip wrapping quotes.
    title = title.strip().strip('"\u201c\u201d\'`「」《》').strip()
    if len(title) > TITLE_MAX_CHARS:
        title = title[:TITLE_MAX_CHARS].rstrip() + '…'
    return title


def generate_conversation_title(messages: list, lang: str | None = None) -> str:
    """Produce a short descriptive title for a conversation.

    Args:
        messages: The conversation's message list (dicts with role/content).
        lang: Optional UI language ('zh' / 'en'). When given, the title is
            forced into that language so it matches the interface; otherwise
            it mirrors the conversation's own language.

    Returns:
        A cleaned, length-capped title. Falls back to the truncated first
        user message if the conversation has no text or the model call
        fails — never returns an empty string for a non-empty conversation.
    """
    if not isinstance(messages, list) or not messages:
        return 'Untitled'

    first_user = ''
    first_assistant = ''
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get('role')
        if role == 'user' and not first_user:
            first_user = _msg_text(msg)
        elif role == 'assistant' and not first_assistant:
            first_assistant = _msg_text(msg)
        if first_user and first_assistant:
            break

    if not first_user:
        logger.info('[TitleGen] no user text found in %d message(s) — '
                    'using fallback', len(messages))
        return _fallback_title(messages)

    has_chinese = bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', first_user))
    logger.info('[TitleGen] start: lang=%s msgs=%d user_chars=%d '
                'has_assistant=%s source_has_chinese=%s',
                lang or 'auto', len(messages), len(first_user),
                bool(first_assistant), has_chinese)

    # Cap the snippets we feed the model — a title doesn't need the full
    # body, and keeping the prompt small keeps the cheap call fast.
    user_snip = first_user[:1500]
    parts = [f'User: {user_snip}']
    if first_assistant:
        parts.append(f'Assistant: {first_assistant[:800]}')
    convo = '\n\n'.join(parts)

    started = time.time()
    try:
        from lib.llm_dispatch import dispatch_chat
        content, _usage = dispatch_chat(
            [
                {'role': 'system', 'content': _system_prompt(lang)},
                {'role': 'user',
                 'content': f'Conversation:\n\n{convo}\n\nTitle:'},
            ],
            # A title is at most TITLE_MAX_CHARS, but the budget must cover
            # the model's reasoning trace too: the 'cheap' pool is full of
            # thinking models (deepseek-v4, glm, qwen3-max, kimi-thinking),
            # and for some of them (e.g. deepseek-reasoner) thinking is on by
            # definition and its tokens count against max_tokens. 32 tokens
            # truncated good titles mid-word (e.g. "更新 GLM-5.2 …" → "更新 GL")
            # and starved thinking models into empty output. dispatch_chat
            # already defaults thinking_enabled=False (disabling thinking where
            # the model honors the flag); the final string is collapsed to one
            # line and hard-capped by _clean_title, so a generous ceiling only
            # buys completeness, never a longer title.
            max_tokens=512,
            temperature=0.2,
            capability='cheap',
            log_prefix='[TitleGen]',
        )
    except Exception as e:
        logger.warning('[TitleGen] dispatch_chat failed after %.1fs: %s — '
                       'falling back to first-message heuristic',
                       time.time() - started, e)
        return _fallback_title(messages)

    elapsed = time.time() - started
    title = _clean_title(content or '')
    if not title:
        logger.info('[TitleGen] empty/unusable model output (%.80r) after '
                    '%.1fs — using fallback', content, elapsed)
        return _fallback_title(messages)
    logger.info('[TitleGen] generated title=%.60r in %.1fs', title, elapsed)
    return title


__all__ = ['generate_conversation_title', 'TITLE_MAX_CHARS']
