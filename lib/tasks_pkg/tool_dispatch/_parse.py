# HOT_PATH
"""Tool-call parsing — parse (or repair) raw ``tool_calls`` into structured tuples.

The single public entry-point is :func:`parse_tool_calls`, extracted from the
inner loop of ``orchestrator.run_task``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from lib.llm_sanitize import _UNNAMED_TOOL_NAME
from lib.log import audit_log, get_logger
from lib.tasks_pkg.executor import SWARM_TOOL_NAMES
from lib.tasks_pkg.manager import append_event
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tool_caller_identity import normalize_tool_caller
from lib.tool_input_repair import HALLUCINATION_ABORT_THRESHOLD, ingest_tool_call
from lib.tool_rejection import stamp_tool_rejection

from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names
from lib.tasks_pkg.tool_dispatch._repair import _apply_repair_to_round, _build_repair_summary

logger = get_logger(__name__)


def _stamp_presentation_parent(target: dict[str, Any],
                               tool_call: dict[str, Any]) -> None:
    """Attach a non-authority parent edge for nested/recovery presentation."""
    parent_id = str(tool_call.get('_presentationParentToolCallId') or '')
    caller = tool_call.get('caller')
    if (not parent_id and isinstance(caller, dict)
            and caller.get('type') == 'program'):
        parent_id = str(caller.get('caller_id') or '')
    if parent_id:
        target['parentToolCallId'] = parent_id


def _invalid_caller_rejection(
    tool_call: dict[str, Any], tool_name: str,
) -> tuple[str, dict[str, Any]] | None:
    """Reject attributed calls whose authority envelope is not trustworthy."""
    if 'caller' not in tool_call or tool_call.get('caller') is None:
        return None
    normalized_caller, reason = normalize_tool_caller(
        tool_call.get('caller'), require_program_identity=False)
    if reason is None:
        if normalized_caller is not None:
            tool_call['caller'] = normalized_caller
        # Parent identity/budget validation belongs to the programmatic
        # execution boundary below.
        return None
    message = (
        '[SYSTEM: ATTRIBUTED TOOL CALL DID NOT RUN]\n'
        f'Tool call `{tool_name}` carried an invalid caller authority '
        f'envelope ({reason}). It was rejected instead of being promoted to '
        'a direct root call. Re-issue it with valid provider attribution.')
    return message, {
        'kind': 'invalid_tool_caller',
        'attempted': tool_name,
        'reason': reason,
        'retryable': True,
    }


def _reject_undispatched(tc, display_name, tc_id, receipt_msg, rejected_meta,
                         task, tool_round_num, round_num, project_enabled,
                         early_entry=None):
    """Give an UNDELIVERABLE tool call a rejected round + a model-facing receipt.

    Rides the exact lane hallucinated tools already use: a ``status='rejected'``
    round the UI renders + a ``parse_error`` the pipeline returns to the model
    as a ``role:'tool'`` message in original tool-call order. The alternative —
    the old ``continue`` — left the model with an unexplained hole that the
    orphan-stripper then erased from the wire, and it INVENTED an explanation
    (``tool-call limit reached`` spam, ).

    Deliberately does NOT consume the round's prose tag (``_ac_tagged``): a
    junk artefact is not model content, so the round's narration belongs with
    the first REAL entry.

    Returns ``(parsed_tuple, tool_round_num)``.
    """
    if early_entry is not None:
        rn, round_entry = early_entry
        # The streaming callback may have announced a malformed/raw name.
        # Canonicalize the durable row; its already-emitted start frame is only
        # provisional and the terminal rejection event carries these facts.
        round_entry['toolName'] = display_name
        round_entry['toolCallId'] = tc_id
        round_entry['toolArgs'] = '{}'
        event_payload = None
    else:
        tool_round_num, round_entry, event_payload = _build_tool_round_entry(
            display_name, {}, tc_id, '{}', tool_round_num, project_enabled,
            conv_id=task.get('convId') or task.get('id'), task=task)
        rn = round_entry['roundNum']
        round_entry['llmRound'] = round_num
        event_payload['llmRound'] = round_num
    round_entry['status'] = 'rejected'
    rejection = stamp_tool_rejection(
        round_entry, rejected_meta, tool_name=display_name,
        reason=receipt_msg,
    )
    _source = str(tc.get('source') or 'native_direct')
    round_entry['source'] = _source
    if isinstance(tc.get('caller'), dict):
        round_entry['caller'] = dict(tc['caller'])
        if tc['caller'].get('type') == 'program' and tc['caller'].get('caller_id'):
            round_entry['_programCallId'] = tc['caller']['caller_id']
    _stamp_presentation_parent(round_entry, tc)
    if event_payload is not None:
        event_payload['status'] = 'rejected'
        stamp_tool_rejection(event_payload, rejection)
        event_payload['source'] = _source
        if isinstance(tc.get('caller'), dict):
            event_payload['caller'] = dict(tc['caller'])
            if (tc['caller'].get('type') == 'program'
                    and tc['caller'].get('caller_id')):
                event_payload['programCallId'] = tc['caller']['caller_id']
        _stamp_presentation_parent(event_payload, tc)
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
    raw_tool_calls = assistant_msg.get('tool_calls')
    if isinstance(raw_tool_calls, list):
        tool_calls = raw_tool_calls
    elif isinstance(raw_tool_calls, tuple):
        tool_calls = list(raw_tool_calls)
    elif raw_tool_calls is None:
        tool_calls = []
    else:
        # A compatibility provider occasionally returns one call object
        # instead of the required array. Preserve that recoverable occurrence;
        # other scalar shapes become one independently rejected receipt.
        tool_calls = [raw_tool_calls]
    assistant_msg['tool_calls'] = tool_calls
    parsed_tcs = []
    _early = early_announced or {}
    _early_claimed: set[str] = set()

    def _take_early(tc_id: str):
        """Claim at most one streamed row for each provider call id.

        Duplicate ids in one assistant response are reminted later by the
        dispatch pipeline. Reusing one mutable row for both tuples lets the
        remint of the second call silently rewrite the first call's identity.
        """
        if tc_id in _early_claimed:
            return None
        candidate = _early.get(tc_id)
        if not (isinstance(candidate, tuple) and len(candidate) == 2):
            return None
        _early_claimed.add(tc_id)
        return candidate
    # Capture per-round assistant content (text LLM emitted alongside tool calls)
    raw_assistant_content = assistant_msg.get('content')
    _assistant_content = (
        raw_assistant_content.strip()
        if isinstance(raw_assistant_content, str) else ''
    )
    _ac_tagged = False  # only tag the first entry per round
    # Capture per-round reasoning/thinking text so Continue can replay it
    #   against APIs that accept thinking continuity (Claude extended-thinking).
    #   Currently sourced from OpenAI-compat `reasoning_content`; if an upstream
    #   proxy surfaces the block-level signature separately we can extend the
    #   key set below (`thinkingSignature`).
    raw_assistant_thinking = assistant_msg.get('reasoning_content')
    _assistant_thinking = (
        raw_assistant_thinking.strip()
        if isinstance(raw_assistant_thinking, str) else ''
    )
    raw_thinking_signature = assistant_msg.get('thinking_signature')
    _assistant_thinking_signature = (
        raw_thinking_signature if isinstance(raw_thinking_signature, str)
        else ''
    )
    raw_responses_items = assistant_msg.get('_responses_items')
    _assistant_responses_items = (
        raw_responses_items if isinstance(raw_responses_items, list) else []
    )
    raw_anthropic_blocks = assistant_msg.get('_anthropic_content_blocks')
    _assistant_anthropic_blocks = (
        raw_anthropic_blocks if isinstance(raw_anthropic_blocks, list) else []
    )

    _total_tcs = len(tool_calls)
    # Live set of REAL tool names for this turn (built-ins + MCP + swarm +
    # memory + custom). Source of truth for both alias resolution (so an MCP
    # tool wins the exact check and is never aliased over) and hallucination
    # classification (an unknown name not in this set is a fake tool).
    _known = _known_tool_names(task)
    for tool_call_index, raw_tc in enumerate(tool_calls):
        if not isinstance(raw_tc, dict):
            tc_id = f'call_{uuid.uuid4().hex[:12]}'
            tc = {
                'id': tc_id,
                'type': 'function',
                'function': {
                    'name': _UNNAMED_TOOL_NAME,
                    'arguments': '{}',
                },
            }
            tool_calls[tool_call_index] = tc
            _shape = type(raw_tc).__name__
            _malformed_msg = (
                '[SYSTEM: TOOL CALL DID NOT RUN]\n'
                f'The provider returned tool_calls[{tool_call_index}] as '
                f'{_shape}, but each entry must be an object. This malformed '
                'occurrence was isolated and not executed; any valid sibling '
                'calls continue normally.')
            _receipt, tool_round_num = _reject_undispatched(
                tc, '(malformed tool call)', tc_id, _malformed_msg,
                {'kind': 'malformed_tool_call_shape', 'attempted': '',
                 'suggestions': [], 'drop_reason': 'malformed_shape'},
                task, tool_round_num, round_num, project_enabled)
            parsed_tcs.append(_receipt)
            continue

        tc = raw_tc
        raw_tc_id = tc.get('id')
        tc_id = str(raw_tc_id or '').strip()[:200]
        if not tc_id:
            tc_id = f'call_{uuid.uuid4().hex[:12]}'
        if raw_tc_id != tc_id:
            tc['id'] = tc_id

        raw_fn_obj = tc.get('function')
        if raw_fn_obj is not None and not isinstance(raw_fn_obj, dict):
            _shape = type(raw_fn_obj).__name__
            tc['function'] = {
                'name': _UNNAMED_TOOL_NAME,
                'arguments': '{}',
            }
            _malformed_msg = (
                '[SYSTEM: TOOL CALL DID NOT RUN]\n'
                f'Tool call {tc_id} had a `{_shape}` function field; an '
                'object with name and arguments is required. This occurrence '
                'was isolated and not executed.')
            _receipt, tool_round_num = _reject_undispatched(
                tc, '(malformed tool call)', tc_id, _malformed_msg,
                {'kind': 'malformed_tool_call_shape', 'attempted': '',
                 'suggestions': [], 'drop_reason': 'malformed_shape'},
                task, tool_round_num, round_num, project_enabled,
                early_entry=_take_early(tc_id))
            parsed_tcs.append(_receipt)
            continue

        fn_obj = raw_fn_obj or {}
        tc['function'] = fn_obj
        raw_fn_name = fn_obj.get('name')
        if raw_fn_name is not None and not isinstance(raw_fn_name, str):
            _shape = type(raw_fn_name).__name__
            fn_obj['name'] = _UNNAMED_TOOL_NAME
            fn_obj['arguments'] = '{}'
            tc['function'] = fn_obj
            _malformed_msg = (
                '[SYSTEM: TOOL CALL DID NOT RUN]\n'
                f'Tool call {tc_id} had a `{_shape}` function name; a string '
                'is required. This occurrence was isolated and not executed.')
            _receipt, tool_round_num = _reject_undispatched(
                tc, '(malformed tool call)', tc_id, _malformed_msg,
                {'kind': 'malformed_tool_call_shape', 'attempted': '',
                 'suggestions': [], 'drop_reason': 'malformed_shape'},
                task, tool_round_num, round_num, project_enabled,
                early_entry=_take_early(tc_id))
            parsed_tcs.append(_receipt)
            continue

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
        # auto-fixed badge, autopilot loop-break, raw-
        # args diagnostic log) stays here — it's not shared behaviour.
        _ingested = ingest_tool_call(
            tool_call=tc, known_tools=_known,
            model=task.get('model', '') or '',
            conv_id=task.get('convId', '') or '',
            contract_documents_by_name=(
                task.get('_toolContractDocumentsByName')
                if '_toolContractDocumentsByName' in task else None),
        )
        # Drop guard: streaming artefacts (antml:thinking, XML-corrupted
        # names, EMPTY names — e.g. the upstream HELLO_CHECK probe). Not
        # executed — but NOT silent either (): a bare ``continue``
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
            # Same write-back for an EMPTY name: this wire dict is replayed
            # verbatim on the next round, and strict vendors hard-400 the
            # WHOLE request on name='' (Kimi "tokenization failed" —
            # live-probed 2026-08-07, task 9a8196f3 R4). The receipt below
            # still tells the model the call never ran; the placeholder only
            # keeps the replayed wire protocol-valid. The build_body
            # chokepoint (_fix_tool_call_wire_shape) heals every OTHER
            # producer — this is the source fix.
            _fn_wire = tc.get('function')
            if isinstance(_fn_wire, dict) and not (_fn_wire.get('name') or ''):
                _fn_wire['name'] = _UNNAMED_TOOL_NAME
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
                task, tool_round_num, round_num, project_enabled,
                early_entry=_take_early(tc_id))
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
        _contract_error = _ingested.contract_error
        if _hallucinated:
            logger.warning(
                '[Task %s] conv=%s Rejected hallucinated tool %r '
                '(suggestions=%s, repeat=%d)',
                tid, task.get('convId', '') or '', fn_name,
                _hallucinated.get('suggestions'), _ingested.repeat_count)

        tc_id = tc.get('id') or f'call_{uuid.uuid4().hex[:12]}'
        if not tc.get('id'):
            tc['id'] = tc_id
        # Harness self-repair tracking — surfaced to the UI so the user knows
        # the displayed/executed args were auto-corrected from a malformed
        # model output.  ``_json_repaired`` = recovered truncated/invalid JSON;
        # ``_repair_log`` = schema-shape coercions (stringified_json, …).
        _json_repaired = _ingested.json_repaired
        _repair_log = _ingested.repair_log or None
        _args_parse_error = _ingested.parse_error
        fn_args = _ingested.fn_args
        # Every pre-dispatch refusal receives one typed descriptor.  Name
        # hallucinations already carry their classifier output; contract and
        # decode failures used to expose only ``status='rejected'``, forcing
        # downstream consumers to guess why the tool did not run.
        _rejection_meta = _hallucinated
        if _rejection_meta is None and _contract_error:
            _rejection_meta = {
                'kind': 'tool_contract_invalid',
                'tool': fn_name,
                'code': _contract_error.get('code') or '',
                'path': _contract_error.get('path') or '',
                'reason': _args_parse_error or '',
                'retryable': bool(_contract_error.get('retryable')),
            }
        elif _rejection_meta is None and _args_parse_error:
            _rejection_meta = {
                'kind': 'invalid_tool_arguments',
                'tool': fn_name,
                'reason': _args_parse_error,
                'retryable': True,
            }

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

        # Caller attribution is an authority boundary. An invalid/unknown
        # envelope must not disappear and silently turn a program/worker call
        # into a direct root invocation.
        _caller_rejection = _invalid_caller_rejection(tc, fn_name)
        if _caller_rejection:
            _caller_msg, _caller_meta = _caller_rejection
            _early_entry = _take_early(tc_id)
            if _early_entry is not None:
                rn, round_entry = _early_entry
                round_entry['status'] = 'rejected'
                stamp_tool_rejection(
                    round_entry, _caller_meta, tool_name=fn_name,
                    reason=_caller_msg,
                )
                parsed_tcs.append((
                    tc, fn_name, tc_id, {}, rn, round_entry, _caller_msg))
            else:
                _receipt, tool_round_num = _reject_undispatched(
                    tc, fn_name, tc_id, _caller_msg, _caller_meta,
                    task, tool_round_num, round_num, project_enabled)
                parsed_tcs.append(_receipt)
            continue

        # Hosted PTC schemas are an upstream routing hint, not an application
        # authorization boundary. Enforce Tofu's explicit allow-list and hard
        # per-program call ceiling before any early-cache reuse or execution.
        from lib.tasks_pkg.orchestrator._programmatic import (
            reject_programmatic_call,
        )
        _program_rejection = reject_programmatic_call(task, tc, fn_name)
        _early_entry = _take_early(tc_id)
        if _program_rejection:
            _program_msg, _program_meta = _program_rejection
            if _early_entry is not None:
                rn, round_entry = _early_entry
                round_entry['status'] = 'rejected'
                stamp_tool_rejection(
                    round_entry, _program_meta, tool_name=fn_name,
                    reason=_program_msg,
                )
                parsed_tcs.append((
                    tc, fn_name, tc_id, {}, rn, round_entry, _program_msg))
            else:
                _receipt, tool_round_num = _reject_undispatched(
                    tc, fn_name, tc_id, _program_msg, _program_meta,
                    task, tool_round_num, round_num, project_enabled)
                parsed_tcs.append(_receipt)
            continue

        # ── Check if this tool was already announced during streaming ──
        if _early_entry is not None:
            rn, round_entry = _early_entry
            round_entry['source'] = str(
                tc.get('source') or round_entry.get('source')
                or 'native_direct')
            if isinstance(tc.get('caller'), dict):
                round_entry['caller'] = dict(tc['caller'])
                if (tc['caller'].get('type') == 'program'
                        and tc['caller'].get('caller_id')):
                    round_entry['_programCallId'] = tc['caller']['caller_id']
            _stamp_presentation_parent(round_entry, tc)
            # Harness fixed this call's args AFTER the streaming early-
            #   announce already rendered the (garbled) display — patch the
            #   stale round entry so the UI shows the corrected line + badge.
            if _repair_summary:
                _patched = _apply_repair_to_round(
                    round_entry, fn_name, fn_args, _repair_summary,
                    project_enabled,
                    task.get('convId') or task.get('id'), task=task)
                if _patched is not None:
                    # The garbled early-announce line is ALREADY on the user's
                    # screen; the settle frame would eventually refresh it, but
                    # a long-running command would show e.g. '$ ?' for its
                    # whole duration (2026-08-06: the gateway cut the stream
                    # mid-arguments, the announce rendered '?'). Push the
                    # corrected display over the live lane NOW. tool_progress
                    # never settles a round (reducer discipline), so this
                    # cannot flip the spinner early.
                    from lib.agent_core.events import EventType, emit as _emit_ev
                    _emit_ev(task, EventType.TOOL_PROGRESS,
                             roundNum=rn, toolCallId=tc_id, toolName=fn_name,
                             query=_patched, _repaired=_repair_summary)
            # A refused tool announced during streaming is restyled by the
            # typed descriptor on its later terminal event.
            if _rejection_meta:
                round_entry['status'] = 'rejected'
                stamp_tool_rejection(
                    round_entry, _rejection_meta, tool_name=fn_name,
                    reason=_args_parse_error or '',
                )
            if _contract_error:
                round_entry['status'] = 'rejected'
                round_entry['_contractError'] = _contract_error
            # Attach per-round prose to the first early-announced entry.
            #   thinking/signature are captured INDEPENDENTLY of content: a
            #   reasoning model routinely emits thinking then calls a tool with
            #   NO interstitial prose (_assistant_content == ''). Gating the
            #   whole block on content dropped that round's thinking, which then
            #   vanished from the settled turn (assemble_segments reads
            #   round['thinking'] when producing its projection).
            if not _ac_tagged and (_assistant_content or _assistant_thinking
                                   or _assistant_thinking_signature
                                   or _assistant_responses_items
                                   or _assistant_anthropic_blocks):
                if _assistant_content:
                    round_entry['assistantContent'] = _assistant_content
                if _assistant_thinking:
                    round_entry['thinking'] = _assistant_thinking
                if _assistant_thinking_signature:
                    round_entry['thinkingSignature'] = _assistant_thinking_signature
                if _assistant_responses_items:
                    round_entry['_responsesItems'] = _assistant_responses_items
                if _assistant_anthropic_blocks:
                    round_entry['_anthropicContentBlocks'] = (
                        _assistant_anthropic_blocks)
                _ac_tagged = True
            # Preserve Gemini thought_signature (and any other vendor-specific
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
            conv_id=task.get('convId') or task.get('id'), task=task,
        )
        rn = round_entry['roundNum']
        # Tag with LLM round so frontend can batch tool calls from the
        #   same assistant turn — needed for accurate Continue grouping.
        round_entry['llmRound'] = round_num
        event_payload['llmRound'] = round_num
        _source = str(tc.get('source') or 'native_direct')
        round_entry['source'] = _source
        event_payload['source'] = _source
        if isinstance(tc.get('caller'), dict):
            round_entry['caller'] = dict(tc['caller'])
            event_payload['caller'] = dict(tc['caller'])
            if (tc['caller'].get('type') == 'program'
                    and tc['caller'].get('caller_id')):
                round_entry['_programCallId'] = tc['caller']['caller_id']
                event_payload['programCallId'] = tc['caller']['caller_id']
        _stamp_presentation_parent(round_entry, tc)
        _stamp_presentation_parent(event_payload, tc)
        # Harness self-repair badge — tells the user this call's arguments
        #   were auto-corrected from a malformed model output.
        if _repair_summary:
            round_entry['_repaired'] = _repair_summary
            event_payload['_repaired'] = _repair_summary
        # Unified rejection state — kind tells consumers whether this was a
        # missing tool, invalid arguments, a contract refusal, or another
        # pre-execution block.  ``_rejected`` remains a compatibility alias.
        if _rejection_meta:
            round_entry['status'] = 'rejected'
            event_payload['status'] = 'rejected'
            rejection = stamp_tool_rejection(
                round_entry, _rejection_meta, tool_name=fn_name,
                reason=_args_parse_error or '',
            )
            stamp_tool_rejection(event_payload, rejection)
        if _contract_error:
            round_entry['status'] = 'rejected'
            round_entry['_contractError'] = _contract_error
            event_payload['status'] = 'rejected'
            event_payload['_contractError'] = _contract_error
        # Tag first entry with per-round prose so Continue can replay it and
        #   the settled segment timeline renders it adjacent to the tool.
        #   thinking/signature are captured INDEPENDENTLY of content — a
        #   thinking-only round (reasoning then a direct tool call, no
        #   interstitial prose) must still stamp its reasoning, or it is lost at
        #   finalization (assemble_segments reads round['thinking'] when
        #   producing the authoritative turn projection).
        if not _ac_tagged and (_assistant_content or _assistant_thinking
                               or _assistant_thinking_signature
                               or _assistant_responses_items
                               or _assistant_anthropic_blocks):
            if _assistant_content:
                round_entry['assistantContent'] = _assistant_content
                event_payload['assistantContent'] = _assistant_content
            if _assistant_thinking:
                round_entry['thinking'] = _assistant_thinking
                event_payload['thinking'] = _assistant_thinking
            if _assistant_thinking_signature:
                round_entry['thinkingSignature'] = _assistant_thinking_signature
                event_payload['thinkingSignature'] = _assistant_thinking_signature
            if _assistant_responses_items:
                # Opaque encrypted/server-compaction state is persisted for
                # stateless Responses replay, but deliberately omitted from
                # the live UI event payload.
                round_entry['_responsesItems'] = _assistant_responses_items
            if _assistant_anthropic_blocks:
                round_entry['_anthropicContentBlocks'] = (
                    _assistant_anthropic_blocks)
            _ac_tagged = True
        # Preserve Gemini thought_signature on the persisted tool round.
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
