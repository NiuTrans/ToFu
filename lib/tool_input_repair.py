"""Centralized tool-argument repair for open-model tool-calling failures.

Implements the validate-then-repair pattern from Awais 2025. The model's
JSON-decoded ``arguments`` dict is walked once against the tool's declared
JSON schema (extracted from ``lib/tools/*`` at startup); each declared
parameter that fails its expected type is run through an ordered repair
stack. **Valid inputs are never touched.** Only the exact failing paths
are repaired.

Repair patterns, applied in this order (ordering is load-bearing):

1. ``null_omission`` — ``{"k": None}`` for an optional / nullable key
   → delete the key entirely.
2. ``stringified_json`` — ``'["a","b"]'`` or ``'{"x":1}'`` arriving as a
   plain string where an array/object is expected → ``json.loads`` it.
3. ``stringified_primitive`` — ``"42"`` / ``"true"`` where an integer or
   boolean is expected → coerce. Catches the recurring trap logged in
   memory ``llm-string-coercion-traps``.
4. ``bare_string_to_array`` — ``"foo"`` where ``["foo"]`` is expected
   → wrap in a single-element array.
5. ``empty_placeholder_unwrap`` — ``{"a": "x"}`` where ``["x"]`` is
   expected → take ``list(d.values())``.
6. ``leaked_tool_call_syntax`` — the model emitted Anthropic native
   text tool-call markup (``<parameter name="path">VALUE``) as a literal
   string into a slot whose expected shape differs. Strip the markup,
   recover ``VALUE``, then re-run the stack (so an array slot still gets
   wrapped). Runs FIRST inside the per-value stack but only AFTER the
   ``actual == expected`` early-return, so well-formed string fields
   (e.g. ``write_file`` ``content``) are never touched.

**Critical:** ``stringified_json`` MUST run before ``bare_string_to_array``
— otherwise ``'["a","b"]'`` would be wrapped to ``['["a","b"]']``,
double-wrapping a recoverable input into garbage.

The orchestrator never raises: a tool whose schema we can't find passes
through untouched, so adding new tools doesn't accidentally break dispatch.

See ``CLAUDE.md`` §4.4 (tool execution) and the ``open-model-harness``
discussion for the design rationale.
"""

from __future__ import annotations

import json
import re
from typing import Any

from lib.log import audit_log, get_logger

logger = get_logger(__name__)


# ── Public type alias ──
RepairLog = list[tuple[str, str]]  # [(json_path, pattern_name), ...]


def _build_schema_index() -> dict[str, dict[str, Any]]:
    """Walk ``lib.tools`` and return ``{tool_name: parameters_schema}``.

    The map is built once at import time. New tools added later won't be
    seen until the process restarts; that matches how every other tool
    registry in this codebase works (PROJECT_TOOL_NAMES, etc.).

    Returns an empty dict on any failure — repair becomes a no-op rather
    than blocking startup.
    """
    index: dict[str, dict[str, Any]] = {}
    try:
        import lib.tools as tools_mod
        candidates: list[Any] = []
        for attr in dir(tools_mod):
            obj = getattr(tools_mod, attr, None)
            if isinstance(obj, list):
                candidates.extend(obj)
            elif isinstance(obj, dict) and obj.get('type') == 'function':
                candidates.append(obj)
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            fn = entry.get('function') or {}
            name = fn.get('name')
            params = fn.get('parameters')
            if name and isinstance(params, dict):
                index[name] = params
    except Exception as e:
        logger.warning('[ToolRepair] Schema index build failed — repair disabled: %s', e)
    logger.info('[ToolRepair] Indexed %d tool schemas', len(index))
    return index


_SCHEMA_INDEX: dict[str, dict[str, Any]] | None = None


def _schemas() -> dict[str, dict[str, Any]]:
    global _SCHEMA_INDEX
    if _SCHEMA_INDEX is None:
        _SCHEMA_INDEX = _build_schema_index()
    return _SCHEMA_INDEX


def _expected_types(tool_name: str) -> dict[str, str]:
    """Return ``{property_name: json_schema_type}`` for one tool.

    Only top-level properties are returned — nested-object repair is
    intentionally out of scope (the value is mostly in the four
    deterministic top-level patterns; deep walks risk over-repair of
    legitimate user data like the ``content`` of a ``write_file`` call).
    """
    params = _schemas().get(tool_name)
    if not params:
        return {}
    props = params.get('properties') or {}
    out: dict[str, str] = {}
    for key, spec in props.items():
        if not isinstance(spec, dict):
            continue
        t = spec.get('type')
        if isinstance(t, str):
            out[key] = t
    return out


def _required_keys(tool_name: str) -> set[str]:
    params = _schemas().get(tool_name) or {}
    req = params.get('required') or []
    return set(req) if isinstance(req, list) else set()


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

    # 3. stringified_primitive — string that coerces to int/number/bool
    if isinstance(value, str) and expected in ('integer', 'number', 'boolean'):
        ok, coerced = _coerce_primitive(value, expected)
        if ok:
            return True, coerced, 'stringified_primitive'

    # 4. bare_string_to_array — a non-JSON string where an array is
    #    expected. Must run AFTER stringified_json: if pattern 2 already
    #    parsed the string, we wouldn't reach here.
    if isinstance(value, str) and expected == 'array':
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


def report_invalid(tool_name: str, fn_args: Any, *, reason: str, model: str = '') -> None:
    """Emit ``tool_input_invalid`` audit when arguments couldn't be repaired.

    Called by the dispatcher when validation still fails after repair —
    surfaces the (tool, model, reason) tuple so the optimizer can spot
    regressions after a model swap.
    """
    audit_log(
        'tool_input_invalid',
        tool=tool_name,
        model=model,
        reason=reason,
        keys=list(fn_args.keys()) if isinstance(fn_args, dict) else None,
    )


__all__ = ['validate_then_repair', 'report_invalid', 'RepairLog']
