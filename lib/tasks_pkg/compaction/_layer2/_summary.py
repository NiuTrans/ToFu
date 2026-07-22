"""Layer 2 — query-aware LLM summary generation.

Wraps the cheap-model dispatch that turns the OLD conversation region into a
concise working-state snapshot (``_generate_query_aware_summary``).
"""

import re

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import _SUMMARY_MAX_TOKENS
from lib.tasks_pkg.compaction._tokens import _human_size
from lib.tasks_pkg.compaction._layer2._prompt import (
    _SUMMARY_SYSTEM_PROMPT,
    _format_messages_for_summary,
    _summary_input_char_budget,
)

logger = get_logger(__name__)


def _generate_query_aware_summary(messages: list, current_query: str,
                                   log_prefix: str = '',
                                   conv_id: str = '',
                                   task: dict | None = None,
                                   on_delta=None) -> str | None:
    """Call a cheap model to generate a query-aware summary.

    Degrades gracefully so the proactive path actually works on a
    vanilla/exported deploy: the input is capped to the model's real token
    window (see ``_summary_input_char_budget``), and if the preferred
    ``capability='cheap'`` dispatch fails (no model tagged cheap, or the
    single model is momentarily exhausted) it retries once against any
    text-capable slot before giving up.

    ``on_delta``: optional ``fn(text_chunk)`` callback. When given, the summary
    is STREAMED (``dispatch_stream``) and every content delta is forwarded to
    ``on_delta`` as it arrives, so a caller can push the growing summary to a
    live UI (the manual /compact card). The full accumulated text is still
    returned — identical result to the non-streaming path — so callers that
    also want the final string are unaffected. ``on_delta`` exceptions are
    swallowed (best-effort UI) and never abort generation. When ``on_delta`` is
    ``None`` the original non-streaming ``dispatch_chat`` path is used verbatim.
    """
    from lib.llm_dispatch import dispatch_chat, dispatch_stream

    tag = f'{log_prefix}[Summary]' if log_prefix else '[Summary]'

    # MESSAGE-AWARE elision: pass the budget INTO the formatter so it trims by
    # dropping middle ASSISTANT content while keeping EVERY user message (summary
    # prompt §6 is MANDATORY). The old code sliced the joined string blindly,
    # which could cut a user turn in half or drop it entirely — losing user
    # instructions. See _format_messages_for_summary / _elide_to_budget.
    _char_budget = _summary_input_char_budget(task)
    _full = _format_messages_for_summary(messages)
    formatted = _format_messages_for_summary(messages, char_budget=_char_budget)

    logger.info('%s Formatting %d messages for summary (%s), query=%.80s',
                tag, len(messages), _human_size(len(formatted)), current_query)
    if len(formatted) < len(_full):
        logger.info('%s Input elided to budget (message-aware, all user msgs '
                    'kept): %s → %s (budget %s)',
                    tag, _human_size(len(_full)), _human_size(len(formatted)),
                    _human_size(_char_budget))

    user_content = (
        f'## Current User Query\n{current_query}\n\n'
        f'## Conversation History to Compress\n\n{formatted}'
    )

    _summary_messages = [
        {'role': 'system', 'content': _SUMMARY_SYSTEM_PROMPT},
        {'role': 'user', 'content': user_content},
    ]

    def _dispatch(capability: str):
        """Return ``(content, usage)`` for either path.

        With ``on_delta`` set we STREAM and forward each delta live; otherwise
        we use the non-streaming ``dispatch_chat`` (byte-identical to before).
        ``dispatch_stream`` returns ``(content, finish_reason, usage)`` — we
        drop the finish_reason so both branches yield the same 2-tuple."""
        if on_delta is None:
            return dispatch_chat(
                _summary_messages,
                max_tokens=_SUMMARY_MAX_TOKENS, temperature=0,
                capability=capability, log_prefix=tag)

        def _on_content(chunk: str):
            if not chunk:
                return
            try:
                on_delta(chunk)
            except Exception as _cb_e:
                # Best-effort live UI — a push failure must never abort the
                # summary generation (the DB rewrite is the source of truth).
                logger.debug('%s on_delta callback failed: %s', tag, _cb_e)

        msg, _finish, usage = dispatch_stream(
            _summary_messages,
            on_content=_on_content,
            max_tokens=_SUMMARY_MAX_TOKENS, temperature=0,
            capability=capability, log_prefix=tag)
        # dispatch_stream returns the assistant message as a dict
        # ({'role': 'assistant', 'content': '...'}), NOT a bare string like
        # dispatch_chat — unwrap it so the shared post-processing (re.sub /
        # .strip below) receives a str. Same canonical unwrap as
        # lib/translate/engine/_engine.py.
        content = msg.get('content', '') if isinstance(msg, dict) else msg
        return content, usage

    try:
        try:
            content, usage = _dispatch('cheap')
        except Exception as _cheap_e:
            # Preferred cheap tier failed — retry once against ANY text slot
            # (a deploy may have no model tagged 'cheap' at all). The
            # dispatcher already widens 'cheap'→'text' internally when no
            # cheap slot exists, so this mainly covers a transient cheap-slot
            # exhaustion; it's cheap insurance and makes the intent explicit.
            logger.warning('%s cheap-tier summary dispatch failed (%s: %s) — '
                           'retrying on any text-capable slot',
                           tag, type(_cheap_e).__name__, _cheap_e)
            content, usage = _dispatch('text')

        if content:
            in_tok = usage.get('prompt_tokens', 0)
            out_tok = usage.get('completion_tokens', 0)
            # Count this summary call's tokens toward the conversation's
            # compaction cost — otherwise the L2 (chatui 'tofu') summary is
            # invisible in task['usage'] and the arm looks cheaper than it is.
            try:
                from lib.tasks_pkg.compaction._compaction_usage import (
                    record_compaction_usage)
                record_compaction_usage(conv_id, usage, kind='L2')
            except Exception as _ru_e:
                logger.debug('%s record_compaction_usage failed: %s', tag, _ru_e)
            content = re.sub(
                r'<analysis>.*?</analysis>\s*',
                '', content, flags=re.DOTALL,
            )
            logger.info('%s Summary generated: %d chars  in=%d  out=%d tokens',
                        tag, len(content), in_tok, out_tok)
            return content.strip()
        else:
            logger.warning('%s Summary model returned empty content', tag)
            return None

    except Exception as e:
        logger.warning('%s Summary generation failed (will keep messages intact): %s: %s',
                       tag, type(e).__name__, e)
        return None
