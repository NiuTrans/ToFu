# HOT_PATH
"""Advanced compaction host (Stage B) — runs structural + LLM-allowed methods.

This is the second host in the compaction pipeline, complementing the
cheap every-round L1 host (``micro_compact``).  Where L1 runs only
``kind='transform'`` + LLM-free steps with in-place content edits, the
advanced host grants the two capabilities that distinguish richer method
forms:

  * ``ctx.edit``      — a :class:`MessageEditor` for whole-turn structural
    surgery (granted to ``kind='structural'`` steps).
  * ``ctx.summarize`` — a budgeted cheap-model summary call (granted to
    ``needs=('llm',)`` steps).

Persistence model
-----------------
The advanced host operates on the **api-form** ``messages`` list for the
current LLM call, exactly like Layer 2's ``execute_compact_tool`` —
the truncation/edits are recomputed every round from the DB-rebuilt
messages, so there is NO durable-placeholder bookkeeping to do here
(that is L1's concern for re-readable tool placeholders).  This is why
structural deletes are safe and simple: next round rebuilds from the
source of truth and the same steps re-apply deterministically.

Opt-in
------
Runs only when ``task['config']['compaction']['advanced_steps']`` is a
non-empty list.  Default off ⇒ shipped behavior unchanged.  Wired into
``run_compaction_pipeline`` AFTER L1 and the force-compact check.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import (
    STEP_KIND_STRUCTURAL,
    STEP_KIND_TRANSFORM,
    CompactionContext,
    MessageEditor,
    make_constants,
    run_steps,
)
import lib.tasks_pkg.compaction._constants as compaction_constants
import lib.tasks_pkg.compaction._faithful_methods  # noqa: F401
import lib.tasks_pkg.compaction._methods  # noqa: F401

logger = get_logger(__name__)


def _make_summarize_fn(conv_id: str, task: dict | None):
    """Build the ``ctx.summarize`` callable, reusing the same cheap-model
    dispatch path Layer 2 uses.  Returns a callable ``(text, *,
    instruction, max_tokens) -> str`` (empty string on failure — a step
    must tolerate a degraded summary, never crash)."""

    # Pin the summarizer to the AGENT'S OWN model (the model running the
    # task), not capability='cheap' dispatcher roulette. Two reasons:
    #   1. Self-consistency: the summary is produced by the same model the
    #      arm is benchmarking, so summarization cost/quality is attributed
    #      to that arm — no silent cross-model contamination (e.g. a
    #      deepseek arm having its summaries written by Opus).
    #   2. Faithfulness: the real systems (OpenCode/Hermes/OpenClaw) drive
    #      compaction with the session model by default.
    # An explicit ``compaction.summaryModel`` override is honored if set.
    _cfg = (task or {}).get('config') or {}
    _summary_model = _cfg.get('compaction', {}).get('summaryModel') \
        if isinstance(_cfg.get('compaction'), dict) else None
    _agent_model = _summary_model or _cfg.get('model') or ''

    def _summarize(text: str, *, instruction: str = '',
                   max_tokens: int = 512) -> str:
        from lib.llm_dispatch import dispatch_chat
        sys_prompt = (instruction or
                      'Summarize the following content concisely, preserving '
                      'file paths, identifiers, error messages, and concrete '
                      'conclusions. Drop filler. Output only the summary.')
        tag = f'[AdvSummary conv={conv_id[:8] if conv_id else "?"}]'
        try:
            from lib.tasks_pkg.manager import task_user_id
            owner_user_id = task_user_id(task)
            content, usage = dispatch_chat(
                [
                    {'role': 'system', 'content': sys_prompt},
                    {'role': 'user', 'content': text[:200_000]},
                ],
                prefer_model=_agent_model or None,
                max_tokens=max_tokens,
                temperature=0,
                capability='cheap' if not _agent_model else 'text',
                owner_user_id=owner_user_id,
                log_prefix=tag,
            )
            # Count this summarizer call's tokens toward compaction cost so
            # the summary-based arms (OpenCode/Hermes/OpenClaw) don't appear
            # artificially cheaper than the prune-only ones.
            try:
                from lib.tasks_pkg.compaction._compaction_usage import (
                    record_compaction_usage)
                record_compaction_usage(conv_id, usage, kind='advanced')
            except Exception as _ru_e:
                logger.debug('%s record_compaction_usage failed: %s', tag, _ru_e)
            return (content or '').strip()
        except Exception as e:
            logger.warning('%s summarize failed (degraded, returning empty): '
                           '%s: %s', tag, type(e).__name__, e)
            return ''

    return _summarize


def advanced_compact(messages: list, conv_id: str = '',
                     task: dict | None = None, *,
                     advanced_steps: list,
                     constant_overrides: dict | None = None,
                     ignore_cache_prefix: bool = False) -> int:
    """Run the configured advanced (structural / LLM) compaction steps.

    Args:
        messages: Live api-form messages list, mutated in place.
        conv_id:  Conversation ID (logging / cache lookup).
        task:     Live task dict.
        advanced_steps: Ordered list of registered step names to run.
            Steps may be ``kind='structural'`` and/or ``needs=('llm',)``.
        constant_overrides: Per-call tunable overlay (concurrency-safe).
        ignore_cache_prefix: Aggressive arm — let steps act inside the
            cache prefix.

    Returns:
        Estimated total tokens saved.
    """
    if not advanced_steps:
        return 0

    # Cache-prefix boundary (same source of truth as L1).
    cache_prefix_count = 0
    if conv_id:
        try:
            from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
            from lib.tasks_pkg.manager import task_user_id
            # Clamp the monotonic boundary to the live message count (see
            # get_cache_prefix_count docstring — history-shrink guard).
            cache_prefix_count = get_cache_prefix_count(
                conv_id,
                user_id=task_user_id(task),
                current_msg_count=len(messages),
            )
        except Exception as e:
            logger.debug('[AdvCompact] cache_tracking unavailable: %s', e)

    ctx = CompactionContext(
        messages=messages,
        conv_id=conv_id,
        task=task,
        constants=make_constants(compaction_constants, constant_overrides),
        cache_prefix_count=cache_prefix_count,
        ignore_cache_prefix=bool(ignore_cache_prefix),
        summarize_fn=_make_summarize_fn(conv_id, task),
    )
    # Grant structural surgery: the editor reads ctx for protections.
    ctx.edit = MessageEditor(ctx)

    saved = run_steps(
        advanced_steps, ctx,
        allow_kinds=(STEP_KIND_TRANSFORM, STEP_KIND_STRUCTURAL),
        allow_llm=True,
    )

    if saved > 0 or ctx.edit.removed_messages:
        logger.info('[AdvCompact] conv=%s  steps=%s  ~%d tokens saved, '
                    '%d message(s) structurally removed',
                    conv_id[:8] if conv_id else '?', advanced_steps,
                    saved, ctx.edit.removed_messages)
    return saved
