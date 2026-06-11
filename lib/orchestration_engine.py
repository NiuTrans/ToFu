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

import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib.env_compat import getenv_compat
from lib.log import get_logger
from lib.orchestration import validate_definition

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

_VERIFIER_ROLES = frozenset({'critic', 'reviewer'})

# State-changing ("deliverable") tools — mirrors
# lib/tasks_pkg/endpoint_review.STATE_CHANGING_TOOLS. Kept as a local copy
# so the engine stays a dependency-free standalone interpreter; if you add
# a state-changing tool, update BOTH sets.
_STATE_CHANGING_TOOLS = frozenset({
    'write_file', 'apply_diff', 'apply_diffs', 'insert_content',
    'insert_contents', 'run_command', 'create_project', 'generate_image',
    'code_exec',
})

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
_STUCK_JACCARD = 0.60

_VERDICT_TAG_RE = re.compile(
    r'\[VERDICT:\s*(STOP|CONTINUE_WORKER|CONTINUE_PLANNER|CONTINUE)\s*\]', re.I)
_PLAN_DEFECT_RE = re.compile(r'\[PLAN_DEFECT:\s*([^\]]+)\]', re.I)
# Phrases that mark a "plan defect" as really a worker-execution problem.
_WORKER_RATIONALIZATIONS = (
    'worker did', "worker didn't", 'worker did not', 'worker needs',
    'worker should', 'still ❌', 'remaining ❌', 'remaining items',
    'more iterations',
)

# Verdict heuristics for loop verifiers (critic / reviewer output).
_STOP_RE = re.compile(r'\b(VERDICT:\s*STOP|approved|looks good|all (?:met|pass)|✅)\b', re.I)
_CONTINUE_RE = re.compile(r'\b(CONTINUE|not met|still (?:failing|broken)|unresolved|❌)\b', re.I)

# Endpoint-faithful guard: a STOP verdict whose feedback STILL contains
# unresolved markers (❌ / "not met" / "still failing" / "unresolved") is
# almost always a worker-didn't-finish problem, not a real done signal.
# We override STOP→CONTINUE in that case, mirroring endpoint mode's
# _parse_verdict (see lib/tasks_pkg/endpoint_review.py + the
# anti-analysis-spiral rewrite). Prevents premature loop termination.
_UNRESOLVED_RE = re.compile(r'(❌|\bNOT met\b|\bstill failing\b|\bunresolved\b)', re.I)


def _replan_enabled() -> bool:
    """Replan kill-switch (shared with endpoint mode): TOFU_ENDPOINT_REPLAN=0
    disables CONTINUE_PLANNER (downgrades to worker)."""
    return getenv_compat('TOFU_ENDPOINT_REPLAN', default='1').strip() != '0'


class FlowExecutionError(Exception):
    """Raised for structural problems discovered at execution time."""


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
                 project_path: str = ''):
        verdict = validate_definition(definition)
        if not verdict['ok']:
            raise FlowExecutionError(
                'cannot execute invalid definition: ' + '; '.join(verdict['errors']))

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

        # Forward / reverse adjacency.
        self.fwd: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        self.rev: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for e in definition.get('edges', []):
            s, d = e.get('from'), e.get('to')
            if s in self.nodes and d in self.nodes:
                self.fwd[s].append(d)
                self.rev[d].append(s)

        self._agents_run = 0
        self._transcript: list[dict] = []
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

    # ── public entry ────────────────────────────────────────────────

    def run(self, *, initial_context: str = '') -> dict:
        """Execute the flow. Blocking — call from a background thread.

        Returns ``{ok, status, final, transcript, agents_run, error}``.
        """
        start = self._find_start()
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
            'agents_run': self._agents_run,
            'artifacts': list(self._artifacts),
            'error': error,
        }

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
                node_id = self._single_next(node_id)
                continue
            if ntype == 'role':
                context = self._run_role(node, context)
                node_id = self._single_next(node_id)
                continue
            if kind == 'parallel':
                context, node_id = self._run_parallel(node_id, context)
                continue
            if kind == 'barrier':
                node_id = self._single_next(node_id)
                continue
            if kind == 'loop':
                context, node_id = self._run_loop(node_id, context)
                continue
            if kind == 'branch':
                node_id = self._run_branch(node_id, context)
                continue
            if kind == 'artifact':
                self._declare_artifact(node)
                node_id = self._single_next(node_id)
                continue
            if kind == 'human':
                context, node_id = self._run_human(node, context)
                continue
            # Unknown node kind — skip defensively.
            logger.warning('[FlowEngine] skipping unknown node %s (kind=%s type=%s)',
                           node_id, kind, ntype)
            node_id = self._single_next(node_id)
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

        # Build the effective per-call context.
        #  * A shared-context node sees its OWN prior attempt + any pending
        #    verifier feedback + any pending guard directive prepended — this
        #    is what makes a loop's worker stateful (endpoint behavior).
        #  * A verifier node gets a Deliverables Snapshot of the producer's
        #    latest turn appended, so it can apply the endpoint pre-verdict
        #    check ("0 state-changing calls → CONTINUE, don't replan").
        #  * A fresh-context non-verifier node sees only upstream context.
        eff_context = context
        if shared:
            eff_context = self._compose_shared_context(nid, context)
        if is_verifier:
            eff_context = self._append_deliverables_snapshot(eff_context)

        self._emit({'type': 'step_start', 'node_id': nid, 'role': role,
                    'name': node.get('name') or role,
                    'isolation': 'shared' if shared else 'fresh'})
        t0 = time.monotonic()
        try:
            res = self._runner(node, eff_context, 0) or {}
        except Exception as e:
            logger.error('[FlowEngine] agent runner crashed on %s: %s', nid, e, exc_info=True)
            res = {'output': '', 'status': 'failed', 'error': str(e)}
        out = str(res.get('output') or '')
        st = res.get('status') or 'completed'

        # Count deliverables (state-changing tool calls) the runner reports.
        sc_count, explore_count, sc_names, reported = self._count_deliverables(res)
        self._record(nid, role, out, st, res.get('error') or '',
                     time.monotonic() - t0, sc_count=sc_count,
                     explore_count=explore_count)

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
                    'status': st, 'preview': out[:200],
                    'state_changing': sc_count})
        return self._append_context(context, role, out)

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
        barrier = self._find_common_barrier(branches)
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
        nxt = self._single_next(barrier) if barrier else None
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
        body_entry, exit_node = self._loop_parts(lid)
        planner_id = self._find_loop_planner(lid, body_entry)
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
            phase, defect = self._classify_verdict(verifier_out)

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

        return context, exit_node

    def _find_loop_planner(self, lid: str, body_entry: str | None) -> str | None:
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
            if body_entry and self._can_reach(body_entry, pred, avoid=lid):
                continue   # this predecessor is inside the loop body (e.g. critic)
            return pred
        return None

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
        # Tag the planner objective so it knows this is a re-plan.
        params = dict(planner_node.get('params') or {})
        params['objective'] = (
            (params.get('objective') or 'Plan the work.')
            + f'\n\n[RE-PLAN #{replan}] Address the structural defect above and '
            'produce a minimal DELTA to the existing plan — do not rewrite or '
            'grow it.')
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
            choice_labels = {t: self._node_label(t) for t in nexts}
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
            return context, self._single_next(nid)

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
            return context, self._single_next(nid)

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
        return context, self._single_next(nid)

    def _human_seq(self) -> int:
        with self._lock:
            self._human_counter = getattr(self, '_human_counter', 0) + 1
            return self._human_counter

    def _node_label(self, nid: str) -> str:
        n = self.nodes.get(nid) or {}
        return n.get('name') or n.get('role') or n.get('kind') or nid

    # ── structure helpers ───────────────────────────────────────────

    def _single_next(self, node_id: str) -> str | None:
        nexts = self.fwd.get(node_id, [])
        return nexts[0] if nexts else None

    def _find_start(self) -> str:
        for nid, n in self.nodes.items():
            if n.get('kind') == 'start':
                return nid
        # fall back to a source node
        for nid in self.nodes:
            if not self.rev.get(nid):
                return nid
        raise FlowExecutionError('no start node and no source node')

    def _loop_parts(self, lid: str) -> tuple[str | None, str | None]:
        """Return (body_entry, exit_node) for a loop node.

        body_entry = a successor that can reach the loop again (cycle).
        exit_node  = the other successor (preferring one that reaches stop).
        """
        succ = list(self.fwd.get(lid, []))
        body, exit_n = None, None
        for s in succ:
            if self._can_reach(s, lid, avoid=lid):
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

    def _find_common_barrier(self, branches: list[str]) -> str | None:
        """Find the nearest barrier node reachable from all branches."""
        if not branches:
            return None
        reach_sets = [self._reachable(b) for b in branches]
        common = set.intersection(*reach_sets) if reach_sets else set()
        barriers = [nid for nid in common if self.nodes[nid].get('kind') == 'barrier']
        if barriers:
            # nearest by BFS distance from first branch
            return min(barriers, key=lambda n: self._distance(branches[0], n))
        # fall back to any common node
        if common:
            return min(common, key=lambda n: self._distance(branches[0], n))
        return None

    def _reachable(self, start: str) -> set[str]:
        seen, stack = set(), [start]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(self.fwd.get(n, []))
        return seen

    def _can_reach(self, start: str, target: str, *, avoid: str = '') -> bool:
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

    def _distance(self, start: str, target: str) -> int:
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

    # ── verdict / context ───────────────────────────────────────────

    def _last_verifier_output(self) -> str:
        for entry in reversed(self._transcript):
            if entry.get('role') in ('critic', 'reviewer'):
                return entry.get('output') or ''
        return self._transcript[-1].get('output') if self._transcript else ''

    def _classify_verdict(self, text: str) -> tuple:
        """Classify a verifier's output into ``(phase, plan_defect)``.

        ``phase`` ∈ {'stop','worker','planner'}; ``plan_defect`` is the
        gated structural reason (or None). Mirrors endpoint mode's
        _parse_verdict gating exactly:
          * Explicit [VERDICT: ...] tag wins; else fall back to the loose
            STOP/CONTINUE heuristics (so plain-language critics still work).
          * STOP with unresolved markers (❌ / "not met" / …) → 'worker'.
          * CONTINUE_PLANNER requires a [PLAN_DEFECT: ...] tag, and the
            reason must NOT be a worker-execution complaint in disguise;
            otherwise it is downgraded to 'worker'.
          * Kill-switch TOFU_ENDPOINT_REPLAN=0 downgrades planner→worker.
        """
        if not text:
            return 'stop', None

        defect = None
        for m in _PLAN_DEFECT_RE.finditer(text):
            defect = m.group(1).strip()

        tag_match = None
        for m in _VERDICT_TAG_RE.finditer(text):
            tag_match = m

        if tag_match is not None:
            tag = tag_match.group(1).upper()
            if tag == 'STOP':
                phase = 'stop'
            elif tag == 'CONTINUE_PLANNER':
                phase = 'planner'
            else:
                phase = 'worker'
        else:
            # No explicit tag — loose heuristics (back-compat with the
            # original engine + plain-language critics).
            if _STOP_RE.search(text):
                phase = 'stop'
            elif _CONTINUE_RE.search(text):
                phase = 'worker'
            else:
                phase = 'stop'   # ambiguous → stop, never spin forever

        # STOP-with-unresolved-marker → worker (anti-analysis-spiral guard).
        if phase == 'stop' and _UNRESOLVED_RE.search(text):
            logger.info('[FlowEngine] STOP overridden → CONTINUE_WORKER: '
                        'feedback still has unresolved markers')
            phase = 'worker'

        # CONTINUE_PLANNER gate.
        if phase == 'planner':
            if not defect:
                logger.info('[FlowEngine] CONTINUE_PLANNER→worker: no '
                            '[PLAN_DEFECT:] tag')
                phase = 'worker'
            elif any(p in defect.lower() for p in _WORKER_RATIONALIZATIONS):
                logger.info('[FlowEngine] CONTINUE_PLANNER→worker: defect is a '
                            'worker-execution complaint: %r', defect)
                phase = 'worker'
            elif not _replan_enabled():
                logger.info('[FlowEngine] CONTINUE_PLANNER→worker: replan '
                            'disabled (TOFU_ENDPOINT_REPLAN=0)')
                phase = 'worker'

        return phase, defect

    def _detect_stuck(self) -> bool:
        """True if the last two verifier feedbacks are >_STUCK_JACCARD similar.

        Mirrors endpoint's _detect_stuck — a repeating critic means the loop
        is not converging; the loop breaks out rather than burning iterations.
        """
        if len(self._feedback_history) < 2:
            return False
        prev = set(self._feedback_history[-2].lower().split())
        curr = set(self._feedback_history[-1].lower().split())
        if not prev or not curr:
            return False
        union = prev | curr
        jaccard = len(prev & curr) / len(union) if union else 0
        return jaccard > _STUCK_JACCARD

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
            objective=params.get('objective') or node.get('name') or 'Execute this step.',
            context=context,
            model_tier=params.get('tier') or 'standard',
        )
        parent = self._parent_task or {
            'id': 'flow', 'convId': 'flow',
            'events_lock': threading.Lock(), 'events': [],
            'toolRounds': [], 'phase': 'tool', 'config': {},
        }
        agent = SubAgent(
            spec,
            parent_task=parent,
            all_tools=self._all_tools,
            model=self._model,
            abort_check=self._abort_check,
            project_path=self._project_path,
        )
        result = agent.run()
        return {
            'output': result.final_answer or '',
            'status': result.status,
            'error': result.error_message if result.status != SubAgentStatus.COMPLETED.value else '',
            # tool_log = [{round, tool, args_brief}, ...] — fed to the
            # engine's deliverables counter (state-changing vs exploratory).
            'tool_log': result.tool_log or [],
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

    def _emit(self, event: dict):
        if not self._on_event:
            return
        try:
            self._on_event(event)
        except Exception as e:
            logger.debug('[FlowEngine] on_event sink error: %s', e)


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
