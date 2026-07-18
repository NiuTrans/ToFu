"""Unified tool-call ingestion seam — the single front door every dispatch
path funnels a raw ``tool_call`` through before executing it.

Historically the ingestion preamble (name-drop guards, name-alias, JSON decode
+ repair, schema/param repair, hallucination reject) was hand-reimplemented at
four sites, each covering only a SUBSET. :func:`ingest_tool_call` is the ONE
place all five stages live so parity is structural, not a checklist. It is pure
orchestration over the existing primitives — it adds NO new repair logic.
Presentation concerns (UI "auto-fixed" badges, SSE early-announce, autopilot
loop-break, phantom empty-arg dedup) stay in the caller.
"""

from __future__ import annotations

import json
from typing import Any

from lib.log import get_logger

from lib.tool_input_repair._classify import classify_tool_call, resolve_tool_name
from lib.tool_input_repair._rejection import (
    build_rejection_message,
    clear_rejection,
    record_rejection,
    report_hallucinated,
    report_tool_name_aliased,
)
from lib.tool_input_repair._repair import schema_hint, validate_then_repair
from lib.tool_input_repair._schema import RepairLog, _schemas

logger = get_logger(__name__)


# Names to DROP outright (never a real tool call): proxy artefacts like
# ``antml:thinking`` / ``__internal`` and XML-corrupted names. Mirrors the
# guards at the top of ``parse_tool_calls``. A dropped call must be skipped by
# the caller, NOT executed and NOT rejected-as-hallucination (it's a streaming
# artefact, not a model decision).
def _tool_name_drop_reason(name: str) -> str | None:
    """Return why a tool name must be dropped, or None if it's dispatchable.

    * ``''`` / non-str → 'missing'
    * contains ``:`` or leading ``__`` → 'internal_artifact' (proxy leak, e.g.
      ``antml:thinking``)
    * not ``[A-Za-z0-9_-]+`` → 'malformed' (XML/HTML corruption, e.g.
      ``list_dir">.</parameter>``)
    """
    if not name or not isinstance(name, str):
        return 'missing'
    if ':' in name or name.startswith('__'):
        return 'internal_artifact'
    if not name.replace('_', '').replace('-', '').isalnum():
        return 'malformed'
    return None


class IngestedToolCall:
    """Normalized result of funnelling one raw ``tool_call`` through the pipe.

    Attributes:
        raw_name: The tool name exactly as the model emitted it.
        fn_name: The dispatchable name AFTER alias resolution (== raw_name when
            no alias fired). Meaningless when ``drop_reason`` is set.
        fn_args: The decoded + repaired argument dict (``{}`` on parse failure).
        alias_kind: ``'alias'`` / ``'casefold'`` when the name was rewritten,
            else ``None``.
        json_repaired: True when ``repair_json`` recovered malformed JSON.
        repair_log: The :data:`RepairLog` from ``validate_then_repair`` (schema/
            param coercions), empty when nothing was touched.
        parse_error: A model-facing error string when the args were unparseable
            OR the call was rejected as a hallucination — the caller returns
            this to the LLM and skips execution. ``None`` on success.
        rejection: The ``classify_tool_call`` descriptor when the name is a
            hallucination (``{kind,attempted,suggestions,_repeat_count}``), else
            ``None``. Presence signals a rejected (never-executed) call.
        drop_reason: Non-None when the name is a streaming artefact that must be
            SKIPPED entirely (not executed, not rejected).
        repeat_count: Consecutive-rejection streak for this name in the conv
            (1 = first), for the caller's loop-breaker. 0 when not a rejection.
    """

    __slots__ = ('raw_name', 'fn_name', 'fn_args', 'alias_kind', 'json_repaired',
                 'repair_log', 'parse_error', 'rejection', 'drop_reason',
                 'repeat_count')

    def __init__(self, *, raw_name='', fn_name='', fn_args=None, alias_kind=None,
                 json_repaired=False, repair_log=None, parse_error=None,
                 rejection=None, drop_reason=None, repeat_count=0):
        self.raw_name = raw_name
        self.fn_name = fn_name
        self.fn_args = fn_args if fn_args is not None else {}
        self.alias_kind = alias_kind
        self.json_repaired = json_repaired
        self.repair_log = repair_log or []
        self.parse_error = parse_error
        self.rejection = rejection
        self.drop_reason = drop_reason
        self.repeat_count = repeat_count

    @property
    def dropped(self) -> bool:
        return self.drop_reason is not None

    @property
    def rejected(self) -> bool:
        return self.rejection is not None

    @property
    def ok(self) -> bool:
        """True when the call is dispatchable (not dropped, not rejected, no
        unrecoverable parse error)."""
        return not self.dropped and not self.rejected and self.parse_error is None

    def __repr__(self) -> str:
        if self.dropped:
            return f'<IngestedToolCall DROP {self.raw_name!r} ({self.drop_reason})>'
        if self.rejected:
            return f'<IngestedToolCall REJECT {self.raw_name!r}>'
        tag = f'{self.raw_name!r}'
        if self.alias_kind:
            tag += f'→{self.fn_name!r}'
        return f'<IngestedToolCall {tag} args={len(self.fn_args)}keys>'


def ingest_tool_call(
    tool_call: dict[str, Any],
    *,
    known_tools: set[str] | None = None,
    model: str = '',
    conv_id: str = '',
    reject_hallucinated: bool = True,
    emit_audit: bool = True,
) -> IngestedToolCall:
    """Funnel one raw ``tool_call`` through the full ingestion pipe.

    The stages, in order (each delegates to an existing primitive — this adds
    no new repair logic):

    1. **Drop guard** — :func:`_tool_name_drop_reason`. Streaming artefacts
       (``antml:thinking``, XML-corrupted names) → ``drop_reason`` set, caller
       SKIPS. Not executed, not rejected.
    2. **Name alias** — :func:`resolve_tool_name` against ``known_tools``.
       A confident 1:1 rewrite sets ``alias_kind`` + emits ``tool_name_aliased``.
       When the name IS real, :func:`clear_rejection` resets any prior streak.

    Args:
        tool_call: The raw ``{'function': {'name', 'arguments'}, 'id'?}`` dict.
        known_tools: The live REAL-tool set for this turn (built-ins + MCP +
            swarm + memory + custom). Used as the membership oracle for BOTH
            alias resolution and hallucination classification. ``None`` falls
            back to the schema-indexed built-ins (correct for the timer path,
            whose alias targets are all built-ins).
        model: Model id for audit telemetry.
        conv_id: Conversation id — keys the rejection streak so it spans
            autopilot follow-up tasks.
        reject_hallucinated: When False, an unknown name is NOT rejected — it
            passes through so the caller's own unknown-tool path handles it
            (e.g. a harness that wants the executor's raw error). Default True.
        emit_audit: When False, suppress the ``tool_name_aliased`` /
            ``tool_hallucinated`` audit events (e.g. a dry-run / test).

    Returns:
        An :class:`IngestedToolCall`. Check ``.dropped`` → skip; ``.rejection``
        / ``.parse_error`` → return the error to the LLM, skip execution;
        else dispatch ``.fn_name`` with ``.fn_args``.
    """
    fn_obj = (tool_call or {}).get('function') or {}
    raw_name = fn_obj.get('name', '') or ''

    # ── Stage 1: drop guard ──
    drop = _tool_name_drop_reason(raw_name)
    if drop:
        return IngestedToolCall(raw_name=raw_name, fn_name=raw_name,
                                drop_reason=drop)

    known = known_tools if known_tools is not None else set(_schemas().keys())

    # ── Stage 2: name alias ──
    fn_name = raw_name
    alias_kind = None
    if raw_name not in known:
        resolved, alias_kind = resolve_tool_name(raw_name, known=known)
        if alias_kind and resolved != raw_name:
            fn_name = resolved
            if emit_audit:
                report_tool_name_aliased(raw_name, resolved, alias_kind, model=model)
        else:
            alias_kind = None

    # ── Stage 5a: hallucination check (before wasting a parse on a fake tool) ──
    # Done here (post-alias, pre-parse) so a rejected call never parses/repairs
    # args it will never use — mirrors the chat dispatcher's short-circuit.
    if reject_hallucinated and fn_name not in known:
        descriptor = classify_tool_call(fn_name, known)
        if descriptor is not None:
            repeat_n = record_rejection(conv_id, fn_name)
            descriptor['_repeat_count'] = repeat_n
            if emit_audit:
                report_hallucinated(fn_name, descriptor, model=model)
            msg = build_rejection_message(descriptor, repeat_count=repeat_n,
                                          known_tools=known)
            return IngestedToolCall(
                raw_name=raw_name, fn_name=fn_name, alias_kind=alias_kind,
                rejection=descriptor, parse_error=msg, repeat_count=repeat_n)
    elif fn_name in known:
        # Real tool → reset any stale rejection streak for this name.
        clear_rejection(conv_id, fn_name)

    # ── Stage 3: JSON decode + repair ──
    raw_args = fn_obj.get('arguments', '') or ''
    json_repaired = False
    parse_error = None
    fn_args: dict[str, Any] = {}
    try:
        if isinstance(raw_args, dict):
            fn_args = raw_args
        else:
            _s = raw_args if isinstance(raw_args, str) else ''
            fn_args = json.loads(_s) if _s.strip() else {}
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        try:
            from lib.utils import repair_json as _repair_json
            fn_args = _repair_json(raw_args if isinstance(raw_args, str) else '{}')
            json_repaired = True
        except Exception as _rj_e:
            logger.debug('[ToolRepair] repair_json fallback failed for %s (%s) — '
                         'returning parse-error hint', fn_name, _rj_e)
            _hint = schema_hint(fn_name)
            parse_error = (
                f'ERROR: Your tool call for `{fn_name}` had malformed JSON '
                f'arguments — {e}. Please retry with valid JSON.'
                + (f' {_hint}' if _hint else ''))
            fn_args = {}
    if not isinstance(fn_args, dict):
        fn_args = {}

    # ── Stage 4: schema / param repair ──
    repair_log: RepairLog = []
    if parse_error is None:
        try:
            fn_args, repair_log = validate_then_repair(fn_name, fn_args, model=model)
        except Exception as e:
            logger.warning('[ingest] validate_then_repair failed for %s '
                           '(passing args through): %s', fn_name, e)

    return IngestedToolCall(
        raw_name=raw_name, fn_name=fn_name, fn_args=fn_args,
        alias_kind=alias_kind, json_repaired=json_repaired,
        repair_log=repair_log, parse_error=parse_error)
