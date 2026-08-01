# HOT_PATH
"""Tool-call parsing — parse (or repair) raw ``tool_calls`` into structured tuples.

The single public entry-point is :func:`parse_tool_calls`, extracted from the
inner loop of ``orchestrator.run_task``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from lib.log import audit_log, get_logger
from lib.tasks_pkg.executor import SWARM_TOOL_NAMES
from lib.tasks_pkg.manager import append_event
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tool_input_repair import HALLUCINATION_ABORT_THRESHOLD, ingest_tool_call

from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names
from lib.tasks_pkg.tool_dispatch._repair import _apply_repair_to_round, _build_repair_summary

logger = get_logger(__name__)


def _reject_undispatched(tc, display_name, tc_id, receipt_msg, rejected_meta,
                         task, tool_round_num, round_num, project_enabled):
    """Give an UNDELIVERABLE tool call a rejected round + a model-facing receipt.

    Rides the exact lane hallucinated tools already use: a ``status='rejected'``
    round the UI renders + a ``parse_error`` the pipeline returns to the model
    as a ``role:'tool'`` message in original tool-call order. The alternative —
    the old ``continue`` — left the model with an unexplained hole that the
    orphan-stripper then erased from the wire, and it INVENTED an explanation
    (``tool-call limit reached`` spam, pt_914bb730).

    Deliberately does NOT consume the round's prose tag (``_ac_tagged``): a
    junk artefact is not model content, so the round's narration belongs with
    the first REAL entry.

    Returns ``(parsed_tuple, tool_round_num)``.
    """
    tool_round_num, round_entry, event_payload = _build_tool_round_entry(
        display_name, {}, tc_id, '{}', tool_round_num, project_enabled,
        conv_id=task.get('convId') or task.get('id'))
    rn = round_entry['roundNum']
    round_entry['llmRound'] = round_num
    event_payload['llmRound'] = round_num
    round_entry['status'] = 'rejected'
    round_entry['_rejected'] = rejected_meta
    event_payload['status'] = 'rejected'
    event_payload['_rejected'] = rejected_meta
    task['toolRounds'].append(round_entry)
    append_event(task, event_payload)
    return ((tc, display_name, tc_id, {}, rn, round_entry, receipt_msg),
            tool_round_num)


def parse_tool_calls(
    assistant_msg: dict[str, Any],
    task: dict[str, Any],
    round_num: int,
    tool_round_num: int,
    project_enabled: bool,
    early_announced: dict[str, tuple] | None = None,
) -> tuple[list[tuple], int]:
    """Parse raw tool_calls from the assistant message into structured tuples.

    For each tool call, parses (or repairs) the JSON arguments, builds the
    display round-entry via ``_build_tool_round_entry``, appends search
    rounds to the task, and emits the corresponding SSE event.

    When ``early_announced`` is provided (from ``StreamingToolAccumulator``),
    tool calls that were already announced during streaming are NOT re-emitted.
    Their existing round entries (already in ``task['toolRounds']``) are
    reused, avoiding duplicate ``tool_start`` events on the frontend.

    Parameters
    ----------
    assistant_msg : dict
        The assistant message with a ``tool_calls`` list.
    task : dict
        Live task dict — mutated (``toolRounds`` appended, events emitted).
    round_num : int
        Zero-based loop iteration index (for logging).
    tool_round_num : int
        Current tool round counter (updated as tool rounds are created).
    project_enabled : bool
        Whether project-mode is active.
    early_announced : dict, optional
        Map of ``tc_id → (roundNum, round_entry)`` for tools already announced
        via ``StreamingToolAccumulator.on_tool_call_ready``.  These will reuse
        the existing round entry and skip SSE emission.

    Returns
    -------
    tuple[list, int]
        ``(parsed_tcs, tool_round_num)`` where ``parsed_tcs`` is a list of
        7-tuples: ``(tc, fn_name, tc_id, fn_args, rn, round_entry,
        _args_parse_error)``.
    """
    tid = task['id'][:8]
    parsed_tcs = []
    _early = early_announced or {}
    # ★ Capture per-round assistant content (text LLM emitted alongside tool calls)
    _assistant_content = (assistant_msg.get('content') or '').strip()
    _ac_tagged = False  # only tag the first entry per round
    # ★ Capture per-round reasoning/thinking text so Continue can replay it
    #   against APIs that accept thinking continuity (Claude extended-thinking).
    #   Currently sourced from OpenAI-compat `reasoning_content`; if an upstream
    #   proxy surfaces the block-level signature separately we can extend the
    #   key set below (`thinkingSignature`).
    _assistant_thinking = (assistant_msg.get('reasoning_content') or '').strip()
    _assistant_thinking_signature = assistant_msg.get('thinking_signature') or ''

    _total_tcs = len(assistant_msg['tool_calls'])
    # Live set of REAL tool names for this turn (built-ins + MCP + swarm +
    # memory + custom). Source of truth for both alias resolution (so an MCP
    # tool wins the exact check and is never aliased over) and hallucination
    # classification (an unknown name not in this set is a fake tool).
    _known = _known_tool_names(task)
    # Build set of function names that have non-empty arguments,
    # so we can identify phantom duplicates (same name, empty args).
    _names_with_real_args = set()
    for _tc in assistant_msg['tool_calls']:
        _fn = (_tc.get('function') or {})
        if (_fn.get('arguments', '') or '').strip():
            _names_with_real_args.add(_fn.get('name', ''))
    for tc in assistant_msg['tool_calls']:
        fn_obj = tc.get('function') or {}
        fn_name = fn_obj.get('name', '')
        # NOTE: the name-drop guards (missing / internal-artefact / malformed)
        # are NOT duplicated here anymore — ``ingest_tool_call``'s stage-1 drop
        # guard is the single classifier, and the dropped branch below re-emits
        # the per-reason WARNINGs verbatim for grep parity. (They used to be
        # three hand-copied ``continue`` guards that ALSO silently dropped the
        # call — the bug this module now fixes by conversion to a receipt.)
        # ── Unified tool-call ingestion ──
        # ONE seam does name-drop guard → name-alias (read_file→read_files,
        # WebFetch→fetch_url, …) → JSON decode+repair → schema/param repair →
        # hallucination reject. Shared verbatim with the swarm sub-agent and
        # timer-poll dispatch paths (lib/tool_input_repair.ingest_tool_call), so
        # a guard added here can never again skip those paths. ``_known`` (not
        # ``tool_registry``) is the membership oracle so MCP / swarm / memory /
        # custom tools are recognised — never aliased over nor mis-flagged.
        # The chat-specific PRESENTATION layered on the result below (UI
        # auto-fixed badge, phantom-empty-args skip, autopilot loop-break, raw-
        # args diagnostic log) stays here — it's not shared behaviour.
        _ingested = ingest_tool_call(
            tool_call=tc, known_tools=_known,
            model=task.get('model', '') or '',
            conv_id=task.get('convId', '') or '',
        )
        # Drop guard: streaming artefacts (antml:thinking, XML-corrupted
        # names, EMPTY names — e.g. the upstream HELLO_CHECK probe). Not
        # executed — but NOT silent either (pt_914bb730): a bare ``continue``
        # used to leave the model with an orphan that the wire-stripper then
        # erased, and the model INVENTED an explanation for the hole
        # ("tool-call limit reached" — a limit that does not exist) and
        # repeated it once per round. Every discard now leaves a rejected
        # round + a receipt the pipeline returns as a role:'tool' message.
        if _ingested.dropped:
            if _ingested.drop_reason == 'internal_artifact':
                logger.warning('[Task %s] Skipping spurious/internal tool call name: %s', tid, fn_name)
            elif _ingested.drop_reason == 'malformed':
                logger.warning('[Task %s] Skipping malformed tool name (non-alphanumeric): %.80s', tid, fn_name)
            else:
                logger.warning('[Task %s] Skipping tool call with missing function name: %s', tid, tc)
            tc_id = tc.get('id') or f'call_{uuid.uuid4().hex[:12]}'
            if not tc.get('id'):
                # The wire assistant message shares this dict — write the mint
                # back so the synthetic tool_result pairs with the tool_use
                # instead of becoming a second, differently-keyed orphan.
                tc['id'] = tc_id
            _drop_reason = _ingested.drop_reason or 'missing'
            if _drop_reason == 'internal_artifact':
                _why = (f'its function name {fn_name!r} is an internal/proxy '
                        'artefact (contains ":" or starts with "__"), not a '
                        'real tool')
            elif _drop_reason == 'malformed':
                _why = (f'its function name {fn_name!r} was corrupted in '
                        'transit (not alphanumeric — typically XML/HTML '
                        'fragments from a broken stream)')
            else:
                _why = ('its function name was EMPTY — a malformed streaming '
                        'artefact, not a call you actually made')
            _drop_msg = (
                '[SYSTEM: TOOL CALL DID NOT RUN]\n'
                f'A tool call in your previous message was discarded without '
                f'being executed: {_why} (tool_call id={tc_id}). No result '
                'exists for it. This is NOT a tool-call limit — this harness '
                'has no per-turn tool-call cap, so do not stop or ask the '
                'user to re-prompt on that assumption. If you intended to '
                'call a tool, re-issue it now with an explicit name from the '
                'available tool list.')
            _receipt, tool_round_num = _reject_undispatched(
                tc, fn_name or '(unnamed tool call)', tc_id, _drop_msg,
                {'kind': 'dropped_artifact', 'attempted': fn_name or '',
                 'suggestions': [], 'drop_reason': _drop_reason},
                task, tool_round_num, round_num, project_enabled)
            parsed_tcs.append(_receipt)
            continue
        _tool_name_aliased = _ingested.raw_name if _ingested.alias_kind else None
        if _ingested.alias_kind:
            logger.info('[Task %s] Aliased tool name %r → %r (%s)',
                        tid, _ingested.raw_name, _ingested.fn_name, _ingested.alias_kind)
        fn_name = _ingested.fn_name
        # Persist the canonical name onto the tool_call so replay/Continue
        # doesn't re-trigger the alias and the stored name matches the executed
        # tool.
        fn_obj['name'] = fn_name
        _hallucinated = _ingested.rejection
        if _hallucinated:
            logger.warning(
                '[Task %s] conv=%s Rejected hallucinated tool %r '
                '(suggestions=%s, repeat=%d)',
                tid, task.get('convId', '') or '', fn_name,
                _hallucinated.get('suggestions'), _ingested.repeat_count)

        # Guard against phantom tool calls: valid name but empty arguments,
        # AND another tool call with the SAME name has real arguments.
        # This avoids dropping legitimate no-arg tools
        # that appear alongside other tool calls.
        _raw_check = (fn_obj.get('arguments', '') or '').strip()
        if not _raw_check and fn_name in _names_with_real_args and not _hallucinated:
            logger.warning('[Task %s] Skipping phantom tool call %s (tc_id=%s) '
                           'with empty arguments — duplicate of another %s call '
                           'with real args',
                           tid, fn_name, tc.get('id', '?')[:12], fn_name)
            # Same no-silent-discard contract as the drop guard above: the
            # empty duplicate is rejected WITH a receipt, so the model never
            # has to guess why one of its two calls produced no result.
            tc_id = tc.get('id') or f'call_{uuid.uuid4().hex[:12]}'
            if not tc.get('id'):
                tc['id'] = tc_id
            _phantom_msg = (
                '[SYSTEM: TOOL CALL DID NOT RUN]\n'
                f'Your tool call for `{fn_name}` (tool_call id={tc_id}) had '
                f'EMPTY arguments and duplicated another `{fn_name}` call in '
                f'the same message that carried real arguments. The empty '
                'duplicate was discarded and never executed; the sibling '
                'call proceeds normally. Do not re-issue the empty call — '
                'this is NOT a tool-call limit.')
            _receipt, tool_round_num = _reject_undispatched(
                tc, fn_name, tc_id, _phantom_msg,
                {'kind': 'phantom_empty_args', 'attempted': fn_name,
                 'suggestions': [], 'drop_reason': 'phantom_empty_args'},
                task, tool_round_num, round_num, project_enabled)
            parsed_tcs.append(_receipt)
            continue
        tc_id = tc.get('id') or f'call_{uuid.uuid4().hex[:12]}'
        # Harness self-repair tracking — surfaced to the UI so the user knows
        # the displayed/executed args were auto-corrected from a malformed
        # model output.  ``_json_repaired`` = recovered truncated/invalid JSON;
        # ``_repair_log`` = schema-shape coercions (stringified_json, …).
        _json_repaired = _ingested.json_repaired
        _repair_log = _ingested.repair_log or None
        _args_parse_error = _ingested.parse_error
        fn_args = _ingested.fn_args

        # ── Autopilot loop breaker (chat-only presentation on the reject) ──
        # A no-suggestion phantom re-emitted under autopilot is a token-burning
        # loop the model can't escape (module_buffer_manager ×7). Abort gates on
        # HALLUCINATION_ABORT_THRESHOLD, DELIBERATELY HIGHER than the escalate
        # threshold: the model first gets ~2 rounds holding the injected real-
        # tool list to self-correct; abort is the true last resort. Only fires
        # for pure inventions (no nearby real tool to suggest).
        if _hallucinated:
            _repeat_n = _ingested.repeat_count
            if (_repeat_n >= HALLUCINATION_ABORT_THRESHOLD
                    and not (_hallucinated.get('suggestions') or [])):
                try:
                    from lib.tasks_pkg.autopilot import is_autopilot_enabled
                    if is_autopilot_enabled(task):
                        logger.warning(
                            '[Task %s] conv=%s Autopilot loop breaker: tool %r '
                            'invented %d× in a row — aborting task to stop the loop',
                            tid, task.get('convId', ''), fn_name, _repeat_n)
                        audit_log('hallucination_loop_break',
                                  tool=fn_name,
                                  repeat=_repeat_n,
                                  conv_id=task.get('convId', '') or '',
                                  task_id=task.get('id', '') or '',
                                  model=task.get('model', '') or '')
                        task['aborted'] = True
                        task['_abort_reason'] = 'hallucination_loop'
                except Exception as _e_brk:
                    logger.debug('[Task %s] autopilot loop-breaker check '
                                 'skipped: %s', tid, _e_brk)
        elif _repair_log:
            logger.debug(
                '[Task %s] tool=%s tc_id=%s: repaired %d arg(s) %s',
                tid, fn_name, tc_id[:12], len(_repair_log), _repair_log,
            )

        # ── Build a UI-facing repair summary (None when nothing was fixed) ──
        _repair_summary = _build_repair_summary(
            _json_repaired, _repair_log,
            tool_name_aliased=_tool_name_aliased, resolved_tool_name=fn_name,
        )

        # ── Check if this tool was already announced during streaming ──
        if tc_id in _early:
            rn, round_entry = _early[tc_id]
            # ★ Harness fixed this call's args AFTER the streaming early-
            #   announce already rendered the (garbled) display — patch the
            #   stale round entry so the UI shows the corrected line + badge.
            if _repair_summary:
                _apply_repair_to_round(round_entry, fn_name, fn_args, _repair_summary,
                                       project_enabled,
                                       task.get('convId') or task.get('id'))
            # ★ Hallucinated tool announced during streaming — mark the
            #   already-rendered round as rejected so the UI restyles it.
            if _hallucinated:
                round_entry['status'] = 'rejected'
                round_entry['_rejected'] = _hallucinated
            # ★ Attach per-round prose to the first early-announced entry.
            #   thinking/signature are captured INDEPENDENTLY of content: a
            #   reasoning model routinely emits thinking then calls a tool with
            #   NO interstitial prose (_assistant_content == ''). Gating the
            #   whole block on content dropped that round's thinking, which then
            #   vanished at finalize (assemble_segments reads round['thinking'],
            #   and committedMessage overwrites the live-stamped copy).
            if not _ac_tagged and (_assistant_content or _assistant_thinking
                                   or _assistant_thinking_signature):
                if _assistant_content:
                    round_entry['assistantContent'] = _assistant_content
                if _assistant_thinking:
                    round_entry['thinking'] = _assistant_thinking
                if _assistant_thinking_signature:
                    round_entry['thinkingSignature'] = _assistant_thinking_signature
                _ac_tagged = True
            # ★ Preserve Gemini thought_signature (and any other vendor-specific
            #   extra_content) so the frontend can round-trip it on Continue.
            #   Gemini 3.x REQUIRES echoing the signature back on subsequent
            #   requests that replay this tool_call, else HTTP 400.  See
            #   memory gemini-thought-signature-openai-compat.
            if tc.get('extra_content'):
                round_entry['extraContent'] = tc['extra_content']
            logger.debug('[Task %s] Reusing early-announced tool_start for '
                         '%s tc_id=%s rn=%d', tid, fn_name, tc_id[:8], rn)
            # Swarm tools need extra bookkeeping
            if fn_name in SWARM_TOOL_NAMES:
                task['_swarmRoundNum'] = rn
            parsed_tcs.append((tc, fn_name, tc_id, fn_args, rn, round_entry, _args_parse_error))
            continue

        # ── Serialize args for continue context ──
        tc_args_str = json.dumps(fn_args, ensure_ascii=False) if fn_args else '{}'

        # ── Build round entry + event via dispatch-dict helper ──
        tool_round_num, round_entry, event_payload = _build_tool_round_entry(
            fn_name, fn_args, tc_id, tc_args_str,
            tool_round_num, project_enabled,
            conv_id=task.get('convId') or task.get('id'),
        )
        rn = round_entry['roundNum']
        # ★ Tag with LLM round so frontend can batch tool calls from the
        #   same assistant turn — needed for accurate Continue grouping.
        round_entry['llmRound'] = round_num
        event_payload['llmRound'] = round_num
        # ★ Harness self-repair badge — tells the user this call's arguments
        #   were auto-corrected from a malformed model output.
        if _repair_summary:
            round_entry['_repaired'] = _repair_summary
            event_payload['_repaired'] = _repair_summary
        # ★ Unified hallucination state — the call was rejected, never run.
        #   The frontend renders `status:'rejected'` + `_rejected` distinctly
        #   (struck-through line + "not a real tool" badge + suggestions).
        if _hallucinated:
            round_entry['status'] = 'rejected'
            round_entry['_rejected'] = _hallucinated
            event_payload['status'] = 'rejected'
            event_payload['_rejected'] = _hallucinated
        # ★ Tag first entry with per-round prose so Continue can replay it and
        #   the settled segment timeline renders it adjacent to the tool.
        #   thinking/signature are captured INDEPENDENTLY of content — a
        #   thinking-only round (reasoning then a direct tool call, no
        #   interstitial prose) must still stamp its reasoning, or it is lost at
        #   finalize (assemble_segments reads round['thinking'] and the
        #   authoritative committedMessage overwrites the live-stamped copy).
        if not _ac_tagged and (_assistant_content or _assistant_thinking
                               or _assistant_thinking_signature):
            if _assistant_content:
                round_entry['assistantContent'] = _assistant_content
                event_payload['assistantContent'] = _assistant_content
            if _assistant_thinking:
                round_entry['thinking'] = _assistant_thinking
                event_payload['thinking'] = _assistant_thinking
            if _assistant_thinking_signature:
                round_entry['thinkingSignature'] = _assistant_thinking_signature
                event_payload['thinkingSignature'] = _assistant_thinking_signature
            _ac_tagged = True
        # ★ Preserve Gemini thought_signature on the persisted tool round.
        #   Captured off the assistant tool_call entry by lib.llm's
        #   streaming parser (see: "Gemini thought_signature: preserve
        #   extra_content" branch in lib/llm/stream.py).  Without this,
        #   a Continue request against Gemini drops the signature and the
        #   next API call 400s.
        if tc.get('extra_content'):
            round_entry['extraContent'] = tc['extra_content']
            event_payload['extraContent'] = tc['extra_content']
        task['toolRounds'].append(round_entry)
        append_event(task, event_payload)

        # Swarm tools need extra bookkeeping for sub-agent event routing
        if fn_name in SWARM_TOOL_NAMES:
            task['_swarmRoundNum'] = rn

        parsed_tcs.append((tc, fn_name, tc_id, fn_args, rn, round_entry, _args_parse_error))

    return parsed_tcs, tool_round_num
