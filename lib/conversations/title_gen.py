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

# ── Thinking-model exclusion list (pt_title_gen_robust P1) ──
# Root cause pinned 2026-07-24 by debug/title_gen_repro.py: cheap-pool
# models carrying BOTH `cheap` AND `thinking` capabilities burn the entire
# max_tokens=512 budget on internal reasoning tokens, leaving the visible
# title empty or a single stray character (the "跨" incident on
# mryjczi2v9ck9k, dispatched to deepseek-v4-pro at 14:07:01). A 5-forced
# sample against deepseek-v4-pro reproduced finish_reason=length +
# empty content 1/5 (20% blast).
#
# Design: rather than introducing a new `non_thinking` capability tier —
# which would fork the capability_taxonomy.py single source of truth — we
# pass this set as ``exclude_models=`` to ``dispatch_chat``. Zero schema
# change; the dispatcher already honors the parameter.
#
# INVARIANT: this set MUST equal every model with BOTH `cheap` AND
# `thinking` capabilities in bootstrap._BUILTIN_PROVIDER_TEMPLATES.
# tests/test_title_gen_robust.py::test_constant_matches_bootstrap_derivation
# guards this at CI time — any new such model added to bootstrap without
# updating this constant will flip that test red.
_THINKING_MODELS_TO_EXCLUDE = frozenset({
    # OpenAI o-series (cheap + thinking)
    'o4-mini',
    # DeepSeek (cheap + thinking pair)
    'deepseek-v4-pro',
    'deepseek-v4-flash',
    # GLM (only glm-4.7 has cheap+thinking; glm-5.x are thinking-only, non-cheap)
    'glm-4.7',
    # Kimi (all 3 are cheap+thinking)
    'kimi-k3',
    'kimi-k2.6',
    'kimi-k2-thinking',
    # Qwen (max/plus are cheap+thinking; flash is cheap-only)
    'qwen3-max',
    'qwen-plus',
    # Gemini (2.5-pro is cheap+thinking; 2.5-flash & 3.1-flash-lite are cheap-only)
    'gemini-2.5-pro',
    # xAI (grok-4.20 is cheap+thinking; 4.1-mini is cheap-only)
    'grok-4.20',
    # Doubao (pro is cheap+thinking; lite is cheap-only)
    'doubao-seed-2-0-pro-260215',
    # OpenRouter relay (google/gemini-3.1-pro-preview is the only
    # cheap+thinking entry in the OpenRouter builtin template)
    'google/gemini-3.1-pro-preview',
})

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


def first_user_text(messages: list, max_chars: int = 280) -> str:
    """First user message as plain text, truncated — for hover previews.

    Unlike ``_fallback_title`` (which is capped at the short ``TITLE_MAX_CHARS``
    sidebar budget), this returns a longer preview snippet of the opening
    question the user actually asked (``originalContent`` when the message was
    auto-translated), suitable for a hover tooltip. Returns ``''`` when the
    conversation has no user text.

    Args:
        messages: The conversation's message list (dicts with role/content).
        max_chars: Hard cap on the returned snippet length.

    Returns:
        The first user message text, truncated with an ellipsis when longer
        than ``max_chars``; ``''`` when there is no user turn.
    """
    if not isinstance(messages, list):
        return ''
    for msg in messages:
        if not isinstance(msg, dict) or msg.get('role') != 'user':
            continue
        text = _msg_text(msg)
        if text:
            return text[:max_chars] + ('…' if len(text) > max_chars else '')
    return ''


def _clean_title(raw: str) -> str:
    """Normalize the model's output into a title.

    P2 pt_title_gen_robust: policy shift from "take the first non-empty line"
    to "merge all non-empty lines" — the old first-line rule silently dropped
    the real payload when a small model split the topic across two lines
    (e.g. ``"跨\\n设备协同状态同步"`` → ``"跨"``, root cause face B of the
    「跨」 incident). Order matters:

    1. Strip ``Title:`` / ``标题：`` label FIRST — otherwise merging then
       stripping fails when the label is on its own line
       (``"Title:\\n主题"`` merged first becomes ``"Title: 主题"`` which the
       regex still matches, but if the label were on a mid-line the merge
       would obscure it; leading-only strip is the safe invariant).
    2. Collapse all non-empty lines to a single space-separated line.
    3. Strip wrapping quotes / brackets.
    4. Hard-cap to ``TITLE_MAX_CHARS`` with an ellipsis marker.
    """
    title = (raw or '').strip()
    # (1) Strip leading label BEFORE merging lines so a label on its own
    # opening line doesn't survive the join. The regex is anchored at
    # start-of-string with re.MULTILINE so it handles both
    # ``"Title: 主题"`` and ``"Title:\n主题"``.
    title = re.sub(r'^\s*(?:title|标题)\s*[:：]\s*', '', title,
                   flags=re.IGNORECASE | re.MULTILINE)
    # (2) Merge all non-empty lines to a single space-separated line.
    lines = [ln.strip() for ln in title.splitlines() if ln.strip()]
    title = ' '.join(lines) if lines else ''
    # (3) Strip wrapping quotes / CJK brackets (repeat to catch nested
    # e.g. ``"「x」"``).
    title = title.strip().strip('"\u201c\u201d\'`「」《》').strip()
    # (4) Hard cap.
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

    prompt_messages = [
        {'role': 'system', 'content': _system_prompt(lang)},
        {'role': 'user',
         'content': f'Conversation:\n\n{convo}\n\nTitle:'},
    ]

    # ── P3 pt_title_gen_robust: single automatic retry on suspicious result ──
    # If the first attempt hits any of the three suspicious patterns
    # (finish=length / cleaned <=3 chars / clean-drop with >=2 lines), rerun
    # ONCE with the offending model added to exclude_models. Two failures →
    # fall back to _fallback_title. Explicit `for attempt in range(2):`
    # loop (per owner: no recursion + private kwargs).
    excluded_this_call: set[str] = set(_THINKING_MODELS_TO_EXCLUDE)
    title = ''
    last_content = ''
    started = time.time()
    for attempt in range(2):
        try:
            from lib.llm_dispatch import dispatch_chat
            content, _usage = dispatch_chat(
                prompt_messages,
                # A title is at most TITLE_MAX_CHARS, but the budget must cover
                # the model's reasoning trace too: the 'cheap' pool is full of
                # thinking models (deepseek-v4, glm, qwen3-max, kimi-thinking),
                # and for some of them (e.g. deepseek-reasoner) thinking is on
                # by definition and its tokens count against max_tokens. 32
                # tokens truncated good titles mid-word (e.g.
                # "更新 GLM-5.2 …" → "更新 GL") and starved thinking models
                # into empty output. dispatch_chat already defaults
                # thinking_enabled=False (disabling thinking where the model
                # honors the flag); the final string is collapsed to one line
                # and hard-capped by _clean_title, so a generous ceiling only
                # buys completeness, never a longer title.
                max_tokens=512,
                temperature=0.2,
                capability='cheap',
                # P1: base thinking-model exclusion; P3 augments with the
                # first-attempt's actual model on retry (see below).
                exclude_models=list(excluded_this_call),
                log_prefix='[TitleGen]' if attempt == 0 else '[TitleGen:retry]',
            )
        except Exception as e:
            logger.warning(
                '[TitleGen] dispatch_chat failed on attempt %d/%d '
                'after %.1fs: %s',
                attempt + 1, 2, time.time() - started, e)
            if attempt == 1:
                logger.warning('[TitleGen] both attempts failed — '
                               'falling back to first-message heuristic')
                return _fallback_title(messages)
            continue

        elapsed = time.time() - started
        last_content = content if isinstance(content, str) else ''

        # ── Root-cause diagnostic capture (P0 log-hardening) ──
        _usage_d = _usage if isinstance(_usage, dict) else {}
        _dispatch = _usage_d.get('_dispatch') or {}
        _actual_model = _dispatch.get('model') or '?'
        _finish_reason = _usage_d.get('finish_reason') or ''
        _prompt_tokens = _usage_d.get('prompt_tokens') or 0
        _completion_tokens = (_usage_d.get('completion_tokens')
                              or _usage_d.get('output_tokens') or 0)
        _reasoning_tokens = _usage_d.get('reasoning_tokens') or 0
        _raw = last_content
        _raw_stripped = _raw.strip()
        _raw_lines = [ln for ln in _raw_stripped.splitlines() if ln.strip()]
        _raw_line_count = len(_raw_lines)

        title = _clean_title(_raw)

        logger.info(
            '[TitleGen] attempt=%d/%d model=%s finish=%s '
            'tokens[prompt=%d comp=%d reason=%d] raw_chars=%d raw_lines=%d '
            'elapsed=%.1fs raw_preview=%.200r clean_title=%.60r',
            attempt + 1, 2, _actual_model, _finish_reason or '?',
            _prompt_tokens, _completion_tokens, _reasoning_tokens,
            len(_raw), _raw_line_count, elapsed,
            _raw_stripped, title)

        # Compute suspicious reasons — same three patterns as the log
        # hardening step, now ALSO drives the retry decision.
        _suspicious_reasons = []
        if _finish_reason == 'length':
            _suspicious_reasons.append(
                f'model hit max_tokens ceiling '
                f'(completion={_completion_tokens}, '
                f'reasoning={_reasoning_tokens})')
        if (_raw_line_count >= 2 and title
                and len(title) < len(_raw_stripped) - 2):
            _dropped = _raw_lines[1:]
            _suspicious_reasons.append(
                f'_clean_title dropped {_raw_line_count - 1} extra non-empty '
                f'line(s): {_dropped[:3]!r}')
        if title and len(title) <= 3 and _raw_line_count <= 1:
            _suspicious_reasons.append(
                f'model produced a title <=3 chars long (title={title!r})')
        if not title:
            _suspicious_reasons.append('cleaned title is empty')

        if _suspicious_reasons:
            logger.warning(
                '[TitleGen] suspicious result on attempt %d/%d model=%s: %s | '
                'raw=%.200r final=%.60r',
                attempt + 1, 2, _actual_model,
                ' | '.join(_suspicious_reasons), _raw_stripped, title)
            if attempt == 0:
                # Add the failing model to the per-call exclusion set so the
                # dispatcher must pick a different slot for the retry.
                if _actual_model and _actual_model != '?':
                    excluded_this_call.add(_actual_model)
                logger.info(
                    '[TitleGen] retrying once with %s added to exclude_models',
                    _actual_model)
                continue
            # attempt == 1: retry also bad — fall through to fallback below.
            break

        # Clean, non-suspicious result — return it.
        return title

    # Both attempts produced suspicious/empty output.
    logger.info(
        '[TitleGen] both attempts produced suspicious output '
        '(last_content=%.80r, last_title=%.60r) — using fallback',
        last_content, title)
    return _fallback_title(messages)


__all__ = ['generate_conversation_title', 'first_user_text', 'TITLE_MAX_CHARS']
