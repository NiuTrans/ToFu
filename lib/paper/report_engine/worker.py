"""Execute one paper-report task from dispatch through durable publication.

Responsibility: own the report agent loop, live event projection, canonical
body selection, artifact persistence, and optional post-report hooks.
Entry point: :func:`run_report_task`.
Dependencies: ``report_runtime`` owns lifecycle state, ``tools`` owns tool
execution, and sibling ``_meta``/``_hooks`` modules own bounded post-processing.
"""

import json
import re
import time
import uuid

import lib as _lib
from lib.agent_loop import AbortSignal, run_agent_loop
from lib.llm_dispatch.api import dispatch_stream
from lib.llm_errors import AbortedError
from lib.log import get_logger
from lib.tasks_pkg.tool_display import tool_round_label as display_query_for
from lib.tool_input_repair import parse_and_repair_tool_args

from lib.paper.images.injection import inject_images_into_report
from lib.paper.images.titles import (
    backfill_library_title,
    extract_title_from_report,
    is_placeholder_title,
    lookup_paper_title,
)
from lib.paper.artifact_repository import PaperArtifactRepository, PaperReport
from lib.paper.report_engine._body import resolve_canonical_report_body
from lib.paper.review._lang import (
    is_rebuttal_lang,
    is_review_family,
    is_review_lang,
)
from lib.paper.review._textproc import (
    finalize_rebuttal_body,
    finalize_review_body,
    parse_rebuttal_decision,
    smarten_quotes,
    strip_slop_dashes,
)
from lib.paper.report_runtime import (
    _append_report_event,
    _cleanup_stale_report_tasks,
    _report_runtime,
)
from lib.paper.tools import (
    PaperToolResultBudgetV2,
    execute_paper_tool,
    apply_paper_tool_epoch_guidance,
    build_paper_full_tool_epoch,
    make_paper_exec_shim,
    paper_effective_tool_name,
)

from lib.paper.report_engine._meta import _build_report_meta
from lib.paper.report_engine._hooks import (
    _maybe_run_insight,
    _maybe_run_termfill,
)

logger = get_logger(__name__)


def run_report_task(task, messages, images):
    """Background worker: runs the tool loop and populates task events.

    Event schema (mirrors chat stream for frontend reuse):
      - {type: 'tool_start', roundNum, toolName, query, toolCallId, toolArgs}
      - {type: 'tool_done',  roundNum, toolName, toolContent (truncated preview), elapsed}
      - {type: 'thinking',   delta}
      - {type: 'delta',      delta}
      - {type: 'enriched',   text}             — post-stream image injection
      - {type: 'done',       report, paperHash}
      - {type: 'error',      error}

    thinking / delta / delta_reset / tool_start / tool_done all carry
    ``llmRound`` (the 0-based dispatch round that produced them) so the
    frontend can fold the ordered event stream into a chat-shaped segment
    timeline (thinking adjacent to the tool calls of the same round).
    """
    task_id = task['task_id']
    _report_runtime.mark_running(task_id)
    _append_report_event(task, {'type': 'status', 'status': 'running'})

    phash = task['paper_hash']
    lang = task['lang']
    # Real UI language for image-injection / appendix headings. For ordinary
    # reports this equals `lang` ('en'/'zh'). For Review Mode, `lang` is a
    # composite cache key (``review:<venue>:<uilang>``), so the route stamps
    # the decoded UI language on the task; fall back to `lang` when absent.
    inj_lang = task.get('ui_lang') or lang
    # Review Mode (composite lang key ``review:<venue>:<uilang>``) is TEXT-ONLY:
    # a peer review is a decision document, not an illustrated explainer, so no
    # figures are injected AND any image the model emitted itself is stripped.
    _is_review = is_review_lang(lang)
    _is_rebuttal = is_rebuttal_lang(lang)
    # Both a review and a rebuttal reply are text-only decision documents.
    _is_review_family = is_review_family(lang)
    inj_appendix = not _is_review_family
    inj_allow_images = not _is_review_family
    model = task['model']
    abort_event = task['abort_event']

    def _abort_check():
        return abort_event.is_set()

    model_name = model or _lib.LLM_MODEL
    t0 = time.time()
    full_content = ''
    # ── Finish-tag accumulators ──
    # Sum token usage across every dispatch round (tool rounds + final write)
    # so the badge reflects the TOTAL cost of producing the report, not just
    # the last call. resolved_model / provider_id are filled from the
    # dispatcher metadata stamped on `usage['_dispatch']`.
    _usage_total = {
        'prompt_tokens': 0, 'completion_tokens': 0,
        'cache_read_tokens': 0, 'cache_write_tokens': 0,
        'reasoning_tokens': 0,
    }
    _resolved_model = ''
    _provider_id = None
    _round_count = 0
    # Extract a short context string for search relevance filtering
    _user_msg = messages[1]['content'] if len(messages) > 1 else ''
    _report_user_question = _user_msg[:300] if _user_msg else ''
    aborted = False

    abort_signal = AbortSignal.from_event(abort_event)

    # Shim for the SHARED dispatch (full tool set): one per run, reused across
    # rounds so per-run state (e.g. the todo checklist) survives between calls.
    paper_epoch = build_paper_full_tool_epoch(
        owner_user_id=task.get('_userId'), model=model_name,
        cfg=task.get('config'))
    task['toolEpochV2'] = paper_epoch.telemetry()
    paper_tools = list(paper_epoch.wire_schemas)
    apply_paper_tool_epoch_guidance(
        messages, paper_epoch, lang=task.get('lang') or 'en')
    _exec_shim = make_paper_exec_shim(task_id=task['task_id'],
                                      abort=abort_signal.is_set,
                                      owner_user_id=task.get('_userId'),
                                      cfg=task.get('config'),
                                      tool_epoch=paper_epoch,
                                      model=model_name)
    _result_budget = PaperToolResultBudgetV2(
        owner_user_id=task.get('_userId'), model=model_name,
        result_envelope=paper_epoch.result_envelope,
        conv_id=task['task_id'])
    task['toolResultPolicyV1'] = _result_budget.telemetry()

    # Per-round content buffer. Tool-calling models often emit a full interim
    # DRAFT of the report in a round that ALSO issues a tool call, then rewrite
    # the whole report from scratch in the final (no-tool-call) round.
    # Accumulating across rounds would bake the draft + final copy into one
    # document — the report rendered TWICE. So we buffer each round separately
    # (reset in _dispatch) and discard a tool-round's draft in _begin_tool_round.
    # Held on a mutable holder so both closures share the same per-round buffer.
    _round = {'content': ''}
    # Authoritative body source. The streamed deltas (``full_content``) are
    # DISPLAY-ONLY: a mid-stream transient retry in stream_chat re-streams the
    # WHOLE response through on_content, doubling the deltas. The dispatcher's
    # RETURNED ``msg['content']`` of the terminal (no-tool) round is a single
    # clean copy regardless of how the deltas arrived, so we persist/enrich from
    # THAT — a re-emit can never double the written document. Captured in
    # _accumulate_usage (fires with msg every round); the last no-tool round is
    # always the terminal one (a no-tool round ends the loop).
    _terminal = {'content': None}

    def _dispatch(rnd, tools):
        _round['content'] = ''

        def _on_content(text):
            nonlocal full_content
            _round['content'] += text
            full_content += text
            task['full_text'] = full_content
            _append_report_event(task, {'type': 'delta', 'delta': text,
                                        'llmRound': rnd})

        def _on_thinking(text):
            _append_report_event(task, {'type': 'thinking', 'delta': text,
                                        'llmRound': rnd})

        logger.info('[Paper:Report] Task %s round %d — model=%s msgs=%d',
                    task['task_id'], rnd + 1, model_name, len(messages))
        # max_tokens: pass a very large ceiling so the report can run to
        #   completion without artificial truncation.  dispatch_stream →
        #   build_body → _clamp_max_tokens() automatically reduces this to each
        #   model's native API limit (GPT=32k, Claude=128k, Qwen per-model
        #   16–64k, etc.), so we get "as much as the model allows" without a cap.
        from lib.llm.stream_result import ensure_provider_stream_result
        return ensure_provider_stream_result(dispatch_stream(
            messages,
            on_content=_on_content,
            on_thinking=_on_thinking,
            abort_check=_abort_check,
            prefer_model=model_name if model else None,
            strict_model=bool(model),
            tools=tools,
            max_tokens=128000,
            temperature=0,
            thinking_enabled=False,
            log_prefix='[Paper:Report]',
        ))

    def _accumulate_usage(rnd, msg, finish, usage):
        # Accumulate token usage + capture the resolved model/provider (the
        # dispatcher may fall back to a different model than asked).
        nonlocal _round_count, _resolved_model, _provider_id
        _round_count += 1
        # Capture the CLEAN returned content of a no-tool (terminal) round as
        # the authoritative body — immune to on_content re-streaming (retries).
        # A round with tool_calls is not terminal; its prose is an interim draft
        # handled by _begin_tool_round, so we only record no-tool rounds.
        if isinstance(msg, dict) and not msg.get('tool_calls'):
            _terminal['content'] = msg.get('content') or ''
        if isinstance(usage, dict):
            from lib.cost import normalize_usage as _normalize_usage
            _nu = _normalize_usage(usage)
            _usage_total['prompt_tokens'] += _nu['input']
            _usage_total['completion_tokens'] += _nu['output']
            _usage_total['cache_read_tokens'] += _nu['cache_read']
            _usage_total['cache_write_tokens'] += _nu['cache_write']
            _usage_total['reasoning_tokens'] += _nu['thinking']
            _disp = usage.get('_dispatch') or {}
            if _disp.get('model'):
                _resolved_model = _disp['model']
            if _disp.get('provider_id'):
                _provider_id = _disp['provider_id']

    def _begin_tool_round(rnd, msg):
        # This round ended with tool calls, so any prose it emitted was a
        # premature interim draft (the model will rewrite the full report after
        # seeing the tool results). Discard that draft from the canonical body
        # and tell pollers/the live stream to reset their accumulated text,
        # otherwise the draft + the final report concatenate and render twice.
        nonlocal full_content
        round_content = _round['content']
        if round_content:
            logger.info('[Paper:Report] Task %s — discarding %d-char interim draft '
                        'emitted alongside tool calls (round %d)',
                        task['task_id'], len(round_content), rnd + 1)
            full_content = full_content[:-len(round_content)]
            task['full_text'] = full_content
            _append_report_event(task, {'type': 'delta_reset', 'llmRound': rnd})
        messages.append(msg)

    def _execute_tool(rnd, tc):
        # Emit chat-compatible tool_start / tool_done events + append result.
        fn_name = tc['function']['name']
        fn_args_raw = tc['function']['arguments']
        tc_id = tc.get('id', '')

        # Parse + schema-repair args ONCE (shared with the executor), so the
        # display label and the actual search see the SAME normalized shape — a
        # bare-string `queries`/`urls` is coerced to a single-element array,
        # never iterated per-character.
        fn_args, _ = parse_and_repair_tool_args(fn_name, fn_args_raw)

        # Build chat-style round entry (subset of what
        # lib.tasks_pkg.tool_display produces — for paper report we only have
        # web_search / fetch_url).
        task['round_counter'] += 1
        rn = task['round_counter']

        display_query = display_query_for(fn_name, fn_args)
        # The dispatch/display name: run_command → code_exec in a project-less
        # engine (chat's tool_display override, mirrored by the adapter).
        effective_name = paper_effective_tool_name(fn_name)

        round_entry = {
            'roundNum': rn,
            'llmRound': rnd,
            'toolName': effective_name,
            'query': display_query,
            'toolCallId': tc_id,
            'toolArgs': fn_args_raw if isinstance(fn_args_raw, str) else json.dumps(fn_args, ensure_ascii=False),
            'status': 'searching',
            'results': None,
        }
        task['tool_rounds'].append(round_entry)

        _append_report_event(task, {
            'type': 'tool_start',
            'roundNum': rn,
            'llmRound': rnd,
            'toolName': effective_name,
            'query': display_query,
            'toolCallId': tc_id,
            'toolArgs': round_entry['toolArgs'],
        })

        tool_t0 = time.time()
        result, display_results, search_diag, engine_breakdown, verticals = execute_paper_tool(
            fn_name, fn_args_raw, user_question=_report_user_question,
            abort=abort_signal.is_set,
            exec_shim=_exec_shim, round_entry=round_entry)
        tool_elapsed = time.time() - tool_t0
        logger.info('[Paper:Report:Tool] %s → %d chars in %.1fs', fn_name, len(result), tool_elapsed)

        # A contract rejection is a settled non-execution, never a successful
        # tool result. The adapter stamps it before returning.
        tool_status = ('rejected' if round_entry.get('status') == 'rejected'
                       else 'done')
        round_entry['status'] = tool_status
        round_entry['_elapsed'] = f'{tool_elapsed:.1f}s'
        round_entry['results'] = display_results
        if engine_breakdown:
            round_entry['engineBreakdown'] = engine_breakdown
        if verticals:
            round_entry['verticals'] = verticals
        # Preview of the tool content (capped, so polling responses stay small)
        tool_preview = result[:4000]
        round_entry['toolContent'] = tool_preview

        tool_done_event = {
            'type': 'tool_done',
            'roundNum': rn,
            'llmRound': rnd,
            'toolName': effective_name,
            'toolCallId': tc_id,
            'elapsed': round(tool_elapsed, 1),
            'toolContent': tool_preview,
            'results': display_results,
            'status': tool_status,
        }
        if round_entry.get('contractError'):
            tool_done_event['contractError'] = round_entry['contractError']
        if search_diag:
            tool_done_event['searchDiag'] = search_diag
        if engine_breakdown:
            tool_done_event['engineBreakdown'] = engine_breakdown
        if verticals:
            tool_done_event['verticals'] = verticals
        _append_report_event(task, tool_done_event)

        _result_budget.append(
            messages, round_index=rnd, tool_name=fn_name,
            tool_call_id=tc_id, content=result, round_entry=round_entry,
            world_version=str(task.get('_worldVersion') or ''),
            tool_arguments=fn_args)

    try:
        _outcome = run_agent_loop(
            abort=abort_signal,
            round_tools=paper_tools,
            dispatch=_dispatch,
            execute_tool=_execute_tool,
            on_round_result=_accumulate_usage,
            on_tool_round=_begin_tool_round,
            on_round_end=_result_budget.finish_round,
        )
        aborted = _outcome.aborted
        if _outcome.completed:
            logger.info('[Paper:Report] Task %s — no tool calls, report complete '
                        '(%d chars, %.1fs)', task['task_id'], len(full_content), time.time() - t0)

        if aborted:
            # User stopped generation. Do NOT persist the partial report or
            # emit `done` — emit a distinct `aborted` terminal event carrying
            # whatever text was produced so the frontend can show it read-only.
            logger.info('[Paper:Report] Task %s stopped by user — %d chars generated in %.1fs',
                        task['task_id'], len(full_content), time.time() - t0)
            _report_runtime.abort(task_id)
            _report_runtime.finish(
                task_id,
                terminal_event_fields={
                    'type': 'aborted', 'partial': full_content,
                },
            )
            return

        elapsed = time.time() - t0
        logger.info('[Paper:Report] Task %s content stream complete — %d chars in %.1fs',
                    task['task_id'], len(full_content), elapsed)

        # ── Authoritative body from the terminal round's CLEAN returned content ──
        # The streamed deltas in ``full_content`` are display-only: a mid-stream
        # transient retry (lib/llm/stream.py) re-streams the WHOLE response
        # through on_content, so ``full_content`` can hold the report twice. The
        # dispatcher's RETURNED ``msg['content']`` of the terminal (no-tool)
        # round is a single clean copy however the deltas arrived. Adopt it as
        # the source of truth for what gets injected + persisted, and tell live
        # pollers to converge (delta_reset) so the live stream matches the
        # written document. Only override when it actually diverges, so the
        # common no-retry path stays byte-identical.
        canonical_body, projection_needs_reset = resolve_canonical_report_body(
            full_content,
            _terminal['content'],
        )
        if projection_needs_reset:
            logger.info('[Paper:Report] Task %s — replacing %d-char streamed body with '
                        '%d-char terminal returned content (deltas were display-only; '
                        'likely a mid-stream re-stream)',
                        task['task_id'], len(full_content), len(canonical_body))
            full_content = canonical_body
            task['full_text'] = full_content
            _append_report_event(task, {'type': 'delta_reset'})
            _append_report_event(task, {'type': 'delta', 'delta': full_content})

        # Strip LLM preamble before the first heading. Models often emit
        # "Now I have enough information..." / "Let me compile..." / multi-
        # paragraph "I'll research..." before the actual Markdown report
        # starts at the first `## ` or `# `. Strip aggressively — any text
        # before the first heading is non-report chatter regardless of length.
        _heading_start = re.search(r'^#{1,6}\s', full_content, re.MULTILINE)
        if _heading_start and _heading_start.start() > 0:
            preamble = full_content[:_heading_start.start()].strip()
            if preamble:
                logger.info('[Paper:Report] Stripping %d-char preamble: %.120s',
                            len(preamble), preamble.replace('\n', ' '))
                full_content = full_content[_heading_start.start():]

        # Resolve the best title from three sources, in priority order:
        #   1. A non-placeholder stored title (user-renamed or already-resolved
        #      — never override it).
        #   2. The title the LLM wrote into the report's Paper Card — this is
        #      the self-heal source for rows stuck at the bare ``arXiv:<id>``
        #      because the up-front arXiv lookup failed.
        #   3. The client-supplied title (race fallback).
        # ``_is_placeholder`` mirrors the backfill predicate: empty or a bare
        # ``arXiv:<id>`` left behind by a failed up-front lookup.
        stored_title = lookup_paper_title(
            phash, user_id=int(task['_userId'])) or task.get('client_title') or ''
        card_title = extract_title_from_report(full_content)

        if not is_placeholder_title(stored_title):
            title = stored_title
        else:
            title = card_title or stored_title
        if title and full_content:
            existing_h1 = re.match(r'^\s*#\s+(.+?)\s*$', full_content, re.MULTILINE)
            first_h1 = existing_h1.group(1).strip() if existing_h1 else ''
            if not existing_h1:
                full_content = f'# {title}\n\n' + full_content.lstrip()
                logger.info('[Paper:Report] Prepended title: %.120s', title)
            elif is_placeholder_title(first_h1) and not is_placeholder_title(title):
                # Model baked a bare `# arXiv:<id>` placeholder as its own H1
                # (the up-front arXiv lookup failed). Swap in the real title.
                full_content = re.sub(r'^\s*#\s+.+?\s*$', f'# {title}',
                                      full_content, count=1, flags=re.MULTILINE)
                logger.info('[Paper:Report] Replaced placeholder H1 with title: %.120s', title)
            else:
                logger.info('[Paper:Report] Title prepend skipped — content already starts with H1')
        else:
            logger.warning('[Paper:Report] No title available for hash=%s — report will lack header', phash)

        # Review Mode: educate straight quotes to smart (curly) quotes on the
        # final body — a peer review must always render with typographic quotes
        # regardless of what the model emitted. Done here (before injection /
        # persistence) so the enriched body, DB row, and `done` event all carry
        # smart quotes. Math ($...$ primes), code, and URLs are preserved.
        if _is_review:
            _pre = full_content
            # Educate quotes + de-slop dashes, THEN make the body submittable:
            # strip any leaked table / dangling-* caption artifact and relocate
            # the venue scorecard below the non-submittable separator so the
            # review text a human pastes carries no scores. Deterministic belt —
            # the prompt asks for the same shape, but LLMs ignore format rules
            # (see paper-report-image-injection), so the guarantee lives here.
            full_content = finalize_review_body(
                strip_slop_dashes(smarten_quotes(full_content)), inj_lang)
            if full_content != _pre:
                task['full_text'] = full_content
                logger.info('[Paper:Review] Task %s — educated quotes, removed slop '
                            'dashes, and finalized submittable body', task['task_id'])
        elif _is_rebuttal:
            # Rebuttal follow-up: same typography discipline; relocate the
            # machine-parseable score decision below its sentinel (kept verbatim
            # so a score value is never corrupted by the comma-rewrite). The
            # structured decision is parsed and stamped on the report meta so the
            # UI can highlight the OA/Confidence change without re-parsing the body.
            _pre = full_content
            full_content = finalize_rebuttal_body(
                strip_slop_dashes(smarten_quotes(full_content)), inj_lang)
            if full_content != _pre:
                task['full_text'] = full_content
                logger.info('[Paper:Rebuttal] Task %s — educated quotes, removed slop '
                            'dashes, and relocated score decision', task['task_id'])
            try:
                _decision = parse_rebuttal_decision(full_content)
                task['rebuttal_decision'] = _decision
                logger.info('[Paper:Rebuttal] Task %s — decision present=%s changed=%s '
                            'OA %s→%s conf %s→%s', task['task_id'],
                            _decision.get('present'), _decision.get('changed'),
                            _decision.get('origOverall'), _decision.get('newOverall'),
                            _decision.get('origConfidence'), _decision.get('newConfidence'))
            except Exception as e:
                logger.warning('[Paper:Rebuttal] decision parse failed (non-fatal): %s', e)

        # Inject figures/tables into the report (text-only for reviews)
        enriched = inject_images_into_report(full_content, images, lang=inj_lang,
                                              appendix=inj_appendix,
                                              allow_images=inj_allow_images)
        task['enriched_text'] = enriched

        # ── Build the "finish tag" meta (model + token usage + cost) ──
        # Persisted alongside the report so re-opening a cached report still
        # shows which model generated it and what it cost. The resolved model
        # (what the dispatcher actually used) wins over the requested one.
        report_model = _resolved_model or model or _lib.LLM_MODEL
        report_meta = _build_report_meta(
            report_model, _provider_id, _usage_total, _round_count,
            time.time() - t0)
        request_policy = dict(task.get('requestPolicyV1') or {})
        cache_isolated = request_policy.get('cacheMode') == 'request_local'
        if request_policy:
            report_meta['requestPolicyV1'] = request_policy

        # ── Citation-hallucination audit (best-effort, zero-LLM) ──
        # Verify the identifiers the report ITSELF cites against free
        # authoritative catalogues (CrossRef / arXiv). Attach a card payload
        # to the meta ONLY when at least one citation is suspicious — an
        # all-clear or only-unverifiable run attaches nothing, so the frontend
        # renders no card. Wrapped: a failure here must never break the report.
        try:
            from lib.paper.citation_audit import build_citation_audit
            _audit = build_citation_audit(enriched or full_content)
            if _audit:
                report_meta['citationAudit'] = _audit
        except Exception as e:
            logger.warning('[Paper:Report] Citation audit failed (non-fatal): %s', e)

        # ── Terminology self-containment audit (best-effort, zero-LLM) ──
        # The report is a single forward pass with the glossary written EARLY,
        # so it forecasts terms rather than indexing them: acronyms used later
        # can lack a glossary row, and a glossary definition can lean on an
        # undefined sibling term. This gate runs over the COMPLETE body (no
        # forward blindness) and attaches an "undefined terms" card ONLY when a
        # real gap is found. Skipped for Review Mode (a decision document, not a
        # glossaried explainer). Wrapped: a failure must never break the report,
        # and — like the citation audit — it only touches meta, never the body,
        # so the double-render / terminal-round logic above is untouched.
        if not _is_review_family:
            try:
                from lib.paper.terminology_audit.audit import build_terminology_audit
                _term_audit = build_terminology_audit(enriched or full_content)
                if _term_audit:
                    report_meta['terminologyAudit'] = _term_audit
            except Exception as e:
                logger.warning('[Paper:Report] Terminology audit failed (non-fatal): %s', e)

        # Carry the structured rebuttal score-decision in meta so the reopened
        # cached row (and the `done` event) surface the OA/Confidence change.
        if _is_rebuttal and task.get('rebuttal_decision'):
            report_meta['rebuttalDecision'] = task['rebuttal_decision']

        task['report_meta'] = report_meta
        # Persist through storage.v1. The worker never owns a database driver,
        # connection, cursor, or transaction.
        if enriched and not cache_isolated:
            try:
                PaperArtifactRepository(int(task['_userId'])).put_report(
                    PaperReport(
                        paper_hash=phash,
                        lang=lang,
                        report=enriched,
                        model=report_model,
                        meta=report_meta,
                        created_at=int(time.time()),
                    ),
                    command_id=f'paper.report.upsert:{uuid.uuid4().hex}',
                )
                logger.info('[Paper:Report] Persisted — hash=%s lang=%s %d chars (%d imgs) '
                            'model=%s cost=%s',
                            phash, lang, len(enriched), len(images),
                            report_model, report_meta.get('costCny'))
            except Exception as e:
                logger.warning('[Paper:Report] Failed to persist: %s', e)
        elif enriched:
            logger.info(
                '[Paper:Report] Request-local policy — canonical cache write '
                'suppressed hash=%s policy=%s',
                phash, request_policy.get('executionFingerprint', '')[:12])

        # ── Self-heal the sidebar title ──
        # If the library row is still stuck at a bare ``arXiv:<id>`` (the
        # up-front arXiv lookup failed at fetch time), backfill the title the
        # LLM extracted into the Paper Card. Only placeholder rows are touched
        # — a user-renamed or correctly-resolved title is never clobbered. The
        # authoritative title is carried in the ``done`` event so the frontend
        # updates the sidebar live, with no manual reload.
        resolved_title = ''
        if card_title and not cache_isolated:
            try:
                resolved_title = backfill_library_title(
                    phash, card_title, user_id=int(task['_userId']))
            except Exception as e:
                logger.warning('[Paper:Report] Title backfill failed for hash=%s: %s', phash, e)
        _report_runtime.update_fields(
            task_id,
            fields={'resolved_title': resolved_title},
            only_if_status='running',
        )

        # If enrichment changed the text, emit an enriched event so pollers
        # replay the image-embedded version as the canonical body.
        if enriched and enriched != full_content:
            _append_report_event(task, {'type': 'enriched', 'text': enriched, 'paperHash': phash})

        final_report = enriched or full_content
        _report_runtime.finish(
            task_id,
            result=final_report,
            terminal_event_fields={
                'type': 'done', 'report': final_report,
                'paperHash': phash, 'meta': report_meta,
                'resolvedTitle': resolved_title,
            },
        )

        # ── Insight second-pass (opt-in, gated, non-destructive) ──
        # After the fidelity report is DONE + persisted, optionally run the
        # insight pass: score the report, and only if its own insight baseline
        # has headroom (<= INSIGHT_GATE_THRESHOLD) generate a grounded
        # synthesis/transfer section and persist it under the SEPARATE
        # ``insight:<ui>`` key (never overwrites the report). Flag-gated OFF by
        # default; skipped for Review Mode. Fully wrapped — a failure here must
        # NEVER taint the already-emitted `done`/persisted report.
        if not cache_isolated:
            try:
                _maybe_run_insight(
                    task, phash, inj_lang, enriched or full_content,
                    truncated_paper=(
                        messages[1]['content'] if len(messages) > 1 else ''),
                    model=report_model)
            except Exception as e:
                logger.warning(
                    '[Paper:Insight] second-pass wrapper failed (non-fatal) '
                    'hash=%s: %s', phash, e, exc_info=True)

        # ── Definition-backfill second-pass (opt-in, gated, non-destructive) ──
        # After `done` + persist, optionally CURE the glossary gaps the
        # terminology audit flagged: generate gap-closing definitions (pure body
        # context) and append them under the SEPARATE ``termfill:<ui>`` key,
        # kept ONLY if a re-audit proves the gap set shrank. Flag-gated OFF;
        # skipped for Review Mode. Fully wrapped — a failure here must NEVER
        # taint the already-emitted `done`/persisted report.
        if not cache_isolated:
            try:
                _maybe_run_termfill(
                    task, phash, inj_lang, enriched or full_content,
                    report_meta, model=report_model)
            except Exception as e:
                logger.warning(
                    '[Paper:TermFill] second-pass wrapper failed (non-fatal) '
                    'hash=%s: %s', phash, e, exc_info=True)

        # ── Checkpoint second-pass (gated, non-destructive) ──
        # Per-section self-test flip cards (active recall), persisted under
        # the SEPARATE ``checkpoints:<ui>`` key. Same wrapper discipline as
        # the insight/termfill passes: a failure never taints the report.
        if not cache_isolated:
            try:
                from lib.paper.report_engine._hooks import _maybe_run_checkpoints
                _maybe_run_checkpoints(
                    task, phash, inj_lang, enriched or full_content,
                    model=report_model)
            except Exception as e:
                logger.warning(
                    '[Paper:Checkpoints] second-pass wrapper failed (non-fatal) '
                    'hash=%s: %s', phash, e, exc_info=True)

    except AbortedError:
        # Raised by the dispatcher when an abort is detected between stream
        # retries. Same clean-stop semantics as the in-loop abort check.
        logger.info('[Paper:Report] Task %s stopped by user (stream retry) — %d chars',
                    task['task_id'], len(full_content))
        _report_runtime.abort(task_id)
        _report_runtime.finish(
            task_id,
            terminal_event_fields={
                'type': 'aborted', 'partial': full_content,
            },
        )

    except Exception as e:
        logger.error('[Paper:Report] Task %s failed after %.1fs: %s',
                     task['task_id'], time.time() - t0, e, exc_info=True)
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, model='', context='paper-report',
            source='routes.paper:report',
        )
        _report_runtime.finish(
            task_id,
            error=envelope,
            error_context='paper-report',
        )
    finally:
        _cleanup_stale_report_tasks()


__all__ = ["run_report_task"]
