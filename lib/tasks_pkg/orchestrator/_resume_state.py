"""Resume-state hydration — pt_03f4cdf1 slice 10.

Extracted from ``lib/tasks_pkg/orchestrator/_run.py`` (previously inline in
``run_task`` between the Section 3.5 eligibility-drift guard and the
``await_memory_prefetch`` join). Bodies are byte-identical to the
pre-slice inline form — no logic changes.

The block hydrates ``task[]`` (and, when eligible, ``messages``) from the
continue-checkpoint keys carried on ``cfg``:

  * ``contentPrefix`` — bookkeeping seed for ``task['content']`` so a
    resumed response displays [preserved text] + [fresh continuation].
    NEVER re-injected into ``messages`` as a trailing assistant turn
    (Anthropic Messages API rejects that shape). The freshly generated
    part begins from the tool-result checkpoint replayed by
    ``inject_tool_history``.

  * ``resumePrefill`` — the capability-gated exception. Set ONLY when
    ``routes/chat.py::resume_prefill_from_segments`` already confirmed
    the target provider TOLERATES a trailing assistant prefill
    (``model_supports_assistant_prefill`` → False for Claude, so Claude
    never reaches the append). Injecting the terminal deliverable tail
    as a trailing assistant turn makes the model CONTINUE the same
    tokens instead of regenerating from the checkpoint.

  * Four ``checkpoint*`` cfg keys → ``task['_checkpoint*']`` verbatim
    stashes, merged into the done event and DB persistence by the
    post-loop finalize block.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger('tofu.orchestrator')


def apply_resume_state(
    *,
    task: dict[str, Any],
    cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
    tid: str,
) -> None:
    """Hydrate resume-state onto ``task`` and (when eligible) ``messages``.

    Never raises: this helper is called on the hot path just before the
    stream loop opens; any exception it swallowed downstream would be
    misattributed to a live-model failure. The three sub-blocks here are
    themselves defensive (guarded by cfg-key presence + provider capability)
    so raising is not possible in practice; the module-level import lands
    inside the function to keep import cost off run_task's cold path when
    resumePrefill is empty (the overwhelmingly common case).
    """
    # ★ Apply preserved content prefix from Continue — ensures backend checkpoints
    #   include text the LLM generated alongside completed tool rounds in the prior
    #   task, so page-refresh mid-stream doesn't lose that content.
    #
    #   ⚠ IMPORTANT: contentPrefix is NEVER re-injected into `messages` as a
    #   trailing assistant turn.  That would only work against OpenAI-compat
    #   endpoints — Anthropic Messages API rejects a trailing assistant turn
    #   ("This model does not support assistant message prefill. The
    #   conversation must end with a user message.").  Rather than branching
    #   by provider we keep the universal behaviour: use contentPrefix only
    #   as a bookkeeping seed for `task['content']` so the resumed response
    #   displays [preserved text] + [freshly generated continuation].  The
    #   freshly generated part begins from the tool-result checkpoint, which
    #   is replayed via `inject_tool_history` above — that shape every
    #   provider accepts.
    _content_prefix = cfg.get('contentPrefix') or ''
    if _content_prefix:
        with task['content_lock']:
            task['content'] = _content_prefix
        logger.debug('[%s] conv=%s Applied contentPrefix (%d chars) from continue checkpoint',
                     tid, task.get('convId', ''), len(_content_prefix))

    # ★ Resume-prefill (epic pt_cb8f98b0cb9b47fb): the capability-gated
    #   exception to the "never inject contentPrefix as a trailing assistant
    #   turn" rule above. resumePrefill is set ONLY when routes/chat.py's
    #   resume_prefill_from_segments already confirmed the target provider
    #   TOLERATES a trailing assistant prefill (model_supports_assistant_
    #   prefill → False for Claude, so Claude never reaches here). Injecting
    #   the terminal deliverable tail as a trailing assistant turn makes the
    #   model CONTINUE the same tokens (case 2: mid-prose after a tool batch;
    #   case 3: mid-answer no-tool turn) instead of regenerating from the
    #   checkpoint. The tool batch (if any) was already replayed by
    #   inject_tool_history above; the pre-tool prose lives on those
    #   assistant(tool_calls) turns, so the prefill (terminal deliverable
    #   only) never double-counts. task['content'] is seeded with the FULL
    #   prior content (contentPrefix) so display = full + continuation.
    #
    #   Defence in depth: even if a dispatcher model-swap routed this to
    #   Claude after the gate, _strip_trailing_assistant_for_claude() in
    #   build_body()/dispatch_stream() would neutralise the trailing turn
    #   (the Claude-4.6 prefill-removal guard) — so a leak degrades to
    #   today's regenerate-from-checkpoint, never an HTTP 400.
    _resume_prefill = cfg.get('resumePrefill') or ''
    if _resume_prefill:
        from lib.model_info import model_supports_assistant_prefill
        if model_supports_assistant_prefill(model):
            messages.append({'role': 'assistant', 'content': _resume_prefill})
            task['_resumePrefill'] = _resume_prefill
            logger.info('[%s] conv=%s Injected resume prefill (%d chars) as trailing '
                        'assistant turn — model=%s will continue the same tokens',
                        tid, task.get('convId', ''), len(_resume_prefill), model)
        else:
            logger.info('[%s] conv=%s resumePrefill present but model=%s rejects prefill '
                        '— falling back to regenerate-from-checkpoint (contentPrefix seed only)',
                        tid, task.get('convId', ''), model)

    # ★ Stash checkpoint metadata for merging into done event and DB persistence.
    #   NOTE: we do NOT pre-populate task['toolRounds'] with checkpoint rounds
    #   because the frontend's state/delta handlers would double-count them
    #   (frontend does _continueToolRounds.concat(ev.toolRounds)).  Instead,
    #   checkpoint rounds are merged only when writing to DB and in the done event.
    _checkpoint_tr = cfg.get('checkpointToolRounds') or []
    if _checkpoint_tr:
        task['_checkpointToolRounds'] = list(_checkpoint_tr)
        logger.debug('[%s] conv=%s Stashed %d checkpoint toolRounds for DB merge',
                     tid, task.get('convId', ''), len(_checkpoint_tr))
    if cfg.get('checkpointUsage'):
        task['_checkpointUsage'] = cfg['checkpointUsage']
    if cfg.get('checkpointApiRounds'):
        task['_checkpointApiRounds'] = cfg['checkpointApiRounds']
    if cfg.get('checkpointModifiedFiles'):
        task['_checkpointModifiedFiles'] = cfg['checkpointModifiedFiles']
    if cfg.get('checkpointModifiedFileList'):
        task['_checkpointModifiedFileList'] = cfg['checkpointModifiedFileList']
