"""Repair primitives + the public validate-then-repair orchestrators.

The per-value repair stack (:func:`_repair_one_value`) and the two public
front doors that walk a tool call's args against its schema:
:func:`validate_then_repair` (decoded dict → repaired dict) and
:func:`parse_and_repair_tool_args` (raw JSON string → decode + repair). Plus
:func:`schema_hint`, the compact one-line arg-shape description used to make a
malformed-JSON retry message actionable.

Repair patterns, applied in this order (ordering is load-bearing) — see the
package ``__init__`` docstring for the full narrative.
"""

from __future__ import annotations

import json
import re
from typing import Any

from lib.log import audit_log, get_logger

from lib.tool_input_repair._schema import (
    RepairLog,
    _expected_types,
    _required_keys,
)
from lib.tool_input_repair._salvage import _try_schema_array_salvage
from lib.tool_input_repair._transform import (
    _apply_param_aliases,
    _apply_structural_transform,
)

logger = get_logger(__name__)


# ══════════════════════════════════════════
#  Repair primitives — pure, total, side-effect-free
# ══════════════════════════════════════════

def _try_parse_json(s: str) -> tuple[bool, Any]:
    """Strict JSON.parse — returns (success, value). Never raises."""
    s = s.strip()
    if not s or s[0] not in '[{"':
        return False, None
    try:
        return True, json.loads(s)
    except (ValueError, TypeError) as _e_audit:
        logger.debug('[tool_input_repair] _try_parse_json caught %s: %s', type(_e_audit).__name__, _e_audit)
        return False, None


def _lenient_json(s: str) -> Any:
    """Best-effort recovery of a malformed JSON string.

    Reuses ``lib.utils.repair_json`` — the same battle-tested repairer the
    dispatcher applies to the *outer* arguments blob — so a malformed
    *inner* stringified value (truncated array, trailing comma,
    unterminated string, missing closer) gets identical treatment. Imported
    lazily to keep this module dependency-light. Raises on unrecoverable
    input; callers guard with try/except.
    """
    from lib.utils import repair_json
    return repair_json(s)


def _coerce_primitive(value: str, target: str) -> tuple[bool, Any]:
    """Coerce a string to ``integer`` / ``number`` / ``boolean``.

    Returns (success, coerced) — caller checks success before substituting.
    """
    s = value.strip()
    if target == 'integer':
        try:
            return True, int(s)
        except (ValueError, TypeError) as _e_audit:
            logger.debug('[tool_input_repair] _coerce_primitive caught %s: %s', type(_e_audit).__name__, _e_audit)
            return False, None
    if target == 'number':
        try:
            return True, float(s)
        except (ValueError, TypeError) as _e_audit:
            logger.debug('[tool_input_repair] _coerce_primitive caught %s: %s', type(_e_audit).__name__, _e_audit)
            return False, None
    if target == 'boolean':
        low = s.lower()
        if low in ('true', '1', 'yes'):
            return True, True
        if low in ('false', '0', 'no'):
            return True, False
        return False, None
    return False, None


# Anthropic native text tool-call markup that some models leak into a
# JSON string argument: ``<parameter name="path">``, ``</parameter>``,
# ``<invoke name="...">``, ``<function_calls>``, plus the ``antml:`` variants.
_LEAKED_TOOL_XML_RE = re.compile(
    r'</?(?:antml:)?(?:parameter|invoke|function_calls)\b[^>]*>',
    re.IGNORECASE,
)


def _strip_leaked_tool_call_syntax(value: str) -> tuple[bool, str]:
    """Strip leaked Anthropic tool-call markup from a string value.

    Returns ``(changed, cleaned)``. ``changed=False`` when no markup was
    present, so callers leave the value untouched.
    """
    if '<' not in value or not _LEAKED_TOOL_XML_RE.search(value):
        return False, value
    cleaned = _LEAKED_TOOL_XML_RE.sub('', value).strip()
    if cleaned != value:
        return True, cleaned
    return False, value


# ══════════════════════════════════════════
#  Per-key repair stack
# ══════════════════════════════════════════

def _repair_one_value(
    value: Any,
    expected: str,
    *,
    is_required: bool,
) -> tuple[bool, Any, str | None]:
    """Apply the ordered repair stack to a single (value, expected_type) pair.

    Returns ``(changed, new_value, pattern_name)``. ``changed=False`` means
    the value already matched its expected type — caller should leave it
    untouched.
    """
    actual = _json_type_of(value)
    if actual == expected:
        return False, value, None

    # 0. leaked_tool_call_syntax — model leaked Anthropic text tool-call
    #    markup (<parameter name="...">VALUE) as a literal string into a
    #    slot whose shape doesn't match. Strip it, recover VALUE, then
    #    re-run the stack so an array slot still gets wrapped. Scoped to
    #    the shape-mismatch path (the early return above protects
    #    well-formed string fields like write_file's content).
    if isinstance(value, str):
        stripped_changed, cleaned = _strip_leaked_tool_call_syntax(value)
        if stripped_changed:
            sub_changed, sub_val, _sub_pattern = _repair_one_value(
                cleaned, expected, is_required=is_required,
            )
            return True, (sub_val if sub_changed else cleaned), 'leaked_tool_call_syntax'

    # 1. null_omission — only valid for non-required keys; signal by
    #    returning a sentinel the caller recognizes as "delete this key".
    if value is None and not is_required:
        return True, _DELETE_KEY, 'null_omission'

    # 2. stringified_json — string that decodes into the expected shape
    if isinstance(value, str) and expected in ('array', 'object'):
        ok, parsed = _try_parse_json(value)
        if ok and _json_type_of(parsed) == expected:
            return True, parsed, 'stringified_json'
        # Strict parse failed but the string clearly INTENDED to be JSON
        # (leading '[' / '{'). Apply the same lenient recovery used on the
        # outer arguments blob (trailing commas, unterminated strings,
        # truncated/missing closers). This rescues a malformed *inner*
        # stringified array — e.g. read_files ``reads`` arriving as
        # ``'[{"path": "x", "end_line": 320'`` — that strict json.loads
        # rejects. Only substitute when the recovered value matches the
        # expected shape, so a genuinely-unrecoverable payload still falls
        # through to an honest tool-level error.
        if value.lstrip()[:1] in ('[', '{'):
            try:
                recovered = _lenient_json(value)
            except Exception as _e_audit:
                logger.debug('[tool_input_repair] lenient JSON recovery failed: %s', _e_audit)
            else:
                if _json_type_of(recovered) == expected:
                    return True, recovered, 'stringified_json'

    # 3. stringified_primitive — string that coerces to int/number/bool
    if isinstance(value, str) and expected in ('integer', 'number', 'boolean'):
        ok, coerced = _coerce_primitive(value, expected)
        if ok:
            return True, coerced, 'stringified_primitive'

    # 4. bare_string_to_array — a non-JSON string where an array is
    #    expected. Must run AFTER stringified_json: if pattern 2 already
    #    parsed the string, we wouldn't reach here.
    if isinstance(value, str) and expected == 'array':
        # A string that clearly INTENDED to be JSON (starts with '[' or
        # '{') but failed pattern 2's json.loads is *malformed JSON*, not a
        # bare scalar. Wrapping it into ``['[{...]']`` manufactures a
        # superficially-valid array that fails one layer deeper with a vague
        # "Invalid edit entry" — and falsely stamps an "auto-fixed" badge.
        # Leave it untouched so the tool returns an honest, actionable error
        # and the model can re-emit a real array next turn.
        if value.lstrip()[:1] in ('[', '{'):
            return False, value, None
        return True, [value], 'bare_string_to_array'

    # 5. empty_placeholder_unwrap — dict where array is expected
    if isinstance(value, dict) and expected == 'array':
        return True, list(value.values()), 'empty_placeholder_unwrap'

    return False, value, None


_DELETE_KEY = object()  # private sentinel; never escapes this module


def _json_type_of(value: Any) -> str:
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, int):
        return 'integer'
    if isinstance(value, float):
        return 'number'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'object'
    return 'unknown'


# ══════════════════════════════════════════
#  Public orchestrator
# ══════════════════════════════════════════

def validate_then_repair(
    tool_name: str,
    fn_args: Any,
    *,
    model: str = '',
) -> tuple[dict[str, Any], RepairLog]:
    """Walk one tool call's args against its schema; repair where possible.

    Args:
        tool_name: Name of the tool being called.
        fn_args: The JSON-decoded ``arguments`` dict from the model.
        model: Optional model identifier for audit telemetry.

    Returns:
        ``(repaired_args, repair_log)``. ``repair_log`` is a list of
        ``(json_path, pattern_name)`` tuples — empty when no repairs were
        needed. The original ``fn_args`` is **not** mutated.
    """
    if not isinstance(fn_args, dict):
        return fn_args if isinstance(fn_args, dict) else {}, []

    expected = _expected_types(tool_name)
    if not expected:
        return fn_args, []

    required = _required_keys(tool_name)
    repaired: dict[str, Any] = dict(fn_args)
    log: RepairLog = []

    # ── Structural transform (right tool, wrong-harness whole-payload shape) ──
    # Runs FIRST: reshapes a foreign payload structure (Claude Code MultiEdit /
    # AskUserQuestion) into our schema BEFORE the key-alias + type passes mop up
    # any residual mismatch inside the reshaped result.
    repaired, _shape_changed = _apply_structural_transform(tool_name, repaired)
    if _shape_changed:
        log.append((tool_name, 'structural_transform'))

    # ── Parameter-KEY alias rename (right tool, wrong-harness arg names) ──
    # Must run BEFORE the type-walk so a renamed key is then type-checked too.
    repaired, _alias_log = _apply_param_aliases(tool_name, repaired, expected)
    log.extend(_alias_log)

    for key, exp_type in expected.items():
        if key not in repaired:
            continue
        changed, new_val, pattern = _repair_one_value(
            repaired[key], exp_type, is_required=(key in required),
        )
        if changed:
            if new_val is _DELETE_KEY:
                del repaired[key]
            else:
                repaired[key] = new_val
            log.append((key, pattern or 'unknown'))
        elif exp_type == 'array' and isinstance(repaired[key], str):
            # Last resort: every generic pass (incl. repair_json) failed to
            # parse this stringified array. Try schema-guided salvage, which
            # anchors on the declared item keys+types instead of the broken
            # punctuation. Gated OFF for free-text editor tools.
            salvaged = _try_schema_array_salvage(tool_name, key, repaired[key])
            if salvaged is not None:
                repaired[key] = salvaged
                log.append((key, 'schema_array_salvage'))

    if log:
        for path, pattern in log:
            audit_log(
                'tool_input_repaired',
                tool=tool_name,
                model=model,
                path=path,
                pattern=pattern,
            )
        logger.info(
            '[ToolRepair] %s: applied %d repair(s) %s',
            tool_name, len(log), log,
        )

    return repaired, log


def parse_and_repair_tool_args(
    tool_name: str,
    args_raw: Any,
    *,
    model: str = '',
) -> tuple[dict[str, Any], RepairLog]:
    """Decode a tool call's raw ``arguments`` AND run the schema repair pass.

    The one-call front door for any agent loop that is NOT the main chat
    dispatcher (paper report / Q&A, and any future secondary harness):
    JSON-decode the raw ``arguments`` payload, then :func:`validate_then_repair`
    against the tool's declared schema. Keeping this here — next to the repair
    logic itself — means every harness coerces a schema-violating shape the
    SAME way, with no inline reimplementation to drift.

    The bug this exists to kill: a model that emits e.g.
    ``{"queries": "<long string>"}`` (a bare string where the schema declares
    an array of objects) must NOT be iterated character-by-character by
    downstream consumers (the "507 punctuation searches" report bug). The
    ``bare_string_to_array`` repair turns it into a single-element array.

    The main chat dispatcher (``lib/tasks_pkg/tool_dispatch/_pipeline.py``) keeps its own
    richer inline path because it additionally tracks malformed-JSON recovery
    for a UI "auto-fixed" badge and builds a model-facing retry message; this
    helper is the simpler ``(dict, log)`` contract its secondary callers need.

    Args:
        tool_name: Name of the tool being called.
        args_raw: The raw ``arguments`` — a JSON string (as the model emits)
            or an already-decoded dict.
        model: Optional model id for audit telemetry.

    Returns:
        ``(fn_args_dict, repair_log)``. Never raises — a JSON parse failure or
        a non-dict payload yields ``({}, [])`` so the caller can surface an
        honest "no arguments" error.
    """
    try:
        fn_args = (json.loads(args_raw) if isinstance(args_raw, str)
                   else (args_raw or {}))
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[ToolRepair] Bad JSON args for %s: %s', tool_name, e)
        return {}, []
    if not isinstance(fn_args, dict):
        return {}, []
    try:
        return validate_then_repair(tool_name, fn_args, model=model)
    except Exception as e:
        logger.warning('[ToolRepair] repair pass failed for %s (passing through): %s',
                       tool_name, e)
        return fn_args, []


def schema_hint(tool_name: str) -> str:
    """Return a compact one-line description of a tool's expected arg shape.

    Used to make the malformed-JSON retry message actionable: instead of a
    bare "retry with valid JSON", the model is shown which keys are required
    and their JSON types, e.g. ``read_files expects JSON like
    {"reads": [array], ...} (required: ). All keys: reads(array),
    path(string), ...``. Returns ``''`` when the tool has no indexed schema
    so the caller can fall back to the generic message.
    """
    expected = _expected_types(tool_name)
    if not expected:
        return ''
    required = _required_keys(tool_name)
    parts = []
    for key, t in expected.items():
        mark = '*' if key in required else ''
        parts.append(f'{key}{mark}({t})')
    req_list = ', '.join(sorted(required)) if required else 'none'
    return (
        f'`{tool_name}` expects a JSON object — keys: {", ".join(parts)} '
        f'(* = required: {req_list}).'
    )
