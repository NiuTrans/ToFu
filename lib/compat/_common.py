"""lib/compat/_common.py — shared request-translation helpers for the
OpenAI- and Anthropic-compat surfaces.

``translate_openai_request`` and ``translate_anthropic_request`` independently
mapped the SAME cross-format fields onto the Tofu ``cfg`` — model/preset,
temperature, max_tokens→maxTokens, top_p→topP, tools, tool_choice — and both
ran the identical "explicit tools → disable auto-injected tools" block followed
by ``apply_headless_personal_defaults(cfg)``. That duplication lived here now so
the two translators share one implementation and only carry their format-
specific extras (OpenAI: seed / response_format / reasoning_effort; Anthropic:
system / stop_sequences / metadata / thinking).

Pure functions, no Flask imports.
"""

from __future__ import annotations

from lib.ids import short_id  # noqa: F401 — re-exported for back-compat
from lib.log import get_logger
from lib.turn_verdict import (
    TerminalTaskFailure,
    TurnVerdict,
    require_deliverable_task,
)

logger = get_logger(__name__)


CompatTerminalFailure = TerminalTaskFailure


def require_deliverable_terminal(
    task: dict,
    terminal_event: dict | None = None,
) -> TurnVerdict:
    """Return one canonical terminal verdict or reject a false success.

    Truncated and explicitly interrupted outputs remain representable in the
    vendor protocols.  A failed verdict must travel on their error channel;
    it must never be normalized to ``stop`` / ``end_turn``.
    """
    return require_deliverable_task(task, terminal_event)

# ``short_id`` now lives in lib/ids.py (the single, dependency-free home shared
# by billing / conversations / tasks / routes). Re-exported here so the compat
# translators + test_compat_common keep importing it from lib.compat._common.


def apply_common_cfg(cfg: dict, body: dict) -> None:
    """Map the fields common to both compat request shapes onto ``cfg``.

    Handles model/preset, temperature, max_tokens→maxTokens, top_p→topP,
    tools, tool_choice. Format-specific fields (seed, response_format, stop
    vs stop_sequences, metadata, thinking, reasoning_effort) are left to the
    caller. Mutates ``cfg`` in place.
    """
    if body.get('model'):
        cfg['model'] = body['model']
        cfg['preset'] = body['model']
    if 'temperature' in body:
        cfg['temperature'] = body['temperature']
    if 'max_tokens' in body:
        cfg['maxTokens'] = body['max_tokens']
    if 'top_p' in body:
        cfg['topP'] = body['top_p']
    if 'tools' in body:
        cfg['tools'] = body['tools']
    if 'tool_choice' in body:
        cfg['toolChoice'] = body['tool_choice']


# ── Inbound effort / thinking semantics (adaptive generation) ─────────
#
# The five effort rungs Claude Opus 4.7+ exposes. Each one is ALSO a legal
# Tofu ``thinkingDepth`` (lib/agent_options.py ``_FIELDS``), so the inbound
# mapping is IDENTITY — an explicit rung needs no approximation. Anything
# outside this set is dropped rather than forwarded: ``thinkingDepth`` is a
# closed enum downstream, and an unknown value would only fail validation far
# from the request that introduced it.
_EFFORT_RUNGS = frozenset({'low', 'medium', 'high', 'xhigh', 'max'})

# OpenAI's ``minimal`` has no Tofu rung of its own. It maps DOWN to ``low``,
# never up: a caller asking for the cheapest possible reasoning must not be
# quietly billed for a deeper tier.
_EFFORT_ALIASES = {'minimal': 'low'}


def _extract_effort(body: dict) -> str:
    """Return the requested effort rung from a compat request body, or ''.

    Reads, in precedence order:
      1. ``output_config.effort`` — the DOCUMENTED position on the Anthropic
         Messages API for the adaptive-thinking generation (Opus 4.7+).
      2. top-level ``effort`` — the position Tofu's own outbound builder uses
         and the one empirically honoured by the AIGC gateway's OpenAI-compat
         line, so a body echoed back at us carries it here.
      3. ``reasoning_effort`` / ``reasoning.effort`` — the OpenAI spelling.

    The documented position wins when several are present. An unrecognised
    rung yields '' (see ``_EFFORT_RUNGS``).
    """
    raw = ''
    oc = body.get('output_config')
    if isinstance(oc, dict) and oc.get('effort'):
        raw = oc['effort']
    if not raw and body.get('effort'):
        raw = body['effort']
    if not raw:
        reasoning = body.get('reasoning')
        raw = (body.get('reasoning_effort')
               or (reasoning.get('effort') if isinstance(reasoning, dict) else '')
               or '')
    if not isinstance(raw, str):
        return ''
    rung = _EFFORT_ALIASES.get(raw.strip().lower(), raw.strip().lower())
    if rung and rung not in _EFFORT_RUNGS:
        logger.debug('[compat] ignoring unrecognised effort rung %r', raw)
        return ''
    return rung


def apply_thinking_cfg(cfg: dict, body: dict) -> None:
    """Map an inbound request's thinking intent onto ``cfg``. In place.

    Handles all four shapes an Anthropic-API client can present, which is the
    whole point: before this existed only ``{'type': 'enabled'}`` was
    understood — the PRE-4.7 form that Opus 4.7+ now rejects with HTTP 400 —
    so every Opus-5-era client fell through and had its stated intent dropped.

      * ``{'type': 'adaptive'}``  → enabled. THE enable form on 4.7+.
      * ``{'type': 'disabled'}``  → explicitly ``False``. Critically NOT the
        same as absent: the direct-model path in
        ``lib/tasks_pkg/model_config._resolve_model_config`` defaults
        ``thinkingEnabled`` to True, so leaving it None INVERTS an explicit
        "off" into "on" (measured on HEAD before this fix).
      * ``{'type': 'enabled', 'budget_tokens': N}`` → enabled, with the
        original token-band approximation preserved for pre-4.7 clients.
      * ABSENT → the model's REAL vendor default, because this surface
        emulates the Anthropic Messages API:
          - adaptive-generation Claude (``is_claude_opus_47``) thinks by
            default → True;
          - pre-4.7 Claude defaults off → False;
          - a NON-Claude model is left UNSET, since we are not emulating some
            other vendor's default and the existing downstream default must
            keep applying. Do not "simplify" this third branch into False —
            that would silently disable thinking for every GLM/Qwen/Kimi
            caller of this endpoint.

    The effort rung (see ``_extract_effort``) is applied whenever thinking is
    not disabled. It is deliberately NOT applied on the disabled path: effort
    is meaningless without thinking, and Anthropic rejects
    ``thinking=disabled`` combined with ``xhigh``/``max`` outright (HTTP 400).
    """
    from lib.model_info import is_claude, is_claude_opus_47

    thinking_obj = body.get('thinking')
    model = body.get('model') or ''
    t_type = ''
    if isinstance(thinking_obj, dict):
        t_type = (thinking_obj.get('type') or '').strip().lower()

    if t_type == 'disabled':
        cfg['thinkingEnabled'] = False
        return

    if t_type in ('adaptive', 'enabled'):
        cfg['thinkingEnabled'] = True
        if t_type == 'enabled':
            budget = thinking_obj.get('budget_tokens')
            if isinstance(budget, int) and budget > 0:
                cfg['thinkingDepth'] = (
                    'medium' if budget <= 8192 else
                    'high' if budget <= 16384 else
                    'xhigh' if budget <= 32768 else 'max')
    elif not t_type:
        # Absent — mirror the model's own default rather than guessing.
        if is_claude_opus_47(model):
            cfg['thinkingEnabled'] = True
        elif is_claude(model):
            cfg['thinkingEnabled'] = False
        # else: non-Claude → leave unset (see docstring).

    # An explicit rung always wins over a budget-derived approximation.
    rung = _extract_effort(body)
    if rung:
        cfg['thinkingDepth'] = rung
        cfg.setdefault('thinkingEnabled', True)


def apply_tools_and_personal_defaults(cfg: dict, body: dict) -> None:
    """Apply the shared post-mapping cfg policy for a compat request.

    1. When the caller supplied an explicit ``tools`` list, treat it as the
       canonical surface: disable Tofu's auto-injected search/fetch/MCP tools
       so the model isn't surprised with extra capabilities (``setdefault`` so
       an explicit caller cfg still wins).
    2. Fail app-personal capabilities (memory store + preference profile)
       closed on this headless surface regardless of tools — the operator's
       personal state must never ride a compat call. Single source of truth:
       ``lib.agent_core.personal_scope``.
    """
    if 'tools' in body:
        cfg.setdefault('searchMode', 'off')
        cfg.setdefault('fetchEnabled', False)
        cfg.setdefault('mcpEnabled', False)

    from lib.agent_core.personal_scope import apply_headless_personal_defaults
    apply_headless_personal_defaults(cfg)


__all__ = [
    'CompatTerminalFailure',
    'apply_common_cfg',
    'apply_thinking_cfg',
    'apply_tools_and_personal_defaults',
    'require_deliverable_terminal',
    'short_id',
]
