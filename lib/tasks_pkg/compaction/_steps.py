# HOT_PATH
"""Compaction step framework — the extension seam for compression methods.

A *compaction method* is a single function ``step(ctx) -> int`` (tokens
saved) registered by name and selected purely by configuration.  The
framework is built around the two axes that distinguish every method
*form*:

  * **kind** — what the method may DO to the message list:
      - ``'transform'``  : edit message *content* in place (the cheap,
        every-round, LLM-free majority — dedup, folding, truncation, …).
      - ``'structural'`` : delete / reshape whole turns.  Granted a
        :class:`MessageEditor` (``ctx.edit``) that keeps tool_call↔tool
        pairing intact so a deletion can never orphan a tool result.
  * **needs** — what the method may CALL:
      - ``()``           : LLM-free (test-pinned for the L1 host).
      - ``('llm',)``     : may call ``ctx.summarize(...)`` (a budgeted
        cheap-model summary).  Only granted by the advanced host.

A method declares its form at registration; the *host* that runs it
grants exactly the capabilities that form is allowed, and nothing else —
so a transform method literally cannot reach ``ctx.edit`` and an
LLM-free method cannot reach ``ctx.summarize``.  Same philosophy as the
original L1 landmines (cache-prefix, durable persistence): the dangerous
primitive isn't in reach unless the form earns it.

Hosts
-----
  * **L1** (``micro_compact``) runs ``kind='transform'`` + LLM-free steps
    every round.  Cheap, no LLM, in-place content edits, durable via the
    ``_stamp_l1`` closure.  This is the default, test-pinned path.
  * **Advanced** (``_advanced.advanced_compact``) runs
    ``kind='structural'`` and/or ``needs=('llm',)`` steps.  Opt-in via
    ``task['config']['compaction']['advanced_steps']`` — default off, so
    shipped behavior is unchanged.

Selection is pure data and never mutates a global, so A/B arms run
concurrently.  A third party adds a method by writing ONE function plus
one ``@register_step(...)`` decorator and naming it in config.

What this module deliberately is NOT
------------------------------------
No ABCs, no entry-point discovery, no config-schema validation, no
external plugin-load path.  A plain dataclass + a registry dict is the
lightest thing that makes ablation, drop-in, and multi-form methods
work, per the project's "no speculative abstraction" rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from lib.log import get_logger

logger = get_logger(__name__)


# Method-form vocabulary.
STEP_KIND_TRANSFORM = 'transform'
STEP_KIND_STRUCTURAL = 'structural'
_VALID_KINDS = frozenset({STEP_KIND_TRANSFORM, STEP_KIND_STRUCTURAL})


# ═══════════════════════════════════════════════════════════════════════════════
#  CompactionContext — the safe primitives handed to every step
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class CompactionContext:
    """Everything a compaction step needs, with the landmines pre-solved.

    A step mutates ``messages`` in place to shrink it and returns the
    estimated number of tokens saved.  It MUST NOT touch the database,
    compute cache boundaries, or emit SSE events directly — those are
    provided as methods/handles so the rules can never be forgotten or
    re-implemented incorrectly:

      * Cache safety: call :meth:`is_in_cache_prefix` before mutating
        ``messages[idx]``.  Mutating a message inside the prompt-cache
        prefix busts the cache (re-cache at 1.25–2.0×).
      * Durability + UX (transform steps): call :meth:`stamp` after
        compacting a tool result so the placeholder is written to the
        source-of-truth ``toolContent`` field and a ``tool_compacted``
        SSE event is emitted.
      * Structural surgery (``kind='structural'`` steps only): use
        :attr:`edit` (a :class:`MessageEditor`).  It is ``None`` for
        transform steps — the capability simply isn't in reach.
      * LLM summary (``needs=('llm',)`` steps only): call
        :meth:`summarize`.  Raises if the host didn't grant it.

    Attributes:
        messages: The live api-form messages list.  Mutated in place.
        conv_id:  Conversation ID (for logging / cache lookup).
        task:     Live task dict, or ``None`` in unit tests.
        constants: The compaction package namespace (or a per-call
            overlay) read at call time so hot-reload / per-call overrides
            propagate.
        cache_prefix_count: Number of leading messages inside the
            prompt-cache prefix that MUST be left byte-identical.
        stamp_fn: Bound ``_stamp_l1`` closure (transform durability).
        ignore_cache_prefix: Aggressive arm — treat the prefix as
            compactable.
        edit: A :class:`MessageEditor`, granted only to structural steps.
        summarize_fn: A bound cheap-model summary callable, granted only
            to ``needs=('llm',)`` steps.
        scratch: Per-pass shared scratch space between steps.
    """

    messages: list
    conv_id: str = ''
    task: Optional[dict] = None
    constants: object = None
    cache_prefix_count: int = 0
    stamp_fn: Optional[Callable[[dict, int, int], None]] = None
    ignore_cache_prefix: bool = False
    edit: Optional['MessageEditor'] = None
    summarize_fn: Optional[Callable[..., str]] = None
    scratch: dict = field(default_factory=dict)

    def is_in_cache_prefix(self, idx: int) -> bool:
        """Return True if ``messages[idx]`` is inside the prompt-cache
        prefix and therefore must be left byte-identical.

        Always False when :attr:`ignore_cache_prefix` is set (aggressive
        arm) so steps are free to compact anywhere."""
        if self.ignore_cache_prefix:
            return False
        return idx < self.cache_prefix_count

    def stamp(self, msg: dict, before_chars: int, after_chars: int) -> None:
        """Emit request-local evidence for a just-compacted tool result."""
        if self.stamp_fn is not None:
            self.stamp_fn(msg, before_chars, after_chars)

    def summarize(self, text: str, *, instruction: str = '',
                  max_tokens: int = 512) -> str:
        """Summarize ``text`` with a cheap model.  Available only to steps
        registered with ``needs=('llm',)`` and run by a host that grants
        the capability — otherwise raises ``RuntimeError`` (the grant is
        opt-in by construction)."""
        if self.summarize_fn is None:
            raise RuntimeError(
                'ctx.summarize is not granted: register the step with '
                "needs=('llm',) and run it under the advanced host")
        return self.summarize_fn(text, instruction=instruction,
                                 max_tokens=max_tokens)


# A compaction step: mutate ctx.messages in place, return tokens saved.
CompactionStep = Callable[[CompactionContext], int]


# ═══════════════════════════════════════════════════════════════════════════════
#  MessageEditor — safe turn-level structural surgery (granted to structural steps)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class _Turn:
    """A conversational turn: a ``user`` message plus every subsequent
    non-user message until the next ``user`` message.  ``indices`` are the
    message-list positions this turn occupies."""
    start: int
    end: int          # exclusive
    indices: list


class MessageEditor:
    """Turn-level structural editing that can never orphan a tool pair.

    The unit of deletion is the **whole turn**.  Because a model's
    ``tool_calls`` and their ``tool`` results always live within the same
    turn (no tool result ever precedes its user request or crosses into
    the next turn), deleting whole turns is structurally safe — it cannot
    leave a ``tool`` message without its ``assistant(tool_calls)`` parent.

    Protections enforced regardless of what a step asks for:
      * the leading ``system`` message (and any pre-first-user messages)
        is never part of a turn, so never removed;
      * the **in-flight (last) turn** is never removed;
      * turns overlapping the **cache prefix** are not removed unless the
        context is in aggressive mode (``ignore_cache_prefix``).

    Operates on ``ctx.messages`` in place.  Granted only to steps
    registered ``kind='structural'``.
    """

    def __init__(self, ctx: CompactionContext):
        self._ctx = ctx
        self.removed_turns = 0
        self.removed_messages = 0

    # ── Inspection ──────────────────────────────────────────────────
    def turns(self) -> list[_Turn]:
        """All turns in the current message list (system prefix excluded)."""
        msgs = self._ctx.messages
        user_idxs = [i for i, m in enumerate(msgs) if m.get('role') == 'user']
        turns: list[_Turn] = []
        for k, start in enumerate(user_idxs):
            end = user_idxs[k + 1] if k + 1 < len(user_idxs) else len(msgs)
            turns.append(_Turn(start, end, list(range(start, end))))
        return turns

    def evictable_turns(self) -> list[_Turn]:
        """Turns that are safe to drop: excludes the in-flight last turn
        and any turn overlapping the cache prefix."""
        turns = self.turns()
        if len(turns) <= 1:
            return []
        out: list[_Turn] = []
        for t in turns[:-1]:  # never the in-flight last turn
            if any(self._ctx.is_in_cache_prefix(i) for i in t.indices):
                continue
            out.append(t)
        return out

    # ── Mutation ────────────────────────────────────────────────────
    def drop_turns(self, turns: list[_Turn]) -> int:
        """Delete the given turns from ``ctx.messages`` in place.

        Silently skips any turn that is protected (in-flight or
        cache-prefix).  Returns the estimated tokens saved.  Applied
        immediately, so a later step sees the already-shortened list.
        """
        if not turns:
            return 0
        all_turns = self.turns()
        last_start = all_turns[-1].start if all_turns else -1

        remove: set[int] = set()
        dropped = 0
        for t in turns:
            if t.start == last_start:
                continue  # never drop the in-flight turn
            if any(self._ctx.is_in_cache_prefix(i) for i in t.indices):
                continue
            remove.update(t.indices)
            dropped += 1

        if not remove:
            return 0

        msgs = self._ctx.messages
        saved = 0
        kept = []
        for i, m in enumerate(msgs):
            if i in remove:
                saved += _estimate_msg_chars(m) // 4
            else:
                kept.append(m)
        msgs[:] = kept
        self.removed_turns += dropped
        self.removed_messages += len(remove)
        logger.info('[Editor] conv=%s  dropped %d turn(s) / %d message(s) '
                    '(~%d tokens saved)',
                    self._ctx.conv_id[:8] if self._ctx.conv_id else '?',
                    dropped, len(remove), saved)
        return saved


def _estimate_msg_chars(msg: dict) -> int:
    """Cheap char estimate for a message (content + reasoning)."""
    total = 0
    for field_name in ('content', 'reasoning_content'):
        v = msg.get(field_name)
        if isinstance(v, str):
            total += len(v)
        elif isinstance(v, list):
            for b in v:
                if isinstance(b, dict) and b.get('type') == 'text':
                    total += len(b.get('text', ''))
    for tc in msg.get('tool_calls', []) or []:
        total += len(tc.get('function', {}).get('arguments', '') or '')
    return total


# ═══════════════════════════════════════════════════════════════════════════════
#  Per-call constant overlay (concurrency-safe tunable overrides)
# ═══════════════════════════════════════════════════════════════════════════════

class _ConstantsView:
    """Read-only overlay of per-call overrides on top of the compaction
    constants module. Attribute reads hit the override dict first, then
    fall through to the package.  Nothing is ever written back to the
    global, so concurrent arms can use different values safely."""

    __slots__ = ('_base', '_overrides')

    def __init__(self, base, overrides: dict):
        object.__setattr__(self, '_base', base)
        object.__setattr__(self, '_overrides', dict(overrides or {}))

    def __getattr__(self, name):
        ov = object.__getattribute__(self, '_overrides')
        if name in ov:
            return ov[name]
        return getattr(object.__getattribute__(self, '_base'), name)


def make_constants(base, overrides: Optional[dict]):
    """Return ``base`` unchanged when there are no overrides (zero-cost,
    preserves the hot-reload identity), else a :class:`_ConstantsView`."""
    if not overrides:
        return base
    return _ConstantsView(base, overrides)


# ═══════════════════════════════════════════════════════════════════════════════
#  Step registry — name → spec (function + declared form)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class _StepSpec:
    fn: CompactionStep
    kind: str
    needs: tuple


_STEP_REGISTRY: dict[str, _StepSpec] = {}


def register_step(name: str, *, kind: str = STEP_KIND_TRANSFORM,
                  needs: tuple = ()) -> Callable[[CompactionStep], CompactionStep]:
    """Register a compaction step under ``name`` with its declared form.

    Args:
        name:  Selection key used in ``compaction.steps`` / ``advanced_steps``.
        kind:  ``'transform'`` (default, in-place content edit) or
               ``'structural'`` (whole-turn surgery via ``ctx.edit``).
        needs: Capability tuple — ``('llm',)`` to request ``ctx.summarize``.

    Usage::

        @register_step('my_method')                      # transform, LLM-free
        @register_step('drop_turns', kind='structural')  # needs ctx.edit
        @register_step('smart_sum', needs=('llm',))       # needs ctx.summarize
        def my_method(ctx: CompactionContext) -> int:
            ...
            return tokens_saved
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f'unknown step kind {kind!r}; must be one of '
                         f'{sorted(_VALID_KINDS)}')
    needs_t = tuple(needs or ())

    def _decorator(fn: CompactionStep) -> CompactionStep:
        if name in _STEP_REGISTRY and _STEP_REGISTRY[name].fn is not fn:
            logger.warning('[Steps] Overwriting already-registered step %r', name)
        _STEP_REGISTRY[name] = _StepSpec(fn=fn, kind=kind, needs=needs_t)
        return fn
    return _decorator


def get_step(name: str) -> Optional[CompactionStep]:
    """Return the registered step function for ``name``, or ``None``."""
    spec = _STEP_REGISTRY.get(name)
    return spec.fn if spec else None


def get_step_spec(name: str) -> Optional[_StepSpec]:
    """Return the full spec (fn + kind + needs) for ``name``, or ``None``."""
    return _STEP_REGISTRY.get(name)


def list_steps(kind: str | None = None) -> list[str]:
    """Return registered step names (sorted).  Filter by ``kind`` when given."""
    if kind is None:
        return sorted(_STEP_REGISTRY)
    return sorted(n for n, s in _STEP_REGISTRY.items() if s.kind == kind)


def run_steps(step_names: list[str], ctx: CompactionContext, *,
              allow_kinds: tuple = (STEP_KIND_TRANSFORM,),
              allow_llm: bool = False) -> int:
    """Run the named steps in order against ``ctx``; return total tokens saved.

    The host declares which capabilities it grants via ``allow_kinds`` /
    ``allow_llm``.  A step whose declared form exceeds the grant is skipped
    with a clear warning — so naming a ``structural`` or LLM step in the
    L1 (transform-only) host is safe, not a crash.  This is what keeps the
    cheap every-round host LLM-free and in-place by construction.

    Unknown names and per-step exceptions are logged and skipped — one
    buggy experimental method must never abort the compaction pass or the
    live turn (CLAUDE.md §2.2: degrade rather than crash the hot path).
    """
    total = 0
    for name in step_names:
        spec = _STEP_REGISTRY.get(name)
        if spec is None:
            logger.warning('[Steps] Unknown compaction step %r — skipping '
                           '(registered: %s)', name, list_steps())
            continue
        if spec.kind not in allow_kinds:
            logger.warning('[Steps] step %r is kind=%s but this host allows '
                           '%s — skipping (use the advanced host for '
                           'structural methods)', name, spec.kind,
                           sorted(allow_kinds))
            continue
        if 'llm' in spec.needs and not allow_llm:
            logger.warning('[Steps] step %r needs llm but this host is '
                           'LLM-free — skipping', name)
            continue
        try:
            saved = spec.fn(ctx)
            total += int(saved or 0)
        except Exception as e:
            logger.error('[Steps] step %r raised — skipping: %s',
                         name, e, exc_info=True)
    return total
