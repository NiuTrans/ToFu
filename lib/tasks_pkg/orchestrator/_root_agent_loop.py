"""Root-orchestrator policy adapter for :func:`lib.agent_loop.run_agent_loop`.

Responsibility
--------------
The shared runner owns the ReAct lifecycle: round numbering, all loop control
sites, abort placements, timeout counting, and checkpoint placement.  This
module supplies only root-chat policy and wire projections (request assembly,
stream analysis, tool events, and semantic progress guards).

Entry point
-----------
``run_root_agent_loop(RootLoopRequest(...)) -> RootLoopResult``.

Flow work turns delegate to root ``run_task`` and therefore inherit this
same lifecycle.  Swarm and the other agentic engines call the same runner with
their own bounded policy hooks; none owns a private ReAct ``while`` loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lib.agent_loop import (
    AbortSignal,
    LoopDirective,
    LoopOutcome,
    run_agent_loop,
)
from lib.llm.stream_result import ProviderStreamResult
from lib.log import get_logger
from lib.tasks_pkg.orchestrator._abort_before_tools import (
    handle_abort_before_tools,
)
from lib.tasks_pkg.orchestrator._abort_round_start import (
    handle_abort_at_round_start,
)
from lib.tasks_pkg.orchestrator._cache_round_accounting import (
    stamp_round_cache_accounting,
)
from lib.tasks_pkg.orchestrator._llm_round_call import (
    run_llm_call_with_fallback,
)
from lib.tasks_pkg.orchestrator._round_checkpoint import (
    run_round_checkpoint_and_close,
)
from lib.tasks_pkg.orchestrator._round_message_hygiene import (
    run_round_message_hygiene,
)
from lib.tasks_pkg.orchestrator._round_open import (
    build_stream_accumulator,
    emit_round_open,
)
from lib.tasks_pkg.orchestrator._round_request_prep import build_round_request
from lib.tasks_pkg.orchestrator._stream_acc_settle import (
    settle_stream_accumulator,
)
from lib.tasks_pkg.orchestrator._stream_decision import apply_stream_decision
from lib.tasks_pkg.orchestrator._swarm_inbox import drain_and_inject_inbox
from lib.tasks_pkg.orchestrator._tool_call_prelude import (
    append_assistant_tool_call_message,
)
from lib.tasks_pkg.orchestrator._tool_dispatch_round import run_tool_dispatch
from lib.tasks_pkg.orchestrator._tool_loop_breaker import (
    finish_after_background_task_acceptance,
    handle_tool_loop_circuit_breaker,
)
from lib.tasks_pkg.orchestrator._tool_timeout_breaker import (
    handle_tool_timeout_circuit_breaker,
)

__all__ = ['RootLoopRequest', 'RootLoopResult', 'run_root_agent_loop']

logger = get_logger(__name__)

_MAX_CONSECUTIVE_TOOL_TIMEOUTS = 3


@dataclass(slots=True)
class RootLoopRequest:
    """Explicit dependencies and request state consumed by the root adapter."""

    task: dict[str, Any]
    state: Any
    messages: list[dict[str, Any]]
    tool_list: list[dict[str, Any]]
    all_search_results_text: list[Any]
    cfg: dict[str, Any]
    tid: str
    thinking_depth: Any
    temperature: Any
    max_tokens: int
    response_format: Any
    project_path: Any
    project_enabled: bool
    search_enabled: bool


@dataclass(slots=True, frozen=True)
class RootLoopResult:
    """Chassis outcome plus the last round index needed by finalization."""

    outcome: LoopOutcome
    last_round_num: int


class _RootLoopHooks:
    """Stateful root policy hooks; lifecycle control remains in the chassis."""

    def __init__(self, request: RootLoopRequest):
        self.request = request
        self.round_num = -1
        self.premature_retry_count = 0
        self.prepared_tool_round = -1
        self.round_context: dict[str, Any] = {}
        self._admission_tool_schema_source: Any = None
        self._admission_tool_schema_model = ''
        self._admission_tool_schema_tokens: int | None = None
        self._admission_tool_schema_fingerprint = ''

    @property
    def task(self) -> dict[str, Any]:
        return self.request.task

    @property
    def state(self) -> Any:
        return self.request.state

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.request.messages

    def dispatch(
        self,
        round_num: int,
        round_tools: Any,
    ) -> ProviderStreamResult:
        self.round_num = round_num
        request = self.request
        state = self.state
        emit_round_open(self.task, state, round_num)
        run_round_message_hygiene(
            self.task, self.messages,
            round_num=round_num, tid=request.tid,
            model=state.model,
            project_path=request.project_path,
            project_enabled=request.project_enabled,
        )
        drain_and_inject_inbox(
            task=self.task, messages=self.messages,
            round_num=round_num, tid=request.tid)

        # Final fail-closed boundary: inbox/tool-surface injections happen
        # after the ordinary compaction pipeline, so measure the complete
        # provider prompt here before constructing or dispatching the body.
        from lib.tasks_pkg.compaction._prompt_admission import (
            enforce_dispatch_prompt_limit,
        )
        from lib.context_telemetry import (
            TOOL_SCHEMA_EVIDENCE_KEY,
            tool_schema_fingerprint_from_evidence,
            validated_tool_schema_token_count,
        )
        from lib.token_counter.base import (
            REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY,
        )
        reusable_schema_evidence = (
            round_tools is self._admission_tool_schema_source
            and state.model == self._admission_tool_schema_model
        )
        reusable_schema_tokens = (
            self._admission_tool_schema_tokens
            if reusable_schema_evidence else None)
        reusable_schema_fingerprint = (
            self._admission_tool_schema_fingerprint
            if reusable_schema_evidence else None)
        prompt_admission = enforce_dispatch_prompt_limit(
            self.messages,
            round_tools,
            self.task,
            round_num=round_num,
            model=state.model,
            precomputed_tool_schema_tokens=reusable_schema_tokens,
        )
        measured_schema_tokens = validated_tool_schema_token_count(
            round_tools, prompt_admission.get('toolSchemaTokens'))
        if measured_schema_tokens is not None:
            if not reusable_schema_evidence:
                self._admission_tool_schema_fingerprint = ''
            self._admission_tool_schema_source = round_tools
            self._admission_tool_schema_model = state.model
            self._admission_tool_schema_tokens = measured_schema_tokens
        else:
            self._admission_tool_schema_source = None
            self._admission_tool_schema_model = ''
            self._admission_tool_schema_tokens = None
            self._admission_tool_schema_fingerprint = ''
        # Pop the identity map into this one synchronous call: it must not live
        # in admission evidence or survive body preparation into the stream.
        tools_this_round, body = build_round_request(
            self.task, state, self.messages, round_tools,
            round_num=round_num, tid=request.tid,
            thinking_depth=request.thinking_depth,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            response_format=request.response_format,
            admitted_input_tokens=prompt_admission.get('totalTokens'),
            admitted_tool_schema_tokens=prompt_admission.get(
                'toolSchemaTokens'),
            admitted_tool_schema_fingerprint=reusable_schema_fingerprint,
            reusable_text_token_counts_by_identity=prompt_admission.pop(
                REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY, None),
        )
        stream_accumulator = build_stream_accumulator(
            self.task, state, request.cfg, round_num,
            request.project_enabled)
        self.round_context = {
            'tools': tools_this_round,
            'stream_accumulator': stream_accumulator,
            'llm_action': 'proceed',
        }
        try:
            llm_action = run_llm_call_with_fallback(
                self.task, state, body, self.messages, round_tools,
                stream_accumulator,
                round_num=round_num, tid=request.tid,
                max_tokens=request.max_tokens)
            measured_schema_fingerprint = (
                tool_schema_fingerprint_from_evidence(
                    body.get(TOOL_SCHEMA_EVIDENCE_KEY)))
            if (measured_schema_fingerprint
                    and round_tools is self._admission_tool_schema_source
                    and state.model == self._admission_tool_schema_model):
                self._admission_tool_schema_fingerprint = (
                    measured_schema_fingerprint)
            self.round_context['llm_action'] = llm_action
            if llm_action != 'break':
                stamp_round_cache_accounting(
                    self.task,
                    round_num=round_num, tid=request.tid, model=state.model,
                    tools=tools_this_round, usage=state.last_usage,
                    assistant_msg=state.assistant_msg,
                    api_rounds=state.api_rounds, messages=self.messages,
                )
                settle_stream_accumulator(
                    stream_accumulator, self.task, state,
                    tid=request.tid, round_num=round_num)
        finally:
            # Normal cache injection closes after harvesting. Provider break,
            # abort, and exception paths skip that seam, so reclaim their
            # bounded speculative queue here without masking the root error.
            stream_accumulator.close(cancel_futures=True, wait=False)
        stream_result = getattr(state, 'last_stream_result', None)
        if isinstance(stream_result, ProviderStreamResult):
            return stream_result
        return ProviderStreamResult.from_legacy(
            state.assistant_msg or {},
            state.last_finish_reason,
            state.last_usage,
        )

    def decide_round(
        self,
        round_num: int,
        _message: dict,
        _finish_reason: Any,
        _usage: Any,
    ) -> LoopDirective:
        state = self.state
        request = self.request
        if self.round_context.get('llm_action') == 'break':
            if self.task.get('aborted') or state.exit_reason == 'user_abort':
                return LoopDirective.abort(state.exit_reason)
            return LoopDirective.stop(state.exit_reason)

        stream_action, self.premature_retry_count = apply_stream_decision(
            self.task, state, round_num=round_num, tid=request.tid,
            premature_retry_count=self.premature_retry_count,
            messages=self.messages)
        if stream_action == 'break':
            if state.abort_phase:
                return LoopDirective.abort(state.exit_reason)
            # A provider no-tool stop is a protocol boundary, not task-oracle
            # evidence. Root finalization owns verified completion; keep this
            # outcome neutral so future callers cannot promote model silence
            # or non-empty prose into ``completed=True``.
            return LoopDirective.stop(state.exit_reason)
        if stream_action == 'continue':
            return LoopDirective.continue_round()
        _round_content = len(
            (state.assistant_msg or {}).get('content', '') or '')
        _round_tcs = len((state.assistant_msg or {}).get('tool_calls', []))
        logger.info(
            '[%s] conv=%s Round %d result: finish_reason=%s model=%s '
            'content=%dchars tool_calls=%d → proceeding to tool execution',
            request.tid, self.task.get('convId', ''), round_num + 1,
            state.last_finish_reason, state.model,
            _round_content, _round_tcs)
        if stream_action == 'program_continue':
            return LoopDirective.continue_round()
        return LoopDirective.proceed()

    def open_tool_round(self, round_num: int, _message: dict) -> None:
        if self.prepared_tool_round == round_num:
            return
        self.state.tool_call_happened = True
        append_assistant_tool_call_message(
            self.task, self.messages,
            round_num=round_num, tid=self.request.tid,
            assistant_msg=self.state.assistant_msg)
        self.prepared_tool_round = round_num

    def before_tools(
        self,
        round_num: int,
        _message: dict,
    ) -> LoopDirective | None:
        if handle_abort_before_tools(
                self.task, self.state, self.messages,
                round_num=round_num, tid=self.request.tid):
            return LoopDirective.abort(self.state.exit_reason)
        return None

    def execute_tools(
        self,
        round_num: int,
        _tool_calls: list,
    ) -> dict[str, bool]:
        stream_accumulator = self.round_context['stream_accumulator']
        tool_timed_out = run_tool_dispatch(
            self.task, self.state, self.messages,
            self.request.all_search_results_text,
            round_num=round_num, tid=self.request.tid,
            cfg=self.request.cfg, project_path=self.request.project_path,
            project_enabled=self.request.project_enabled,
            tool_list=self.request.tool_list,
            announced_tc_map=stream_accumulator.announced_tc_map,
        )
        return {'timed_out': bool(tool_timed_out)}

    def observe_timeout_state(
        self,
        round_num: int,
        tool_timed_out: bool,
        consecutive_count: int,
        timeout_limit: int,
    ) -> None:
        handle_tool_timeout_circuit_breaker(
            self.task, self.state,
            round_num=round_num, tid=self.request.tid,
            tool_timed_out=tool_timed_out,
            max_consecutive_tool_timeouts=timeout_limit,
            chassis_consecutive_count=consecutive_count,
        )

    def after_tools(
        self,
        round_num: int,
        _message: dict,
        _note: dict | None,
    ) -> LoopDirective | None:
        if finish_after_background_task_acceptance(
                self.task, self.state, round_num=round_num,
                tid=self.request.tid):
            return LoopDirective.halt(self.state.exit_reason)
        if handle_tool_loop_circuit_breaker(
                self.task, self.state, messages=self.messages,
                round_num=round_num, tid=self.request.tid):
            return LoopDirective.halt(self.state.exit_reason)
        return None

    def close_round(self, round_num: int) -> None:
        run_round_checkpoint_and_close(
            self.task, self.state,
            round_num=round_num, tid=self.request.tid)

    def observe_abort(
        self,
        phase: str,
        round_num: int,
        message: dict | None,
    ) -> str | None:
        self.round_num = round_num
        if phase == 'before_round':
            handle_abort_at_round_start(
                self.task, self.state,
                round_num=round_num, tid=self.request.tid)
            return self.state.exit_reason
        if phase == 'post_stream' and message and message.get('tool_calls'):
            self.open_tool_round(round_num, message)
            handle_abort_before_tools(
                self.task, self.state, self.messages,
                round_num=round_num, tid=self.request.tid)
            return self.state.exit_reason
        return None


def run_root_agent_loop(request: RootLoopRequest) -> RootLoopResult:
    """Run the root chat/tool lifecycle on the shared agent-loop chassis."""
    hooks = _RootLoopHooks(request)
    outcome = run_agent_loop(
        abort=AbortSignal.from_task_flag(request.task),
        round_tools=request.tool_list,
        dispatch=hooks.dispatch,
        decide_round=hooks.decide_round,
        on_tool_round=hooks.open_tool_round,
        before_tools=hooks.before_tools,
        execute_tools=hooks.execute_tools,
        max_consecutive_tool_timeouts=_MAX_CONSECUTIVE_TOOL_TIMEOUTS,
        on_tool_timeout_state=hooks.observe_timeout_state,
        after_tools=hooks.after_tools,
        on_round_end=hooks.close_round,
        on_abort=hooks.observe_abort,
    )
    if request.state.exit_reason == 'running':
        request.state.exit_reason = outcome.exit_reason
    return RootLoopResult(outcome=outcome, last_round_num=hooks.round_num)
