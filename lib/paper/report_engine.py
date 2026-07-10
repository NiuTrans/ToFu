"""Background worker for paper report generation.

Drives the LLM tool-calling loop (web_search / fetch_url for landscape
research), emits chat-compatible events (tool_start / tool_done / delta /
thinking / done / enriched / error), and persists the enriched report to
``paper_reports`` on completion.
"""

import json
import re
import time

import lib as _lib
from lib.agent_loop import AbortSignal, run_agent_loop
from lib.database import get_thread_db
from lib.llm_dispatch.api import dispatch_stream
from lib.llm_errors import AbortedError
from lib.log import get_logger

from .images import (
    _backfill_library_title,
    _extract_title_from_report,
    _inject_images_into_report,
    _is_placeholder_title,
    _lookup_paper_title,
)
from .prompts import _MAX_REPORT_TOOL_ROUNDS, _REPORT_TOOLS
from .report_runtime import _append_report_event, _cleanup_stale_report_tasks
from .tools import (
    _execute_report_tool,
    display_query_for,
    parse_and_repair_tool_args,
)

logger = get_logger(__name__)


def _build_report_meta(model, provider_id, usage_total, round_count, elapsed_s):
    """Assemble the report "finish tag" metadata dict.

    Combines the resolved generation model, accumulated token usage, and the
    computed cost (via ``lib.cost.compute_cost`` — the same math the chat
    finish-info bar uses) into a small JSON-serialisable dict the frontend
    renders as a badge under the report. Cost is best-effort: a pricing miss
    leaves ``costCny``/``costUsd`` as None but the model + token counts still
    show.
    """
    cost = None
    try:
        from lib.cost import compute_cost
        cost = compute_cost(usage_total, model_id=model, provider_id=provider_id)
    except Exception as e:
        logger.warning('[Paper:Report] cost computation failed: %s', e)
    meta = {
        'model': model or '',
        'providerId': provider_id or '',
        'rounds': round_count,
        'elapsedSec': round(elapsed_s, 1),
        'promptTokens': usage_total.get('prompt_tokens', 0),
        'completionTokens': usage_total.get('completion_tokens', 0),
        'cacheReadTokens': usage_total.get('cache_read_tokens', 0),
        'cacheWriteTokens': usage_total.get('cache_write_tokens', 0),
        'costUsd': cost.get('costUsd') if cost else None,
        'costCny': cost.get('costCny') if cost else None,
    }
    return meta


def _maybe_run_insight(task, phash, ui_lang, report_md, *, truncated_paper, model):
    """Run the gated insight second-pass after a report completes (best-effort).

    Skips entirely unless ``TOFU_PAPER_INSIGHT`` is on and this is a plain
    report (never Review Mode). Emits an ``insight_start`` event so the reader
    can show a "synthesizing insight…" affordance, then either an ``insight``
    event carrying the rendered section (gate fired + produced) or an
    ``insight_skipped`` event (gate withheld / nothing produced). Persistence is
    handled inside ``run_report_insight`` (``insight:<ui>`` key).

    ``allow_personal_context`` is resolved via ``lib/agent_core/personal_scope``
    from the task's cfg — the interactive report route leaves it unset → the
    resolver defaults True (owner keeps the transfer moat); every headless
    cfg-builder stamps ``paperInsightPersonalContext=False`` so a BYO caller's
    analysis never gets the operator's library/memories.
    """
    from .insight_engine import insight_enabled, run_report_insight
    from .review import is_review_lang

    if not insight_enabled():
        return
    if is_review_lang(task.get('lang') or ''):
        return
    if not (report_md or '').strip():
        return

    from lib.agent_core.personal_scope import resolve_paper_insight_personal_context
    allow_personal = resolve_paper_insight_personal_context(task.get('config'))

    abort_event = task.get('abort_event')

    def _abort():
        return bool(abort_event and abort_event.is_set())

    def _on_tool_event(ev):
        # Forward the insight research tool_start/tool_done into the SAME event
        # log the report uses, tagged so the frontend routes them to the insight
        # affordance rather than the report's tool panel.
        ev = dict(ev)
        ev['insight'] = True
        _append_report_event(task, ev)

    _append_report_event(task, {'type': 'insight_start', 'paperHash': phash})
    logger.info('[Paper:Insight] Starting gated second-pass — hash=%s ui_lang=%s '
                'personal_ctx=%s', phash, ui_lang, allow_personal)

    result = run_report_insight(
        truncated_paper, report_md, ui_lang, phash=phash, model=model,
        abort=_abort, on_tool_event=_on_tool_event,
        allow_personal_context=allow_personal)

    if result.get('markdown') and result.get('insight'):
        task['insight_text'] = result['markdown']
        _append_report_event(task, {
            'type': 'insight', 'paperHash': phash,
            'insight': result['markdown'],
            'lang': ui_lang,
            'baseline': result.get('baseline'),
            'grounded': result.get('grounded', 0),
            'selfref': result.get('selfref', 0),
        })
        logger.info('[Paper:Insight] Emitted insight — hash=%s fired=%s baseline=%s '
                    '%d chars', phash, result['fired'], result.get('baseline'),
                    len(result['markdown']))
    else:
        _append_report_event(task, {
            'type': 'insight_skipped', 'paperHash': phash,
            'fired': result.get('fired', False),
            'baseline': result.get('baseline'),
            'llmError': result.get('llmError', False),
        })
        logger.info('[Paper:Insight] No insight surfaced — hash=%s fired=%s baseline=%s',
                    phash, result.get('fired'), result.get('baseline'))


def _run_report_task(task, messages, images):
    """Background worker: runs the tool loop and populates task events.

    Event schema (mirrors chat stream for frontend reuse):
      - {type: 'tool_start', roundNum, toolName, query, toolCallId, toolArgs}
      - {type: 'tool_done',  roundNum, toolName, toolContent (truncated preview), elapsed}
      - {type: 'thinking',   delta}
      - {type: 'delta',      delta}
      - {type: 'enriched',   text}             — post-stream image injection
      - {type: 'done',       report, paperHash}
      - {type: 'error',      error}
    """
    task['status'] = 'running'
    _append_report_event(task, {'type': 'status', 'status': 'running'})

    phash = task['paper_hash']
    lang = task['lang']
    # Real UI language for image-injection / appendix headings. For ordinary
    # reports this equals `lang` ('en'/'zh'). For Review Mode, `lang` is a
    # composite cache key (``review:<venue>:<uilang>``), so the route stamps
    # the decoded UI language on the task; fall back to `lang` when absent.
    inj_lang = task.get('ui_lang') or lang
    # Review Mode (composite lang key ``review:<venue>:<uilang>``) suppresses
    # the figure appendix — a peer review must not be padded with every
    # extracted figure; only figures the reviewer actually cites land inline.
    from .review import is_review_lang
    inj_appendix = not is_review_lang(lang)
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

    # Per-round content buffer. Tool-calling models often emit a full interim
    # DRAFT of the report in a round that ALSO issues a tool call, then rewrite
    # the whole report from scratch in the final (no-tool-call) round.
    # Accumulating across rounds would bake the draft + final copy into one
    # document — the report rendered TWICE. So we buffer each round separately
    # (reset in _dispatch) and discard a tool-round's draft in _begin_tool_round.
    # Held on a mutable holder so both closures share the same per-round buffer.
    _round = {'content': ''}

    def _dispatch(rnd, tools):
        _round['content'] = ''

        def _on_content(text):
            nonlocal full_content
            _round['content'] += text
            full_content += text
            task['full_text'] = full_content
            _append_report_event(task, {'type': 'delta', 'delta': text})

        def _on_thinking(text):
            _append_report_event(task, {'type': 'thinking', 'delta': text})

        logger.info('[Paper:Report] Task %s round %d — model=%s msgs=%d',
                    task['task_id'], rnd + 1, model_name, len(messages))
        # ★ max_tokens: pass a very large ceiling so the report can run to
        #   completion without artificial truncation.  dispatch_stream →
        #   build_body → _clamp_max_tokens() automatically reduces this to each
        #   model's native API limit (GPT=32k, Claude=128k, Qwen per-model
        #   16–64k, etc.), so we get "as much as the model allows" without a cap.
        return dispatch_stream(
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
        )

    def _accumulate_usage(rnd, msg, finish, usage):
        # Accumulate token usage + capture the resolved model/provider (the
        # dispatcher may fall back to a different model than asked).
        nonlocal _round_count, _resolved_model, _provider_id
        _round_count += 1
        if isinstance(usage, dict):
            _usage_total['prompt_tokens'] += int(
                usage.get('prompt_tokens') or usage.get('input_tokens') or 0)
            _usage_total['completion_tokens'] += int(
                usage.get('completion_tokens') or usage.get('output_tokens') or 0)
            _usage_total['cache_read_tokens'] += int(
                usage.get('cache_read_tokens')
                or usage.get('cache_read_input_tokens') or 0)
            _usage_total['cache_write_tokens'] += int(
                usage.get('cache_write_tokens')
                or usage.get('cache_creation_input_tokens') or 0)
            _usage_total['reasoning_tokens'] += int(
                usage.get('reasoning_tokens')
                or usage.get('thinking_tokens') or 0)
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
            _append_report_event(task, {'type': 'delta_reset'})
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

        round_entry = {
            'roundNum': rn,
            'toolName': fn_name,
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
            'toolName': fn_name,
            'query': display_query,
            'toolCallId': tc_id,
            'toolArgs': round_entry['toolArgs'],
        })

        tool_t0 = time.time()
        result, display_results, search_diag, engine_breakdown, verticals = _execute_report_tool(
            fn_name, fn_args_raw, user_question=_report_user_question,
            abort=abort_signal.is_set)
        tool_elapsed = time.time() - tool_t0
        logger.info('[Paper:Report:Tool] %s → %d chars in %.1fs', fn_name, len(result), tool_elapsed)

        # Update round entry → done
        round_entry['status'] = 'done'
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
            'toolName': fn_name,
            'toolCallId': tc_id,
            'elapsed': round(tool_elapsed, 1),
            'toolContent': tool_preview,
            'results': display_results,
        }
        if search_diag:
            tool_done_event['searchDiag'] = search_diag
        if engine_breakdown:
            tool_done_event['engineBreakdown'] = engine_breakdown
        if verticals:
            tool_done_event['verticals'] = verticals
        _append_report_event(task, tool_done_event)

        messages.append({
            'role': 'tool',
            'tool_call_id': tc_id,
            'content': result[:30000],
        })

    try:
        _outcome = run_agent_loop(
            abort=abort_signal,
            max_tool_rounds=_MAX_REPORT_TOOL_ROUNDS,
            round_tools=_REPORT_TOOLS,
            dispatch=_dispatch,
            execute_tool=_execute_tool,
            on_round_result=_accumulate_usage,
            on_tool_round=_begin_tool_round,
        )
        aborted = _outcome.aborted
        if _outcome.completed:
            logger.info('[Paper:Report] Task %s — no tool calls, report complete '
                        '(%d chars, %.1fs)', task['task_id'], len(full_content), time.time() - t0)

        if aborted:
            # User stopped generation. Do NOT persist the partial report or
            # emit `done` — emit a distinct `aborted` terminal event carrying
            # whatever text was produced so the frontend can show it read-only.
            task['status'] = 'aborted'
            task['finished_at'] = time.time()
            logger.info('[Paper:Report] Task %s stopped by user — %d chars generated in %.1fs',
                        task['task_id'], len(full_content), time.time() - t0)
            _append_report_event(task, {'type': 'aborted', 'partial': full_content})
            return

        elapsed = time.time() - t0
        logger.info('[Paper:Report] Task %s content stream complete — %d chars in %.1fs',
                    task['task_id'], len(full_content), elapsed)

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
        stored_title = _lookup_paper_title(phash) or task.get('client_title') or ''
        card_title = _extract_title_from_report(full_content)

        if not _is_placeholder_title(stored_title):
            title = stored_title
        else:
            title = card_title or stored_title
        if title and full_content:
            existing_h1 = re.match(r'^\s*#\s+(.+?)\s*$', full_content, re.MULTILINE)
            first_h1 = existing_h1.group(1).strip() if existing_h1 else ''
            if not existing_h1:
                full_content = f'# {title}\n\n' + full_content.lstrip()
                logger.info('[Paper:Report] Prepended title: %.120s', title)
            elif _is_placeholder_title(first_h1) and not _is_placeholder_title(title):
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
        if is_review_lang(lang):
            from .review import smarten_quotes
            _pre = full_content
            full_content = smarten_quotes(full_content)
            if full_content != _pre:
                task['full_text'] = full_content
                logger.info('[Paper:Review] Task %s — educated straight quotes to '
                            'smart quotes', task['task_id'])

        # Inject figures/tables into the report
        enriched = _inject_images_into_report(full_content, images, lang=inj_lang,
                                              appendix=inj_appendix)
        task['enriched_text'] = enriched

        # ── Build the "finish tag" meta (model + token usage + cost) ──
        # Persisted alongside the report so re-opening a cached report still
        # shows which model generated it and what it cost. The resolved model
        # (what the dispatcher actually used) wins over the requested one.
        report_model = _resolved_model or model or _lib.LLM_MODEL
        report_meta = _build_report_meta(
            report_model, _provider_id, _usage_total, _round_count,
            time.time() - t0)

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

        task['report_meta'] = report_meta
        meta_json = json.dumps(report_meta, ensure_ascii=False)

        # Persist to DB
        if enriched:
            try:
                db2 = get_thread_db()
                from lib.database._core_schema import PAPER_REPORTS, upsert
                upsert(db2, PAPER_REPORTS, {
                    'paper_hash': phash, 'lang': lang, 'report': enriched,
                    'model': report_model, 'meta': meta_json,
                    'created_at': int(time.time()),
                }, retry=True)
                logger.info('[Paper:Report] Persisted — hash=%s lang=%s %d chars (%d imgs) '
                            'model=%s cost=%s',
                            phash, lang, len(enriched), len(images),
                            report_model, report_meta.get('costCny'))
            except Exception as e:
                logger.warning('[Paper:Report] Failed to persist: %s', e)

        # ── Self-heal the sidebar title ──
        # If the library row is still stuck at a bare ``arXiv:<id>`` (the
        # up-front arXiv lookup failed at fetch time), backfill the title the
        # LLM extracted into the Paper Card. Only placeholder rows are touched
        # — a user-renamed or correctly-resolved title is never clobbered. The
        # authoritative title is carried in the ``done`` event so the frontend
        # updates the sidebar live, with no manual reload.
        resolved_title = ''
        if card_title:
            try:
                resolved_title = _backfill_library_title(phash, card_title)
            except Exception as e:
                logger.warning('[Paper:Report] Title backfill failed for hash=%s: %s', phash, e)
        task['resolved_title'] = resolved_title

        # If enrichment changed the text, emit an enriched event so pollers
        # replay the image-embedded version as the canonical body.
        if enriched and enriched != full_content:
            _append_report_event(task, {'type': 'enriched', 'text': enriched, 'paperHash': phash})

        task['status'] = 'done'
        task['finished_at'] = time.time()
        _append_report_event(task, {'type': 'done', 'report': enriched or full_content,
                                    'paperHash': phash, 'meta': report_meta,
                                    'resolvedTitle': resolved_title})

        # ── Insight second-pass (opt-in, gated, non-destructive) ──
        # After the fidelity report is DONE + persisted, optionally run the
        # insight pass: score the report, and only if its own insight baseline
        # has headroom (<= INSIGHT_GATE_THRESHOLD) generate a grounded
        # synthesis/transfer section and persist it under the SEPARATE
        # ``insight:<ui>`` key (never overwrites the report). Flag-gated OFF by
        # default; skipped for Review Mode. Fully wrapped — a failure here must
        # NEVER taint the already-emitted `done`/persisted report.
        try:
            _maybe_run_insight(task, phash, inj_lang, enriched or full_content,
                               truncated_paper=(messages[1]['content'] if len(messages) > 1 else ''),
                               model=report_model)
        except Exception as e:
            logger.warning('[Paper:Insight] second-pass wrapper failed (non-fatal) '
                           'hash=%s: %s', phash, e, exc_info=True)

    except AbortedError:
        # Raised by the dispatcher when an abort is detected between stream
        # retries. Same clean-stop semantics as the in-loop abort check.
        task['status'] = 'aborted'
        task['finished_at'] = time.time()
        logger.info('[Paper:Report] Task %s stopped by user (stream retry) — %d chars',
                    task['task_id'], len(full_content))
        _append_report_event(task, {'type': 'aborted', 'partial': full_content})

    except Exception as e:
        logger.error('[Paper:Report] Task %s failed after %.1fs: %s',
                     task['task_id'], time.time() - t0, e, exc_info=True)
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, model='', context='paper-report',
            source='routes.paper:report',
        )
        task['status'] = 'error'
        task['error'] = envelope
        task['finished_at'] = time.time()
        _append_report_event(task, {'type': 'error', 'error': envelope})
    finally:
        _cleanup_stale_report_tasks()
