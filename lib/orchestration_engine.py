"""lib/orchestration_engine.py — Execute a tofu.orchestration/v1 graph.

This is the long-missing executor: it interprets a *validated* definition
(role nodes + control nodes + edges) and actually runs the agents, in the
topology the graph describes. It unifies verifier loops and swarm fan-out under
one declarative engine.

Architecture
------------
The engine is a **graph interpreter**. It walks forward from the ``start``
node; agent execution is abstracted behind a single injectable
``agent_runner(node, context, iteration) -> {output, status, error}``.

  * The default adapter in :mod:`lib.orchestration_agent_runner` builds a
    :class:`SubTaskSpec` + :class:`SubAgent` from the swarm substrate and runs
    it — same agents the swarm uses, with the same role→tool scoping + tiers.
  * Tests inject a mock runner, so the interpreter (the part with the
    real logic + the loop/fan-out/branch control flow) is fully covered
    in CI without any LLM call.

Supported control semantics (v1)
--------------------------------
  start    — entry; single outgoing edge.
  role     — run an agent; single outgoing edge; output appended to context.
  parallel — every outgoing edge is a branch; branches run concurrently
             (thread pool) and re-converge at their common ``barrier``.
  barrier  — join marker; single outgoing edge.
  loop     — two outgoing edges: a *body* entry (a path that loops back to
             the loop node) and an *exit*. The body runs repeatedly until a
             verifier verdict says STOP or ``max_iterations`` is hit; then
             the exit edge is taken. This is a verifier loop expressed as data.
  branch   — picks ONE outgoing edge (v1: the first; a future classifier
             agent will choose). Documented limitation.
  stop     — terminal; returns the converged result.

Safety: total agent runs are capped (``max_agents``) and every loop has a
hard ``max_iterations`` cap, so a malformed graph can never spin forever.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from lib.agent_verdict import (
    classify_verdict as _classify_verdict_core,
    parse_progress as _parse_progress,
)
from lib.log import get_logger
from lib.orchestration._subflow_expansion import expand_subflows
from lib.orchestration._validate import validate_definition
from lib.orchestration._role_axes import VERIFIER_ROLES
from lib.orchestration.human_gate_request_identity import (
    HumanGateRequestIdentity,
)
from lib.orchestration.human_gate_runtime import OrchestrationHumanGateRuntime
from lib.orchestration.human_gate_runtime_ports import HumanGateRequestPorts
from lib.orchestration_agent_runner import (
    OrchestrationAgentRunnerConfig,
    OrchestrationSubAgentRunner,
)
from lib.orchestration_budget import OrchestrationAgentBudget
from lib.orchestration_graph import FlowExecutionError, GraphNavigator
from lib.orchestration.outcome_ledger import OrchestrationOutcomeLedger
from lib.orchestration_plan import compile_plan
from lib.orchestration_dataflow import OrchestrationDataflow
from lib.orchestration_execution_runtime import OrchestrationExecutionRuntime
from lib.orchestration_feedback import (
    CARRY_ATTEMPT_CHARS as _CARRY_ATTEMPT_CHARS,
    CARRY_FEEDBACK_CHARS as _CARRY_FEEDBACK_CHARS,
    STUCK_JACCARD as _STUCK_JACCARD,
    OrchestrationFeedbackState,
)
from lib.orchestration_progress import (
    REPLAN_SUMMARY_CHARS as _PROGRESS_SUMMARY_CHARS,
    OrchestrationProgressLedger,
)
from lib.orchestration_runner_result import (
    OrchestrationAgentResult,
    OrchestrationAgentRunnerPort,
)
from lib.orchestration_role_runtime import OrchestrationRoleRuntime
from lib.orchestration_branch_runtime import OrchestrationBranchRuntime
from lib.orchestration_replan_runtime import OrchestrationReplanRuntime
from lib.orchestration_loop_runtime import (
    OrchestrationLoopAborted,
    OrchestrationLoopRuntime,
)
from lib.orchestration.loop_policy import (
    DEFAULT_EXECUTOR_MAX_ITERATIONS,
    bounded_executor_iterations,
)
from lib.orchestration_parallel_runtime import (
    OrchestrationParallelAborted,
    OrchestrationParallelRuntime,
)
from lib.orchestration_subflow_runtime import (
    OrchestrationSubflowAborted,
    OrchestrationSubflowRuntime,
)
from lib.orchestration_trace import (
    TRACE_INPUT_CHARS as _TRACE_INPUT_CHARS,
    TRACE_OUTPUT_CHARS as _TRACE_OUTPUT_CHARS,
    OrchestrationTraceRecorder,
)
from lib.orchestration_transcript import (
    OrchestrationTranscript,
)

logger = get_logger(__name__)

# Hard ceilings — defense against malformed graphs.
_DEFAULT_MAX_AGENTS = 200
_DEFAULT_PARALLEL = 8

# Roles that close a loop iteration by emitting a verdict. ``virtual_user``
# stands in for the human in autopilot mode: its reply (and its
# [VU: TASK_DONE] / [VERDICT: STOP] signal) drives the same loop boundary a
# critic does. The verdict heuristics below also recognise the VU sentinel.

# Autopilot completion and deliverable-tool policy are shared backend
# contracts. Runner result shape normalization lives at the dedicated
# ``orchestration_tool_usage`` boundary instead of in this interpreter.

# ── Replan branch (CONTINUE_PLANNER + PLAN_DEFECT gate) ──
# A loop's verifier may request a structural re-plan. The request
# MUST carry a [PLAN_DEFECT: ...] reason, and reasons that are really
# worker-execution complaints are rejected.
# Verdict parsing + gating (tag regexes, PLAN_DEFECT gate, STOP-with-
# unresolved-markers override, replan kill-switch and its cap) all live in
# ``lib.agent_verdict.classify_verdict`` now — see ``_classify_verdict``
# below, which adapts it to the engine's loose-fallback + virtual_user
# semantics.


class FlowExecutor:
    """Interpret and run one orchestration definition.

    Parameters
    ----------
    definition : dict
        A ``tofu.orchestration/v1`` graph. Validated on construction.
    agent_runner : OrchestrationAgentRunnerPort, optional
        Runs one agent node. Typed results are preferred; legacy mappings are
        normalized at the runner-result boundary. Defaults to the
        SubAgent-backed runner.
    on_event : callable(dict), optional
        Progress sink. Event shapes mirror the swarm SSE vocabulary so the
        frontend can reuse its renderer.
    abort_check : callable() -> bool, optional
        Return True to stop scheduling new work.
    """

    def __init__(self, definition: dict, *,
                 agent_runner: OrchestrationAgentRunnerPort | None = None,
                 on_event: Callable | None = None,
                 abort_check: Callable | None = None,
                 max_agents: int = _DEFAULT_MAX_AGENTS,
                 max_iterations: int = DEFAULT_EXECUTOR_MAX_ITERATIONS,
                 max_parallel: int = _DEFAULT_PARALLEL,
                 # forwarded to the default SubAgent runner
                 parent_task: dict | None = None,
                 all_tools: list | None = None,
                 model: str = '',
                 model_routing_policy: str = 'role_tier',
                 project_path: str = '',
                 system_prompt_base: str = '',
                 thinking_enabled: bool = True,
                 subflow_resolver: Callable | None = None,
                 human_gate_ports: HumanGateRequestPorts | None = None,
                 human_gate_scope: str = '',
                 human_gate_owner_user_id: int | None = None,
                 _agent_budget: OrchestrationAgentBudget | None = None,
                 _human_gate_identity: HumanGateRequestIdentity | None = None,
                 _subflow_depth: int = 0):
        verdict = validate_definition(definition)
        if not verdict['ok']:
            raise FlowExecutionError(
                'cannot execute invalid definition: ' + '; '.join(verdict['errors']))

        # Flatten any subflow ("big role" = small roles) nodes into one flat
        # graph the interpreter runs unchanged. Embedded subflows expand with
        # no resolver; ``params.ref`` subflows need ``subflow_resolver``.
        try:
            definition = expand_subflows(definition, resolver=subflow_resolver)
        except ValueError as e:
            raise FlowExecutionError(f'subflow expansion failed: {e}') from e

        self.defn = definition
        self.nodes: dict[str, dict] = {n['id']: n for n in definition.get('nodes', [])}
        self._on_event = on_event
        self._abort_check = abort_check or (lambda: False)
        self.max_agents = max(1, int(max_agents))
        self._agent_budget = _agent_budget or OrchestrationAgentBudget(
            self.max_agents)
        # Nested executors share the root budget; expose its canonical ceiling
        # in diagnostics even if a caller supplied a different local value.
        self.max_agents = self._agent_budget.limit
        self.max_iterations = bounded_executor_iterations(max_iterations)
        self.max_parallel = max(1, int(max_parallel))

        self._default_runner_config = OrchestrationAgentRunnerConfig(
            parent_task=parent_task,
            all_tools=all_tools or [],
            model=model,
            model_routing_policy=model_routing_policy,
            project_path=project_path,
            # Chat-launched flows inherit the resolved worker prompt. The
            # adapter must not silently drop project/system instructions.
            system_prompt_base=system_prompt_base or '',
            thinking_enabled=bool(thinking_enabled),
        )
        self._default_runner_adapter = OrchestrationSubAgentRunner(
            self._default_runner_config,
            emit=self._emit,
            abort_check=self._abort_check,
        )
        self._runner = agent_runner or self._default_runner
        # Whether a runner was explicitly injected (tests / custom). A nested
        # executor for an isolated subflow must reuse an injected runner, but
        # let a default-runner parent hand the child its OWN default runner
        # (bound-method identity is unreliable, so track it with a flag).
        self._custom_runner = agent_runner is not None
        # Resolver + current nesting depth, threaded into nested executors so
        # an ``isolated`` subflow (a black box) runs in its own FlowExecutor.
        self._subflow_resolver = subflow_resolver
        self._subflow_depth = int(_subflow_depth)
        self._human_gate_ports = human_gate_ports
        parent_owner_user_id = None
        if isinstance(parent_task, dict) and parent_task.get('_userId') is not None:
            from lib.tasks_pkg.manager import task_user_id
            parent_owner_user_id = task_user_id(parent_task)
        if human_gate_owner_user_id is not None:
            from lib.identity import require_user_id
            human_gate_owner_user_id = require_user_id(
                human_gate_owner_user_id,
                context='orchestration human gate owner',
            )
        if (parent_owner_user_id is not None
                and human_gate_owner_user_id is not None
                and parent_owner_user_id != human_gate_owner_user_id):
            raise ValueError(
                'orchestration human gate owner does not match parent task')
        self._human_gate_owner_user_id = (
            parent_owner_user_id
            if parent_owner_user_id is not None
            else human_gate_owner_user_id
        )
        parent_task_id = (str(parent_task.get('id') or '')
                          if isinstance(parent_task, dict) else '')
        self._human_gate_identity = (
            _human_gate_identity or HumanGateRequestIdentity(
                human_gate_scope or parent_task_id)
        )

        # Pure graph-topology queries (walk targets, loop body/exit, barrier
        # join, reachability and adjacency) live on the navigator.
        self._nav = GraphNavigator.from_edges(
            self.nodes,
            definition.get('edges', []),
        )
        self.fwd = self._nav.fwd
        self.rev = self._nav.rev

        self._agents_run = 0
        self._cur_iteration = 0   # active loop iteration (0 = outside a loop)
        self._lock = threading.Lock()
        self._transcript_ledger = OrchestrationTranscript(lock=self._lock)
        self._dataflow = OrchestrationDataflow(lock=self._lock)
        self._progress = OrchestrationProgressLedger(lock=self._lock)
        self._feedback = OrchestrationFeedbackState(lock=self._lock)
        self._outcomes = OrchestrationOutcomeLedger(lock=self._lock)
        self._trace_recorder = OrchestrationTraceRecorder(
            emit=self._emit,
            lock=self._lock,
        )
        self._role_runtime = OrchestrationRoleRuntime(
            budget=self._agent_budget,
            runner=self._runner,
            dataflow=self._dataflow,
            feedback=self._feedback,
            progress=self._progress,
            outcomes=self._outcomes,
            trace_recorder=self._trace_recorder,
            transcript=self._transcript_ledger,
            emit=self._emit,
            on_agent_claimed=self._increment_agents_run,
        )
        self._subflow_runtime = OrchestrationSubflowRuntime(
            budget=self._agent_budget,
            depth=self._subflow_depth,
            resolver=self._subflow_resolver,
            child_executor_factory=self._make_isolated_child,
            dataflow=self._dataflow,
            outcomes=self._outcomes,
            trace_recorder=self._trace_recorder,
            transcript=self._transcript_ledger,
            emit=self._emit,
            on_child_agents=self._increment_agents_run_by,
        )
        self._replan_runtime = OrchestrationReplanRuntime(
            nodes=self.nodes,
            progress=self._progress,
            transcript=self._transcript_ledger,
            run_role=self._run_role,
            verifier_roles=VERIFIER_ROLES,
            summary_limit=_PROGRESS_SUMMARY_CHARS,
        )
        self._loop_runtime = OrchestrationLoopRuntime(
            navigator=self._nav,
            nodes=self.nodes,
            max_iterations=self.max_iterations,
            feedback=self._feedback,
            progress=self._progress,
            outcomes=self._outcomes,
            transcript=self._transcript_ledger,
            emit=self._emit,
            abort_check=self._abort_check,
            walk=self._walk,
            run_replan=self._run_replan,
            classify_verdict=self._classify_verdict,
            progress_parser=lambda text: _parse_progress(text),
            on_iteration_change=self._set_current_iteration,
        )
        self._parallel_runtime = OrchestrationParallelRuntime(
            navigator=self._nav,
            branches=lambda node_id: list(self.fwd.get(node_id, [])),
            walk=self._walk,
            outcomes=self._outcomes,
            emit=self._emit,
            max_parallel=self.max_parallel,
            current_iteration=lambda: self._cur_iteration,
            abort_errors=(_AbortSignal,),
        )
        self._branch_runtime = OrchestrationBranchRuntime(
            navigator=self._nav,
            nodes=self.nodes,
            successors=lambda node_id: list(self.fwd.get(node_id, [])),
            run_classifier=lambda node, context: self._role_runtime.run_output(
                node, context, iteration=self._cur_iteration),
            emit=self._emit,
        )
        self._human_gates = OrchestrationHumanGateRuntime(
            emit=self._emit,
            abort_check=self._abort_check,
            ports=human_gate_ports,
            identity=self._human_gate_identity,
            owner_user_id=self._human_gate_owner_user_id,
        )
        self._execution_runtime = OrchestrationExecutionRuntime(
            definition=self.defn,
            nodes=self.nodes,
            navigator=self._nav,
            dataflow=self._dataflow,
            outcomes=self._outcomes,
            transcript=self._transcript_ledger,
            trace=self._trace_recorder,
            walk=lambda node_id, context: self._walk(node_id, context),
            agents_run=lambda: self.agents_run,
            emit=lambda event: self._emit(event),
            abort_errors=(_AbortSignal,),
        )

    # ── public entry ────────────────────────────────────────────────

    def run(self, *, initial_context: str = '') -> dict:
        """Execute the flow through the focused top-level lifecycle."""
        return self._execution_runtime.run(initial_context=initial_context)

    @property
    def trace(self) -> list[dict]:
        """The per-node run trace accumulated so far (live-readable).

        Each entry: ``{seq, node_id, role, name, kind, iteration, emits,
        isolation, subflow, brief, input, input_truncated, output,
        output_truncated, status, error, elapsed, state_changing,
        exploratory, state_changing_tools, ts}``. Powers the canvas /
        inspector overlay.
        """
        return self._trace_recorder.snapshot()

    @property
    def agents_run(self) -> int:
        """Number of leaf agents completed by this executor tree."""
        with self._lock:
            return self._agents_run

    # Compatibility patch points retained after ledger ownership moved into
    # OrchestrationOutcomeLedger. Production writes use its record methods.
    @property
    def _loop_exits(self) -> list[dict]:
        return self._outcomes.loop_exits_live

    @property
    def _node_failures(self) -> list[dict]:
        return self._outcomes.node_failures_live

    @property
    def _artifacts(self) -> list[dict]:
        return self._outcomes.artifacts_live

    # ── graph walk ──────────────────────────────────────────────────

    def _walk(self, node_id: str, context: str, *, stop_at: str | None = None) -> str:
        """Execute a linear chain from *node_id* until stop / stop_at / dead-end.

        Returns the accumulated context (latest output last). Control nodes
        (parallel / loop / branch) recurse into their sub-regions.
        """
        guard = 0
        while node_id and node_id != stop_at:
            if self._abort_check():
                raise _AbortSignal()
            guard += 1
            if guard > len(self.nodes) * (self.max_iterations + 2):
                raise FlowExecutionError('walk exceeded node budget — '
                                         'likely an unhandled cycle')
            node = self.nodes.get(node_id)
            if node is None:
                break
            kind = node.get('kind')
            ntype = node.get('type')

            if kind == 'stop':
                break
            if kind == 'start':
                node_id = self._nav.single_next(node_id)
                continue
            if ntype == 'role':
                context = self._run_role(node, context)
                node_id = self._nav.single_next(node_id)
                continue
            if ntype == 'subflow':
                # Only isolated subflows survive expand_subflows; inline ones
                # were already flattened into this graph.
                context = self._run_subflow_isolated(node, context)
                node_id = self._nav.single_next(node_id)
                continue
            if kind == 'parallel':
                context, node_id = self._run_parallel(node_id, context)
                continue
            if kind == 'barrier':
                node_id = self._nav.single_next(node_id)
                continue
            if kind == 'loop':
                context, node_id = self._run_loop(node_id, context)
                continue
            if kind == 'branch':
                node_id = self._run_branch(node_id, context)
                continue
            if kind == 'artifact':
                self._declare_artifact(node)
                node_id = self._nav.single_next(node_id)
                continue
            if kind == 'human':
                context, node_id = self._run_human(node, context)
                continue
            # Unknown node kind — skip defensively.
            logger.warning('[FlowEngine] skipping unknown node %s (kind=%s type=%s)',
                           node_id, kind, ntype)
            node_id = self._nav.single_next(node_id)
        return context

    def _run_role(self, node: dict, context: str) -> str:
        return self._role_runtime.run(
            node,
            context,
            iteration=self._cur_iteration,
        )

    def _increment_agents_run(self) -> None:
        self._increment_agents_run_by(1)

    def _increment_agents_run_by(self, count: int) -> None:
        with self._lock:
            self._agents_run += max(0, int(count))

    def _set_current_iteration(self, iteration: int) -> None:
        self._cur_iteration = max(0, int(iteration))

    def _run_subflow_isolated(self, node: dict, context: str) -> str:
        """Compatibility bridge to the isolated-subflow runtime."""
        try:
            return self._subflow_runtime.run(
                node,
                context,
                iteration=self._cur_iteration,
            )
        except OrchestrationSubflowAborted:
            raise _AbortSignal()

    def _make_isolated_child(self, definition: dict) -> 'FlowExecutor':
        """Construct a nested executor while replaying the parent ports."""
        return FlowExecutor(
            definition,
            agent_runner=self._runner if self._custom_runner else None,
            on_event=self._on_event,
            abort_check=self._abort_check,
            max_agents=self.max_agents,
            max_iterations=self.max_iterations,
            max_parallel=self.max_parallel,
            **self._default_runner_config.executor_options(),
            subflow_resolver=self._subflow_resolver,
            human_gate_ports=self._human_gate_ports,
            human_gate_owner_user_id=self._human_gate_owner_user_id,
            _agent_budget=self._agent_budget,
            _human_gate_identity=self._human_gate_identity,
            _subflow_depth=self._subflow_depth + 1,
        )

    def _aggregate_iter_producers(self) -> dict:
        """Compatibility proxy for the extracted producer progress ledger."""
        return self._progress.aggregate_iteration()

    def _append_deliverables_snapshot(self, context: str) -> str:
        """Compatibility proxy for verifier deliverables injection."""
        with self._lock:
            in_loop = self._cur_iteration > 0
        return self._progress.append_deliverables_snapshot(
            context,
            in_loop=in_loop,
        )

    @property
    def _iter_producers(self) -> list[dict]:
        """Compatibility view of current-iteration producer snapshots."""
        return self._progress.iteration_snapshot()

    @_iter_producers.setter
    def _iter_producers(self, snapshots: list[dict]) -> None:
        self._progress.replace_iteration(snapshots)

    @property
    def _last_producer_snapshot(self) -> dict:
        """Compatibility view of the latest producer snapshot."""
        return self._progress.latest_snapshot()

    @_last_producer_snapshot.setter
    def _last_producer_snapshot(self, snapshot: dict) -> None:
        self._progress.replace_latest(snapshot)

    @property
    def _node_memory(self) -> dict[str, str]:
        """Compatibility view of shared-context node attempt memory."""
        return self._feedback.node_memory_snapshot()

    @_node_memory.setter
    def _node_memory(self, memory: dict[str, str]) -> None:
        self._feedback.replace_node_memory(memory)

    @property
    def _pending_feedback(self) -> str:
        """Compatibility view of the next producer's reviewer feedback."""
        return self._feedback.pending_feedback()

    @_pending_feedback.setter
    def _pending_feedback(self, feedback: str) -> None:
        self._feedback.replace_pending_feedback(feedback)

    @property
    def _pending_directive(self) -> str:
        """Compatibility view of the next producer's guard directive."""
        return self._feedback.pending_directive()

    @_pending_directive.setter
    def _pending_directive(self, directive: str) -> None:
        self._feedback.replace_pending_directive(directive)

    @property
    def _feedback_history(self) -> list[str]:
        """Compatibility view of current-loop verifier feedback history."""
        return self._feedback.history_snapshot()

    @_feedback_history.setter
    def _feedback_history(self, history: list[str]) -> None:
        self._feedback.replace_history(history)

    @property
    def _vu_progress(self) -> list[dict]:
        """Compatibility view of current-loop virtual-user progress."""
        return self._feedback.vu_progress_snapshot()

    @_vu_progress.setter
    def _vu_progress(self, progress: list[dict]) -> None:
        self._feedback.replace_vu_progress(progress)

    def _run_parallel(self, pid: str, context: str) -> tuple[str, str]:
        """Compatibility bridge to the focused fan-out runtime."""
        try:
            return self._parallel_runtime.run(pid, context)
        except OrchestrationParallelAborted:
            raise _AbortSignal()

    def _run_loop(self, lid: str, context: str) -> tuple[str, str]:
        """Compatibility bridge to the verifier-loop runtime."""
        try:
            return self._loop_runtime.run(lid, context)
        except OrchestrationLoopAborted:
            raise _AbortSignal()

    def _run_replan(self, planner_id: str, context: str, defect: str | None,
                    replan: int) -> str:
        """Compatibility bridge to the focused structural re-plan runtime."""
        return self._replan_runtime.run(
            planner_id, context, defect, replan)

    def _run_branch(self, bid: str, context: str) -> str | None:
        """Compatibility bridge to the focused one-of-many router."""
        return self._branch_runtime.run(bid, context)

    def _declare_artifact(self, node: dict) -> None:
        """Record a declared deliverable (artifact node) and emit an event.

        Artifact nodes are inert in the data flow — they carry no agent and
        don't transform the context. They declare an *expected* intermediate
        output (path + description) so the run log shows what each stage is
        contracted to produce. Tracked in ``self._artifacts`` for the result.
        """
        params = node.get('params') or {}
        entry = {
            'node_id': node.get('id'),
            'name': node.get('name') or params.get('path') or 'deliverable',
            'path': params.get('path') or '',
            'format': params.get('format') or 'file',
            'description': params.get('description') or '',
        }
        self._outcomes.record_artifact(entry)
        self._emit({'type': 'artifact_declared', **entry})
        logger.info('[FlowEngine] artifact declared node=%s path=%r',
                    entry['node_id'], entry['path'])

    def _run_human(self, node: dict, context: str) -> tuple[str, str | None]:
        """Compatibility bridge to the focused human-gate runtime boundary."""
        result = self._human_gates.execute(node, context)
        if result.aborted:
            raise _AbortSignal()
        return result.context, self._nav.single_next(node.get('id'))

    # ── structure helpers ───────────────────────────────────────────
    # Pure graph-topology queries now live on ``self._nav``
    # (GraphNavigator): node_label / single_next / find_start / loop_parts /
    # find_loop_planner / find_common_barrier / reachable / can_reach / distance.

    # ── verdict / context ───────────────────────────────────────────

    def _classify_verdict(self, text: str, *, verifier_role: str = '') -> tuple:
        """Classify a verifier's output into ``(phase, plan_defect)``.

        ``phase`` ∈ {'stop','worker','planner'}; ``plan_defect`` is the
        gated structural reason (or None).  Adapts the shared
        :func:`lib.agent_verdict.classify_verdict` to the engine's needs:
        ``loose_fallback=True`` (a tag-free verifier still classifies via the
        plain-language STOP/CONTINUE heuristics — back-compat with plain
        critics and an empty verifier → STOP) and ``verifier_role`` so a
        ``virtual_user`` inverts the default (autopilot keeps the loop going
        unless the VU emits the [VU: TASK_DONE] sentinel or a STOP verdict).

        The STOP-with-unresolved-markers override, the [PLAN_DEFECT:] gate,
        and the TOFU_FLOW_REPLAN=0 kill-switch all live in the shared
        core — there is no longer an engine-local copy to drift.
        """
        res = _classify_verdict_core(
            text, verifier_role=verifier_role, loose_fallback=True)
        return res['phase'], res['plan_defect']

    # ── compatibility patch point for the extracted default adapter ─────

    def _default_runner(
        self, node: dict, context: str, iteration: int,
    ) -> OrchestrationAgentResult:
        """Compatibility patch point delegating to the production adapter."""
        return self._default_runner_adapter(node, context, iteration)

    # ── plumbing ────────────────────────────────────────────────────

    def _record(self, node_id, role, output, status, error, elapsed,
                *, sc_count=0, explore_count=0):
        self._transcript_ledger.record(
            node_id, role, output, status, error, elapsed,
            state_changing=sc_count,
            exploratory=explore_count,
        )

    def _emit(self, event: dict):
        if not self._on_event:
            return
        try:
            self._on_event(event)
        except Exception as e:
            logger.debug('[FlowEngine] on_event sink error: %s', e)

class _AbortSignal(Exception):
    """Internal — unwinds the walk when abort_check fires."""


__all__ = [
    'FlowExecutor',
    'FlowExecutionError',
    'compile_plan',
    '_TRACE_INPUT_CHARS',
    '_TRACE_OUTPUT_CHARS',
    '_CARRY_ATTEMPT_CHARS',
    '_CARRY_FEEDBACK_CHARS',
    '_STUCK_JACCARD',
    '_PROGRESS_SUMMARY_CHARS',
]
