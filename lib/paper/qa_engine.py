"""Background worker for agentic paper Q&A.

Runs the SAME tool-calling loop the report engine proves (web_search /
fetch_url via ``execute_paper_tool``), but for a single user question. The
message context is built by ``qa_context.build_qa_messages`` —
generated-report and paper sections selected under one shared budget — so the
model can answer both "what did you mean in the Limitations section?" (from
the report) and "find the follow-up paper" (via tools), without the legacy
100k blind truncation.
Three consecutive identical call+world rounds halt as an honest task error
before the fourth duplicate tool execution can spend more resources.

Emits chat-compatible events (tool_start / tool_done / delta / done / error)
so the frontend reuses ``renderToolRoundsHTML`` and the report poll renderer.
"""

import time

import lib as _lib
from lib.agent_loop.agent_loop import AbortSignal
from lib.llm_dispatch.api import dispatch_stream
from lib.log import get_logger

from .agent_loop_policy import run_guarded_paper_agent_loop
from .agent_usage import PaperAgentUsageMeter
from .qa_runtime import _append_qa_event, _cleanup_stale_qa_tasks, _qa_runtime
from .tools import (
    PaperToolResultBudgetV2,
    execute_paper_tool,
    apply_paper_tool_epoch_guidance,
    build_paper_full_tool_epoch,
    make_paper_exec_shim,
    make_paper_task_tool_executor,
)

logger = get_logger(__name__)


def _run_qa_task(task, messages):
    """Background worker: run the Q&A tool loop and populate task events.

    Args:
        task: the Q&A task dict (from ``_new_qa_task``).
        messages: the assembled message list (from ``build_qa_messages``).
    """
    task_id = task['task_id']
    _qa_runtime.mark_running(task_id)
    _append_qa_event(task, {'type': 'status', 'status': 'running'})

    model = task['model']
    abort_event = task['abort_event']

    def _abort_check():
        return abort_event.is_set()

    model_name = model or _lib.LLM_MODEL
    _agent_usage = PaperAgentUsageMeter.for_stage(
        'qa', fallback_model=model_name)
    task['agentUsageV1'] = _agent_usage.snapshot()
    t0 = time.time()
    full_content = ''
    question = task.get('question', '')
    user_question = question[:300]

    abort_signal = AbortSignal.from_event(abort_event)
    # Shim for the SHARED dispatch (full tool set) — one per run; see the
    # report engine for the policy + state-survival rationale.
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
        from lib.llm.stream_result import ensure_provider_stream_result
        return ensure_provider_stream_result(dispatch_stream(
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
        ))

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

    def _publish_agent_usage(_rnd, _msg, _finish, _usage):
        task['agentUsageV1'] = _agent_usage.snapshot()

    _execute_tool = make_paper_task_tool_executor(
        task, messages, user_question=user_question,
        abort_signal=abort_signal, exec_shim=_exec_shim,
        result_budget=_result_budget, paper_tool_executor=execute_paper_tool,
        on_tool_event=lambda event: _append_qa_event(task, event),
        log_prefix='[Paper:QA]',
    )

    try:
        _outcome = run_guarded_paper_agent_loop(
            context='Paper Q&A agent',
            allow_aborted_outcome=True,
            usage_meter=_agent_usage,
            abort=abort_signal,
            round_tools=paper_tools,
            dispatch=_dispatch,
            execute_tool=_execute_tool,
            on_round_result=_publish_agent_usage,
            on_tool_round=_begin_tool_round,
            on_round_end=_result_budget.finish_round,
        )
        if _outcome.completed:
            logger.info('[Paper:QA] Task %s — answer complete (%d chars, %.1fs)',
                        task['task_id'], len(full_content), time.time() - t0)
        if _outcome.aborted:
            logger.info('[Paper:QA] Task %s aborted', task['task_id'])
            _qa_runtime.abort(task_id)
            _qa_runtime.finish(
                task_id,
                terminal_event_fields={
                    'type': 'aborted', 'partial': full_content,
                    'paperHash': task['paper_hash'],
                    'agentUsageV1': _agent_usage.snapshot(),
                },
            )
            return
        elapsed = time.time() - t0
        logger.info('[Paper:QA] Task %s complete — %d chars, %.1fs',
                    task['task_id'], len(full_content), elapsed)
        _qa_runtime.finish(
            task_id,
            result=full_content,
            terminal_event_fields={
                'type': 'done', 'answer': full_content,
                'paperHash': task['paper_hash'],
                'agentUsageV1': _agent_usage.snapshot(),
            },
        )

    except Exception as e:
        logger.error('[Paper:QA] Task %s failed after %.1fs: %s',
                     task['task_id'], time.time() - t0, e, exc_info=True)
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, model='', context='paper-qa', source='routes.paper:qa')
        _qa_runtime.finish(
            task_id,
            error=envelope,
            error_context='paper-qa',
            terminal_event_fields={
                'agentUsageV1': _agent_usage.snapshot(),
            },
        )
    finally:
        task['agentUsageV1'] = _agent_usage.snapshot()
        _cleanup_stale_qa_tasks()


# Q&A uses the same full executable catalog plus bounded Tool Search projection
# as the report engine. Research-only engines keep the narrow search/fetch set.
