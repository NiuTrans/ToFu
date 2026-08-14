"""Background worker for agentic paper Q&A.

Runs the SAME tool-calling loop the report engine proves (web_search /
fetch_url via ``_execute_report_tool``), but for a single user question. The
message context is built by ``qa_context.build_qa_messages`` — full generated
report + question-relevant paper sections — so the model can answer both
"what did you mean in the Limitations section?" (from the report) and "find
the follow-up paper" (via tools), without the legacy 100k blind truncation.

Emits chat-compatible events (tool_start / tool_done / delta / done / error)
so the frontend reuses ``renderToolRoundsHTML`` and the report poll renderer.
"""

import json
import time

import lib as _lib
from lib.agent_loop import AbortSignal, run_agent_loop
from lib.llm_dispatch.api import dispatch_stream
from lib.log import get_logger

from .qa_runtime import _append_qa_event, _cleanup_stale_qa_tasks
from .tools import (
    _execute_report_tool,
    cap_tool_result,
    display_query_for as _display_query_for,
    make_paper_exec_shim,
    paper_effective_tool_name,
    parse_and_repair_tool_args,
)

logger = get_logger(__name__)


def _run_qa_task(task, messages):
    """Background worker: run the Q&A tool loop and populate task events.

    Args:
        task: the Q&A task dict (from ``_new_qa_task``).
        messages: the assembled message list (from ``build_qa_messages``).
    """
    task['status'] = 'running'
    _append_qa_event(task, {'type': 'status', 'status': 'running'})

    model = task['model']
    abort_event = task['abort_event']

    def _abort_check():
        return abort_event.is_set()

    model_name = model or _lib.LLM_MODEL
    t0 = time.time()
    full_content = ''
    question = task.get('question', '')
    user_question = question[:300]

    abort_signal = AbortSignal.from_event(abort_event)
    # Shim for the SHARED dispatch (full tool set) — one per run; see the
    # report engine for the policy + state-survival rationale.
    _exec_shim = make_paper_exec_shim(task_id=task['task_id'],
                                      abort=abort_signal.is_set)
    # Per-round content buffer (reset each dispatch), shared with the
    # draft-discard hook via a mutable holder — same interim-draft fix as the
    # report engine.
    _round = {'content': ''}

    def _dispatch(rnd, tools):
        _round['content'] = ''

        def _on_content(text):
            nonlocal full_content
            _round['content'] += text
            full_content += text
            task['full_text'] = full_content
            _append_qa_event(task, {'type': 'delta', 'delta': text})

        logger.info('[Paper:QA] Task %s round %d — model=%s msgs=%d',
                    task['task_id'], rnd + 1, model_name, len(messages))
        return dispatch_stream(
            messages,
            on_content=_on_content,
            abort_check=_abort_check,
            prefer_model=model_name if model else None,
            strict_model=bool(model),
            tools=tools,
            max_tokens=8000,
            temperature=0,
            thinking_enabled=False,
            log_prefix='[Paper:QA]',
        )

    def _begin_tool_round(rnd, msg):
        # Discard any interim draft prose this round emitted (it will be
        # rewritten after the tool results land).
        nonlocal full_content
        round_content = _round['content']
        if round_content:
            full_content = full_content[:-len(round_content)]
            task['full_text'] = full_content
            _append_qa_event(task, {'type': 'delta_reset'})
        messages.append(msg)

    def _execute_tool(rnd, tc):
        fn_name = tc['function']['name']
        fn_args_raw = tc['function']['arguments']
        tc_id = tc.get('id', '')

        # Parse + schema-repair args ONCE (shared with the executor), so the
        # display label and the actual search see the SAME normalized shape — a
        # bare-string `queries`/`urls` is coerced to a single-element array,
        # never iterated per-character.
        fn_args, _ = parse_and_repair_tool_args(fn_name, fn_args_raw)

        task['round_counter'] += 1
        rn = task['round_counter']
        display_query = _display_query_for(fn_name, fn_args)
        # run_command → code_exec in a project-less engine (mirrors chat).
        effective_name = paper_effective_tool_name(fn_name)

        round_entry = {
            'roundNum': rn, 'toolName': effective_name, 'query': display_query,
            'toolCallId': tc_id,
            'toolArgs': (fn_args_raw if isinstance(fn_args_raw, str)
                         else json.dumps(fn_args, ensure_ascii=False)),
            'status': 'searching', 'results': None,
        }
        task['tool_rounds'].append(round_entry)
        _append_qa_event(task, {
            'type': 'tool_start', 'roundNum': rn, 'toolName': effective_name,
            'query': display_query, 'toolCallId': tc_id,
            'toolArgs': round_entry['toolArgs'],
        })

        tool_t0 = time.time()
        result, display_results, search_diag, engine_breakdown, verticals = _execute_report_tool(
            fn_name, fn_args_raw, user_question=user_question,
            abort=abort_signal.is_set,
            exec_shim=_exec_shim, round_entry=round_entry)
        tool_elapsed = time.time() - tool_t0
        logger.info('[Paper:QA:Tool] %s → %d chars in %.1fs',
                    fn_name, len(result), tool_elapsed)

        round_entry['status'] = 'done'
        round_entry['_elapsed'] = f'{tool_elapsed:.1f}s'
        round_entry['results'] = display_results
        if engine_breakdown:
            round_entry['engineBreakdown'] = engine_breakdown
        if verticals:
            round_entry['verticals'] = verticals
        round_entry['toolContent'] = result[:4000]

        done_ev = {
            'type': 'tool_done', 'roundNum': rn, 'toolName': effective_name,
            'toolCallId': tc_id, 'elapsed': round(tool_elapsed, 1),
            'toolContent': result[:4000], 'results': display_results,
        }
        if search_diag:
            done_ev['searchDiag'] = search_diag
        if engine_breakdown:
            done_ev['engineBreakdown'] = engine_breakdown
        if verticals:
            done_ev['verticals'] = verticals
        _append_qa_event(task, done_ev)

        messages.append({
            'role': 'tool', 'tool_call_id': tc_id,
            'content': cap_tool_result(result, fn_name, tc_id,
                                       conv_id=f"paper-qa-{task['paper_hash']}",
                                       can_read=True),
        })

    try:
        _outcome = run_agent_loop(
            abort=abort_signal,
            round_tools=_QA_TOOLS,
            dispatch=_dispatch,
            execute_tool=_execute_tool,
            on_tool_round=_begin_tool_round,
        )
        if _outcome.completed:
            logger.info('[Paper:QA] Task %s — answer complete (%d chars, %.1fs)',
                        task['task_id'], len(full_content), time.time() - t0)
        elif _outcome.aborted:
            logger.info('[Paper:QA] Task %s aborted', task['task_id'])

        elapsed = time.time() - t0
        logger.info('[Paper:QA] Task %s complete — %d chars, %.1fs',
                    task['task_id'], len(full_content), elapsed)
        task['status'] = 'done'
        task['finished_at'] = time.time()
        _append_qa_event(task, {'type': 'done', 'answer': full_content,
                                'paperHash': task['paper_hash']})

    except Exception as e:
        logger.error('[Paper:QA] Task %s failed after %.1fs: %s',
                     task['task_id'], time.time() - t0, e, exc_info=True)
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, model='', context='paper-qa', source='routes.paper:qa')
        task['status'] = 'error'
        task['error'] = envelope
        task['finished_at'] = time.time()
        _append_qa_event(task, {'type': 'error', 'error': envelope})
    finally:
        _cleanup_stale_qa_tasks()


# Tool list — the FULL chat-tier set (report engine parity): search/fetch
# plus read_files / code_exec / memory / todo / scheduler / …, assembled via
# the shared registry. The research-only engines (insight/recommend/ideate)
# deliberately keep the narrow ``_REPORT_TOOLS``.
from .prompts import _FULL_REPORT_TOOLS as _QA_TOOLS  # noqa: E402
