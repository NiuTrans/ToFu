"""Autopilot mode — virtual user that auto-replies when the LLM stops.

When the model would normally hand control back to the user (either by
calling ``ask_human`` or by emitting a final assistant message with
``finish_reason='stop'``), Autopilot runs a one-shot LLM as the *user*
and feeds its reply back to the orchestrator as a brand-new turn.

Design constraints (locked in by the user, do not relax silently):

  • **Runs BEFORE the ``done`` SSE event.**  The hook fires inside
    ``_finalize_and_emit_done`` after the post-loop work but *before*
    ``append_event(done_evt)`` / ``persist_task_result``.  This lets
    the ``done`` event carry ``autopilotNextTaskId`` +
    ``autopilotVuMessage`` so the frontend attaches to the follow-up
    task directly instead of polling ``/api/chat/active`` after the
    SSE stream has already closed.  (Earlier design ran autopilot
    *after* persist; the SSE pipe was closed by the time the VU
    finished, so the synthetic user msg was invisible until manual
    refresh.)

  • **Independent of endpoint mode.**  Autopilot and endpoint mode are
    mutually exclusive — both share the same termination boundary
    ("the model stopped") so running them together would double-loop.
    The frontend hides one toggle when the other is on; this module
    additionally bails out when ``task['_endpoint_managed']`` is set.

  • **Reuse the conversation's main model.**  No separate VU model.

  • **Same tools as the worker.**  The VU runs through the full
    orchestrator (``_run_single_turn``) so it has access to every tool
    the parent task has — read_files, search, project edits, browser,
    memory, MCP, etc.  This lets the simulated user investigate before
    composing its reply (e.g. "let me check the file the assistant
    referenced before answering").  Inherited from
    ``task['config']`` verbatim — same tool list as
    ``_assemble_tool_list`` would build for the parent.

  • **Role override via a trailing directive turn**, mirroring how
    endpoint-mode's planner/critic announce their role.  We do NOT
    role-swap the conversation history: the LLM sees the real
    conversation and a final user-turn that says "for THIS turn play
    the simulated user".  Prefix-cache-friendly and avoids the
    swapped-history confusion with the orchestrator's injected
    system prompt.

  • **Full conversation passed through.**  We do NOT trim history
    here; the orchestrator's compaction layer (run_compaction_pipeline)
    handles bounding.  This keeps tool_calls / tool_result pairs
    contiguous and removes one place where context choices can drift
    between the worker and the simulated user.

  • **No turn cap, no state-change watchdog.**  The only graceful stop
    signal is the VU itself emitting ``[VU: TASK_DONE]``.  Other stops
    are: real-user abort, real-user sending a new message (handled
    automatically by ``abort_running_tasks_for_conv``), an error path,
    or the queue having a real queued message waiting (deferred to).

  • **Empty VU output does NOT stop the loop.**  An empty reply is
    treated as a valid "yeah, keep going" — the orchestrator just
    starts a fresh turn with that empty user message.  This is the
    user's explicit choice — see the design discussion in
    docs/ARCHITECTURE.md if rebooting decisions.

The "don't stop on empty output" rule means the only correctness escape
hatch is the real user clicking Stop or sending a new message.  Both are
already wired through ``task['aborted']`` and the freshness guard in
``manager._conv_latest_task``, so we don't need extra plumbing here.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid

from lib.agent_core.events import EventType, build_event
from lib.log import audit_log, get_logger

logger = get_logger(__name__)


from lib.agent_verdict import VU_DONE_SENTINEL as _VU_DONE_SENTINEL
from lib.agent_verdict import classify_verdict as _classify_verdict


_VU_ROLE_PROMPT = (
    'You are the PROJECT OWNER driving this task to completion. The '
    'assistant reports to YOU. Your job is not to answer the assistant '
    'or to be agreeable — it is to keep the work moving toward the '
    'objective and to refuse to declare victory until the objective is '
    'actually met.\n\n'
    'Trust nothing you have not checked. The assistant\'s self-report '
    '("done", "tests pass", "I created X") is a claim, not evidence.\n\n'
    'Before you reply, do this:\n'
    '1. VERIFY the assistant\'s most consequential claim using your '
    'tools. If it said tests pass, run or inspect them; if it said it '
    'created/edited a file, read_files it; if it claimed a behavior '
    'works, check it. You MUST verify any checkable claim that the '
    'objective depends on — do not skip this.\n'
    '2. ASSESS the gap between the real current state and the objective '
    'stated at the top of this turn.\n'
    '3. DECIDE the genuine next step toward that objective — NOT merely '
    'a response to whatever the assistant last said. If the assistant '
    'asked you a decision question, answer it from the objective\'s '
    'perspective. If it declared the task finished, hold it to the '
    'objective\'s acceptance criteria.\n'
    '4. THINK CREATIVELY. A good owner does more than grade the '
    'assistant\'s homework. Use what you learned while investigating to '
    'surface things the assistant has NOT considered: an edge case or '
    'failure mode it missed, a simpler or more robust approach, a '
    'hidden assumption worth challenging, a related part of the '
    'objective it has not touched yet, or a concrete improvement. When '
    'you have such an insight, lead with it — it is more valuable than '
    'a verification report.\n\n'
    '=== PROVENANCE (read carefully) ===\n'
    'Your tool calls and your private reasoning are NOT sent to the '
    'assistant — they are shown to the human watching, but the assistant '
    'only ever receives your final REPLY TEXT as its next user message. '
    'So investigate as deeply as you need (the cost is yours to spend), '
    'but distil the result: your reply must be a clean, self-contained '
    'instruction that stands on its own without the investigation behind '
    'it. Do not say "as I found above" or reference your tool output — '
    'state the conclusion and the next step directly.\n'
    '=== END PROVENANCE ===\n\n'
    'Decision rules:\n'
    '- For code / engineering tasks: demand the most robust long-term '
    'solution. Do not accept shortcuts that optimize for cost, '
    'implementation speed, or backward compatibility. Prefer fixing '
    'root causes over patches.\n'
    '- For open-ended discussion: use your own judgment, stay concrete, '
    'pick a direction instead of asking more questions.\n'
    '- Stop ONLY when you have VERIFIED that the objective\'s acceptance '
    'criteria are genuinely met (the check actually ran, the file is '
    'actually correct, the behavior actually works) — not when the '
    'assistant says so. When (and only when) that is true, reply '
    f'EXACTLY: {_VU_DONE_SENTINEL}\n'
    '- If the objective is NOT yet met, give the assistant the specific '
    'unmet criterion or the next concrete step. Do not emit '
    f'{_VU_DONE_SENTINEL} while anything remains unresolved.\n'
    '- Never invent product requirements beyond the stated objective.\n'
    '- Reply in the first person as the owner, in the same language the '
    'assistant used. Be concise but cite the specific evidence you '
    'verified or the criterion you are holding the assistant to.\n'
    '- Output ONLY the reply text — no quotation marks, no role labels, '
    f'no preamble. The {_VU_DONE_SENTINEL} sentinel must appear on its '
    'own when used.'
)


# Content-derived prompt version marker.  Stamped into every VU directive turn
# so a stale-vs-live prompt mismatch is mechanically detectable (one glance at
# the directive text / message dict) instead of relying on eyeballing the prose.
# Derived from the prompt body itself, so it changes AUTOMATICALLY whenever the
# prompt text changes — no one can forget to bump it.  A directive carrying an
# old marker (or none) was produced by a server still running a pre-edit
# import-time constant; restart picks up the new prompt.  See
# tests/test_autopilot_verify.py for the regression assertion.
VU_PROMPT_VERSION = hashlib.sha256(
    _VU_ROLE_PROMPT.encode('utf-8')).hexdigest()[:8]

# Emit the loaded prompt version once at import so the RUNNING process's
# prompt is greppable in app.log without needing a directive paste to infer
# it ("is the live process current?" answered directly from logs).
logger.info('[Autopilot] VU prompt v%s loaded', VU_PROMPT_VERSION)


# ──────────────────────────────────────────────────────────────────
#  Summary reporter — one read-only synthesis turn at TASK_DONE
# ──────────────────────────────────────────────────────────────────

_REPORTER_ROLE_PROMPT = (
    'The autopilot run you just drove has reached its objective. Your '
    'job now is to write ONE comprehensive close-out report of the whole '
    'run, addressed to the human who set the objective. You are no longer '
    'the simulated user — you are the agent reporting back what was '
    'attempted and accomplished.\n\n'
    'WHO YOU ARE WRITING FOR: a human who did NOT watch the run, does not '
    'see the transcript, and does not know this codebase\'s internal '
    'names, tool names, or IDs. The report is the ONLY thing they will '
    'read. It MUST therefore be fully SELF-CONTAINED and understandable '
    'on its own — a reader with no prior context should finish it knowing '
    'what you set out to do, what actually happened, and where things '
    'stand.\n\n'
    'TRANSLATE, DO NOT TRANSCRIBE. The conversation above is the FULL '
    'transcript of the run (the original objective, every simulated-user '
    'instruction, and the agent\'s work and tool results). It is your '
    'evidence, NOT your template. Do not replay it turn-by-turn or dump '
    'tool calls. For every point, say in plain language WHAT was done and '
    'WHY it mattered toward the objective — the meaning, not the '
    'mechanics.\n\n'
    'HARD RULES (a report that breaks these has failed):\n'
    '  - Never paste raw tool-call names, function signatures, internal '
    'identifiers (task IDs, run IDs, hashes), stack traces, or control '
    'markers like "[VU: TASK_DONE]" as if the reader knows them.\n'
    '  - When you must name a concrete artifact (a file, a command, a '
    'test), give it a one-line plain-language gloss of what it is and why '
    'it was touched — never a bare path or command with no explanation.\n'
    '  - Expand or drop jargon and abbreviations; if a term is '
    'unavoidable, define it in-line the first time.\n'
    '  - Prefer a short narrative of the meaningful decisions over an '
    'exhaustive step log. Omit routine/mechanical steps that carry no '
    'information for the reader.\n\n'
    'Do not run any tools and do not start new work — just synthesise '
    'what is already in the transcript. Be honest: this is a debrief, not '
    'a victory lap. If the objective was only partially met or the '
    'evidence is thin, say so directly.\n\n'
    'Structure the report in the human\'s language with these sections '
    '(use Markdown headings):\n'
    '1. **Objective** — in one or two plain sentences, what the run was '
    'asked to achieve. Written so it makes sense with zero prior context.\n'
    '2. **Outcome** — lead with the verdict against the objective: was it '
    'met, partially met, or not met, and in one line, why.\n'
    '3. **What was done** — the meaningful steps and decisions, in plain '
    'language, condensed. Each artifact you mention gets a short gloss of '
    'what it is / why it mattered — not a bare list of files or commands.\n'
    '4. **Verification** — what was actually checked (tests that ran and '
    'their results, behaviour confirmed, outputs read back) versus merely '
    'claimed. State results in words, not just exit codes.\n'
    '5. **Gaps & risks** — what is NOT done, known limitations, '
    'follow-ups worth doing, or assumptions that remain unverified. If '
    'there are none, say so plainly.\n\n'
    'Keep it tight and skimmable — bullets over prose where it helps, but '
    'every bullet must be a self-explanatory sentence, not a fragment of '
    'transcript. Output ONLY the report (Markdown), no preamble, no role '
    'labels.'
)

# Content-derived reporter version marker — same stale-process-detection
# discipline as VU_PROMPT_VERSION (a directive carrying an old marker was
# produced by a server still running a pre-edit import-time constant).
REPORTER_PROMPT_VERSION = hashlib.sha256(
    _REPORTER_ROLE_PROMPT.encode('utf-8')).hexdigest()[:8]


def _extract_objective(messages: list) -> str:
    """Return the original objective = the FIRST real user message text.

    Skips VU directive turns (``_isVuDirective``) and synthetic virtual-user
    turns (``_isVirtualUser``) so the anchor is always the human's opening
    ask, never an autopilot-generated reply.  Returns '' when none found.
    """
    for m in messages or []:
        if not isinstance(m, dict) or m.get('role') != 'user':
            continue
        if m.get('_isVuDirective') or m.get('_isVirtualUser'):
            continue
        content = m.get('content')
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            # Multimodal content blocks — concatenate the text parts.
            parts = [b.get('text', '') for b in content
                     if isinstance(b, dict) and b.get('type') == 'text']
            text = ' '.join(p for p in parts if p).strip()
        else:
            text = ''
        if text:
            return text
    return ''


def _get_or_persist_objective(conv_id: str, messages: list) -> str:
    """Resolve the immutable autopilot objective for a conversation.

    The objective is the north star the virtual user measures the assistant
    against.  It is captured ONCE (the first real user message) and pinned to
    ``settings.autopilotObjective`` so every follow-up task's VU sees the SAME
    anchor even after compaction has trimmed the early conversation history.

    Read-through cache: returns the persisted value if present; otherwise
    derives it from ``messages``, persists it, and returns it.  All failures
    are non-fatal — the caller falls back to deriving from the live messages.
    """
    if not conv_id:
        return _extract_objective(messages)
    try:
        from lib.database import (
            DOMAIN_CHAT,
            db_execute_with_retry,
            get_thread_db,
        )
        db = get_thread_db(DOMAIN_CHAT)
        srow = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        settings = {}
        if srow:
            try:
                settings = json.loads(srow[0] or '{}')
            except (json.JSONDecodeError, TypeError) as e:
                logger.debug('[Autopilot] objective: settings parse failed '
                             'conv=%s: %s', conv_id[:8], e)
                settings = {}
        existing = (settings.get('autopilotObjective') or '').strip()
        if existing:
            return existing
        objective = _extract_objective(messages)
        if objective and srow is not None:
            settings['autopilotObjective'] = objective
            db_execute_with_retry(
                db,
                'UPDATE conversations SET settings=? WHERE id=? AND user_id=1',
                (json.dumps(settings, ensure_ascii=False), conv_id),
            )
            logger.info('[Autopilot] conv=%s pinned objective (%d chars)',
                        conv_id[:8], len(objective))
        return objective
    except Exception as e:
        logger.warning('[Autopilot] objective resolve failed conv=%s: %s — '
                       'deriving from live messages', conv_id[:8], e)
        return _extract_objective(messages)


def _get_or_persist_run_id(conv_id: str) -> str:
    """Resolve the immutable autopilot run id for a conversation.

    The run id is the EXPLICIT boundary that lets the frontend group a whole
    autopilot run ``[VU turn … summary]`` into one collapsible fold without
    role-scanning the flat message list (which breaks on edits, branches, and
    back-to-back runs). It is minted ONCE per run and pinned to
    ``settings.autopilotRunId`` alongside ``settings.autopilotObjective`` — both
    are cleared together when the run concludes (``disarm`` / TASK_DONE), so the
    next run gets a fresh id.

    Read-through cache: returns the persisted value if present; otherwise mints
    a new uuid, persists it, and returns it. Failures are non-fatal — returns a
    fresh (unpersisted) id so stamping still works for the current turn.
    """
    new_id = 'ar-' + uuid.uuid4().hex[:12]
    if not conv_id:
        return new_id
    try:
        from lib.database import (
            DOMAIN_CHAT,
            db_execute_with_retry,
            get_thread_db,
        )
        db = get_thread_db(DOMAIN_CHAT)
        srow = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        settings = {}
        if srow:
            try:
                settings = json.loads(srow[0] or '{}')
            except (json.JSONDecodeError, TypeError) as e:
                logger.debug('[Autopilot] runId: settings parse failed '
                             'conv=%s: %s', conv_id[:8], e)
                settings = {}
        existing = (settings.get('autopilotRunId') or '').strip()
        if existing:
            return existing
        if srow is not None:
            settings['autopilotRunId'] = new_id
            db_execute_with_retry(
                db,
                'UPDATE conversations SET settings=? WHERE id=? AND user_id=1',
                (json.dumps(settings, ensure_ascii=False), conv_id),
            )
            logger.info('[Autopilot] conv=%s minted runId=%s', conv_id[:8], new_id)
        return new_id
    except Exception as e:
        logger.warning('[Autopilot] runId resolve failed conv=%s: %s — '
                       'using ephemeral id', conv_id[:8], e)
        return new_id


def _clear_run_id(conv_id: str) -> None:
    """Clear the pinned run id + objective when a run concludes.

    Called on TASK_DONE (after the summary is generated) so the NEXT autopilot
    run on the same conversation mints a fresh ``autopilotRunId`` and re-derives
    its objective. Best-effort — a stale pin only means the next run reuses the
    old id (cosmetic), so failures are swallowed at debug level.
    """
    if not conv_id:
        return
    try:
        from lib.database import (
            DOMAIN_CHAT,
            db_execute_with_retry,
            get_thread_db,
        )
        db = get_thread_db(DOMAIN_CHAT)
        srow = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not srow:
            return
        try:
            settings = json.loads(srow[0] or '{}')
        except (json.JSONDecodeError, TypeError):
            return
        changed = False
        for k in ('autopilotRunId', 'autopilotObjective'):
            if settings.pop(k, None) is not None:
                changed = True
        if changed:
            db_execute_with_retry(
                db,
                'UPDATE conversations SET settings=? WHERE id=? AND user_id=1',
                (json.dumps(settings, ensure_ascii=False), conv_id),
            )
            logger.info('[Autopilot] conv=%s cleared runId+objective (run concluded)',
                        conv_id[:8])
    except Exception as e:
        logger.debug('[Autopilot] _clear_run_id failed conv=%s: %s', conv_id[:8], e)


def run_summary_reporter(task: dict) -> dict | None:
    """Run ONE read-only LLM turn that summarises the concluded autopilot run.

    Called from ``maybe_run_autopilot`` at the TASK_DONE boundary (the VU just
    emitted ``[VU: TASK_DONE]``). Mirrors :func:`run_virtual_user`'s sub-task
    pattern — a fresh ``_run_single_turn`` over the parent's full message list
    plus a trailing directive — but with the REPORTER role and ALL tools
    stripped (search/fetch/project/code_exec/browser/memory/swarm/scheduler/…
    off → ``_assemble_tool_list`` returns no tools). The run's transcript
    already contains every tool result the VU and workers produced, so the
    reporter only needs to synthesise; spending another verification budget is
    redundant.

    Returns ``{'text': str}`` on success (non-empty report), or ``None`` when
    there is nothing useful to report (empty output / sub-task error / abort).
    """
    tid = task['id'][:8]
    if task.get('aborted'):
        return None
    parent_messages = task.get('messages') or []
    if not parent_messages:
        return None

    objective = _get_or_persist_objective(task.get('convId') or '',
                                           parent_messages)
    objective_block = ''
    if objective:
        objective_block = (
            '=== ORIGINAL OBJECTIVE (the north star this run was driven '
            'toward) ===\n'
            f'{objective}\n'
            '=== End objective ===\n\n'
        )

    reporter_messages = [dict(m) for m in parent_messages]
    reporter_messages.append({
        'role': 'user',
        'content': (
            f'{objective_block}'
            f'=== Your role for THIS turn: Run Reporter '
            f'[prompt v{REPORTER_PROMPT_VERSION}] ===\n'
            f'{_REPORTER_ROLE_PROMPT}\n'
            '=== End reporter role ===\n\n'
            'Write the close-out report now.'
        ),
        '_isReporterDirective': True,
        '_reporterPromptVersion': REPORTER_PROMPT_VERSION,
    })

    from lib.tasks_pkg import create_task
    from lib.tasks_pkg.orchestrator import _run_single_turn

    sub_cfg = dict(task.get('config') or {})
    for stale_key in (
        'excludeLast', 'toolHistory', 'contentPrefix',
        'checkpointToolRounds', 'checkpointUsage', 'checkpointApiRounds',
        'checkpointModifiedFiles', 'checkpointModifiedFileList',
    ):
        sub_cfg.pop(stale_key, None)
    sub_cfg['endpointMode'] = False
    sub_cfg['autopilot'] = False
    # Read-only: strip every tool-enabling feature so the reporter purely
    # synthesises the existing transcript (no new state changes, cheap).
    sub_cfg['searchMode'] = 'off'
    sub_cfg['fetchEnabled'] = False
    sub_cfg['projectPath'] = ''
    sub_cfg['codeExecEnabled'] = False
    sub_cfg['browserEnabled'] = False
    sub_cfg['desktopEnabled'] = False
    sub_cfg['memoryEnabled'] = False
    sub_cfg['swarmEnabled'] = False
    sub_cfg['imageGenEnabled'] = False
    sub_cfg['humanGuidanceEnabled'] = False
    sub_cfg['schedulerEnabled'] = False
    sub_cfg.pop('tools', None)

    sub_task = create_task('', reporter_messages, sub_cfg)
    sub_task['_inline_messages'] = True
    sub_task['_vu_subtask'] = True
    sub_task['_autopilotParent'] = task.get('id', '')

    try:
        result = _run_single_turn(sub_task)
    except Exception as e:
        logger.warning('[Autopilot %s] Summary reporter sub-task raised: %s — '
                       'skipping report', tid, e, exc_info=True)
        return None

    if task.get('aborted'):
        return None
    err = result.get('error')
    if err:
        logger.warning('[Autopilot %s] Summary reporter error: %.200s — '
                       'skipping report', tid, err)
        return None

    text = (result.get('content') or '').strip()
    if not text:
        logger.info('[Autopilot %s] Summary reporter produced empty output — '
                    'skipping report', tid)
        return None
    logger.info('[Autopilot %s] Summary report ready (%d chars)', tid, len(text))
    return {'text': text}


def _store_run_record(conv_id: str, run_id: str, *,
                      reason: str,
                      text: str = '',
                      translated: str = '') -> dict | None:
    """Persist the SINGLE authoritative per-run record in the SIDECAR.

    ONE record per run carries BOTH facts the frontend needs — that the run has
    ``status='concluded'`` (with its ``reason``) AND the optional close-out
    ``content`` (a manual stop has none). There is deliberately no second map
    and no ``status='running'`` record: only the TERMINAL fact is persisted, so
    the frontend's ``syncConversationToServer`` (which rebuilds the whole
    ``settings`` column from a whitelist) can never clobber a mid-run marker it
    doesn't yet hold.

    Stored under ``settings.autopilotSummaries[run_id]`` (kept for the existing
    read/write/IDB whitelist + reload round-trip), which surfaces to the
    frontend as ``conv.autopilotSummaries[run_id]`` without ever becoming a chat
    turn. The frontend gates the run fold on ``rec.status==='concluded'`` and
    renders ``content`` (when present) as the fold's read-only report panel.

    Read-modify-write is idempotent per run: re-concluding merges (a later
    ``task_done`` with a report supersedes an earlier bare ``stopped`` marker,
    and never downgrades a report back to empty). Returns the stored record
    (``{runId, status, reason, content?, translatedContent?, ts, _summaryId}``),
    or ``None`` on failure. The record has NO ``role`` and NO ``_msgId`` — it is
    not a message.
    """
    try:
        from lib.database import (
            DOMAIN_CHAT,
            db_execute_with_retry,
            get_thread_db,
        )
    except Exception as e:
        logger.warning('[Autopilot] run record: DB import failed: %s', e)
        return None
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            logger.warning('[Autopilot] run record: conv=%s not found', conv_id[:8])
            return None
        try:
            settings = json.loads(row[0] or '{}') if row[0] else {}
            if not isinstance(settings, dict):
                settings = {}
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Autopilot] run record: conv=%s settings parse failed: %s',
                           conv_id[:8], e)
            settings = {}
        summaries = settings.get('autopilotSummaries')
        if not isinstance(summaries, dict):
            summaries = {}
        prior = summaries.get(run_id) if isinstance(summaries.get(run_id), dict) else {}
        # Never downgrade a run that already carries a report to an empty one:
        # a later manual-stop conclude on a run that cleanly reported keeps the
        # report + upgrades the reason only if the new one is task_done.
        content = text or (prior.get('content') or '')
        translated_final = translated or (prior.get('translatedContent') or '')
        # 'task_done' is the strongest reason and never downgrades: a manual
        # stop that races a clean close-out keeps the clean verdict.
        reason_final = ('task_done'
                        if prior.get('reason') == 'task_done' else reason)
        record = {
            'runId': run_id,
            'status': 'concluded',
            'reason': reason_final,
            'ts': int(time.time() * 1000),
            '_summaryId': prior.get('_summaryId') or str(uuid.uuid4()),
        }
        if content:
            record['content'] = content
        if translated_final:
            record['translatedContent'] = translated_final
        summaries[run_id] = record
        settings['autopilotSummaries'] = summaries
        db_execute_with_retry(
            db,
            'UPDATE conversations SET settings=? WHERE id=? AND user_id=1',
            (json.dumps(settings, ensure_ascii=False), conv_id),
        )
        logger.info('[Autopilot] conv=%s ✅ Stored concluded run record in sidecar '
                    '(reason=%s, %d report chars, run=%s, NOT a message)',
                    conv_id[:8], reason, len(content), run_id)
        return record
    except Exception as e:
        logger.error('[Autopilot] conv=%s run record sidecar store failed: %s',
                     conv_id[:8], e, exc_info=True)
        return None


def _store_run_summary(conv_id: str, run_id: str, text: str,
                       translated: str = '') -> dict | None:
    """Persist a CLEAN close-out run (VU emitted [VU: TASK_DONE]) + its report.

    Thin wrapper over :func:`_store_run_record` with ``reason='task_done'`` —
    the record carries both the concluded status and the human-only report.
    Kept as the summary-writing entry point; see ``_store_run_record`` for the
    single-source-of-truth persistence + no-clobber rationale.
    """
    return _store_run_record(conv_id, run_id, reason='task_done',
                             text=text, translated=translated)


def _translate_summary_sync(text: str) -> str:
    """Best-effort synchronous EN→ZH translation of a summary for display.

    The summary lives in the sidecar (not the message list), so the
    index-based ``_maybe_auto_translate_assistant`` safety net does not apply.
    We translate it inline here via the shared free-text engine and stash the
    result on the sidecar record, so a Chinese UI reads the debrief in Chinese.
    Returns the translated text, or ``''`` on skip/failure (caller stores the
    original only).
    """
    if not text:
        return ''
    try:
        from lib.text_lang import is_predominantly_chinese
        if is_predominantly_chinese(text):
            return ''  # already Chinese — no-op
        from lib.translate import _build_translate_prompt, _translate_freetext
        system_prompt = _build_translate_prompt('Chinese', 'English')
        translated, _u = _translate_freetext(
            text, system_prompt, chunk_label=':ap-summary',
            source='English', target='Chinese')
        return (translated or '').strip()
    except Exception as e:
        logger.debug('[Autopilot] summary translate skipped: %s', e)
        return ''


def is_autopilot_enabled(task: dict) -> bool:
    """True iff autopilot is active for this task AND endpoint mode is not.

    Autopilot is "active" when EITHER:
      • ``config['autopilot']`` is set (config-driven — toggle was ON at the
        real send, propagated into the task and its follow-ups), OR
      • a persistent autopilot armed-marker exists for the conversation
        (the mid-stream / idle "arm" gesture; survives page reload and is
        cancellable from the queue bar).

    Endpoint mode wins the mutual exclusion (both share the same
    "model stopped" boundary).  The VU sub-task (``_vu_subtask``) and
    inline tasks never consult the marker — only DB-backed parent/follow-up
    tasks do, so the cheap config flag covers the hot recursion guard.
    """
    cfg = task.get('config') or {}
    if cfg.get('endpointMode') or task.get('_endpoint_managed'):
        return False
    if cfg.get('autopilot'):
        return True
    # Persistent armed-marker fallback (mid-stream arm / reload survival).
    if task.get('_vu_subtask') or task.get('_inline_messages'):
        return False
    conv_id = task.get('convId') or ''
    if not conv_id:
        return False
    try:
        from lib.message_queue import has_autopilot_marker
        return has_autopilot_marker(conv_id)
    except Exception as e:
        logger.debug('[Autopilot] marker probe failed (non-fatal): %s', e)
        return False


_VU_FORWARD_TYPES = frozenset({
    'delta', 'phase',
    'tool_start', 'tool_result', 'tool_progress', 'tool_complete',
    'tool_compacted',
    'stdin_request', 'stdin_resolved',
    'write_approval_request',
    'human_guidance_request', 'human_guidance_response',
})


class _VUEventForwarder(list):
    """List subclass that forwards the VU sub-task's events to the parent.

    The orchestrator drives all SSE updates by calling
    ``manager.append_event(task, ev)`` which does
    ``task['events'].append(ev)`` under the task's events_lock.  By
    swapping ``sub_task['events']`` with this subclass we get a hook on
    every event the VU sub-task emits, without monkey-patching
    ``append_event`` globally.

    For each VU event we still append it to the underlying list (so the
    sub-task's own SSE stream stays intact for any reader that ever
    connects to it), and additionally forward two flavours of derived
    events onto the PARENT task's stream:

      1. ``autopilot_vu_event`` — wraps the original VU sub-task event
         (delta / tool_start / tool_result / tool_progress / tool_complete /
         tool_compacted / stdin_* / write_approval_request /
         human_guidance_*) so the frontend can render the VU's reply +
         tool calls into the synthetic-user bubble *as they happen*,
         instead of materializing the whole bubble after the VU
         finishes.  The wrapper carries ``vuMsgId`` so the frontend can
         target the right message.

    The synthetic-user bubble itself is created eagerly by the
    ``autopilot_vu_start`` event (emitted from ``maybe_run_autopilot``
    BEFORE the VU sub-task runs), so the user sees an "Autopilot ·
    composing…" bubble in the USER lane the moment the worker stops —
    NOT a phase chip glued to the worker bubble.  All VU thinking, tool
    calls, and reply text then stream into that bubble via the wrapped
    events above.
    """

    def __init__(self, parent_task, vu_msg_id):
        super().__init__()
        self._parent = parent_task
        self._vu_msg_id = vu_msg_id

    def append(self, ev):
        super().append(ev)
        try:
            self._forward_to_parent(ev)
        except Exception as e:
            logger.debug('[Autopilot] event forward failed (non-fatal): %s', e)

    def _forward_to_parent(self, ev):
        from lib.tasks_pkg.manager import append_event as _ap_event
        et = (ev or {}).get('type')

        # Forward the inner event verbatim, wrapped so the frontend
        # routes it into the VU bubble (by vuMsgId) instead of the
        # parent's worker bubble.  We re-emit the parent-stream phase
        # chip below as well; the two are not mutually exclusive (one
        # paints the VU bubble, the other annotates the parent's chip).
        if et in _VU_FORWARD_TYPES:
            _ap_event(self._parent, build_event(
                EventType.AUTOPILOT_VU_EVENT,
                vuMsgId=self._vu_msg_id,
                inner=ev,
            ))


def run_virtual_user(task: dict, vu_msg_id: str | None = None) -> dict | None:
    """Run the VU LLM (with full tools) and return its reply + investigation.

    The VU runs as a fresh sub-task through the orchestrator's
    ``_run_single_turn``, inheriting the parent task's config so the
    same tools (read_files, search, project edits, memory, MCP, …)
    are available.  The trailing directive user-turn announces the
    "simulated user" role for THIS turn only — the conversation
    history itself is not role-swapped.

    Returns ``{'text': str, 'rounds': list}`` on success, where
    ``rounds`` is the VU sub-task's tool round history (suitable for
    attaching to the persisted synthetic user message so the user can
    see what Autopilot probed).  Returns ``None`` when the loop should
    stop — either because the VU emitted ``[VU: TASK_DONE]``, the
    sub-task failed, or the parent task was aborted while the VU was
    thinking.  An empty ``text`` is a valid "keep going" reply.
    """
    tid = task['id'][:8]
    if task.get('aborted'):
        logger.info('[Autopilot %s] Skip — task aborted', tid)
        return None

    parent_messages = task.get('messages') or []
    if not parent_messages:
        logger.warning('[Autopilot %s] No messages — stopping', tid)
        return None

    # Append the role-override directive as a trailing user turn —
    # same pattern as endpoint_review._run_planner_turn / _run_critic_turn.
    # We pass the parent's full message list verbatim so the VU sees the
    # entire conversation (including tool_calls / tool_result pairs);
    # the orchestrator's compaction layer handles context bounding.
    # Resolve the immutable objective anchor (north star).  Pinned to
    # settings.autopilotObjective so it survives across follow-up tasks and
    # compaction; falls back to deriving from the live messages.
    objective = _get_or_persist_objective(task.get('convId') or '',
                                           parent_messages)
    objective_block = ''
    if objective:
        objective_block = (
            '=== ORIGINAL OBJECTIVE (your north star — does NOT change '
            'across turns) ===\n'
            f'{objective}\n'
            '=== The assistant works for YOU toward this objective. '
            'Hold it to this, not to its own self-report. ===\n\n'
        )

    vu_messages = [dict(m) for m in parent_messages]
    vu_messages.append({
        'role': 'user',
        'content': (
            f'{objective_block}'
            f'=== Your role for THIS turn: Simulated User '
            f'[prompt v{VU_PROMPT_VERSION}] ===\n'
            f'{_VU_ROLE_PROMPT}\n'
            '=== End simulated-user role ===\n\n'
            'Based on the conversation above, produce the simulated '
            'user\'s reply now.  Verify the assistant\'s key claims with '
            'tools first, then output the reply text only.'
        ),
        '_isVuDirective': True,
        '_vuPromptVersion': VU_PROMPT_VERSION,
    })

    # Build a fresh sub-task that inherits the parent's config so
    # _assemble_tool_list constructs the same tool list the worker had.
    # ``_inline_messages=True`` keeps it out of the conv DB sync path,
    # ``convId=''`` keeps it out of the latest-task registry, and
    # ``_endpoint_managed=True`` suppresses the orchestrator's done
    # event + autopilot recursion.
    from lib.tasks_pkg import create_task
    from lib.tasks_pkg.orchestrator import _run_single_turn

    sub_cfg = dict(task.get('config') or {})
    # Strip checkpoint/continue flags so the sub-task starts clean.
    for stale_key in (
        'excludeLast', 'toolHistory', 'contentPrefix',
        'checkpointToolRounds', 'checkpointUsage', 'checkpointApiRounds',
        'checkpointModifiedFiles', 'checkpointModifiedFileList',
    ):
        sub_cfg.pop(stale_key, None)
    # Endpoint mode is gated by is_autopilot_enabled but be defensive —
    # the sub-task must never re-enter endpoint mode.
    sub_cfg['endpointMode'] = False
    # Autopilot must NOT recurse (the parent's hook already runs us).
    sub_cfg['autopilot'] = False
    # Disable ask_human for the simulated user — the VU IS the user, so
    # asking another human makes no sense and would block forever (the
    # in-handler autopilot fallback is gated on cfg.autopilot which we
    # just turned off above).
    sub_cfg['humanGuidanceEnabled'] = False

    sub_task = create_task('', vu_messages, sub_cfg)
    sub_task['_inline_messages'] = True
    sub_task['_vu_subtask'] = True
    sub_task['_autopilotParent'] = task.get('id', '')

    # Swap in a forwarding event list so the VU sub-task's events
    # surface live on the parent stream:
    #   • inner events (delta / tool_start / tool_result / tool_progress
    #     / tool_complete / tool_compacted / stdin_* /
    #     write_approval_request / human_guidance_*) are wrapped as
    #     `autopilot_vu_event` and routed by the frontend into the VU
    #     bubble (created eagerly by the `autopilot_vu_start` event
    #     above) identified by `vuMsgId` — so the user sees the VU's
    #     tool calls and reply STREAM in, not "pop in" once the VU
    #     finishes.
    sub_task['events'] = _VUEventForwarder(task, vu_msg_id or '')

    # Mirror parent abort onto the sub-task so user-clicked Stop while
    # the VU is mid-tool-loop tears the sub-task down too.  Single
    # threaded poll is fine — the sub-task is short-lived and the
    # orchestrator already polls task['aborted'] each round.
    _stop_mirror = threading.Event()

    def _mirror_abort():
        while not _stop_mirror.is_set():
            if task.get('aborted') and not sub_task.get('aborted'):
                sub_task['aborted'] = True
                sub_task['_abort_timestamp'] = time.time()
                sub_task['_abort_reason'] = 'parent_aborted'
                logger.info('[Autopilot %s] Mirroring parent abort onto '
                            'VU sub-task %s', tid, sub_task['id'][:8])
                return
            _stop_mirror.wait(0.5)

    _mirror_thread = threading.Thread(
        target=_mirror_abort,
        name=f'autopilot-abort-mirror-{tid}',
        daemon=True,
    )
    _mirror_thread.start()

    try:
        result = _run_single_turn(sub_task)
    except Exception as e:
        logger.warning('[Autopilot %s] VU sub-task raised: %s — '
                       'stopping autopilot for this conv', tid, e,
                       exc_info=True)
        return None
    finally:
        _stop_mirror.set()

    if task.get('aborted'):
        logger.info('[Autopilot %s] Aborted during VU sub-task — stopping', tid)
        return None

    err = result.get('error')
    if err:
        logger.warning('[Autopilot %s] VU sub-task error: %.200s — '
                       'stopping autopilot for this conv', tid, err)
        return None

    text = (result.get('content') or '').strip()
    rounds = list(sub_task.get('toolRounds') or [])
    # Route the stop decision through the single source of truth.  The
    # virtual_user policy ends the loop only on an explicit TASK_DONE/STOP
    # AND downgrades that to "keep going" when the reply itself still flags
    # unresolved work (❌ / "NOT met" / "still failing" / "unresolved") — the
    # anti-premature-done guard lives in lib/agent_verdict.py, NOT here.
    verdict = _classify_verdict(text, verifier_role='virtual_user')
    if verdict['phase'] == 'stop':
        logger.info('[Autopilot %s] VU emitted TASK_DONE — stopping loop', tid)
        # Signal the hook to clear the persistent armed-marker (disarm) so the
        # loop ends and the queue-bar sentinel disappears.
        task['_vu_emitted_done'] = True
        audit_log('autopilot_stop',
                  task_id=task.get('id', ''),
                  conv_id=task.get('convId', ''),
                  reason='vu_task_done')
        return None

    # The verdict downgraded a premature TASK_DONE to "keep going": the reply
    # may still literally carry the sentinel token.  Strip it so the
    # synthetic user message we feed back is clean instructional text, not a
    # stray sentinel the next turn would mis-read.
    if _VU_DONE_SENTINEL in text:
        text = text.replace(_VU_DONE_SENTINEL, '').strip()

    logger.info('[Autopilot %s] VU reply: %.200s%s (used %d tool round(s))',
                tid, text, ' …' if len(text) > 200 else '', len(rounds))
    return {'text': text, 'rounds': rounds}


# ──────────────────────────────────────────────────────────────────
#  Follow-up scheduling — append a synthetic user msg + start a task
# ──────────────────────────────────────────────────────────────────

def _presync_parent_reply(task: dict) -> None:
    """Commit the parent task's FINAL assistant message to the conv DB.

    MUST run before this hook appends the VU turn / spawns the follow-up:
    once a follow-up registers as ``_conv_latest_task`` the freshness guard
    in ``manager._sync_result_to_conversation`` rejects the parent's final
    write, freezing the reply at its last streaming checkpoint (truncated
    content, ``finishReason=None``) and feeding that truncated copy to the
    follow-up.

    The orchestrator already calls this once before the hook when autopilot
    was enabled at task-creation time.  We repeat it here so the RUNTIME-ARM
    path (autopilot flipped on mid-stream via ``arm_autopilot``) is equally
    safe regardless of whether the arm landed before or after the
    orchestrator's gate — ``_sync_result_to_conversation`` only FILLS the
    trailing assistant slot (find-or-append), so a second call is an
    idempotent no-op when the orchestrator already synced.
    """
    conv_id = task.get('convId') or ''
    if not conv_id or task.get('_inline_messages'):
        return
    try:
        from lib.tasks_pkg.manager import (
            _sync_result_to_conversation,
            build_result_meta,
        )
        _sync_result_to_conversation(task, build_result_meta(task))
    except Exception as e:
        logger.warning('[Autopilot] parent pre-sync failed: %s — follow-up '
                       'may see a truncated parent reply', e, exc_info=True)


def _has_pending_real_message(conv_id: str) -> bool:
    """True if a real user message is queued — autopilot must defer."""
    if not conv_id:
        return False
    try:
        from lib.message_queue import get_queue_depth
        return get_queue_depth(conv_id) > 0
    except Exception as e:
        logger.debug('[Autopilot] queue depth probe failed (non-fatal): %s', e)
        return False


def _successor_already_running(task: dict, conv_id: str) -> bool:
    """True if another task has already taken over for this conversation.

    ``persist_task_result`` runs ``_dispatch_queued_message`` before our
    hook fires, so a queued real-user message will already have spawned
    its own follow-up task.  Spawning a VU follow-up on top of that
    would (a) abort the queued task via ``abort_running_tasks_for_conv``
    and (b) clobber the user's actual question.  Detect this by looking
    at the latest-task registry.
    """
    if not conv_id:
        return False
    try:
        from lib.tasks_pkg.manager import (
            _conv_latest_task,
            _conv_latest_task_lock,
        )
        with _conv_latest_task_lock:
            latest = _conv_latest_task.get(conv_id)
        return bool(latest) and latest != task.get('id')
    except Exception as e:
        logger.debug('[Autopilot] latest-task probe failed (non-fatal): %s', e)
        return False


def _append_vu_message_to_conv(conv_id: str, vu_msg_id: str,
                                text: str,
                                rounds: list | None = None,
                                run_id: str = '') -> dict | None:
    """Append the VU's reply as a user message in the conversation DB.

    Called ONLY after the VU has successfully produced a reply (i.e.
    after ``run_virtual_user`` returned non-``None``).  This is a
    deliberate design choice:

      • We DO NOT pre-write an empty placeholder before the VU runs.
        Doing so used to leave orphan empty rows in the DB whenever
        the cleanup path was missed (server crash, abort race, etc.)
        — visible to the user as "an empty VU bubble at the bottom"
        even when autopilot never actually took over.

      • The frontend lazily creates the VU bubble in memory when it
        receives the first ``autopilot_vu_event`` carrying actual
        content (``delta`` with text or ``tool_start``).  No DB write
        happens until success — so a VU that bails out (``[VU:
        TASK_DONE]``, abort, real user msg) leaves NO trace on disk.

    ``_msgId`` is the caller-minted id that the frontend used to route
    streaming updates; persisting it here lets a page reload right
    AFTER autopilot completes find the same message id and reconcile.
    """
    try:
        from lib.database import (
            DOMAIN_CHAT,
            db_execute_with_retry,
            get_thread_db,
            json_dumps_pg,
        )
    except Exception as e:
        logger.warning('[Autopilot] DB import failed: %s', e)
        return None

    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            logger.warning('[Autopilot] conv=%s not found — cannot append VU msg',
                           conv_id[:8])
            return None
        try:
            messages = json.loads(row[0] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Autopilot] conv=%s messages parse failed: %s',
                           conv_id[:8], e)
            return None

        vu_msg = {
            'role': 'user',
            'content': text,
            'timestamp': int(time.time() * 1000),
            '_msgId': vu_msg_id,
            '_isVirtualUser': True,
        }
        if run_id:
            vu_msg['_autopilotRunId'] = run_id
        if rounds:
            vu_msg['toolRounds'] = rounds
        messages.append(vu_msg)

        now_ms = int(time.time() * 1000)
        try:
            from lib.conversations import build_search_text
            search_text = build_search_text(messages)
        except Exception as e:
            logger.debug('[Autopilot] build_search_text failed: %s', e)
            search_text = ''

        db_execute_with_retry(
            db,
            '''UPDATE conversations
                  SET messages=?, updated_at=?, msg_count=?, search_text=?
                  WHERE id=? AND user_id=1''',
            (json_dumps_pg(messages), now_ms, len(messages), search_text,
             conv_id),
        )
        logger.info('[Autopilot] conv=%s ✅ Appended VU msg %s (%d chars, %d rounds)',
                    conv_id[:8], vu_msg_id[:12], len(text), len(rounds or []))
        return vu_msg
    except Exception as e:
        logger.error('[Autopilot] conv=%s append failed: %s',
                     conv_id[:8], e, exc_info=True)
        return None


def _start_followup_task(task: dict, conv_id: str) -> str | None:
    """Build api_messages from the conversation and spawn a new task.

    Mirrors what ``_start_task_for_conv`` does, but inlined to avoid
    importing from ``routes`` (orchestrator must not pull route-layer
    code at module scope — circular).
    """
    from lib.tasks_pkg import create_task
    from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
    from lib.tasks_pkg.manager import abort_running_tasks_for_conv

    cfg = dict(task.get('config') or {})
    # Strip checkpoint / continue flags so the follow-up runs fresh.
    for stale_key in (
        'excludeLast', 'toolHistory', 'contentPrefix',
        'checkpointToolRounds', 'checkpointUsage', 'checkpointApiRounds',
        'checkpointModifiedFiles', 'checkpointModifiedFileList',
    ):
        cfg.pop(stale_key, None)

    api_messages = build_api_messages_from_db(conv_id, cfg)
    if not api_messages:
        logger.warning('[Autopilot] conv=%s build_api_messages returned '
                       'empty — cannot start follow-up', conv_id[:8])
        return None

    # Belt-and-braces: any other still-running task for this conv is
    # superseded by this autopilot follow-up, same as a real user send.
    abort_running_tasks_for_conv(conv_id)

    new_task = create_task(conv_id, api_messages, cfg)
    new_task_id = new_task['id']
    new_task['_autopilotParent'] = task.get('id')

    logger.info('[Autopilot] Spawning follow-up task %s for conv=%s '
                '(parent=%s)', new_task_id[:8], conv_id[:8],
                task.get('id', '?')[:8])
    audit_log('autopilot_followup',
              parent_task_id=task.get('id', ''),
              new_task_id=new_task_id,
              conv_id=conv_id)

    try:
        from lib.tasks_pkg import spawn_task as _spawn_task
        _spawn_task(new_task)
    except Exception as e:
        logger.error('[Autopilot] Failed to start follow-up thread: %s',
                     e, exc_info=True)
        from lib.error_envelope import make_envelope as _make_env
        new_task['status'] = 'error'
        new_task['error'] = _make_env(
            'internal',
            detail='Autopilot failed to spawn follow-up thread.',
            model=cfg.get('model', ''),
            context='autopilot',
            source='autopilot',
            raw=str(e),
        )
        return None

    # Update conversation settings.activeTaskId so reload still finds the
    # live task.  Best-effort — failure here doesn't break the loop.
    try:
        from lib.database import (
            DOMAIN_CHAT,
            db_execute_with_retry,
            get_thread_db,
        )
        db = get_thread_db(DOMAIN_CHAT)
        srow = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if srow:
            try:
                settings = json.loads(srow[0] or '{}')
            except (json.JSONDecodeError, TypeError) as _e_audit:
                logger.debug('[autopilot] _start_followup_task caught %s: %s', type(_e_audit).__name__, _e_audit)
                settings = {}
            settings['activeTaskId'] = new_task_id
            db_execute_with_retry(
                db,
                'UPDATE conversations SET settings=? WHERE id=? AND user_id=1',
                (json.dumps(settings, ensure_ascii=False), conv_id),
            )
    except Exception as e:
        logger.debug('[Autopilot] activeTaskId update skipped: %s', e)

    try:
        from lib.conversations import invalidate_meta_cache
        invalidate_meta_cache()
    except Exception as e:
        logger.debug('[Autopilot] meta cache invalidation skipped: %s', e)

    return new_task_id


def _emit_run_concluded(conv_id: str, run_id: str, text: str,
                        config: dict | None) -> None:
    """Emit ONE project-brain 'run_concluded' Activity event for a finished run.

    Autopilot per-turn 'started'/'completed' events are SUPPRESSED at the task
    seams (config.autopilotRunId set), so a deep run surfaces in the feed as a
    single human-meaningful pulse here, at run close-out — keyed on the run's
    project (config.projectPath). Best-effort: never raises into the caller.
    """
    try:
        proj = ((config or {}).get('projectPath') or '').strip()
        if not proj or not conv_id:
            return
        from lib.conversations.project_feed import emit_project_event
        summary = (text or '').strip().splitlines()[0] if text else ''
        emit_project_event(
            proj, conv_id, 'run_concluded',
            summary or 'Autopilot run concluded',
            payload={'runId': run_id})
    except Exception as e:
        logger.debug('[Autopilot] run_concluded feed emit skipped: %s', e)


def conclude_run(conv_id: str, reason: str = 'stopped',
                 run_id: str = '') -> dict | None:
    """Record the BACKEND-AUTHORITATIVE 'this autopilot run is over' fact.

    The single close-out seam for the paths that end a run WITHOUT a clean
    ``[VU: TASK_DONE]`` — a manual Stop, the toggle-OFF / queue-cancel disarm,
    or a superseding real user message. Historically these were "dumb": they
    cleared the marker but emitted NO run-level signal, so the frontend was
    forced to INFER run-end from stream/task absence (the inter-turn-gap
    heuristic that caused premature folds). This makes the terminal fact
    explicit and durable instead.

    Writes ONE concluded record (no report ``content`` — a manual stop has
    none) to the sidecar via :func:`_store_run_record`, then clears the run pin
    (``autopilotRunId`` + objective) so the next run is fresh — exactly the
    clean-close-out ordering. Idempotent: concluding an already-concluded run
    just refreshes the record (and never downgrades a ``task_done`` verdict).

    ``run_id`` may be passed explicitly; when omitted it is resolved from the
    most recent VU turn's ``_autopilotRunId`` so an already-disarmed run still
    folds. Returns the stored record, or ``None`` when there is no run to
    conclude (no run id resolvable → nothing was ever an autopilot run).
    """
    if not conv_id:
        return None
    if not run_id:
        run_id = _resolve_recent_run_id(conv_id)
    if not run_id:
        logger.debug('[Autopilot] conclude_run: conv=%s no run id — nothing to '
                     'conclude', conv_id[:8])
        return None
    record = _store_run_record(conv_id, run_id, reason=reason)
    _clear_run_id(conv_id)
    return record


def _resolve_recent_run_id(conv_id: str) -> str:
    """Return the most recent VU turn's ``_autopilotRunId`` for a conversation.

    Prefers the still-pinned ``settings.autopilotRunId`` (the live run); falls
    back to scanning the message tail for the newest ``_autopilotRunId`` stamp
    (an already-disarmed run whose pin was cleared). Returns '' when the
    conversation has no autopilot run at all. Best-effort — failures return ''.
    """
    if not conv_id:
        return ''
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT settings, messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            return ''
        try:
            settings = json.loads(row[0] or '{}') if row[0] else {}
        except (json.JSONDecodeError, TypeError):
            settings = {}
        pinned = (settings.get('autopilotRunId') or '').strip()
        if pinned:
            return pinned
        try:
            msgs = json.loads(row[1] or '[]') if row[1] else []
        except (json.JSONDecodeError, TypeError):
            msgs = []
        for m in reversed(msgs):
            if isinstance(m, dict) and (m.get('_autopilotRunId') or '').strip():
                return m['_autopilotRunId'].strip()
    except Exception as e:
        logger.debug('[Autopilot] _resolve_recent_run_id failed conv=%s: %s',
                     conv_id[:8], e)
    return ''


def _emit_run_summary(task: dict, conv_id: str, run_id: str) -> dict | None:
    """Generate, persist (sidecar), translate and emit the run close-out summary.

    Ties together the read-only reporter turn (:func:`run_summary_reporter`),
    SIDECAR persistence (:func:`_store_run_summary` — NOT the message list),
    a synchronous best-effort EN→ZH translation for display, and the
    ``autopilot_run_concluded`` SSE event (reason=task_done, carrying the report
    ``record``) so the frontend folds the run and shows the summary as the run
    fold's read-only report panel (human-only — it never enters the chat
    transcript nor the LLM context). This is the clean-close-out arm of the
    symmetric conclude contract; :func:`conclude_run` is the manual-stop arm.

    Returns the stored sidecar record, or ``None`` when no report was produced
    (empty / errored / aborted) — folding then keys on ``run_id`` alone and
    shows the last VU turn as the tail.
    """
    from lib.tasks_pkg.manager import append_event

    tid = task['id'][:8]
    report = run_summary_reporter(task)
    if report is None:
        return None

    # The reporter composes in the assistant's language; honour the per-conv
    # autoTranslate by translating SYNCHRONOUSLY for display (the summary is in
    # the sidecar, so the index-based auto-translate safety net can't reach it).
    translated = ''
    try:
        from lib.conv_config import resolve_auto_translate
        if resolve_auto_translate(task.get('config') or {}):
            translated = _translate_summary_sync(report['text'])
    except Exception as e:
        logger.debug('[Autopilot %s] summary translate skipped: %s', tid, e)

    record = _store_run_summary(conv_id, run_id, report['text'], translated)
    if record is None:
        return None

    _emit_run_concluded(conv_id, run_id, report['text'], task.get('config'))

    try:
        append_event(task, build_event(
            EventType.AUTOPILOT_RUN_CONCLUDED,
            runId=run_id,
            record=record,
        ))
    except Exception as e:
        logger.debug('[Autopilot %s] run-concluded emit failed: %s', tid, e)
    return record


def summarize_run(conv_id: str, run_id: str = '', config: dict | None = None) -> dict:
    """On-demand close-out report for a concluded (e.g. manually-stopped) run.

    Backs the "Summarize this run" affordance for autopilot runs that ended
    WITHOUT a clean ``[VU: TASK_DONE]`` (Stop / a new user message) — those get
    no auto-summary, so the user can request one after the fact. The run is
    finished and lives in conversation history, so we build a read-only
    reporter sub-task over the FULL conversation and store the summary in the
    sidecar (human-only — NOT appended to the message list).

    ``run_id`` groups the resulting summary into the run's fold. When omitted,
    it is resolved from the most recent VU turn's ``_autopilotRunId`` so an
    already-disarmed run (pin cleared) still folds correctly.

    Returns ``{ok, summary?, runId?, error?}`` — ``summary`` is the human-only
    SIDECAR record (NOT a chat message). Synchronous (the caller is an explicit
    user click, not the hot loop).
    """
    if not conv_id:
        return {'ok': False, 'error': 'conv_id is required'}

    from lib.tasks_pkg import create_task
    from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db

    # Resolve run_id from the most recent VU turn if not provided.
    if not run_id:
        try:
            from lib.database import DOMAIN_CHAT, get_thread_db
            db = get_thread_db(DOMAIN_CHAT)
            row = db.execute(
                'SELECT messages FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)
            ).fetchone()
            if row:
                msgs = json.loads(row[0] or '[]')
                for m in reversed(msgs):
                    if isinstance(m, dict) and m.get('_autopilotRunId'):
                        run_id = m['_autopilotRunId']
                        break
        except Exception as e:
            logger.debug('[Autopilot] summarize_run: runId resolve failed: %s', e)
    if not run_id:
        run_id = 'ar-' + uuid.uuid4().hex[:12]

    cfg = dict(config or {})
    api_messages = build_api_messages_from_db(conv_id, cfg)
    if not api_messages:
        return {'ok': False, 'error': 'conversation_empty'}

    # Reuse the reporter via a throwaway carrier task holding the messages.
    # ``run_summary_reporter`` is SYNCHRONOUS and runs its OWN ``conv_id=''``
    # sub-task; this carrier is only a read-only parameter bundle (messages +
    # config + convId for objective resolution), never spawned, never finalized.
    #
    # CRITICAL: create it with ``conv_id=''`` — matching the VU/reporter
    # sub-task convention (see run_virtual_user ~L733: "convId='' keeps it out
    # of the latest-task registry"). With the real conv_id it would (1) be
    # marked ``status='running'`` and claim ``_conv_latest_task[conv_id]`` BEFORE
    # we can flag it ``_inline_messages`` (a registration WINDOW), and (2) never
    # reach a terminal status, so it lingers forever (TTL cleanup only evicts
    # done/error/aborted) — ``/api/chat/active`` then reports a phantom running
    # task for this conv and the frontend orphan-recovery spawns a permanently
    # stuck "Waiting…" placeholder. ``conv_id=''`` means no conv ever maps to it
    # via the convId-keyed orphan-recovery, atomically (no window). The summary
    # write uses the REAL conv_id via _store_run_summary below; objective
    # resolution falls back to deriving from the messages (which already carry
    # the original request). discard_task then pops it from the registry too.
    from lib.tasks_pkg import discard_task
    carrier = create_task('', api_messages, cfg)
    carrier['_inline_messages'] = True
    try:
        report = run_summary_reporter(carrier)
    finally:
        discard_task(carrier['id'])
    if report is None:
        return {'ok': False, 'error': 'empty_report'}
    translated = ''
    try:
        from lib.conv_config import resolve_auto_translate
        if resolve_auto_translate(cfg):
            translated = _translate_summary_sync(report['text'])
    except Exception as e:
        logger.debug('[Autopilot] summarize_run translate skipped: %s', e)
    record = _store_run_summary(conv_id, run_id, report['text'], translated)
    if record is None:
        return {'ok': False, 'error': 'persist_failed'}
    _emit_run_concluded(conv_id, run_id, report['text'], cfg)
    audit_log('autopilot_manual_summary', conv_id=conv_id, run_id=run_id)
    return {'ok': True, 'summary': record, 'runId': run_id}


def maybe_run_autopilot(task: dict) -> dict | None:
    """End-of-turn hook: run the VU and spawn a follow-up task if eligible.

    Called from ``_finalize_and_emit_done`` BEFORE ``append_event(done_evt)``
    so the returned info can be embedded in the same ``done`` SSE event
    that finishes the current turn.  This eliminates the polling race
    where the SSE stream closed before the VU had time to spawn the
    follow-up task — the synthetic user message is now delivered
    in-band on the same connection.

    Returns ``{'next_task_id': str, 'vu_msg': dict}`` when a follow-up
    was spawned, ``None`` otherwise (no autopilot, no eligible context,
    VU emitted ``[VU: TASK_DONE]``, real user message queued, or any
    failure path).  The orchestrator inlines the dict into ``done_evt``
    as ``autopilotNextTaskId`` + ``autopilotVuMessage``.
    """
    tid = task['id'][:8]

    if not is_autopilot_enabled(task):
        # Log at debug level so silencing is invisible in normal mode
        # but findable when someone wonders "why didn't it take over?".
        cfg = task.get('config') or {}
        logger.debug('[Autopilot %s] Skip — not enabled '
                     '(autopilot=%s, endpointMode=%s, _endpoint_managed=%s)',
                     tid, cfg.get('autopilot'), cfg.get('endpointMode'),
                     task.get('_endpoint_managed'))
        return None

    conv_id = task.get('convId') or ''
    if not conv_id or task.get('_inline_messages'):
        logger.debug('[Autopilot %s] Skip — no DB-backed conversation', tid)
        return None
    if task.get('aborted'):
        logger.info('[Autopilot %s] Skip — task aborted before VU could run', tid)
        return None
    if task.get('error'):
        logger.info('[Autopilot %s] Skip — task ended in error: %.120s',
                    tid, str(task.get('error')))
        return None
    if task.get('finishReason') == 'tool_rounds_exhausted':
        logger.info('[Autopilot %s] Skip — tool rounds exhausted', tid)
        return None

    if _has_pending_real_message(conv_id):
        logger.info('[Autopilot %s] Skip — real user message queued '
                    '(it takes priority)', tid)
        return None
    # ``_successor_already_running`` is largely redundant in the new
    # ordering (queue dispatch happens AFTER us via persist_task_result),
    # but keep it as defense-in-depth for endpoint-mode / branch flows
    # that may have already advanced the latest-task registry for this
    # conversation.
    if _successor_already_running(task, conv_id):
        logger.info('[Autopilot %s] Skip — another task already took over '
                    'for conv=%s', tid, conv_id[:8])
        return None

    from lib.tasks_pkg.manager import append_event

    # Mint the VU message id up front and EAGERLY emit `autopilot_vu_start`
    # so the frontend creates the simulated-user bubble in the USER lane
    # the moment the worker stops — showing "Autopilot · composing…" with
    # the Autopilot avatar, exactly like a real pending user turn.  The
    # VU's thinking / tool calls / reply then stream INTO that bubble via
    # the wrapped `autopilot_vu_event` frames (see _VUEventForwarder).
    #
    # IMPORTANT — the start event is IN-MEMORY ONLY: it does NOT write
    # anything to the conv DB.  Persistence happens exactly once, on
    # success, in `_append_vu_message_to_conv` (fired right before
    # `autopilot_vu_done`).  Failure paths (TASK_DONE / abort / queued
    # real user msg) emit `autopilot_vu_cancel`, which removes the
    # in-memory bubble and leaves NO trace on disk — preserving the
    # "no ghost empty VU at the bottom" guarantee.
    vu_msg_id = str(uuid.uuid4())
    # Resolve the explicit run boundary up front so it can be stamped on
    # BOTH the VU turn (below) and the summary report (TASK_DONE branch).
    run_id = _get_or_persist_run_id(conv_id)

    try:
        append_event(task, build_event(
            EventType.AUTOPILOT_VU_START,
            vuMsgId=vu_msg_id,
        ))
    except Exception as e:
        logger.debug('[Autopilot %s] vu_start emit failed: %s', tid, e)

    vu_result = run_virtual_user(task, vu_msg_id=vu_msg_id)
    if vu_result is None:
        # VU emitted [VU: TASK_DONE], errored, or task was aborted.
        # On a graceful TASK_DONE, disarm the persistent marker so the loop
        # ends and the queue-bar sentinel disappears.  (Abort/error leave the
        # marker intact — a transient failure shouldn't silently disarm.)
        if task.get('_vu_emitted_done'):
            # The run reached its objective. Generate ONE read-only summary
            # report (the visible tail of the now-foldable run) BEFORE
            # disarming, then clear the run pin so the next run is fresh.
            summary_rec = None
            try:
                summary_rec = _emit_run_summary(task, conv_id, run_id)
            except Exception as e:
                logger.warning('[Autopilot %s] summary generation failed '
                               '(non-fatal): %s', tid, e, exc_info=True)
            # A clean [VU: TASK_DONE] ALWAYS concludes the run — even when the
            # reporter produced nothing (empty/errored). Without this, an
            # empty-report clean close-out would write no concluded record and
            # the run could never fold. When _emit_run_summary DID write a
            # (reason=task_done, with-report) record, this is a harmless
            # idempotent refresh that keeps the report.
            if summary_rec is None:
                try:
                    _store_run_record(conv_id, run_id, reason='task_done')
                except Exception as e:
                    logger.warning('[Autopilot %s] concluded-record fallback '
                                   'failed (non-fatal): %s', tid, e, exc_info=True)
            try:
                from lib.message_queue import clear_autopilot_marker
                clear_autopilot_marker(conv_id)
            except Exception as e:
                logger.debug('[Autopilot %s] marker clear failed: %s', tid, e)
            _clear_run_id(conv_id)
        # Tell the frontend to discard any in-memory bubble it may
        # have lazily created from inner stream events; nothing was
        # ever persisted.
        try:
            append_event(task, build_event(
                EventType.AUTOPILOT_VU_CANCEL,
                vuMsgId=vu_msg_id,
            ))
        except Exception as e:
            logger.debug('[Autopilot %s] vu_cancel emit failed: %s', tid, e)
        return None
    vu_text = vu_result['text']
    vu_rounds = vu_result.get('rounds') or []

    # Race-close: a real user may have submitted a message while the VU
    # LLM call was running.  If so, defer to that real message instead
    # of clobbering it with a synthetic VU turn.
    if _has_pending_real_message(conv_id):
        logger.info('[Autopilot %s] Real user message arrived during VU '
                    'call — deferring to queue', tid)
        try:
            append_event(task, build_event(EventType.AUTOPILOT_VU_CANCEL,
                                 vuMsgId=vu_msg_id))
        except Exception as e:
            logger.debug('[Autopilot %s] vu_cancel emit failed: %s', tid, e)
        return None
    if task.get('aborted'):
        logger.info('[Autopilot %s] Aborted while VU was running — stopping', tid)
        try:
            append_event(task, build_event(EventType.AUTOPILOT_VU_CANCEL,
                                 vuMsgId=vu_msg_id))
        except Exception as e:
            logger.debug('[Autopilot %s] vu_cancel emit failed: %s', tid, e)
        return None

    # VU produced a reply — NOW commit it to the conv DB.  But FIRST make
    # sure the parent's final assistant reply is committed: on the
    # runtime-arm path (autopilot flipped on mid-stream) the orchestrator's
    # pre-hook sync may have been skipped (it gates on is_autopilot_enabled
    # evaluated a few lines earlier), so do it here too — idempotent.
    _presync_parent_reply(task)
    vu_msg = _append_vu_message_to_conv(
        conv_id, vu_msg_id, vu_text, rounds=vu_rounds, run_id=run_id,
    )
    if vu_msg is None:
        return None

    # Tell the frontend the VU bubble is fully baked.  Carries the
    # final content + rounds so a client that lazily built the bubble
    # from streaming deltas — or one that missed them entirely (cold
    # replay, late connect) — can reconcile in one shot.
    try:
        append_event(task, build_event(
            EventType.AUTOPILOT_VU_DONE,
            vuMsgId=vu_msg_id,
            vuMessage=vu_msg,
        ))
    except Exception as e:
        logger.debug('[Autopilot %s] vu_done emit failed: %s', tid, e)

    next_task_id = _start_followup_task(task, conv_id)
    if next_task_id is None:
        return None

    # Tell ``_dispatch_queued_message`` (which runs slightly after us
    # inside ``persist_task_result``) that autopilot already spawned a
    # successor for this conversation.  Otherwise a real user message
    # that landed in the tiny window between our post-VU queue re-check
    # and now would race-spawn its own task and abort our follow-up.
    # The queued message will be picked up when the autopilot follow-up
    # itself completes.
    task['_autopilot_spawned_followup'] = next_task_id

    return {'next_task_id': next_task_id, 'vu_msg': vu_msg}


# ──────────────────────────────────────────────────────────────────
#  Kick from idle — start the VU loop on a FINISHED conversation
# ──────────────────────────────────────────────────────────────────

def _run_autopilot_kick(task: dict) -> None:
    """Carrier-task entry: run the VU hook directly, with NO worker turn.

    Used by the "push the conversation forward" gesture (empty-Enter on a
    finished conversation with autopilot ON).  Unlike a normal task, this
    carrier never calls the LLM as the assistant — the conversation already
    ended and the last message is the agent's reply, so the virtual user
    should answer it straight away.  We reuse the SAME end-of-turn hook the
    natural-stop path runs (``maybe_run_autopilot``): it emits the
    ``autopilot_vu_*`` stream, appends the synthetic user message, spawns the
    follow-up worker task, and returns the ``next_task_id`` / ``vu_msg``
    baton.  The baton rides out on this carrier's ``done`` event (and on
    ``task['_autopilot_followup']`` for the poll path) exactly as it does at
    a natural stop, so the frontend attaches to the follow-up with no extra
    plumbing.

    Invoked from ``orchestrator.run_task`` when ``task['_autopilot_kick']``
    is set.
    """
    from lib.tasks_pkg.manager import append_event, persist_task_result

    tid = task['id'][:8]
    # The carrier produces no assistant content of its own; flip to 'done'
    # immediately so the SSE generator / poll treat the (in-flight) autopilot
    # decision window correctly via the _autopilot_deciding latch below.
    task['status'] = 'done'

    done_evt = build_event(EventType.DONE)
    if task.get('model'):
        done_evt['model'] = task['model']

    task['_autopilot_deciding'] = True
    try:
        ap_result = maybe_run_autopilot(task)
        if ap_result:
            done_evt['autopilotNextTaskId'] = ap_result['next_task_id']
            done_evt['autopilotVuMessage'] = ap_result['vu_msg']
            # Same transport-agnostic stash as the natural-stop path so a
            # client that fell back to /api/chat/poll still gets the baton.
            task['_autopilot_followup'] = ap_result
            logger.info('[Autopilot kick %s] VU took over conv=%s → follow-up %s',
                        tid, task.get('convId', '')[:8],
                        ap_result['next_task_id'][:8])
        else:
            logger.info('[Autopilot kick %s] VU declined to take over conv=%s '
                        '(TASK_DONE / no eligible context)', tid,
                        task.get('convId', '')[:8])
    except Exception as e:
        # Failure → no baton will arrive; clear the latch so the stream can
        # finalize.  The success path keeps it set until AFTER append_event
        # (below) so the SSE generator never sees a terminal task before the
        # baton-carrying done event is buffered (same window as the
        # natural-stop path in orchestrator._finalize_and_emit_done).
        task['_autopilot_deciding'] = False
        logger.error('[Autopilot kick %s] hook raised: %s', tid, e, exc_info=True)

    append_event(task, done_evt)
    # Baton is now buffered — safe to let _task_terminal() report finished.
    task['_autopilot_deciding'] = False
    persist_task_result(task)


def kick_autopilot(conv_id: str, config: dict | None = None) -> dict:
    """Start the virtual-user loop on a conversation whose reply has finished.

    The "push it forward for me" gesture: the user chatted with autopilot ON,
    the turn ended, and they want the virtual user to keep the conversation
    going WITHOUT typing anything.  Because ``maybe_run_autopilot`` only runs
    as an end-of-turn hook (there is no live task to hang it on once the reply
    finished), we spawn a thin carrier task whose ``run_task`` short-circuits
    straight to :func:`_run_autopilot_kick`.

    Refuses (``taskId=None``) when a non-VU task is already ``running`` for the
    conversation — in that case the caller should ARM the live task instead
    (see :func:`arm_autopilot`), so we never double-drive the loop.

    Also persists ``settings.autopilotEnabled=true`` so subsequent manual
    sends keep looping, mirroring the arm route.

    Returns ``{'taskId': str}`` on success, or ``{'taskId': None, 'error':
    str}`` when there is nothing to kick (no conversation, empty history, or a
    task is already running).
    """
    if not conv_id:
        return {'taskId': None, 'error': 'conv_id is required'}

    # Refuse if a live (non-VU) task is already running — arm it instead.
    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        for t in tasks.values():
            if (t.get('convId') == conv_id
                    and t.get('status') == 'running'
                    and not t.get('_vu_subtask')):
                logger.info('[Autopilot kick] conv=%s already has a running '
                            'task %s — refusing kick (arm instead)',
                            conv_id[:8], t.get('id', '?')[:8])
                return {'taskId': None, 'error': 'task_already_running'}

    cfg = dict(config or {})
    cfg['autopilot'] = True
    cfg['endpointMode'] = False
    for stale_key in (
        'excludeLast', 'toolHistory', 'contentPrefix',
        'checkpointToolRounds', 'checkpointUsage', 'checkpointApiRounds',
        'checkpointModifiedFiles', 'checkpointModifiedFileList',
    ):
        cfg.pop(stale_key, None)

    from lib.tasks_pkg import create_task, spawn_task
    from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db

    api_messages = build_api_messages_from_db(conv_id, cfg)
    if api_messages is None:
        return {'taskId': None, 'error': 'conversation_not_found'}
    if not api_messages:
        return {'taskId': None, 'error': 'conversation_empty'}

    task = create_task(conv_id, api_messages, cfg)
    task['_autopilot_kick'] = True

    # Persist the setting so the loop keeps going on any later manual send.
    try:
        from lib.database import (
            DOMAIN_CHAT,
            db_execute_with_retry,
            get_thread_db,
        )
        db = get_thread_db(DOMAIN_CHAT)
        srow = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if srow:
            try:
                settings = json.loads(srow[0] or '{}')
            except (json.JSONDecodeError, TypeError) as e:
                logger.debug('[Autopilot kick] settings parse failed conv=%s: %s',
                             conv_id[:8], e)
                settings = {}
            settings['autopilotEnabled'] = True
            settings['activeTaskId'] = task['id']
            db_execute_with_retry(
                db,
                'UPDATE conversations SET settings=? WHERE id=? AND user_id=1',
                (json.dumps(settings, ensure_ascii=False), conv_id),
            )
    except Exception as e:
        logger.warning('[Autopilot kick] persist autopilotEnabled failed '
                       'conv=%s: %s', conv_id[:8], e)

    logger.info('[Autopilot kick] conv=%s spawning carrier task %s',
                conv_id[:8], task['id'][:8])
    audit_log('autopilot_kick', conv_id=conv_id, task_id=task['id'])
    spawn_task(task)
    return {'taskId': task['id']}


def resume_armed_autopilot_after_crash(
        extra_conv_ids: list[str] | None = None) -> list[str]:
    """Re-kick every autopilot run left armed when the server died.

    When the server dies while an autopilot follow-up is in flight, the
    end-of-turn hook (:func:`maybe_run_autopilot`) never finished: no VU reply
    was persisted, no follow-up spawned, and no ``done`` baton was emitted.
    Startup recovery (:func:`recover_stale_tasks_on_startup`) restores the
    interrupted assistant reply into the conversation, but it does NOT resume
    the loop — so the run is left settled-but-armed and only continues on the
    user's next manual send. This bridges that gap.

    SCOPE — the DURABLE armed-marker is the AUTHORITATIVE source, NOT the set of
    crash-recovered tasks. We enumerate :func:`list_armed_autopilot_convs` (every
    conv carrying a ``KIND_AUTOPILOT`` marker row, which survives restart) and
    re-kick each. This deliberately catches the armed-but-idle case that a
    recovered-tasks-only gate would MISS: a conversation armed from idle whose
    carrier never spawned, or whose reply already finished before the crash, has
    an armed marker but was never an interrupted task — so it is absent from
    ``recovered_conv_ids`` yet must still resume. ``extra_conv_ids`` (the
    recovery set) is unioned in for belt-and-braces, but the marker scan is what
    guarantees completeness.

    Only conversations with an armed marker are resumed — a run that concluded
    cleanly (``[VU: TASK_DONE]``) or was disarmed cleared its marker, so it is
    correctly left alone. ``kick_autopilot`` itself refuses (``taskId=None``) if
    a live non-VU task is already running for the conv, so calling it
    unconditionally is safe — no double-driving. Best-effort per conv: one
    failure never aborts the batch.

    Returns the list of conv_ids for which a resume carrier was spawned.
    """
    resumed: list[str] = []
    try:
        from lib.message_queue import (
            get_autopilot_marker_config,
            has_autopilot_marker,
            list_armed_autopilot_convs,
        )
    except Exception as e:
        logger.warning('[Autopilot] resume-after-crash: message_queue import '
                       'failed: %s', e)
        return resumed

    # Authoritative: every conv with a durable armed marker. Union the recovery
    # set only for logging symmetry — has_autopilot_marker re-gates each below,
    # so a recovered conv WITHOUT a marker (clean-closed / disarmed) is skipped.
    try:
        armed = set(list_armed_autopilot_convs())
    except Exception as e:
        logger.warning('[Autopilot] resume-after-crash: marker scan failed: %s', e)
        armed = set()
    candidates = armed | {c for c in (extra_conv_ids or []) if c}

    for conv_id in candidates:
        if not conv_id:
            continue
        try:
            if not has_autopilot_marker(conv_id):
                continue
            cfg = get_autopilot_marker_config(conv_id) or {}
            res = kick_autopilot(conv_id, cfg)
            new_tid = res.get('taskId')
            if new_tid:
                resumed.append(conv_id)
                logger.info('[Autopilot] Resumed armed run after crash for '
                            'conv=%s → carrier %s', conv_id[:8], new_tid[:8])
                audit_log('autopilot_resume_after_crash',
                          conv_id=conv_id, task_id=new_tid)
            else:
                logger.info('[Autopilot] resume-after-crash skipped conv=%s '
                            '(%s)', conv_id[:8], res.get('error', 'no task'))
        except Exception as e:
            logger.warning('[Autopilot] resume-after-crash failed for conv=%s: '
                           '%s', conv_id[:8], e, exc_info=True)
    return resumed


# ──────────────────────────────────────────────────────────────────
#  Runtime arming — turn autopilot on for an ALREADY-RUNNING task
# ──────────────────────────────────────────────────────────────────
#  Runtime arming — turn autopilot on for an ALREADY-RUNNING task
# ──────────────────────────────────────────────────────────────────

def arm_autopilot(conv_id: str) -> dict:
    """Arm autopilot for a conversation whose task is already in flight.

    Use case: the user chatted with autopilot OFF, then decides to step
    away mid-reply and wants the virtual user to take over at the next
    natural stop.  Toggling the frontend button only affects the NEXT
    task — the in-flight task's ``config['autopilot']`` was frozen at
    creation time, so its end-of-turn hook would never fire.

    This flips ``config['autopilot'] = True`` on every live (status=
    ``running``) task for the conversation.  Because ``_finalize_and_emit_done``
    re-reads ``is_autopilot_enabled(task)`` at finalize, the running task
    will now run the VU hook when it stops.  Mutating ``config`` (rather
    than a side flag) also means the value propagates to autopilot
    follow-ups via ``_start_followup_task``'s ``dict(task['config'])``,
    so the loop continues until the VU emits ``[VU: TASK_DONE]``.

    Endpoint-managed tasks are skipped — autopilot and endpoint mode are
    mutually exclusive (they share the same termination boundary).

    Returns ``{'armed': bool, 'taskIds': [...]}`` — ``armed`` is True iff
    at least one live task was flipped.  When no task is live (the reply
    already finished), ``armed`` is False and the caller should rely on
    the persisted ``autopilotEnabled`` setting to kick off the loop on the
    user's next send.
    """
    from lib.tasks_pkg.manager import tasks, tasks_lock

    armed_ids: list[str] = []
    marker_cfg: dict = {}
    endpoint_blocked = False
    with tasks_lock:
        # Pass 1 — mutual exclusion: if ANY live task for the conv is endpoint
        # mode, refuse to arm autopilot (they share the same termination
        # boundary; running both double-loops).
        for tid, t in tasks.items():
            if t.get('convId') != conv_id or t.get('status') != 'running':
                continue
            if t.get('_vu_subtask'):
                continue
            cfg = t.get('config')
            if t.get('_endpoint_managed') or (isinstance(cfg, dict) and cfg.get('endpointMode')):
                endpoint_blocked = True
                break
        # Pass 2 — flip config.autopilot on live non-endpoint tasks + capture
        # a config to seed the marker.
        if not endpoint_blocked:
            for tid, t in tasks.items():
                if t.get('convId') != conv_id or t.get('status') != 'running':
                    continue
                if t.get('_endpoint_managed') or t.get('_vu_subtask'):
                    continue
                cfg = t.get('config')
                if not isinstance(cfg, dict):
                    continue
                if not marker_cfg:
                    marker_cfg = dict(cfg)
                if not cfg.get('autopilot'):
                    cfg['autopilot'] = True
                    armed_ids.append(tid)

    if endpoint_blocked:
        logger.info('[Autopilot] Arm refused for conv=%s — endpoint mode is '
                    'live (mutually exclusive)', conv_id[:8])
        return {'armed': False, 'taskIds': [], 'markerAdded': False}

    # Persist the armed-marker sentinel in the queue so the arm survives a
    # page reload, shows in the queue bar (cancellable), and — critically —
    # keeps autopilot armed even when no task is live (the "I'll step away,
    # take over when the current reply finishes" gesture works whether or not
    # a reply is still streaming).  Idempotent: at most one marker per conv.
    marker_added = False
    try:
        from lib.message_queue import arm_autopilot_marker
        res = arm_autopilot_marker(conv_id, marker_cfg)
        marker_added = res.get('armed', False)
    except Exception as e:
        logger.warning('[Autopilot] failed to persist armed-marker for '
                       'conv=%s: %s', conv_id[:8], e)

    if armed_ids:
        logger.info('[Autopilot] Armed %d live task(s) for conv=%s: %s '
                    '(marker_added=%s)', len(armed_ids), conv_id[:8],
                    [t[:8] for t in armed_ids], marker_added)
    else:
        logger.info('[Autopilot] Arm requested for conv=%s — no live task to '
                    'flip; persistent marker now governs (marker_added=%s)',
                    conv_id[:8], marker_added)
    audit_log('autopilot_armed', conv_id=conv_id, task_ids=armed_ids,
              marker_added=marker_added)

    # ``armed`` reflects whether autopilot is now armed for the conv — True if
    # a live task was flipped OR a marker is in place.
    armed = bool(armed_ids) or marker_added or _marker_exists(conv_id)
    return {'armed': armed, 'taskIds': armed_ids, 'markerAdded': marker_added}


def _marker_exists(conv_id: str) -> bool:
    try:
        from lib.message_queue import has_autopilot_marker
        return has_autopilot_marker(conv_id)
    except Exception:
        return False


def disarm_autopilot(conv_id: str) -> dict:
    """Cancel autopilot for a conversation: clear the marker + live config.

    The inverse of :func:`arm_autopilot`.  Removes the persistent armed-marker
    sentinel AND flips ``config['autopilot']=False`` on any live task so the
    loop stops at the current turn's natural end.  Used by the queue-bar
    cancel button and the toggle-OFF gesture.

    Returns ``{disarmed, markerCleared, taskIds}``.
    """
    from lib.tasks_pkg.manager import tasks, tasks_lock

    marker_cleared = False
    try:
        from lib.message_queue import clear_autopilot_marker
        marker_cleared = clear_autopilot_marker(conv_id)
    except Exception as e:
        logger.warning('[Autopilot] disarm: marker clear failed for conv=%s: %s',
                       conv_id[:8], e)

    cleared_ids: list[str] = []
    with tasks_lock:
        for tid, t in tasks.items():
            if t.get('convId') != conv_id or t.get('_vu_subtask'):
                continue
            cfg = t.get('config')
            if isinstance(cfg, dict) and cfg.get('autopilot'):
                cfg['autopilot'] = False
                cleared_ids.append(tid)

    # ★ Symmetric close-out — the manual-stop arm of the conclude contract.
    #   Historically disarm was "dumb": it cleared the marker/flag but emitted
    #   NO run-level fact, forcing the frontend to INFER run-end from stream
    #   absence (the inter-turn-gap heuristic behind premature folds). Now we
    #   write the BACKEND-AUTHORITATIVE concluded record (reason=stopped, no
    #   report) so the fold keys on a durable fact — and return it so the
    #   calling client (which may have NO live SSE stream, the idle-disarm
    #   case) can fold instantly without a reload. Self-guards: no run id →
    #   None (nothing was ever an autopilot run to conclude).
    concluded = None
    try:
        concluded = conclude_run(conv_id, reason='stopped')
    except Exception as e:
        logger.warning('[Autopilot] disarm: conclude_run failed for conv=%s: %s',
                       conv_id[:8], e, exc_info=True)

    logger.info('[Autopilot] Disarmed conv=%s (markerCleared=%s, tasks=%s, concluded=%s)',
                conv_id[:8], marker_cleared, [t[:8] for t in cleared_ids],
                bool(concluded))
    audit_log('autopilot_disarmed', conv_id=conv_id,
              marker_cleared=marker_cleared, task_ids=cleared_ids,
              concluded=bool(concluded))
    result = {'disarmed': marker_cleared or bool(cleared_ids),
              'markerCleared': marker_cleared, 'taskIds': cleared_ids}
    if concluded is not None:
        result['runConcluded'] = concluded
    return result
