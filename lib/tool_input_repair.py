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
   If strict parsing fails but the string clearly intended to be JSON
   (leading ``[`` / ``{``), fall back to ``lib.utils.repair_json`` so a
   slightly-malformed inner blob (trailing comma, truncation, unterminated
   string, missing closer) is recovered the same way the outer arguments
   blob is. Only substituted when the recovered value matches the expected
   shape — genuinely-ambiguous payloads still fall through to an honest
   tool-level error.
3. ``stringified_primitive`` — ``"42"`` / ``"true"`` where an integer or
   boolean is expected → coerce. Catches the recurring trap logged in
   memory ``llm-string-coercion-traps``.
4. ``bare_string_to_array`` — ``"foo"`` where ``["foo"]`` is expected
   → wrap in a single-element array. A string that LOOKS like JSON
   (starts with ``[`` / ``{``) but failed pattern 2's parse is left
   untouched — wrapping malformed JSON only hides the error one layer
   deeper, so the tool returns an honest error instead.
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
#  Tool-NAME repair (alias resolution)
# ══════════════════════════════════════════
#
# Open models routinely emit a tool name borrowed from a *different* harness
# (Claude Code, OpenAI function-calling demos, plain Unix) instead of Tofu's
# canonical name. Each such call previously hit a hard "Unknown tool" wall and
# wasted a full round (top offender in the log audit: ``read_file`` 73×/day,
# plus ``bash``, ``read_text``, ``write_files``, ``grep_file``, ``cat`` …).
#
# This map rewrites the well-known wrong names to the canonical Tofu tool the
# model obviously intended. Only 1:1, unambiguous synonyms belong here — never
# map to a tool whose argument schema differs in a way the model can't satisfy.
# Keys are matched case-insensitively (see :func:`resolve_tool_name`), so the
# Claude-Code CamelCase variants (``Read``/``Grep``/``Edit`` …) are covered by
# the same lowercase entries.
_TOOL_NAME_ALIASES: dict[str, str] = {
    # ── file reading ──
    'read_file': 'read_files',
    'read': 'read_files',
    'read_text': 'read_files',
    'read_text_file': 'read_files',
    'readfile': 'read_files',
    'cat': 'read_files',
    'open_file': 'read_files',
    'view_file': 'read_files',
    'view': 'read_files',
    # ── directory listing ──
    'ls': 'list_dir',
    'list_directory': 'list_dir',
    'list_files': 'list_dir',
    'listdir': 'list_dir',
    'dir': 'list_dir',
    # ── content search ──
    'grep': 'grep_search',
    'grep_file': 'grep_search',
    'search': 'grep_search',
    'search_text': 'grep_search',
    'ripgrep': 'grep_search',
    'rg': 'grep_search',
    # ── filename search ──
    'find': 'find_files',
    'find_file': 'find_files',
    'glob': 'find_files',
    'search_files': 'find_files',
    # ── writing ──
    'write': 'write_file',
    'writefile': 'write_file',
    'write_files': 'write_file',
    'create_file': 'write_file',
    'save_file': 'write_file',
    # ── editing ──
    'edit': 'apply_diff',
    'edit_file': 'apply_diff',
    'str_replace': 'apply_diff',
    'str_replace_editor': 'apply_diff',
    'search_replace': 'apply_diff',
    'replace': 'apply_diff',
    'edits': 'apply_diffs',
    'insert': 'insert_content',
    # ── shell ──
    'bash': 'run_command',
    'shell': 'run_command',
    'sh': 'run_command',
    'exec': 'run_command',
    'execute': 'run_command',
    'execute_command': 'run_command',
    'terminal': 'run_command',
    'command': 'run_command',
    # ── fetch / search ──
    'fetch': 'fetch_url',
    'fetch_page': 'fetch_url',
    'browse': 'fetch_url',
    'open_url': 'fetch_url',
    'websearch': 'web_search',
    'google': 'web_search',
}


def resolve_tool_name(name: str, known: set[str] | None = None) -> tuple[str, str | None]:
    """Map a possibly-wrong tool name to a canonical Tofu tool name.

    Resolution order (first match wins):

    1. **Exact** — ``name`` is already a real tool → returned untouched
       (``alias_kind=None``). This is the overwhelmingly common path and
       must stay byte-cheap.
    2. **Static alias** — ``name.lower()`` is in :data:`_TOOL_NAME_ALIASES`
       *and* the target is a known tool → rewrite (``alias_kind='alias'``).
    3. **Case-insensitive** — a single known tool equals ``name`` ignoring
       case (catches Claude-Code ``Read``/``Grep`` and stray capitalisation)
       → rewrite (``alias_kind='casefold'``).

    Args:
        name: The tool name the model emitted.
        known: Set of valid tool names for this session (exact + MCP +
            swarm + memory tools). When ``None``, falls back to the
            schema-indexed built-in tools. Passing the live registry set
            lets dynamically-registered tools (MCP, swarm) win the exact
            check so we never alias over a real tool.

    Returns:
        ``(resolved_name, alias_kind)``. ``alias_kind`` is ``None`` when no
        rewrite happened (exact match or no confident mapping), else the
        kind of rewrite applied (for telemetry / UI badge).
    """
    if not name or not isinstance(name, str):
        return name, None

    valid = known if known is not None else set(_schemas().keys())

    # 1. Already valid — do nothing (hot path).
    if name in valid:
        return name, None

    # 2. Static alias table (case-insensitive key), but only if the target
    #    actually exists in this session — never invent a tool.
    target = _TOOL_NAME_ALIASES.get(name.lower())
    if target and target in valid:
        return target, 'alias'

    # 3. Case-insensitive match against a real tool (e.g. 'Grep' → no static
    #    entry needed if 'grep_search' weren't aliased; 'Read_Files' → ...).
    low = name.lower()
    ci_matches = [t for t in valid if t.lower() == low]
    if len(ci_matches) == 1:
        return ci_matches[0], 'casefold'

    # No confident mapping — leave untouched so the caller surfaces an
    # honest "unknown tool" error the model can correct.
    return name, None


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


__all__ = ['validate_then_repair', 'report_invalid', 'resolve_tool_name', 'schema_hint', 'RepairLog']
