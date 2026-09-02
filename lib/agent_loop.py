"""lib/agent_loop.py — Shared multi-round tool-calling loop + abort seam.

Several engines run the SAME agentic shell: an open-ended tool loop that
dispatches an LLM turn, and — if the turn asked for tools — executes each tool
and feeds the result back for another turn, until the model stops calling
tools. Each engine hand-rolled that shell together
with its own abort/stop plumbing, and the abort *signal* itself was spelled
three different ways across the codebase:

  * ``threading.Event``          — the paper report / Q&A engines
    (``task['abort_event']``);
  * a ``task['aborted']`` flag    — the chat orchestrator / Flow engine;
  * an ``abort_check`` callback   — swarm sub-agents.

This module unifies both concerns so future adopters need no re-plumbing:

  * :class:`AbortSignal` wraps ANY of the three mechanisms behind one
    ``.aborted`` predicate (and is itself callable / exposes ``.is_set`` so it
    drops straight into ``dispatch_stream(abort_check=…)``).
  * :func:`run_agent_loop` owns the round loop and the **three** abort-check
    placements, promoting the report engine's proven pattern to the default:

        (1) BEFORE each round      — don't start a turn after Stop;
        (2) AFTER the stream       — the stream may return a partial turn when
                                     the abort lands mid-flight;
        (3) BETWEEN queued tools   — a round may issue several slow tools; Stop
                                     pressed during one must skip the rest AND
                                     not start a fresh round. This is the check
                                     that fixed the "Stop has limited effect"
                                     bug — it MUST stay.

Everything engine-specific (the exact ``dispatch_stream`` kwargs, per-round
content buffering / interim-draft discard, tool-result events, usage
accumulation) stays in the caller via small hooks. The loop deliberately does
NOT catch exceptions — a dispatcher ``AbortedError`` propagates to the caller's
own handler unchanged.

Generic per-round machinery extensions (all opt-in, all owned HERE so no
engine re-implements them): ``before_round`` halt hook (timeouts),
``retry_bonus`` (bounded premature-close retry), ``execute_tools`` batch
hook (parallel pools), ``max_consecutive_tool_timeouts`` (timeout circuit
breaker), ``max_consecutive_nonretryable_failure_rounds`` (typed terminal
failure breaker) and ``on_round_end`` (crash-checkpoint placement).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from lib.llm.stream_result import (
    ProviderStreamResult,
    require_verified_provider_stream_result,
)
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['AbortSignal', 'LoopDirective', 'LoopOutcome', 'run_agent_loop',
           'unparseable_tool_calls']


def unparseable_tool_calls(msg: dict) -> list:
    """Return the tool calls whose ``function.arguments`` is not valid JSON.

    A premature SSE close (``usage['_missing_done']``) can sever the stream
    MID-ARGUMENTS: the accumulated tool call then holds truncated JSON that
    no executor can parse — running it would execute a tool on corrupt
    arguments, or on the sanitizer's ``{}`` substitution (the "tool ran with
    empty arguments" class). JSON is self-delimiting, so a cut that still
    parses is byte-complete in practice; an unparseable arguments string is
    therefore a reliable truncation signature.

    Both agent-loop consumers gate on this BEFORE a round's tool calls run:
    the chat orchestrator (``stream_handler.analyse_stream_result``) retries
    the round transparently, and the swarm SubAgent does the same via the
    chassis ``retry_bonus``.
    """
    if not isinstance(msg, dict):
        return []
    bad = []
    for tc in msg.get('tool_calls') or []:
        if not isinstance(tc, dict):
            continue
        args = (tc.get('function') or {}).get('arguments', '')
        if isinstance(args, dict):
            continue  # already-decoded shape — nothing to parse
        try:
            json.loads(args or '{}')
        except (json.JSONDecodeError, TypeError, ValueError):
            bad.append(tc)
    return bad


class AbortSignal:
    """Uniform abort predicate over the project's three abort mechanisms.

    Construct via a classmethod matching the caller's mechanism; read via the
    ``.aborted`` property (or call the instance / ``.is_set()`` — both aliases,
    so an ``AbortSignal`` can be handed to any API expecting an event-like
    ``.is_set`` or a ``() -> bool`` callback, e.g.
    ``dispatch_stream(abort_check=signal.is_set)``).
    """

    __slots__ = ('_predicate',)

    def __init__(self, predicate: Callable[[], bool]):
        self._predicate = predicate

    @classmethod
    def from_event(cls, event) -> 'AbortSignal':
        """Wrap a ``threading.Event`` (report/Q&A engines' ``abort_event``)."""
        return cls(lambda: bool(event.is_set()))

    @classmethod
    def from_task_flag(cls, task: dict, key: str = 'aborted') -> 'AbortSignal':
        """Wrap a truthy ``task[key]`` flag (chat orchestrator / endpoint)."""
        return cls(lambda: bool(task.get(key)))

    @classmethod
    def from_callback(cls, fn: Callable[[], bool] | None) -> 'AbortSignal':
        """Wrap an ``abort_check`` callback (swarm). ``None`` → never aborts."""
        if fn is None:
            return cls(lambda: False)
        return cls(lambda: bool(fn()))

    @classmethod
    def never(cls) -> 'AbortSignal':
        """A signal that never trips (e.g. timer polls have no abort path)."""
        return cls(lambda: False)

    @property
    def aborted(self) -> bool:
        try:
            return bool(self._predicate())
        except Exception as e:  # a broken predicate must not wedge the loop
            logger.warning('[AgentLoop] abort predicate raised: %s', e)
            return False

    def is_set(self) -> bool:
        return self.aborted

    def __call__(self) -> bool:
        return self.aborted


class LoopDirective:
    """Typed control decision returned by optional runner policy hooks.

    The shared runner owns *where* a round continues or stops.  Callers own
    policy (budget gates, provider-protocol continuations, task-specific
    semantic breakers) and express that policy through one of these named
    constructors instead of returning magic strings or re-growing a private
    ``while`` loop.

    ``proceed`` means normal tool-call inspection/execution.  Every other
    constructor is terminal or continues at the next round boundary.  The
    outcome flags deliberately mirror :class:`LoopOutcome` so a caller can
    distinguish natural completion, user abort, semantic halt, and a neutral
    provider stop without inferring state from response text.
    """

    __slots__ = ('action', 'exit_reason', 'completed', 'aborted', 'halted')

    _PROCEED = 'proceed'
    _CONTINUE = 'continue'
    _STOP = 'stop'

    def __init__(
        self,
        action: str,
        *,
        exit_reason: str | None = None,
        completed: bool = False,
        aborted: bool = False,
        halted: bool = False,
    ):
        if action not in {self._PROCEED, self._CONTINUE, self._STOP}:
            raise ValueError(f'unsupported loop directive action: {action!r}')
        terminal_flags = sum(bool(v) for v in (completed, aborted, halted))
        if terminal_flags > 1:
            raise ValueError('loop directive terminal flags are mutually exclusive')
        if action != self._STOP and terminal_flags:
            raise ValueError('only a stop directive may carry terminal flags')
        self.action = action
        self.exit_reason = exit_reason
        self.completed = bool(completed)
        self.aborted = bool(aborted)
        self.halted = bool(halted)

    @classmethod
    def proceed(cls) -> 'LoopDirective':
        return cls(cls._PROCEED)

    @classmethod
    def continue_round(cls) -> 'LoopDirective':
        return cls(cls._CONTINUE)

    @classmethod
    def stop(cls, reason: str) -> 'LoopDirective':
        return cls(cls._STOP, exit_reason=reason)

    @classmethod
    def complete(cls, reason: str = 'completed') -> 'LoopDirective':
        return cls(cls._STOP, exit_reason=reason, completed=True)

    @classmethod
    def abort(cls, reason: str) -> 'LoopDirective':
        return cls(cls._STOP, exit_reason=reason, aborted=True)

    @classmethod
    def halt(cls, reason: str) -> 'LoopDirective':
        return cls(cls._STOP, exit_reason=reason, halted=True)


class LoopOutcome:
    """Result of :func:`run_agent_loop`.

    Attributes:
        aborted: an abort check tripped (before-round / post-stream /
            between-tools) — the caller should NOT persist a final result.
        completed: the model returned a turn with no tool calls (natural end).
        rounds: number of dispatch rounds actually executed.
        exit_reason: WHY the loop stopped, for the orchestrator's diagnostic
            parity — one of ``completed``, ``aborted_before_round``,
            ``aborted_post_stream``, ``aborted_between_tools``,
            ``no_progress``, ``nonretryable_tool_failure``, or the custom reason
            string returned by a ``before_round`` halt hook (e.g.
            ``'timeout'``).
        halted: a semantic breaker or ``before_round`` hook stopped the loop
            (``exit_reason`` carries the reason), distinct from abort.
        retry_bonus_used: how many premature-close retries were consumed.
    """

    __slots__ = ('aborted', 'completed', 'rounds', 'exit_reason',
                 'retry_bonus_used', 'halted', 'consecutive_tool_timeouts',
                 'consecutive_no_progress_rounds',
                 'consecutive_nonretryable_failure_rounds')

    def __init__(self, aborted: bool = False, completed: bool = False,
                 rounds: int = 0, exit_reason: str = 'running',
                 retry_bonus_used: int = 0, halted: bool = False,
                 consecutive_tool_timeouts: int = 0,
                 consecutive_no_progress_rounds: int = 0,
                 consecutive_nonretryable_failure_rounds: int = 0):
        self.aborted = aborted
        self.completed = completed
        self.rounds = rounds
        self.exit_reason = exit_reason
        self.retry_bonus_used = retry_bonus_used
        self.halted = halted
        self.consecutive_tool_timeouts = consecutive_tool_timeouts
        self.consecutive_no_progress_rounds = consecutive_no_progress_rounds
        self.consecutive_nonretryable_failure_rounds = (
            consecutive_nonretryable_failure_rounds)


def run_agent_loop(
    *,
    abort: AbortSignal,
    round_tools: Any,
    dispatch: Callable[[int, Any], ProviderStreamResult | tuple],
    execute_tool: Callable[[int, dict], None] | None = None,
    on_round_result: Callable[[int, dict, Any, Any], None] | None = None,
    on_tool_round: Callable[[int, dict], None] | None = None,
    retry_bonus: Callable[[int, dict, Any, Any], bool] | None = None,
    max_retry_bonus: int = 2,
    before_round: Callable[[int], str | None] | None = None,
    execute_tools: Callable[[int, list], dict | None] | None = None,
    decide_round: Callable[[int, dict, Any, Any], LoopDirective | None] | None = None,
    before_tools: Callable[[int, dict], LoopDirective | None] | None = None,
    after_tools: Callable[[int, dict, dict | None], LoopDirective | None] | None = None,
    on_abort: Callable[[str, int, dict | None], str | None] | None = None,
    max_consecutive_tool_timeouts: int = 0,
    on_tool_timeout_state: Callable[[int, bool, int, int], None] | None = None,
    max_consecutive_no_progress_rounds: int = 0,
    max_consecutive_nonretryable_failure_rounds: int = 0,
    progress_probe: Callable[[], dict] | None = None,
    on_round_end: Callable[[int], None] | None = None,
) -> LoopOutcome:
    """Drive an LLM tool-calling loop until the model naturally completes.

    The loop owns control flow + the three abort-check placements; all
    engine-specific I/O is delegated to the hooks below. It never catches
    exceptions raised by ``dispatch`` / ``execute_tool`` (so a dispatcher
    ``AbortedError`` reaches the caller's handler).

    Args:
        abort: the unified abort signal (checked at all three points).
        round_tools: tool schema list passed unchanged to every dispatch.
        dispatch: normally ``dispatch(rnd, tools) -> ProviderStreamResult``.
            A historical three-tuple remains accepted for non-provider test
            adapters. The chassis refuses an unverified typed result before
            treating a no-tool round as natural completion or executing tools;
            a caller-owned ``decide_round`` may consume it first to perform a
            bounded recovery. ``msg['tool_calls']`` drives tool execution.
        execute_tool: ``execute_tool(rnd, tool_call) -> None``. Runs ONE tool
            and is responsible for emitting the engine's tool events and
            appending the tool-result message. Called only AFTER the
            between-tools abort check passes.
        on_round_result: optional ``(rnd, msg, finish, usage) -> None`` hook
            fired after every dispatch (e.g. usage accumulation).
        on_tool_round: optional ``(rnd, msg) -> None`` hook fired once when a
            round HAS tool calls, before executing them (e.g. interim-draft
            discard + appending the assistant message to the history).
        retry_bonus: optional ``(rnd, msg, finish, usage) -> bool`` hook fired
            after ``on_round_result``. Returning True means the stream ended
            prematurely and the same logical round should be retried. The
            retry is capped by ``max_retry_bonus`` and is not treated as a
            natural completion.
        max_retry_bonus: maximum premature-close retries (default 2).
        before_round: optional ``(rnd) -> str | None`` halt hook checked at
            the TOP of every round (after the abort check). Returning a
            non-empty reason string stops the loop with
            ``outcome.halted=True`` and ``exit_reason=<reason>`` — the generic
            seam for per-round guards the chassis does not own (swarm's
            wall-clock timeout is the first adopter). Returning None lets
            the round proceed.
        execute_tools: optional BATCH hook ``(rnd, tool_calls) ->
            dict | None``. When provided it replaces the per-tool
            ``execute_tool`` loop ENTIRELY (including the between-tools
            abort checks — the hook holds the ``abort`` signal and owns its
            own intra-batch behavior). This exists for engines like swarm
            that execute a round's tools in a parallel pool; prefer the
            per-tool ``execute_tool`` contract for new engines so the
            between-tools abort check (the "Stop has limited effect" fix)
            keeps biting. The hook MAY return a note dict; the chassis
            reads ``'timed_out'`` (bool) and
            ``'nonretryable_failure_signatures'`` (stable error codes). See
            the matching breaker arguments below.
        decide_round: optional typed policy hook fired after a successful
            dispatch (and the legacy ``retry_bonus`` hook) but before the
            runner's post-stream abort check and tool-call inspection.  It
            may return :class:`LoopDirective` to continue a provider protocol
            round, stop on a caller-owned gate, or declare a caller-verified
            completion. ``None`` / ``LoopDirective.proceed()`` preserves the
            default behavior.
        before_tools: optional typed gate after ``on_tool_round`` and before
            any tool executes.  This is where a caller performs task-specific
            cleanup for an abort that races with assistant-message append. A
            terminal directive is allowed; ``continue_round`` is rejected
            because it would leave an orphaned assistant tool-call message.
        after_tools: optional typed semantic gate after tools and the generic
            timeout counter, but before ``on_round_end``.  It is the seam for
            task-specific progress/oracle breakers; a stop skips the normal
            checkpoint just like the historical inline loops did.
            ``continue_round`` is rejected because every successfully
            executed tool round must cross its settlement/checkpoint boundary.
        on_abort: optional ``(phase, rnd, msg) -> reason | None`` observer for
            the runner-owned abort placements. ``phase`` is one of
            ``before_round``, ``post_stream``, or ``between_tools``.  The hook
            may perform caller-specific event/history cleanup and override the
            generic outcome reason without changing the stop decision.
        max_consecutive_tool_timeouts: consecutive-tool-timeout circuit
            breaker (0 = off, default). When > 0, the chassis counts
            CONSECUTIVE batch notes carrying ``timed_out=True`` (a round
            whose note is falsy/absent resets the count) and halts the
            loop at the threshold with ``outcome.halted=True`` and
            ``exit_reason='tool_timeout'`` — the generic form of the
            chat orchestrator's ``_MAX_CONSECUTIVE_TOOL_TIMEOUTS`` guard.
            Detection stays with the engine (it knows what a timeout is);
            the counter + halt mechanics live here, not re-implemented
            per engine (mirrors the orchestrator: breaker break happens
            BEFORE the crash-checkpoint, so a halted round fires no
            ``on_round_end``).
        on_tool_timeout_state: optional observer called after the chassis
            updates its timeout counter as ``(rnd, timed_out, consecutive,
            limit)``.  Presentation/error-envelope policy can mirror the
            authoritative counter without reimplementing it.
        max_consecutive_no_progress_rounds: wedged-loop circuit breaker
            (0 = off, default). When > 0, the chassis fingerprints each
            round's tool calls (name + arguments, in order) and counts
            CONSECUTIVE rounds whose fingerprint is IDENTICAL to the
            previous round's; a differing fingerprint resets the count. At
            the threshold the loop halts with ``outcome.halted=True`` and
            ``exit_reason='no_progress'``.

            This is the guard the 2026-07-27 runaway needed: one sub-agent
            re-issued the same tool call for 26.7M rounds (3.5h, 9.1 GB of
            log) while making no semantic progress.

            The criterion is REPETITION, not empty content: measured across
            the 07-24..07-26 logs, 866/1723 (50.3%) of legitimate rounds have
            ``content_len == 0`` — an empty-content round is simply what a
            pure tool-calling turn looks like, so halting on it would kill
            half of all real agents.
        max_consecutive_nonretryable_failure_rounds: terminal tool-failure
            circuit breaker (0 = off, default). When > 0, the chassis counts
            consecutive batch notes whose ``nonretryable_failure_signatures``
            identify the same explicit typed failures. The batch adopter must
            emit signatures only when EVERY tool in the round returned a
            canonical ``retryable=false`` error; success, retryable failure,
            mixed, malformed, and legacy-string rounds pass no signatures and
            reset the streak. Tool arguments are deliberately irrelevant: a
            model cannot turn a stable capability denial into progress by
            changing a tab id, selector, or path. At the threshold the loop
            halts with ``exit_reason='nonretryable_tool_failure'``.
        on_round_end: optional ``(rnd) -> None`` hook fired at the natural
            end of a round whose tools were executed WITHOUT an abort and
            WITHOUT a timeout-breaker halt — the seam for crash-recovery
            checkpoints (the orchestrator's throttled ``checkpoint_task_
            partial`` and swarm's per-round ``_checkpoint`` both live here;
            throttling policy stays in the hook, the PLACEMENT is owned by
            the chassis so the two engines can't drift into two shapes).

    Returns:
        LoopOutcome describing why the loop stopped (incl. ``exit_reason``).
    """
    outcome = LoopOutcome()

    def apply_directive(
        directive: LoopDirective | None,
        *,
        hook_name: str,
        allow_continue: bool,
    ) -> str:
        """Apply a typed hook decision and return its runner action."""
        if directive is None:
            return LoopDirective._PROCEED
        if not isinstance(directive, LoopDirective):
            raise TypeError(
                'agent-loop policy hooks must return LoopDirective or None')
        if directive.action == LoopDirective._CONTINUE and not allow_continue:
            raise ValueError(
                f'{hook_name} cannot continue: doing so would skip a required '
                'tool or round-settlement boundary')
        if directive.action == LoopDirective._STOP:
            outcome.exit_reason = directive.exit_reason or 'stopped'
            outcome.completed = directive.completed
            outcome.aborted = directive.aborted
            outcome.halted = directive.halted
        return directive.action

    def record_abort(phase: str, rnd: int, msg: dict | None = None) -> None:
        reason = f'aborted_{phase}'
        if on_abort is not None:
            caller_reason = on_abort(phase, rnd, msg)
            if caller_reason:
                reason = caller_reason
        outcome.aborted = True
        outcome.exit_reason = reason

    # There is deliberately no round ceiling. A productive model keeps its
    # tools until it returns a natural no-tool response; only explicit aborts,
    # semantic breakers, or caller-supplied guards may stop it earlier.
    bonus = 0
    rnd = -1
    progress_ledger = None
    if (max_consecutive_no_progress_rounds > 0
            or max_consecutive_nonretryable_failure_rounds > 0):
        from lib.agent_core.progress_ledger import ProgressLedgerV2
        progress_ledger = ProgressLedgerV2()
    while True:
        rnd += 1
        # (1) BEFORE-ROUND — don't start a turn after Stop.
        if abort.aborted:
            record_abort('before_round', rnd)
            break

        # Generic per-round halt hook (timeout and future guards live here,
        # NOT re-implemented per engine).
        if before_round is not None:
            reason = before_round(rnd)
            if reason:
                outcome.halted = True
                outcome.exit_reason = reason
                break

        dispatch_result = dispatch(rnd, round_tools)
        msg, finish, usage = dispatch_result
        outcome.rounds += 1
        if on_round_result is not None:
            on_round_result(rnd, msg, finish, usage)

        # Premature-close retry (capped); do not treat this poisoned stream as
        # a natural completion.
        if retry_bonus is not None and bonus < max_retry_bonus \
                and retry_bonus(rnd, msg, finish, usage):
            bonus += 1
            outcome.retry_bonus_used += 1
            continue

        if decide_round is not None:
            directive_action = apply_directive(
                decide_round(rnd, msg, finish, usage),
                hook_name='decide_round', allow_continue=True)
            if directive_action == LoopDirective._CONTINUE:
                continue
            if directive_action == LoopDirective._STOP:
                break

        # (2) POST-STREAM — the stream can return a partial turn when the
        # abort landed during line iteration (no raise).
        if abort.aborted:
            record_abort('post_stream', rnd, msg)
            break

        if isinstance(dispatch_result, ProviderStreamResult):
            # A typed provider attempt is never allowed to fall through the
            # generic "no tools means completed" rule without positive finish
            # evidence. Root chat's decide_round owns lossless retry and exits
            # above; standalone paper/swarm consumers fail honestly here.
            require_verified_provider_stream_result(
                dispatch_result, context='agent-loop round')

        tool_calls = msg.get('tool_calls') if isinstance(msg, dict) else None
        if not tool_calls:
            outcome.completed = True
            outcome.exit_reason = 'completed'
            break

        if on_tool_round is not None:
            on_tool_round(rnd, msg)

        if before_tools is not None:
            directive_action = apply_directive(
                before_tools(rnd, msg),
                hook_name='before_tools', allow_continue=False)
            if directive_action == LoopDirective._CONTINUE:
                continue
            if directive_action == LoopDirective._STOP:
                break

        # Wedged-loop breaker: a round that re-issues the PREVIOUS round's
        # tool calls byte-for-byte made no progress. Consecutive repeats are
        # counted; any differing fingerprint resets the streak. Checked here
        # (before the tools run) so a wedged agent cannot keep re-executing
        # the same side-effecting call while the counter climbs.
        if max_consecutive_no_progress_rounds > 0:
            assert progress_ledger is not None
            probe = progress_probe() if progress_probe is not None else {}
            probe = probe if isinstance(probe, dict) else {}
            decision = progress_ledger.observe(
                tool_calls,
                world_version=str(probe.get('worldVersion') or ''),
                evidence_ids=probe.get('evidenceIds') or (),
                verification=str(probe.get('verification') or ''),
            )
            outcome.consecutive_no_progress_rounds = int(
                decision['noProgressStreak'])
            if outcome.consecutive_no_progress_rounds \
                    >= max_consecutive_no_progress_rounds:
                logger.warning(
                    '[AgentLoop] no-progress breaker tripped at round %d: '
                    '%d repeated call+world rounds without new evidence',
                    rnd + 1, outcome.consecutive_no_progress_rounds)
                outcome.halted = True
                outcome.exit_reason = 'no_progress'
                break

        note = None
        if execute_tools is not None:
            # Batch path (e.g. swarm's parallel tool pool): the hook owns
            # intra-batch behavior incl. any abort checks.
            note = execute_tools(rnd, tool_calls)
        else:
            for tc in tool_calls:
                # (3) BETWEEN-TOOLS — a Stop pressed during a slow tool must
                # skip the remaining queued tools and NOT start a fresh
                # round. Removing this check reintroduces the "Stop has
                # limited effect" bug.
                if abort.aborted:
                    record_abort('between_tools', rnd, msg)
                    break
                execute_tool(rnd, tc)

        if outcome.aborted:
            break

        # Consecutive-tool-timeout circuit breaker (before on_round_end:
        # a halted round is NOT checkpointed, mirroring the orchestrator).
        if max_consecutive_tool_timeouts > 0:
            timed_out = bool(note and note.get('timed_out'))
            if timed_out:
                outcome.consecutive_tool_timeouts += 1
            else:
                outcome.consecutive_tool_timeouts = 0
            if on_tool_timeout_state is not None:
                on_tool_timeout_state(
                    rnd, timed_out, outcome.consecutive_tool_timeouts,
                    max_consecutive_tool_timeouts)
            if outcome.consecutive_tool_timeouts \
                    >= max_consecutive_tool_timeouts:
                outcome.halted = True
                outcome.exit_reason = 'tool_timeout'
                break

        # Typed terminal-failure breaker. This is intentionally post-execution:
        # unlike the exact-call guard above, it reasons from authoritative tool
        # RESULTS. Unknown/legacy results fail open, and the batch hook only
        # reports a signature when every call failed non-retryably.
        if max_consecutive_nonretryable_failure_rounds > 0:
            assert progress_ledger is not None
            note_dict = note if isinstance(note, dict) else {}
            decision = progress_ledger.observe_nonretryable_failures(
                note_dict.get('nonretryable_failure_signatures') or ())
            outcome.consecutive_nonretryable_failure_rounds = int(
                decision['nonretryableFailureStreak'])
            if outcome.consecutive_nonretryable_failure_rounds \
                    >= max_consecutive_nonretryable_failure_rounds:
                logger.warning(
                    '[AgentLoop] terminal tool-failure breaker tripped at '
                    'round %d: %d rounds with the same non-retryable failure',
                    rnd + 1,
                    outcome.consecutive_nonretryable_failure_rounds)
                outcome.halted = True
                outcome.exit_reason = 'nonretryable_tool_failure'
                break

        if after_tools is not None:
            directive_action = apply_directive(
                after_tools(rnd, msg, note),
                hook_name='after_tools', allow_continue=False)
            if directive_action == LoopDirective._CONTINUE:
                continue
            if directive_action == LoopDirective._STOP:
                break

        if on_round_end is not None:
            on_round_end(rnd)

    return outcome
