"""Execute one paper-report task from dispatch through durable publication.

Responsibility: connect the shared agent loop to report streaming, canonical
body projection, artifact persistence, and optional post-report hooks.
Entry point: :func:`run_report_task`.
Dependencies: ``report_runtime`` owns lifecycle state, ``tools`` owns tool
execution, tool-round projection and bounded feedback. ``_body`` owns streamed
text and terminal convergence; ``_publication`` owns confirmed artifact
publication under the shared finish claim. Sibling ``_meta`` and
``_hooks`` modules own bounded post-processing.
"""

import re
import time

import lib as _lib
from lib.agent_loop.agent_loop import AbortSignal
from lib.llm_dispatch.api import dispatch_stream
from lib.llm.llm_errors import AbortedError
from lib.log import get_logger

from lib.paper.images.injection import inject_images_into_report
from lib.paper.images.titles import (
    backfill_library_title,
    extract_title_from_report,
    is_placeholder_title,
    lookup_paper_title,
)
from lib.paper.artifact_repository import PaperArtifactRepository, PaperReport
from lib.paper.agent_loop_policy import run_guarded_paper_agent_loop
from lib.paper.agent_usage import PaperAgentUsageMeter
from lib.paper.report_engine._body import ReportBodyProjection
from lib.paper.report_engine._publication import publish_report
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
    make_paper_task_tool_executor,
)

from lib.paper.report_engine._meta import _build_report_meta
from lib.paper.report_engine._hooks import (
    run_report_postpasses,
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
      - {type: 'done',       report, paperHash, meta.agentUsageV1}
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
    _agent_usage = PaperAgentUsageMeter.for_stage(
        'report', fallback_model=model_name)
    task['agentUsageV1'] = _agent_usage.snapshot()
    t0 = time.time()
    body = ReportBodyProjection(
        task, append_event=_append_report_event, logger=logger)
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
        messages, paper_epoch, lang=task.get('lang') or 'en', task=task)
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

    def _dispatch(rnd, tools):
        body.begin_round()

        def _on_content(text):
            body.append_delta(text, rnd)

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
        body.observe_response(msg)
        if isinstance(usage, dict):
            from lib.cost import normalize_usage as _normalize_usage
            _nu = _normalize_usage(usage)
            _usage_total['prompt_tokens'] += _nu['input']
            _usage_total['completion_tokens'] += _nu['output']
            _usage_total['cache_read_tokens'] += _nu['cache_read']
            _usage_total['cache_write_tokens'] += _nu['cache_write']
            _usage_total['reasoning_tokens'] += _nu['thinking']
            _disp = usage.get('_dispatch') or {}
            if _disp.get('logical_model') or _disp.get('model'):
                _resolved_model = _disp.get('logical_model') or _disp['model']
            if _disp.get('provider_id'):
                _provider_id = _disp['provider_id']
        task['agentUsageV1'] = _agent_usage.snapshot()

    def _begin_tool_round(rnd, msg):
        body.discard_tool_draft(rnd)
        messages.append(msg)

    _execute_tool = make_paper_task_tool_executor(
        task, messages, user_question=_report_user_question,
        abort_signal=abort_signal, exec_shim=_exec_shim,
        result_budget=_result_budget, paper_tool_executor=execute_paper_tool,
        on_tool_event=lambda event: _append_report_event(task, event),
        log_prefix='[Paper:Report]', include_llm_round=True,
    )

    try:
        _outcome = run_guarded_paper_agent_loop(
            context='Paper report agent',
            allow_aborted_outcome=True,
            usage_meter=_agent_usage,
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
                        '(%d chars, %.1fs)', task['task_id'], len(body.text), time.time() - t0)

        if aborted:
            # User stopped generation. Do NOT persist the partial report or
            # emit `done` — emit a distinct `aborted` terminal event carrying
            # whatever text was produced so the frontend can show it read-only.
            logger.info('[Paper:Report] Task %s stopped by user — %d chars generated in %.1fs',
                        task['task_id'], len(body.text), time.time() - t0)
            _report_runtime.abort(task_id)
            _report_runtime.finish(
                task_id,
                terminal_event_fields={
                    'type': 'aborted', 'partial': body.text,
                    'agentUsageV1': _agent_usage.snapshot(),
                },
            )
            return

        elapsed = time.time() - t0
        logger.info('[Paper:Report] Task %s content stream complete — %d chars in %.1fs',
                    task['task_id'], len(body.text), elapsed)

        body.adopt_terminal_response()

        # Strip LLM preamble before the first heading. Models often emit
        # "Now I have enough information..." / "Let me compile..." / multi-
        # paragraph "I'll research..." before the actual Markdown report
        # starts at the first `## ` or `# `. Strip aggressively — any text
        # before the first heading is non-report chatter regardless of length.
        _heading_start = re.search(r'^#{1,6}\s', body.text, re.MULTILINE)
        if _heading_start and _heading_start.start() > 0:
            preamble = body.text[:_heading_start.start()].strip()
            if preamble:
                logger.info('[Paper:Report] Stripping %d-char preamble: %.120s',
                            len(preamble), preamble.replace('\n', ' '))
                body.text = body.text[_heading_start.start():]

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
        card_title = extract_title_from_report(body.text)

        if not is_placeholder_title(stored_title):
            title = stored_title
        else:
            title = card_title or stored_title
        if title and body.text:
            existing_h1 = re.match(r'^\s*#\s+(.+?)\s*$', body.text, re.MULTILINE)
            first_h1 = existing_h1.group(1).strip() if existing_h1 else ''
            if not existing_h1:
                body.text = f'# {title}\n\n' + body.text.lstrip()
                logger.info('[Paper:Report] Prepended title: %.120s', title)
            elif is_placeholder_title(first_h1) and not is_placeholder_title(title):
                # Model baked a bare `# arXiv:<id>` placeholder as its own H1
                # (the up-front arXiv lookup failed). Swap in the real title.
                body.text = re.sub(r'^\s*#\s+.+?\s*$', f'# {title}',
                                      body.text, count=1, flags=re.MULTILINE)
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
            _pre = body.text
            # Educate quotes + de-slop dashes, THEN make the body submittable:
            # strip any leaked table / dangling-* caption artifact and relocate
            # the venue scorecard below the non-submittable separator so the
            # review text a human pastes carries no scores. Deterministic belt —
            # the prompt asks for the same shape, but LLMs ignore format rules
            # (see paper-report-image-injection), so the guarantee lives here.
            body.text = finalize_review_body(
                strip_slop_dashes(smarten_quotes(body.text)), inj_lang)
            if body.text != _pre:
                task['full_text'] = body.text
                logger.info('[Paper:Review] Task %s — educated quotes, removed slop '
                            'dashes, and finalized submittable body', task['task_id'])
        elif _is_rebuttal:
            # Rebuttal follow-up: same typography discipline; relocate the
            # machine-parseable score decision below its sentinel (kept verbatim
            # so a score value is never corrupted by the comma-rewrite). The
            # structured decision is parsed and stamped on the report meta so the
            # UI can highlight the OA/Confidence change without re-parsing the body.
            _pre = body.text
            body.text = finalize_rebuttal_body(
                strip_slop_dashes(smarten_quotes(body.text)), inj_lang)
            if body.text != _pre:
                task['full_text'] = body.text
                logger.info('[Paper:Rebuttal] Task %s — educated quotes, removed slop '
                            'dashes, and relocated score decision', task['task_id'])
            try:
                _decision = parse_rebuttal_decision(body.text)
                task['rebuttal_decision'] = _decision
                logger.info('[Paper:Rebuttal] Task %s — decision present=%s changed=%s '
                            'OA %s→%s conf %s→%s', task['task_id'],
                            _decision.get('present'), _decision.get('changed'),
                            _decision.get('origOverall'), _decision.get('newOverall'),
                            _decision.get('origConfidence'), _decision.get('newConfidence'))
            except Exception as e:
                logger.warning('[Paper:Rebuttal] decision parse failed (non-fatal): %s', e)

        # Inject figures/tables into the report (text-only for reviews)
        enriched = inject_images_into_report(body.text, images, lang=inj_lang,
                                              appendix=inj_appendix,
                                              allow_images=inj_allow_images)
        task['enriched_text'] = enriched
        final_report = enriched or body.text
        if not final_report.strip():
            raise ValueError('Paper report enrichment produced an empty artifact')

        # ── Build the "finish tag" meta (model + token usage + cost) ──
        # Persisted alongside the report so re-opening a cached report still
        # shows which model generated it and what it cost. The resolved model
        # (what the dispatcher actually used) wins over the requested one.
        report_model = _resolved_model or model or _lib.LLM_MODEL
        report_meta = _build_report_meta(
            report_model, _provider_id, _usage_total, _round_count,
            time.time() - t0)
        report_meta['agentUsageV1'] = _agent_usage.snapshot()
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
            _audit = build_citation_audit(enriched or body.text)
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
                _term_audit = build_terminology_audit(enriched or body.text)
                if _term_audit:
                    report_meta['terminologyAudit'] = _term_audit
            except Exception as e:
                logger.warning('[Paper:Report] Terminology audit failed (non-fatal): %s', e)

        # Carry the structured rebuttal score-decision in meta so the reopened
        # cached row (and the `done` event) surface the OA/Confidence change.
        if _is_rebuttal and task.get('rebuttal_decision'):
            report_meta['rebuttalDecision'] = task['rebuttal_decision']

        task['report_meta'] = report_meta
        # The shared finish claim decides Stop versus publication. Resource
        # cleanup must succeed before this callback can write the artifact.
        finished = _report_runtime.finish(
            task_id,
            commit_result=lambda: publish_report(
                task,
                PaperReport(
                    paper_hash=phash, lang=lang, report=final_report,
                    model=report_model, meta=report_meta,
                    created_at=int(time.time()),
                ),
                prepared_body=body.text, images=images, card_title=card_title,
                repository_factory=PaperArtifactRepository,
                backfill_title=backfill_library_title, runtime=_report_runtime,
                append_event=_append_report_event,
            ),
            terminal_event_fields={
                'type': 'aborted', 'partial': body.text,
                'agentUsageV1': _agent_usage.snapshot(),
            },
        )
        if not finished or task['status'] != 'done':
            return

        # Additive passes share one ordered best-effort owner. Request-local
        # output never enters these canonical artifact-writing stages.
        if not cache_isolated:
            run_report_postpasses(
                task, phash, inj_lang, enriched or body.text, report_meta,
                messages=messages, model=report_model, insight=_maybe_run_insight,
                termfill=_maybe_run_termfill,
            )

    except AbortedError:
        # Raised by the dispatcher when an abort is detected between stream
        # retries. Same clean-stop semantics as the in-loop abort check.
        logger.info('[Paper:Report] Task %s stopped by user (stream retry) — %d chars',
                    task['task_id'], len(body.text))
        _report_runtime.abort(task_id)
        _report_runtime.finish(
            task_id,
            terminal_event_fields={
                'type': 'aborted', 'partial': body.text,
                'agentUsageV1': _agent_usage.snapshot(),
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
            terminal_event_fields={
                'agentUsageV1': _agent_usage.snapshot(),
            },
        )
    finally:
        task['agentUsageV1'] = _agent_usage.snapshot()
        _cleanup_stale_report_tasks()


__all__ = ["run_report_task"]
