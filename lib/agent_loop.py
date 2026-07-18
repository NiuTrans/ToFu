"""lib/agent_loop.py — Shared multi-round tool-calling loop + abort seam.

Several engines run the SAME agentic shell: a bounded ``for round`` loop that
dispatches an LLM turn, and — if the turn asked for tools — executes each tool
and feeds the result back for another turn, until the model stops calling
tools (or a round cap is hit). Each engine hand-rolled that shell together
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
"""

from __future__ import annotations

from typing import Any, Callable

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['AbortSignal', 'LoopOutcome', 'run_agent_loop']


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
            ``max_rounds_exhausted``.
        retry_bonus_used: how many premature-close bonus rounds were granted.
    """

    __slots__ = ('aborted', 'completed', 'rounds', 'exit_reason',
                 'retry_bonus_used')

    def __init__(self, aborted: bool = False, completed: bool = False,
                 rounds: int = 0, exit_reason: str = 'max_rounds_exhausted',
                 retry_bonus_used: int = 0):
        self.aborted = aborted
        self.completed = completed
        self.rounds = rounds
        self.exit_reason = exit_reason
        self.retry_bonus_used = retry_bonus_used


def run_agent_loop(
    *,
    abort: AbortSignal,
    max_tool_rounds: int,
    round_tools: Any,
    dispatch: Callable[[int, Any], tuple],
    execute_tool: Callable[[int, dict], None],
    on_round_result: Callable[[int, dict, Any, Any], None] | None = None,
    on_tool_round: Callable[[int, dict], None] | None = None,
    retry_bonus: Callable[[int, dict, Any, Any], bool] | None = None,
    max_retry_bonus: int = 2,
) -> LoopOutcome:
    """Drive a bounded LLM tool-calling loop with the triple abort check.

    The loop owns control flow + the three abort-check placements; all
    engine-specific I/O is delegated to the hooks below. It never catches
    exceptions raised by ``dispatch`` / ``execute_tool`` (so a dispatcher
    ``AbortedError`` reaches the caller's handler).

    Args:
        abort: the unified abort signal (checked at all three points).
        max_tool_rounds: number of tool-eligible rounds. Rounds ``0 ..
            max_tool_rounds-1`` are offered ``round_tools``; the final round
            (index ``max_tool_rounds``) is offered ``None`` so the model must
            produce its answer without more tools.
        round_tools: tool schema list passed to ``dispatch`` on tool-eligible
            rounds (``None`` on the final round).
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
            after ``on_round_result``. Returning True means "this turn ended
            prematurely (e.g. a premature stream close) — grant ONE extra
            round". It dynamically expands the round ceiling exactly like the
            chat orchestrator's ``_premature_retry_count`` (so even a
            no-tools turn can be retried), capped at ``max_retry_bonus``. When
            it fires, the round is NOT treated as a natural completion. Default
            ``None`` → the loop is byte-equivalent to the original for-range.
        max_retry_bonus: ceiling on total bonus rounds ``retry_bonus`` may
            grant, so a stuck premature-close can't loop forever (default 2,
            matching orchestrator's ``_PREMATURE_RETRY_MAX``).

    Returns:
        LoopOutcome describing why the loop stopped (incl. ``exit_reason``).
    """
    outcome = LoopOutcome()

    # Dynamic ceiling (mirrors the orchestrator's while-loop): the base cap is
    # ``max_tool_rounds`` tool-eligible rounds + 1 final tools=None round; a
    # premature-close retry_bonus grows ``bonus`` so the ceiling expands
    # mid-loop. rnd runs 0.. and the loop continues while rnd <= cap + bonus.
    bonus = 0
    rnd = -1
    while True:
        rnd += 1
        if rnd > max_tool_rounds + bonus:
            outcome.exit_reason = 'max_rounds_exhausted'
            break

        # (1) BEFORE-ROUND — don't start a turn after Stop.
        if abort.aborted:
            outcome.aborted = True
            outcome.exit_reason = 'aborted_before_round'
            break

        tools = round_tools if rnd < max_tool_rounds else None
        msg, finish, usage = dispatch(rnd, tools)
        outcome.rounds += 1
        if on_round_result is not None:
            on_round_result(rnd, msg, finish, usage)

        # Premature-close retry: grant one bonus round (capped) and DON'T treat
        # this turn as a natural completion.
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

        for tc in tool_calls:
            # (3) BETWEEN-TOOLS — a Stop pressed during a slow tool must skip
            # the remaining queued tools and NOT start a fresh round. Removing
            # this check reintroduces the "Stop has limited effect" bug.
            if abort.aborted:
                outcome.aborted = True
                outcome.exit_reason = 'aborted_between_tools'
                break
            execute_tool(rnd, tc)

        if outcome.aborted:
            break

    return outcome
