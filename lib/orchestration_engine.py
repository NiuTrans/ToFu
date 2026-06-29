"""lib/orchestration_engine.py — Execute a tofu.orchestration/v1 graph.

This is the long-missing executor: it interprets a *validated* definition
(role nodes + control nodes + edges) and actually runs the agents, in the
topology the graph describes. It is the piece that finally unifies the two
hand-built orchestrators — endpoint mode (loop + verifier) and the swarm
(fan-out) — under one declarative engine.

Architecture
------------
The engine is a **graph interpreter**. It walks forward from the ``start``
node; agent execution is abstracted behind a single injectable
``agent_runner(node, context, iteration) -> {output, status, error}``.

  * Default runner builds a :class:`SubTaskSpec` + :class:`SubAgent` from
    the swarm substrate (``lib/swarm``) and runs it — same agents the
    swarm uses, with the same role→tool scoping + model tiers.
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
             the exit edge is taken. This IS endpoint mode expressed as data.
  branch   — picks ONE outgoing edge (v1: the first; a future classifier
             agent will choose). Documented limitation.
  stop     — terminal; returns the converged result.

Safety: total agent runs are capped (``max_agents``) and every loop has a
hard ``max_iterations`` cap, so a malformed graph can never spin forever.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib.agent_verdict import (
    STATE_CHANGING_TOOLS_WITH_CODE_EXEC as _STATE_CHANGING_TOOLS,
    classify_verdict as _classify_verdict_core,
    detect_stuck as _detect_stuck_core,
)
from lib.log import get_logger
from lib.orchestration import (
    IO_START_REF, MAX_SUBFLOW_DEPTH, expand_subflows,
    node_output_names, parse_io_ref, render_role_brief, resolve_emits,
    validate_definition,
)

logger = get_logger(__name__)

# Hard ceilings — defense against malformed graphs.
_DEFAULT_MAX_AGENTS = 200
_DEFAULT_MAX_ITERATIONS = 12
_DEFAULT_PARALLEL = 8

# Per-iteration carry-forward budget. Endpoint mode deliberately forwards a
# BOUNDED progress summary (not the full transcript) to avoid the
# analysis-spiral / context-bloat failure mode. We mirror that here.
_CARRY_ATTEMPT_CHARS = 1800
_CARRY_FEEDBACK_CHARS = 1800

# Per-node run-trace bounds. The trace is a durable record of WHAT each node
# saw and produced — for the canvas/inspector overlay, NOT for context
# carry-forward — so it can be generous, but still bounded so a giant tool
# dump never bloats the stored run. Input + output are capped independently.
_TRACE_INPUT_CHARS = 8000
_TRACE_OUTPUT_CHARS = 16000

# Roles that close a loop iteration by emitting a verdict. ``virtual_user``
# stands in for the human in autopilot mode: its reply (and its
# [VU: TASK_DONE] / [VERDICT: STOP] signal) drives the same loop boundary a
# critic does. The verdict heuristics below also recognise the VU sentinel.
_VERIFIER_ROLES = frozenset({'critic', 'reviewer', 'virtual_user'})

# ``_VU_DONE_SENTINEL`` (autopilot graceful-stop sentinel) and
# ``_STATE_CHANGING_TOOLS`` (the deliverable-tool set, flat-name variant
# WITH code_exec — the engine counts from a flat tool-name list) are now
# imported from ``lib.agent_verdict`` (the single source of truth).

# Endpoint's zero-deliverable guard: after this many consecutive producer
# turns with zero state-changing tool calls, the loop injects a directive
# forcing the producer to act (instead of looping on pure analysis).
_MAX_ZERO_DELIVERABLE_TURNS = 2
_ZERO_DELIVERABLE_DIRECTIVE = (
    'STOP ANALYZING — START EXECUTING. Your last attempts produced ZERO '
    'state-changing actions (no file writes, edits, or commands). Your very '
    'next step MUST be a concrete state-changing tool call that advances the '
    'plan. Do not just read, search, or describe.'
)

# ── Replan branch (endpoint CONTINUE_PLANNER + PLAN_DEFECT gate) ──
# A loop's verifier may request a structural re-plan. We mirror endpoint
# mode's gating (lib/tasks_pkg/endpoint_review._parse_verdict): the request
# MUST carry a [PLAN_DEFECT: ...] reason, and reasons that are really
# worker-execution complaints are rejected. Bounded by _MAX_REPLANS.
_MAX_REPLANS = 3
_PROGRESS_SUMMARY_CHARS = 2000

# Stuck detection (endpoint's _detect_stuck): if two consecutive verifier
# feedbacks are >_STUCK_JACCARD similar, the critic is repeating itself and
# the loop is not converging — break out instead of burning iterations.
# The threshold mirrors lib.agent_verdict.STUCK_JACCARD.
_STUCK_JACCARD = 0.60

# Verdict parsing + gating (tag regexes, PLAN_DEFECT gate, STOP-with-
# unresolved-markers override, replan kill-switch) all live in
# ``lib.agent_verdict.classify_verdict`` now — see ``_classify_verdict``
# below, which adapts it to the engine's loose-fallback + virtual_user
# semantics.


class FlowExecutionError(Exception):
    """Raised for structural problems discovered at execution time."""


class GraphNavigator:
    """Pure graph-topology queries over a flattened orchestration definition.

    Holds only the immutable structure — the node map and forward/reverse
    adjacency — built once from the (subflow-expanded) definition.  Every
    method here is a PURE function of that structure: no runtime state, no
    locks, no agent execution.  Extracted from ``FlowExecutor`` (2026-06-24)
    so the topology layer (walk targets, loop body/exit detection, barrier
    join, reachability/distance) is separable from the control-execution +
    verdict + tracing concerns that remain on the executor.

    The executor constructs one of these as ``self._nav`` and delegates all
    structural questions to it.
    """

    def __init__(self, nodes: dict[str, dict],
                 fwd: dict[str, list[str]], rev: dict[str, list[str]]):
        self.nodes = nodes
        self.fwd = fwd
        self.rev = rev

    def node_label(self, nid: str) -> str:
        n = self.nodes.get(nid) or {}
        return n.get('name') or n.get('role') or n.get('kind') or nid

    def single_next(self, node_id: str) -> str | None:
        nexts = self.fwd.get(node_id, [])
        return nexts[0] if nexts else None

    def find_start(self) -> str:
        for nid, n in self.nodes.items():
            if n.get('kind') == 'start':
                return nid
        # fall back to a source node
        for nid in self.nodes:
            if not self.rev.get(nid):
                return nid
        raise FlowExecutionError('no start node and no source node')

    def loop_parts(self, lid: str) -> tuple[str | None, str | None]:
        """Return (body_entry, exit_node) for a loop node.

        body_entry = a successor that can reach the loop again (cycle).
        exit_node  = the other successor (preferring one that reaches stop).
        """
        succ = list(self.fwd.get(lid, []))
        body, exit_n = None, None
        for s in succ:
            if self.can_reach(s, lid, avoid=lid):
                body = body or s
            else:
                exit_n = exit_n or s
        # Fallbacks if heuristic was inconclusive.
        if body is None and succ:
            body = succ[0]
        if exit_n is None:
            for s in succ:
                if s != body:
                    exit_n = s
                    break
        return body, exit_n

    def find_loop_planner(self, lid: str, body_entry: str | None) -> str | None:
        """Find the planner node feeding a loop.

        = a role predecessor of the loop node that is NOT part of the loop
        body (i.e. cannot be reached from body_entry without leaving via the
        loop). In the canonical endpoint graph (start→planner→loop,
        critic→loop) this isolates ``planner`` from the body's ``critic``.
        """
        for pred in self.rev.get(lid, []):
            n = self.nodes.get(pred) or {}
            if n.get('type') != 'role':
                continue
            if body_entry and self.can_reach(body_entry, pred, avoid=lid):
                continue   # this predecessor is inside the loop body (e.g. critic)
            return pred
        return None

    def find_common_barrier(self, branches: list[str]) -> str | None:
        """Find the nearest barrier node reachable from all branches."""
        if not branches:
            return None
        reach_sets = [self.reachable(b) for b in branches]
        common = set.intersection(*reach_sets) if reach_sets else set()
        barriers = [nid for nid in common if self.nodes[nid].get('kind') == 'barrier']
        if barriers:
            # nearest by BFS distance from first branch
            return min(barriers, key=lambda n: self.distance(branches[0], n))
        # fall back to any common node
        if common:
            return min(common, key=lambda n: self.distance(branches[0], n))
        return None

    def reachable(self, start: str) -> set[str]:
        seen, stack = set(), [start]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(self.fwd.get(n, []))
        return seen

    def can_reach(self, start: str, target: str, *, avoid: str = '') -> bool:
        seen, stack = set(), [start]
        while stack:
            n = stack.pop()
            if n == target:
                return True
            if n in seen or n == avoid and n != start:
                continue
            seen.add(n)
            for m in self.fwd.get(n, []):
                if m == target:
                    return True
                if m != avoid:
                    stack.append(m)
        return False

    def distance(self, start: str, target: str) -> int:
        from collections import deque
        q = deque([(start, 0)])
        seen = {start}
        while q:
            n, d = q.popleft()
            if n == target:
                return d
            for m in self.fwd.get(n, []):
                if m not in seen:
                    seen.add(m)
                    q.append((m, d + 1))
        return 1 << 30


class FlowExecutor:
    """Interpret and run one orchestration definition.

    Parameters
    ----------
    definition : dict
        A ``tofu.orchestration/v1`` graph. Validated on construction.
    agent_runner : callable(node, context, iteration) -> dict, optional
        Runs one agent node. Must return ``{'output': str, 'status': str,
        'error': str}``. Defaults to the SubAgent-backed runner.
    on_event : callable(dict), optional
        Progress sink. Event shapes mirror the swarm SSE vocabulary so the
        frontend can reuse its renderer.
    abort_check : callable() -> bool, optional
        Return True to stop scheduling new work.
    """

    def __init__(self, definition: dict, *,
                 agent_runner: Callable | None = None,
                 on_event: Callable | None = None,
                 abort_check: Callable | None = None,
                 max_agents: int = _DEFAULT_MAX_AGENTS,
                 max_iterations: int = _DEFAULT_MAX_ITERATIONS,
                 max_parallel: int = _DEFAULT_PARALLEL,
                 # forwarded to the default SubAgent runner
                 parent_task: dict | None = None,
                 all_tools: list | None = None,
                 model: str = '',
                 project_path: str = '',
                 subflow_resolver: Callable | None = None,
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
        self.max_iterations = max(1, int(max_iterations))
        self.max_parallel = max(1, int(max_parallel))

        # Default-runner config.
        self._parent_task = parent_task
        self._all_tools = all_tools or []
        self._model = model
        self._project_path = project_path

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

        # Forward / reverse adjacency.
        self.fwd: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        self.rev: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for e in definition.get('edges', []):
            s, d = e.get('from'), e.get('to')
            if s in self.nodes and d in self.nodes:
                self.fwd[s].append(d)
                self.rev[d].append(s)

        # Pure graph-topology queries (walk targets, loop body/exit, barrier
        # join, reachability) live on the navigator — see GraphNavigator.
        self._nav = GraphNavigator(self.nodes, self.fwd, self.rev)

        self._agents_run = 0
        self._transcript: list[dict] = []
        # ── Per-node run trace (the traceability axis) ──
        # One entry per executed node — the RESOLVED brief it ran with, a
        # bounded copy of its effective input context, its full (bounded)
        # output, emits/isolation/iteration, deliverable counts, and timing.
        # Powers the canvas/inspector overlay (what each node actually saw +
        # produced + the prompt it ran). Distinct from _transcript (which the
        # engine uses internally for progress summaries / deliverables).
        self._trace: list[dict] = []
        self._trace_seq = 0
        self._cur_iteration = 0   # active loop iteration (0 = outside a loop)
        self._lock = threading.Lock()

        # ── Shared-context memory ──
        # A node with params.isolation == 'shared-context' accumulates its
        # own prior-attempt lineage across loop iterations (like endpoint's
        # stateful worker). Keyed by node id; value is its last output.
        # Fresh-context nodes never read this. Bounded by _CARRY_ATTEMPT_CHARS.
        self._node_memory: dict[str, str] = {}
        # The latest verifier feedback, forwarded into the NEXT body
        # iteration (endpoint's feedback-injection). Reset per loop entry.
        self._pending_feedback: str = ''
        # A directive injected into the producer's NEXT context (set by the
        # loop's zero-deliverable guard). Consumed once, like feedback.
        self._pending_directive: str = ''
        # Deliverables snapshot of the most recent producer (non-verifier)
        # role turn — fed to the verifier so it can make the endpoint call.
        self._last_producer_snapshot: dict = {}
        # Verifier feedback history (per active loop) for stuck-detection.
        self._feedback_history: list[str] = []
        # Declared deliverables (artifact nodes) encountered during the walk.
        self._artifacts: list[dict] = []

        # ── Typed I/O store (the dataflow axis) ──
        # Per producer node: {output_name: value}. A node with no declared
        # io.outputs writes its turn under the implicit DEFAULT_OUTPUT_NAME
        # ('text'). A node that declares io.inputs reads ONLY the referenced
        # producer outputs (Dify-style), instead of the whole accumulating
        # scratchpad. Legacy flows (no io block anywhere) never touch this.
        self._io_outputs: dict[str, dict] = {}
        # The flow's initial seed context, referenceable as the 'start' input.
        self._initial_context: str = ''

    # ── public entry ────────────────────────────────────────────────

    def run(self, *, initial_context: str = '') -> dict:
        """Execute the flow. Blocking — call from a background thread.

        Returns ``{ok, status, final, transcript, agents_run, error}``.
        """
        start = self._nav.find_start()
        # The Start node may carry a `seed` (the request the author baked
        # into the canvas). An explicit initial_context (e.g. typed in the
        # Run panel) wins; otherwise the seed is the flow's entry input,
        # making Start a real self-contained entry point, not a bare marker.
        if not (initial_context or '').strip():
            start_node = self.nodes.get(start) or {}
            seed = (start_node.get('params') or {}).get('seed')
            if isinstance(seed, str) and seed.strip():
                initial_context = seed
        self._initial_context = initial_context or ''
        t0 = time.monotonic()
        self._emit({'type': 'flow_start', 'name': self.defn.get('name'),
                    'nodes': len(self.nodes)})
        logger.info('[FlowEngine] run START name=%r nodes=%d start=%s',
                    self.defn.get('name'), len(self.nodes), start)

        status, final, error = 'completed', '', None
        try:
            final = self._walk(start, initial_context)
        except _AbortSignal:
            status, error = 'aborted', 'aborted'
            logger.info('[FlowEngine] run ABORTED')
        except FlowExecutionError as e:
            status, error = 'failed', str(e)
            logger.error('[FlowEngine] structural failure: %s', e)
        except Exception as e:
            status, error = 'failed', f'{type(e).__name__}: {e}'
            logger.error('[FlowEngine] run crashed: %s', e, exc_info=True)

        elapsed = time.monotonic() - t0
        self._emit({'type': 'flow_complete', 'status': status,
                    'agents_run': self._agents_run, 'elapsed': round(elapsed, 1)})
        logger.info('[FlowEngine] run DONE status=%s agents=%d elapsed=%.1fs',
                    status, self._agents_run, elapsed)
        return {
            'ok': status == 'completed',
            'status': status,
            'final': final,
            'transcript': self._transcript,
            'trace': list(self._trace),
            'agents_run': self._agents_run,
            'artifacts': list(self._artifacts),
            'error': error,
        }

    @property
    def trace(self) -> list[dict]:
        """The per-node run trace accumulated so far (live-readable).

        Each entry: ``{seq, node_id, role, name, kind, iteration, emits,
        isolation, subflow, brief, input, input_truncated, output,
        output_truncated, status, error, elapsed, state_changing,
        exploratory, state_changing_tools, ts}``. Powers the canvas /
        inspector overlay.
        """
        with self._lock:
            return list(self._trace)

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
        with self._lock:
            if self._agents_run >= self.max_agents:
                raise FlowExecutionError(
                    f'agent budget exhausted ({self.max_agents})')
            self._agents_run += 1
        nid = node.get('id')
        role = node.get('role', 'general')
        params = node.get('params') or {}
        shared = params.get('isolation') == 'shared-context'
        is_verifier = role in _VERIFIER_ROLES
        # Message axis (orthogonal to role): does this turn land as a user
        # or assistant message? Explicit params.emits wins; else derived
        # from role (critic/reviewer/virtual_user → user, else assistant).
        emits = resolve_emits(node)

        # Build the effective per-call context.
        #  * A shared-context node sees its OWN prior attempt + any pending
        #    verifier feedback + any pending guard directive prepended — this
        #    is what makes a loop's worker stateful (endpoint behavior).
        #  * A verifier node gets a Deliverables Snapshot of the producer's
        #    latest turn appended, so it can apply the endpoint pre-verdict
        #    check ("0 state-changing calls → CONTINUE, don't replan").
        #  * A fresh-context non-verifier node sees only upstream context.
        #  * A node that declares params.io.inputs reads ONLY those wired
        #    producer outputs (typed dataflow), instead of the accumulating
        #    scratchpad — this is the Dify-style strict-input mode.
        eff_context = context
        typed_in = self._compose_typed_inputs(node)
        if typed_in is not None:
            eff_context = typed_in
        if shared:
            eff_context = self._compose_shared_context(
                nid, eff_context if typed_in is not None else context)
        if is_verifier:
            eff_context = self._append_deliverables_snapshot(eff_context)

        self._emit({'type': 'step_start', 'node_id': nid, 'role': role,
                    'name': node.get('name') or role, 'emits': emits,
                    'isolation': 'shared' if shared else 'fresh'})
        t0 = time.monotonic()
        try:
            res = self._runner(node, eff_context, 0) or {}
        except Exception as e:
            logger.error('[FlowEngine] agent runner crashed on %s: %s', nid, e, exc_info=True)
            res = {'output': '', 'status': 'failed', 'error': str(e)}
        out = str(res.get('output') or '')
        st = res.get('status') or 'completed'
        # Full streamed reasoning the runner accumulated (default SubAgent
        # runner). Carried through step_complete + step_trace so the turn's
        # finalized message / Task-Mode trace keep the thinking block.
        thinking = str(res.get('thinking') or '')

        # Count deliverables (state-changing tool calls) the runner reports.
        sc_count, explore_count, sc_names, reported = self._count_deliverables(res)
        _elapsed = time.monotonic() - t0
        self._record(nid, role, out, st, res.get('error') or '',
                     _elapsed, sc_count=sc_count,
                     explore_count=explore_count)
        # Durable per-node trace: the RESOLVED brief (rendered role prompt),
        # the bounded effective input, and the full bounded output — for the
        # canvas/inspector overlay. render_role_brief is pure (same text the
        # default runner sends as SubTaskSpec.objective).
        self._trace_node(
            node, brief=render_role_brief(node), eff_context=eff_context,
            output=out, status=st, error=res.get('error') or '',
            elapsed=_elapsed, emits=emits,
            isolation='shared' if shared else 'fresh',
            sc_count=sc_count, explore_count=explore_count, sc_names=sc_names,
            thinking=thinking)

        # Publish this node's typed outputs so downstream wired inputs can
        # read them. A node with no declared io.outputs exposes its turn as
        # the implicit 'text' output; an 'artifact'-typed output is filled
        # with a change manifest synthesized from the runner's tool log.
        self._publish_outputs(node, out, sc_names, explore_count)

        # Persist shared-context memory + capture verifier feedback / producer
        # snapshot for the next iteration. Pending feedback + directive are
        # consumed exactly once by the producer that reads them.
        if shared:
            with self._lock:
                self._node_memory[nid] = out
                self._pending_feedback = ''
                self._pending_directive = ''
        if is_verifier:
            with self._lock:
                self._pending_feedback = out
        else:
            # A producer (non-verifier) turn — record its deliverables so the
            # next verifier + the loop's zero-deliverable guard can use them.
            # ``reported`` distinguishes "ran 0 state-changing tools" from
            # "runner didn't report tool info" — the guard only fires on the
            # former, so mock runners without tool data never trip it.
            with self._lock:
                self._last_producer_snapshot = {
                    'node_id': nid, 'role': role, 'sc_count': sc_count,
                    'explore_count': explore_count, 'names': sc_names,
                    'reported': reported,
                }

        self._emit({'type': 'step_complete', 'node_id': nid, 'role': role,
                    'status': st, 'preview': out[:200], 'output': out,
                    'thinking': thinking,
                    'emits': emits, 'state_changing': sc_count})
        return self._append_context(context, role, out)

    def _run_subflow_isolated(self, node: dict, context: str) -> str:
        """Run an ``isolated`` subflow as a black box and append its result.

        This is the true nested scope (vs ``inline``, which ``expand_subflows``
        already flattened away before the engine ever sees it). The child runs
        in its OWN :class:`FlowExecutor` with a fresh context:

          * Input membrane — the child sees ONLY the upstream ``context`` as
            its seed (``run(initial_context=...)``). None of this engine's
            accumulated shared-context / verifier feedback / node memory
            crosses in.
          * Output membrane — the child's converged ``final`` is the only
            thing that crosses back out, appended to the parent context under
            the subflow's ``role`` label exactly like a role turn. The child's
            internal turns never enter the parent transcript.

        To the parent graph the subflow is indistinguishable from a role:
        it counts against ``max_agents``, emits ``step_start`` / ``step_complete``
        with its ``emits`` axis, and records one transcript entry. The nested
        executor reuses the entire engine (loop / parallel / verdict machinery
        all work inside the box for free); ``MAX_SUBFLOW_DEPTH`` bounds the
        recursion.
        """
        # The subflow is a CONTAINER, not an agent — its child's nested runs
        # are the real agents, folded into our count below. We only gate on
        # the budget here so an already-exhausted run can't open a new box.
        with self._lock:
            if self._agents_run >= self.max_agents:
                raise FlowExecutionError(
                    f'agent budget exhausted ({self.max_agents})')

        nid = node.get('id')
        role = node.get('role') or 'general'
        emits = resolve_emits(node)
        params = node.get('params') or {}

        if self._subflow_depth + 1 > MAX_SUBFLOW_DEPTH:
            raise FlowExecutionError(
                f'isolated subflow {nid!r} nesting exceeds '
                f'MAX_SUBFLOW_DEPTH ({MAX_SUBFLOW_DEPTH})')

        child = params.get('definition')
        if child is None:
            ref = params.get('ref')
            if not (self._subflow_resolver and ref):
                raise FlowExecutionError(
                    f'isolated subflow {nid!r} has ref {ref!r} but no '
                    'resolver was supplied')
            child = self._subflow_resolver(ref)
            if not isinstance(child, dict):
                raise FlowExecutionError(
                    f'isolated subflow {nid!r} ref {ref!r} did not resolve '
                    'to a definition')

        self._emit({'type': 'step_start', 'node_id': nid, 'role': role,
                    'name': node.get('name') or role, 'emits': emits,
                    'isolation': 'isolated', 'subflow': True})
        t0 = time.monotonic()
        logger.info('[FlowEngine] isolated subflow %s START role=%s depth=%d',
                    nid, role, self._subflow_depth + 1)

        child_engine = FlowExecutor(
            child,
            agent_runner=self._runner if self._custom_runner else None,
            on_event=self._on_event,
            abort_check=self._abort_check,
            max_agents=self.max_agents,
            max_iterations=self.max_iterations,
            max_parallel=self.max_parallel,
            parent_task=self._parent_task,
            all_tools=self._all_tools,
            model=self._model,
            project_path=self._project_path,
            subflow_resolver=self._subflow_resolver,
            _subflow_depth=self._subflow_depth + 1,
        )
        try:
            result = child_engine.run(initial_context=context)
        except _AbortSignal:
            raise
        except Exception as e:
            logger.error('[FlowEngine] isolated subflow %s crashed: %s',
                         nid, e, exc_info=True)
            result = {'ok': False, 'status': 'failed', 'final': '', 'error': str(e)}

        # The child's nested agent runs are already counted in ITS engine; fold
        # the count into ours so max_agents stays a global ceiling.
        with self._lock:
            self._agents_run += child_engine._agents_run

        # Only the child's converged DELIVERABLE crosses the membrane — not
        # its accumulated scratchpad (which carries inner role labels) and not
        # a trailing verifier verdict. That deliverable is the last producer
        # (non-verifier) turn; fall back to the raw final if the box had none.
        out = self._subflow_deliverable(result)
        st = result.get('status') or 'completed'
        if st == 'aborted':
            raise _AbortSignal()
        # Record + emit BEFORE deciding to halt, so the event stream + transcript
        # always show the box's outcome even when it fails.
        _sf_elapsed = time.monotonic() - t0
        self._record(nid, role, out, st, result.get('error') or '',
                     _sf_elapsed)
        # Durable trace for the black box (its child's internal turns are
        # traced by the nested engine; here we record the box's own I/O).
        self._trace_node(
            node, brief=render_role_brief(node), eff_context=context,
            output=out, status=st, error=result.get('error') or '',
            elapsed=_sf_elapsed, emits=emits, isolation='isolated',
            subflow=True)
        # The black box exposes its deliverable on the dataflow axis too, so a
        # downstream node can wire to its typed output like any role.
        self._publish_outputs(node, out, [], 0)
        self._emit({'type': 'step_complete', 'node_id': nid, 'role': role,
                    'status': st, 'preview': out[:200], 'output': out,
                    'emits': emits, 'subflow': True})
        logger.info('[FlowEngine] isolated subflow %s DONE status=%s', nid, st)
        # A STRUCTURAL failure of the box (could not execute the sub-graph) is
        # fatal — it must NOT silently hand an empty deliverable to the parent
        # walk. Propagate as the same FlowExecutionError the engine raises for
        # its own structural failures (budget, cycles), which surfaces as the
        # parent run's status='failed'. A box that *completed* with an empty
        # deliverable is a different, legitimate case and continues (matching
        # role semantics: "ran, produced nothing").
        if st == 'failed':
            raise FlowExecutionError(
                f'isolated subflow {nid!r} failed: '
                f'{result.get("error") or "no detail"}')
        return self._append_context(context, role, out)

    @staticmethod
    def _subflow_deliverable(result: dict) -> str:
        """Extract the value an isolated subflow exports across its membrane.

        A child engine's ``final`` is its accumulated context — it carries
        inner ``[role]`` block labels and may end on a verifier verdict
        (e.g. a critic's ``VERDICT: STOP``), neither of which should leak to
        the parent. The black box's real output is its last *producer*
        (non-verifier) turn. Falls back to the raw ``final`` when the child
        had no producer turn (e.g. an empty or all-verifier flow).
        """
        transcript = result.get('transcript') or []
        for entry in reversed(transcript):
            if entry.get('role') in _VERIFIER_ROLES:
                continue
            out = entry.get('output')
            if out:
                return str(out)
        return str(result.get('final') or '')

    def _count_deliverables(self, res: dict) -> tuple:
        """Count state-changing vs exploratory tool calls in a runner result.

        The runner may report tools either as ``tool_names`` (list[str]) or
        ``tool_log`` ([{tool|toolName}, ...] — SubAgentResult shape). Returns
        ``(state_changing_count, exploratory_count, state_changing_names,
        reported)`` where ``reported`` is True iff the runner actually
        supplied tool info. The zero-deliverable guard fires only when
        ``reported`` is True — so a runner that omits tool data (e.g. a bare
        mock) is never mistaken for an analysis-paralysed producer.
        """
        reported = ('tool_names' in res) or ('tool_log' in res)
        names = res.get('tool_names')
        if names is None:
            names = []
            for entry in (res.get('tool_log') or []):
                if isinstance(entry, dict):
                    names.append(entry.get('tool') or entry.get('toolName') or '')
                elif isinstance(entry, str):
                    names.append(entry)
        sc, explore = [], 0
        for n in names:
            if n in _STATE_CHANGING_TOOLS:
                sc.append(n)
            elif n:
                explore += 1
        return len(sc), explore, sc, reported

    def _append_deliverables_snapshot(self, context: str) -> str:
        """Append the producer's latest-turn deliverables block for a verifier.

        Mirrors endpoint's _format_deliverables_snapshot: tells the verifier
        how many state-changing vs exploratory calls the producer just made,
        with the endpoint pre-verdict hint when the producer did zero work.
        """
        with self._lock:
            snap = dict(self._last_producer_snapshot)
        if not snap or not snap.get('reported'):
            return context
        sc = snap.get('sc_count', 0)
        names = snap.get('names') or []
        counts: dict[str, int] = {}
        for n in names:
            counts[n] = counts.get(n, 0) + 1
        names_str = ', '.join(f'{n}×{c}' if c > 1 else n
                              for n, c in counts.items()) or '(none)'
        if sc == 0:
            hint = ('GUIDANCE: the producer made ZERO state-changing calls '
                    'this turn. The correct verdict is almost always '
                    'CONTINUE with "execute, stop analyzing" feedback.')
        else:
            hint = ('GUIDANCE: the producer made real edits — verify they '
                    'close the checklist before approving.')
        block = (
            '\n\n───── Deliverables Snapshot (engine-injected) ─────\n'
            f'- Producer latest turn: {sc} state-changing, '
            f'{snap.get("explore_count", 0)} exploratory tool calls.\n'
            f'- State-changing calls: {names_str}\n'
            f'- {hint}\n'
            '───────────────────────────────────────────────────'
        )
        return context + block

    def _publish_outputs(self, node: dict, out: str, sc_names: list,
                         explore_count: int) -> None:
        """Record a producer node's typed outputs into the I/O store.

        A node with no declared ``io.outputs`` publishes its turn text under
        the implicit ``text`` output. A node that declares named outputs maps
        each one to a value by its declared ``type``:

          * ``artifact`` / ``file`` → a synthesized CHANGE MANIFEST built from
            the state-changing tool calls the runner reported (this is how a
            tool-heavy worker exposes its many intermediate operations as ONE
            machine-readable output, instead of a prose blob).
          * everything else (``text`` / ``json`` / ``number`` / ``bool`` /
            ``any``) → the turn text. (Strong typing of those is a future
            phase; today the engine threads strings.)

        Cheap + lock-guarded. Never raises.
        """
        nid = node.get('id')
        if not nid:
            return
        outs = node_output_names(node)
        io = (node.get('params') or {}).get('io')
        type_by_name: dict = {}
        if isinstance(io, dict) and isinstance(io.get('outputs'), list):
            for o in io['outputs']:
                if isinstance(o, dict) and isinstance(o.get('name'), str):
                    type_by_name[o['name']] = o.get('type')
        manifest = None
        values: dict = {}
        for name in outs:
            otype = type_by_name.get(name)
            if otype in ('artifact', 'file'):
                if manifest is None:
                    manifest = self._build_change_manifest(sc_names, explore_count)
                values[name] = manifest
            else:
                values[name] = out
        with self._lock:
            self._io_outputs[nid] = values

    @staticmethod
    def _build_change_manifest(sc_names: list, explore_count: int) -> str:
        """Synthesize a worker's change manifest from its tool log.

        Turns the raw state-changing tool calls into a compact, deterministic
        list — the typed ``artifact`` output a tool-heavy worker exposes. A
        downstream packager / notifier wires to THIS instead of re-parsing the
        worker's prose.
        """
        counts: dict[str, int] = {}
        for n in (sc_names or []):
            counts[n] = counts.get(n, 0) + 1
        if not counts:
            return ('## Change manifest\n(no state-changing actions; '
                    f'{explore_count} exploratory calls)')
        lines = [f'- {tool} ×{c}' if c > 1 else f'- {tool}'
                 for tool, c in sorted(counts.items())]
        total = sum(counts.values())
        return ('## Change manifest\n'
                f'{total} state-changing action(s), '
                f'{explore_count} exploratory:\n' + '\n'.join(lines))

    def _compose_typed_inputs(self, node: dict):
        """Compose a node's effective context from its declared ``io.inputs``.

        Returns the assembled context string when the node declares at least
        one input port, else ``None`` (signalling the caller to keep the
        legacy accumulating-scratchpad behavior). Each input pulls its wired
        producer output from the I/O store (``'<id>'`` / ``'<id>.<out>'`` /
        the literal ``'start'`` seed); an unresolved ref contributes nothing
        but is logged. Inputs render as labeled sections so the downstream
        agent sees a clean, named bundle rather than one opaque blob.
        """
        io = (node.get('params') or {}).get('io')
        if not isinstance(io, dict):
            return None
        inputs = io.get('inputs')
        if not isinstance(inputs, list) or not inputs:
            return None

        parts: list[str] = []
        with self._lock:
            store = dict(self._io_outputs)
            seed = self._initial_context
        for port in inputs:
            if not isinstance(port, dict):
                continue
            frm = port.get('from')
            label = port.get('name') or 'input'
            if not isinstance(frm, str) or not frm.strip():
                continue
            src_id, src_out = parse_io_ref(frm)
            if src_id == IO_START_REF:
                val = seed
            else:
                produced = store.get(src_id) or {}
                if src_out is not None:
                    val = produced.get(src_out)
                else:
                    # Primary output = the producer's first declared port
                    # (or the implicit 'text'). dict insertion order holds it.
                    val = next(iter(produced.values()), None) if produced else None
            if val is None or val == '':
                logger.debug('[FlowEngine] typed input %r on %s unresolved '
                             '(from=%r)', label, node.get('id'), frm)
                continue
            parts.append(f'## {label}\n{val}')
        return '\n\n'.join(parts)

    def _compose_shared_context(self, nid: str, upstream: str) -> str:
        """Build a shared-context node's effective input.

        = upstream context + its own bounded last attempt + the latest
        verifier feedback. Bounded so a long loop never spirals the context.
        """
        with self._lock:
            prior = self._node_memory.get(nid, '')
            feedback = self._pending_feedback
            directive = self._pending_directive
        parts = []
        if upstream:
            parts.append(upstream)
        if prior:
            parts.append('## Your previous attempt\n'
                         + prior[-_CARRY_ATTEMPT_CHARS:])
        if feedback:
            parts.append('## Reviewer feedback to address\n'
                         + feedback[-_CARRY_FEEDBACK_CHARS:])
        if directive:
            parts.append('## ⚠️ Directive\n' + directive)
        return '\n\n'.join(parts)

    def _run_parallel(self, pid: str, context: str) -> tuple[str, str]:
        """Run every branch of a parallel node concurrently; join at barrier."""
        branches = list(self.fwd.get(pid, []))
        barrier = self._nav.find_common_barrier(branches)
        self._emit({'type': 'parallel_start', 'node_id': pid,
                    'branches': len(branches)})
        logger.info('[FlowEngine] parallel %s → %d branches, barrier=%s',
                    pid, len(branches), barrier)

        outputs: list[str] = []
        # Each branch is a linear chain from the branch entry up to (not
        # including) the barrier. Runs concurrently.
        def _run_branch_chain(entry: str) -> str:
            return self._walk(entry, context, stop_at=barrier)

        if len(branches) == 1:
            outputs.append(_run_branch_chain(branches[0]))
        else:
            with ThreadPoolExecutor(max_workers=min(self.max_parallel, max(1, len(branches))),
                                    thread_name_prefix='flow-par') as pool:
                futs = {pool.submit(_run_branch_chain, b): b for b in branches}
                for fut in as_completed(futs):
                    try:
                        outputs.append(fut.result())
                    except _AbortSignal:
                        raise
                    except Exception as e:
                        logger.error('[FlowEngine] parallel branch %s failed: %s',
                                     futs[fut], e, exc_info=True)

        merged = context
        for o in outputs:
            merged = o if not merged else merged + '\n\n' + o
        nxt = self._nav.single_next(barrier) if barrier else None
        return merged, nxt

    def _run_loop(self, lid: str, context: str) -> tuple[str, str]:
        """Run the loop body until verifier STOP or max_iterations; take exit.

        Honors three verdict outcomes from the verifier (see
        ``_classify_verdict``):
          * stop    — leave the loop via the exit edge.
          * worker  — iterate again (default CONTINUE).
          * planner — a GATED structural re-plan: re-run the loop's planner
            node with the [PLAN_DEFECT] reason + a bounded progress summary,
            then iterate. Capped by ``_MAX_REPLANS`` so a flapping critic
            can't replan forever (endpoint's CONTINUE_PLANNER behavior).
        """
        body_entry, exit_node = self._nav.loop_parts(lid)
        planner_id = self._nav.find_loop_planner(lid, body_entry)
        node = self.nodes[lid]
        cap = min(self.max_iterations, int((node.get('params') or {}).get('max_iterations') or self.max_iterations))
        cap = max(1, cap)
        self._emit({'type': 'loop_start', 'node_id': lid, 'max_iterations': cap,
                    'planner': planner_id})
        logger.info('[FlowEngine] loop %s body=%s exit=%s planner=%s cap=%d',
                    lid, body_entry, exit_node, planner_id, cap)

        with self._lock:
            self._pending_feedback = ''
            self._pending_directive = ''
            self._feedback_history = []
        zero_streak = 0
        replans = 0
        for i in range(cap):
            if self._abort_check():
                raise _AbortSignal()
            self._cur_iteration = i + 1
            self._emit({'type': 'loop_iteration', 'node_id': lid,
                        'iteration': i + 1, 'max': cap})
            # Run the body chain, stopping when it loops back to the loop node.
            context = self._walk(body_entry, context, stop_at=lid)

            # ── Zero-deliverable guard (endpoint-faithful) ──
            snap = self._last_producer_snapshot
            if snap and snap.get('reported') and snap.get('sc_count', 0) == 0:
                zero_streak += 1
            else:
                zero_streak = 0

            if zero_streak >= _MAX_ZERO_DELIVERABLE_TURNS and i + 1 < cap:
                with self._lock:
                    self._pending_directive = _ZERO_DELIVERABLE_DIRECTIVE
                self._emit({'type': 'zero_deliverable_guard', 'node_id': lid,
                            'iteration': i + 1, 'streak': zero_streak})
                logger.info('[FlowEngine] loop %s zero-deliverable guard fired '
                            '(streak=%d) — forcing CONTINUE with directive',
                            lid, zero_streak)
                zero_streak = 0
                continue   # force another iteration, skip the verdict check

            verifier_out = self._last_verifier_output()
            self._feedback_history.append(verifier_out)
            phase, defect = self._classify_verdict(
                verifier_out, verifier_role=self._last_verifier_role())

            if phase == 'stop':
                logger.info('[FlowEngine] loop %s STOP after iteration %d', lid, i + 1)
                break

            # ── Stuck detection: a repeating critic means no convergence ──
            if self._detect_stuck() and i + 1 < cap:
                self._emit({'type': 'stuck_detected', 'node_id': lid,
                            'iteration': i + 1})
                logger.info('[FlowEngine] loop %s STUCK (repeating feedback) — '
                            'breaking after iteration %d', lid, i + 1)
                break

            if (phase == 'planner' and planner_id and replans < _MAX_REPLANS
                    and i + 1 < cap):
                replans += 1
                self._emit({'type': 'replan', 'node_id': lid,
                            'planner': planner_id, 'replan': replans,
                            'defect': (defect or '')[:200]})
                logger.info('[FlowEngine] loop %s REPLAN #%d (defect=%r) → '
                            're-running planner %s', lid, replans, defect, planner_id)
                context = self._run_replan(planner_id, context, defect, replans)
                continue

            # phase == 'worker' (or planner exhausted/downgraded) → iterate.
        else:
            logger.info('[FlowEngine] loop %s hit cap %d', lid, cap)

        self._cur_iteration = 0   # left the loop — subsequent nodes are post-loop
        return context, exit_node

    def _run_replan(self, planner_id: str, context: str, defect: str | None,
                    replan: int) -> str:
        """Re-run the planner on a structural defect, carrying bounded progress.

        Endpoint's replan path: the planner sees the defect + a compact
        progress summary (so the worker doesn't re-explore from scratch) and
        is told to produce a DELTA, not grow the plan. The summary is bounded
        to avoid the context-bloat that drove the original analysis-spiral.
        """
        planner_node = dict(self.nodes[planner_id])
        progress = self._build_progress_summary()
        replan_ctx_parts = [context] if context else []
        if defect:
            replan_ctx_parts.append('## Structural plan defect to fix\n' + defect)
        if progress:
            replan_ctx_parts.append('## Progress so far (do NOT discard — '
                                    'produce a DELTA, do not regrow the plan)\n'
                                    + progress)
        replan_ctx = '\n\n'.join(replan_ctx_parts)
        # Tag the planner objective so it knows this is a re-plan. Render the
        # full structured brief first, then append the re-plan directive into
        # the objective field (the renderer treats objective as the lead).
        params = dict(planner_node.get('params') or {})
        base_brief = render_role_brief(planner_node) or 'Plan the work.'
        params['objective'] = (
            base_brief
            + f'\n\n[RE-PLAN #{replan}] Address the structural defect above and '
            'produce a minimal DELTA to the existing plan — do not rewrite or '
            'grow it.')
        # Drop the other structured fields: they are already folded into
        # base_brief, so leaving them would double-render on the next pass.
        for _k in list(params.keys()):
            if _k not in ('objective', 'tier', 'isolation', 'emits', 'name'):
                params.pop(_k, None)
        planner_node['params'] = params
        return self._run_role(planner_node, replan_ctx)

    def _build_progress_summary(self) -> str:
        """Compact, bounded summary of producer progress for a re-plan.

        Walks the transcript for producer (non-verifier) turns and lists
        their state-changing tool counts + a short output preview. Bounded by
        ``_PROGRESS_SUMMARY_CHARS`` so re-plans never balloon the context.
        """
        lines: list[str] = []
        with self._lock:
            entries = list(self._transcript)
        for e in entries:
            if e.get('role') in _VERIFIER_ROLES:
                continue
            sc = e.get('state_changing', 0)
            preview = (e.get('output') or '').strip().replace('\n', ' ')[:160]
            lines.append(f'- {e.get("role")}: {sc} state-changing calls. {preview}')
        summary = '\n'.join(lines)
        return summary[-_PROGRESS_SUMMARY_CHARS:]

    def _run_branch(self, bid: str, context: str) -> str | None:
        """Pick one outgoing edge.

        If the branch node names a ``classifier`` role, run it as an agent
        and match its answer against the candidate targets (by node name /
        id / role); otherwise (or on no match) fall back to the first edge.
        """
        nexts = self.fwd.get(bid, [])
        if not nexts:
            self._emit({'type': 'branch_pick', 'node_id': bid,
                        'chosen': None, 'options': 0})
            return None

        node = self.nodes[bid]
        params = node.get('params') or {}
        classifier_role = params.get('classifier')
        chosen = nexts[0]
        how = 'first-edge'

        if classifier_role and len(nexts) > 1:
            choice_labels = {t: self._nav.node_label(t) for t in nexts}
            prompt = (
                f'{context}\n\n## Routing decision\n'
                f'Choose exactly ONE next step by replying with its label.\n'
                'Options: '
                + ', '.join(f'{lbl!r}' for lbl in choice_labels.values()))
            classifier_node = {
                'id': f'{bid}__classifier', 'type': 'role', 'role': classifier_role,
                'name': f'{node.get("name") or "branch"} classifier',
                'params': {'objective': prompt, 'tier': params.get('tier') or 'light'},
            }
            picked = self._run_role(classifier_node, context)
            verdict = picked.lower()
            for target, label in choice_labels.items():
                if label and label.lower() in verdict:
                    chosen, how = target, 'classifier'
                    break

        self._emit({'type': 'branch_pick', 'node_id': bid, 'chosen': chosen,
                    'options': len(nexts), 'how': how})
        logger.info('[FlowEngine] branch %s → %s (of %d, %s)',
                    bid, chosen, len(nexts), how)
        return chosen

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
        with self._lock:
            self._artifacts.append(entry)
        self._emit({'type': 'artifact_declared', **entry})
        logger.info('[FlowEngine] artifact declared node=%s path=%r',
                    entry['node_id'], entry['path'])

    def _run_human(self, node: dict, context: str) -> tuple[str, str | None]:
        """Execute a human-in-the-loop gate.

        Reuses the SAME plumbing the live chat task uses — no parallel
        implementation:
          * ``approve`` → :func:`request_write_approval` (the file-write
            confirmation primitive). Rejection halts the flow.
          * ``input``   → :func:`request_human_guidance` (the ``ask_human``
            primitive). The answer is appended to the flow context so
            downstream agents can read it.
          * ``notify``  → non-blocking; just surfaces a message and
            continues.

        Returns ``(context, next_node_id)``.
        """
        params = node.get('params') or {}
        mode = params.get('mode') or 'approve'
        prompt = (params.get('prompt') or '').strip()
        nid = node.get('id')
        label = node.get('name') or 'Human'
        req_id = f'orch_{nid}_{self._human_seq()}'

        if mode == 'notify':
            self._emit({'type': 'human_notify', 'node_id': nid, 'name': label,
                        'prompt': prompt})
            logger.info('[FlowEngine] human notify node=%s', nid)
            return context, self._nav.single_next(nid)

        # Abort-aware task shim so the blocking primitives unblock when the
        # run is aborted (they probe task.get('aborted')).
        task_shim = _AbortAwareShim(self._abort_check, req_id)
        self._emit({'type': 'human_request', 'node_id': nid, 'name': label,
                    'mode': mode, 'prompt': prompt, 'request_id': req_id})
        logger.info('[FlowEngine] human gate node=%s mode=%s req=%s blocking',
                    nid, mode, req_id)

        if mode == 'input':
            from lib.tasks_pkg.human_guidance import request_human_guidance
            answer = request_human_guidance(req_id, task=task_shim)
            if answer is None:
                logger.info('[FlowEngine] human input %s aborted/cancelled', req_id)
                raise _AbortSignal()
            self._emit({'type': 'human_resolved', 'node_id': nid, 'mode': mode,
                        'request_id': req_id, 'preview': answer[:200]})
            block = f'[Human input — {label}]\n{answer}'
            context = (context + '\n\n' + block) if context else block
            return context, self._nav.single_next(nid)

        # mode == 'approve'
        from lib.tasks_pkg.approval import request_write_approval
        timeout = params.get('timeout_sec')
        try:
            timeout = int(timeout) if timeout not in (None, '') else 300
        except (ValueError, TypeError):
            timeout = 300
        approved = request_write_approval(req_id, timeout=timeout)
        self._emit({'type': 'human_resolved', 'node_id': nid, 'mode': mode,
                    'request_id': req_id, 'approved': approved})
        if not approved:
            logger.info('[FlowEngine] human gate %s NOT approved — halting flow', req_id)
            raise _AbortSignal()
        logger.info('[FlowEngine] human gate %s approved — continuing', req_id)
        return context, self._nav.single_next(nid)

    def _human_seq(self) -> int:
        with self._lock:
            self._human_counter = getattr(self, '_human_counter', 0) + 1
            return self._human_counter

    # ── structure helpers ───────────────────────────────────────────
    # Pure graph-topology queries now live on ``self._nav``
    # (GraphNavigator): node_label / single_next / find_start / loop_parts /
    # find_loop_planner / find_common_barrier / reachable / can_reach / distance.

    # ── verdict / context ───────────────────────────────────────────

    def _last_verifier_output(self) -> str:
        for entry in reversed(self._transcript):
            if entry.get('role') in _VERIFIER_ROLES:
                return entry.get('output') or ''
        return self._transcript[-1].get('output') if self._transcript else ''

    def _last_verifier_role(self) -> str:
        for entry in reversed(self._transcript):
            if entry.get('role') in _VERIFIER_ROLES:
                return entry.get('role') or ''
        return ''

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
        and the TOFU_ENDPOINT_REPLAN=0 kill-switch all live in the shared
        core — there is no longer an engine-local copy to drift.
        """
        res = _classify_verdict_core(
            text, verifier_role=verifier_role, loose_fallback=True)
        return res['phase'], res['plan_defect']

    def _detect_stuck(self) -> bool:
        """True if the last two verifier feedbacks are >_STUCK_JACCARD similar.

        Delegates to the shared :func:`lib.agent_verdict.detect_stuck` — a
        repeating critic means the loop is not converging; the loop breaks
        out rather than burning iterations.
        """
        return _detect_stuck_core(self._feedback_history, threshold=_STUCK_JACCARD)

    def _append_context(self, context: str, role: str, out: str) -> str:
        block = f'[{role}]\n{out}'.strip()
        return block if not context else context + '\n\n' + block

    # ── default SubAgent-backed runner ──────────────────────────────

    def _default_runner(self, node: dict, context: str, iteration: int) -> dict:
        """Run one role node as a swarm SubAgent. Blocking."""
        from lib.swarm.agent import SubAgent
        from lib.swarm.protocol import SubAgentStatus, SubTaskSpec

        params = node.get('params') or {}
        spec = SubTaskSpec(
            role=node.get('role', 'general'),
            objective=(render_role_brief(node) or node.get('name')
                       or 'Execute this step.'),
            context=context,
            model_tier=params.get('tier') or 'standard',
        )
        parent = self._parent_task or {
            'id': 'flow', 'convId': 'flow',
            'events_lock': threading.Lock(), 'events': [],
            'toolRounds': [], 'phase': 'tool', 'config': {},
        }
        # Live token streaming: forward each content/thinking delta as a
        # ``step_delta`` engine event tagged with this node's id/role/emits, so
        # a consumer (EndpointEventAdapter) can stream the turn into the chat
        # bubble live — identical to a first-class agent turn — instead of
        # waiting for the whole turn and showing only step_complete.
        nid = node.get('id')
        role = node.get('role', 'general')
        emits = resolve_emits(node)

        # Accumulate the FULL streamed thinking for this node so the turn's
        # finalized message can carry it (identical to a first-class agent
        # turn). We capture it from the live stream rather than from
        # SubAgentResult.reasoning_trace because the latter is truncated to
        # 2000 chars/round — the live chunks are the complete reasoning.
        _thinking_parts: list[str] = []

        def _stream_sink(kind: str, chunk: str, *, phase: str = '', **meta):
            # 'content'/'thinking' → a step_delta (streamed output chunk).
            # 'phase' → a step_phase (transient status: "waiting for model…" /
            #   "retrying…" while the agent's dispatch is in flight). Both are
            #   ENGINE-INTERNAL events (self._emit, NOT the registered wire
            #   contract); the EndpointEventAdapter translates them to wire
            #   delta / phase events. Keeping step_phase out of the registry
            #   mirrors step_delta/step_trace (see the orphan-event test).
            if kind == 'phase':
                self._emit({'type': 'step_phase', 'node_id': nid, 'role': role,
                            'emits': emits, 'phase': phase or 'working',
                            'detail': chunk, **meta})
            else:
                if kind == 'thinking' and chunk:
                    _thinking_parts.append(chunk)
                self._emit({'type': 'step_delta', 'node_id': nid, 'role': role,
                            'emits': emits, 'kind': kind, 'chunk': chunk})

        agent = SubAgent(
            spec,
            parent_task=parent,
            all_tools=self._all_tools,
            model=self._model,
            abort_check=self._abort_check,
            project_path=self._project_path,
            stream_sink=_stream_sink,
        )
        result = agent.run()
        return {
            'output': result.final_answer or '',
            'status': result.status,
            'error': result.error_message if result.status != SubAgentStatus.COMPLETED.value else '',
            # tool_log = [{round, tool, args_brief}, ...] — fed to the
            # engine's deliverables counter (state-changing vs exploratory).
            'tool_log': result.tool_log or [],
            # Full streamed reasoning for this node — carried through
            # step_complete / step_trace so the turn's finalized message
            # keeps its thinking block (parity with a first-class agent turn).
            'thinking': ''.join(_thinking_parts),
        }

    # ── plumbing ────────────────────────────────────────────────────

    def _record(self, node_id, role, output, status, error, elapsed,
                *, sc_count=0, explore_count=0):
        with self._lock:
            self._transcript.append({
                'node_id': node_id, 'role': role, 'output': output,
                'status': status, 'error': error, 'elapsed': round(elapsed, 2),
                'state_changing': sc_count, 'exploratory': explore_count,
            })

    def _trace_node(self, node: dict, *, brief: str, eff_context: str,
                    output: str, status: str, error: str, elapsed: float,
                    emits: str, isolation: str, sc_count: int = 0,
                    explore_count: int = 0, sc_names: list | None = None,
                    subflow: bool = False, thinking: str = '') -> None:
        """Append one durable per-node trace entry.

        Captures everything the canvas/inspector overlay needs to explain a
        run: the RESOLVED delegation brief the node actually ran with (the
        rendered role prompt — answers "what is this role doing?"), a bounded
        copy of its effective input context, its bounded full output, the
        message axis / isolation / loop iteration, and deliverable counts +
        timing. Lock-guarded; never raises (a trace failure must not abort a
        run).
        """
        try:
            nid = node.get('id')
            entry = {
                'seq': 0,  # filled under lock
                'node_id': nid,
                'role': node.get('role') or '',
                'name': node.get('name') or '',
                'kind': node.get('type') or '',
                'iteration': self._cur_iteration,
                'emits': emits,
                'isolation': isolation,
                'subflow': bool(subflow),
                'brief': (brief or '')[:_TRACE_INPUT_CHARS],
                'input': (eff_context or '')[:_TRACE_INPUT_CHARS],
                'input_truncated': len(eff_context or '') > _TRACE_INPUT_CHARS,
                'output': (output or '')[:_TRACE_OUTPUT_CHARS],
                'output_truncated': len(output or '') > _TRACE_OUTPUT_CHARS,
                'thinking': (thinking or '')[:_TRACE_OUTPUT_CHARS],
                'thinking_truncated': len(thinking or '') > _TRACE_OUTPUT_CHARS,
                'status': status,
                'error': error or '',
                'elapsed': round(elapsed, 2),
                'state_changing': sc_count,
                'exploratory': explore_count,
                'state_changing_tools': list(sc_names or []),
                'ts': _now_iso(),
            }
            with self._lock:
                self._trace_seq += 1
                entry['seq'] = self._trace_seq
                self._trace.append(entry)
            # Emit the entry as a durable ``step_trace`` event so a reopenable
            # run (Task Mode) can reconstruct the per-node trace from its event
            # log alone — the in-memory ``self._trace`` (and the chat task's
            # ``_flow_trace``) does not survive a restart, but the persisted
            # event log does. ``step_trace`` is self-contained: it carries the
            # resolved brief + bounded input + full bounded output.
            self._emit({'type': 'step_trace', **entry})
        except Exception as e:
            logger.debug('[FlowEngine] trace capture failed for %s: %s',
                         node.get('id'), e)

    def _emit(self, event: dict):
        if not self._on_event:
            return
        try:
            self._on_event(event)
        except Exception as e:
            logger.debug('[FlowEngine] on_event sink error: %s', e)


def _now_iso() -> str:
    """Wall-clock timestamp for trace entries (UI display)."""
    return time.strftime('%Y-%m-%dT%H:%M:%S')


class _AbortSignal(Exception):
    """Internal — unwinds the walk when abort_check fires."""


class _AbortAwareShim:
    """Minimal task-like object for the blocking human primitives.

    :func:`request_human_guidance` polls ``task.get('aborted')`` so it can
    unblock when the run is aborted. The flow engine tracks abort via an
    ``abort_check`` callable, so this shim adapts one to the other without
    pulling in the full TaskRuntime task dict.
    """

    def __init__(self, abort_check: Callable[[], bool], req_id: str):
        self._abort_check = abort_check
        self._id = req_id

    def get(self, key, default=None):
        if key == 'aborted':
            try:
                return bool(self._abort_check())
            except Exception:
                return False
        if key == 'id':
            return self._id
        return default


def compile_plan(definition: dict) -> dict:
    """Dry-run: describe the execution order WITHOUT running agents.

    Returns ``{ok, steps, error}`` where steps is an ordered list of
    ``{node_id, role|kind, action}`` entries. Safe (no LLM, no agents) —
    used by the ``/plan`` endpoint to preview what a flow would do.
    """
    verdict = validate_definition(definition)
    if not verdict['ok']:
        return {'ok': False, 'steps': [], 'error': '; '.join(verdict['errors'])}

    # Flatten subflows so the preview shows the real (inlined) steps.
    try:
        definition = expand_subflows(definition)
    except ValueError as e:
        return {'ok': False, 'steps': [], 'error': f'subflow: {e}'}

    nodes = {n['id']: n for n in definition.get('nodes', [])}
    fwd: dict[str, list[str]] = {nid: [] for nid in nodes}
    rev: dict[str, list[str]] = {nid: [] for nid in nodes}
    for e in definition.get('edges', []):
        if e.get('from') in nodes and e.get('to') in nodes:
            fwd[e['from']].append(e['to'])
            rev[e['to']].append(e['from'])

    start = next((nid for nid, n in nodes.items() if n.get('kind') == 'start'),
                 next((nid for nid in nodes if not rev.get(nid)), None))
    steps: list[dict] = []
    seen: set[str] = set()
    cur = start
    guard = 0
    while cur and guard < len(nodes) * 3:
        guard += 1
        if cur in seen:
            steps.append({'node_id': cur, 'action': 'loop-back'})
            break
        seen.add(cur)
        n = nodes.get(cur)
        if not n:
            break
        if n.get('type') == 'role':
            steps.append({'node_id': cur, 'role': n.get('role'),
                          'action': 'run-agent'})
        elif n.get('type') == 'subflow':
            # Survives expansion only when isolated (inline was flattened).
            steps.append({'node_id': cur, 'role': n.get('role'),
                          'action': 'run-subflow', 'scope': 'isolated'})
        elif n.get('kind') == 'artifact':
            steps.append({'node_id': cur, 'kind': 'artifact',
                          'action': 'declare-deliverable',
                          'path': (n.get('params') or {}).get('path') or ''})
        elif n.get('kind') == 'human':
            mode = (n.get('params') or {}).get('mode') or 'approve'
            steps.append({'node_id': cur, 'kind': 'human',
                          'action': f'human-{mode}'})
        else:
            steps.append({'node_id': cur, 'kind': n.get('kind'),
                          'action': n.get('kind')})
        if n.get('kind') == 'stop':
            break
        nxt = fwd.get(cur, [])
        cur = nxt[0] if nxt else None
    return {'ok': True, 'steps': steps, 'error': None}


__all__ = ['FlowExecutor', 'FlowExecutionError', 'compile_plan']
