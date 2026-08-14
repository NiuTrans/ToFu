"""lib/agent_loop.py — Shared multi-round tool-calling loop + abort seam.

Several engines run the SAME agentic shell: an open-ended tool loop that
dispatches an LLM turn, and — if the turn asked for tools — executes each tool
and feeds the result back for another turn, until the model stops calling
tools. Each engine hand-rolled that shell together
with its own abort/stop plumbing, and the abort *signal* itself was spelled
three different ways across the codebase:

  * ``threading.Event``          — the paper report / Q&A engines
    (``task['abort_event']``);
  * a ``task['aborted']`` flag    — the chat orchestrator / endpoint loop;
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
breaker) and ``on_round_end`` (crash-checkpoint placement).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['AbortSignal', 'LoopOutcome', 'run_agent_loop',
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
            ``no_progress``, or the custom reason
            string returned by a ``before_round`` halt hook (e.g.
            ``'timeout'``).
        halted: a semantic breaker or ``before_round`` hook stopped the loop
            (``exit_reason`` carries the reason), distinct from abort.
        retry_bonus_used: how many premature-close retries were consumed.
    """

    __slots__ = ('aborted', 'completed', 'rounds', 'exit_reason',
                 'retry_bonus_used', 'halted', 'consecutive_tool_timeouts',
                 'consecutive_no_progress_rounds')

    def __init__(self, aborted: bool = False, completed: bool = False,
                 rounds: int = 0, exit_reason: str = 'running',
                 retry_bonus_used: int = 0, halted: bool = False,
                 consecutive_tool_timeouts: int = 0,
                 consecutive_no_progress_rounds: int = 0):
        self.aborted = aborted
        self.completed = completed
        self.rounds = rounds
        self.exit_reason = exit_reason
        self.retry_bonus_used = retry_bonus_used
        self.halted = halted
        self.consecutive_tool_timeouts = consecutive_tool_timeouts
        self.consecutive_no_progress_rounds = consecutive_no_progress_rounds


def run_agent_loop(
    *,
    abort: AbortSignal,
    round_tools: Any,
    dispatch: Callable[[int, Any], tuple],
    execute_tool: Callable[[int, dict], None] | None = None,
    on_round_result: Callable[[int, dict, Any, Any], None] | None = None,
    on_tool_round: Callable[[int, dict], None] | None = None,
    retry_bonus: Callable[[int, dict, Any, Any], bool] | None = None,
    max_retry_bonus: int = 2,
    before_round: Callable[[int], str | None] | None = None,
    execute_tools: Callable[[int, list], dict | None] | None = None,
    max_consecutive_tool_timeouts: int = 0,
    max_consecutive_no_progress_rounds: int = 0,
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
        dispatch: ``dispatch(rnd, tools) -> (msg, finish, usage)``. Performs
            the LLM turn (typically wrapping ``dispatch_stream`` with the
            engine's callbacks). ``msg`` must be a dict; a truthy
            ``msg['tool_calls']`` list drives tool execution.
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
            currently reads one key: ``'timed_out'`` (bool) — see
            ``max_consecutive_tool_timeouts``.
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

    # There is deliberately no round ceiling. A productive model keeps its
    # tools until it returns a natural no-tool response; only explicit aborts,
    # semantic breakers, or caller-supplied guards may stop it earlier.
    bonus = 0
    rnd = -1
    prev_tool_fingerprint = None
    while True:
        rnd += 1
        # (1) BEFORE-ROUND — don't start a turn after Stop.
        if abort.aborted:
            outcome.aborted = True
            outcome.exit_reason = 'aborted_before_round'
            break

        # Generic per-round halt hook (timeout and future guards live here,
        # NOT re-implemented per engine).
        if before_round is not None:
            reason = before_round(rnd)
            if reason:
                outcome.halted = True
                outcome.exit_reason = reason
                break

        msg, finish, usage = dispatch(rnd, round_tools)
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

        # (2) POST-STREAM — the stream can return a partial turn when the
        # abort landed during line iteration (no raise).
        if abort.aborted:
            outcome.aborted = True
            outcome.exit_reason = 'aborted_post_stream'
            break

        tool_calls = msg.get('tool_calls') if isinstance(msg, dict) else None
        if not tool_calls:
            outcome.completed = True
            outcome.exit_reason = 'completed'
            break

        if on_tool_round is not None:
            on_tool_round(rnd, msg)

        # Wedged-loop breaker: a round that re-issues the PREVIOUS round's
        # tool calls byte-for-byte made no progress. Consecutive repeats are
        # counted; any differing fingerprint resets the streak. Checked here
        # (before the tools run) so a wedged agent cannot keep re-executing
        # the same side-effecting call while the counter climbs.
        if max_consecutive_no_progress_rounds > 0:
            fingerprint = repr([
                (tc.get('function', {}).get('name', ''),
                 tc.get('function', {}).get('arguments', ''))
                for tc in tool_calls if isinstance(tc, dict)
            ])
            if fingerprint == prev_tool_fingerprint:
                outcome.consecutive_no_progress_rounds += 1
                if outcome.consecutive_no_progress_rounds \
                        >= max_consecutive_no_progress_rounds:
                    logger.warning(
                        '[AgentLoop] no-progress breaker tripped at round %d: '
                        '%d consecutive identical tool-call rounds',
                        rnd + 1, outcome.consecutive_no_progress_rounds)
                    outcome.halted = True
                    outcome.exit_reason = 'no_progress'
                    break
            else:
                outcome.consecutive_no_progress_rounds = 0
            prev_tool_fingerprint = fingerprint

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
                    outcome.aborted = True
                    outcome.exit_reason = 'aborted_between_tools'
                    break
                execute_tool(rnd, tc)

        if outcome.aborted:
            break

        # Consecutive-tool-timeout circuit breaker (before on_round_end:
        # a halted round is NOT checkpointed, mirroring the orchestrator).
        if max_consecutive_tool_timeouts > 0:
            if note and note.get('timed_out'):
                outcome.consecutive_tool_timeouts += 1
                if outcome.consecutive_tool_timeouts \
                        >= max_consecutive_tool_timeouts:
                    outcome.halted = True
                    outcome.exit_reason = 'tool_timeout'
                    break
            else:
                outcome.consecutive_tool_timeouts = 0

        if on_round_end is not None:
            on_round_end(rnd)

    return outcome
