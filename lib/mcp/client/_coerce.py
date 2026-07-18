"""lib/mcp/client/_coerce.py — tool-argument coercion + annotation extraction.

Best-effort coercion of LLM-shaped argument values to a tool's declared JSON
schema types, plus extraction of the MCP ``readOnlyHint`` annotation. Pure
leaf module.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


def _coerce_one(value: Any, schema: dict[str, Any]) -> Any:
    """Best-effort coerce ``value`` to match ``schema``'s declared type.

    Handles the most common LLM-shaped mistakes: strings-instead-of-ints,
    strings-instead-of-bools, and single-value-instead-of-array. Unknown
    / unparseable values are returned unchanged so downstream jsonschema
    validation still surfaces a clear error for genuine type mismatches.

    Supports JSON Schema ``type`` as either a single string or a list
    (e.g. ``["integer","null"]``) — the first non-null entry is used.
    """
    if not isinstance(schema, dict):
        return value
    t = schema.get('type')
    # resolve `type: ["integer", "null"]` → "integer"
    if isinstance(t, list):
        t = next((x for x in t if x != 'null'), None)

    # anyOf / oneOf: try each branch, return the first that produces a
    # value whose Python type matches the branch. Keeps the behavior
    # conservative — if none match, fall through.
    for key in ('anyOf', 'oneOf'):
        branches = schema.get(key)
        if isinstance(branches, list) and branches:
            for branch in branches:
                coerced = _coerce_one(value, branch)
                if coerced is not value:
                    return coerced
            return value

    if t == 'integer' and isinstance(value, str):
        s = value.strip()
        if s and (s.lstrip('-').isdigit()):
            try:
                return int(s)
            except ValueError as _e_audit:
                logger.debug('[client] _coerce_one caught %s: %s', type(_e_audit).__name__, _e_audit)
                return value
    elif t == 'number' and isinstance(value, str):
        s = value.strip()
        try:
            return float(s)
        except ValueError as _e_audit:
            logger.debug('[client] _coerce_one caught %s: %s', type(_e_audit).__name__, _e_audit)
            return value
    elif t == 'boolean' and isinstance(value, str):
        s = value.strip().lower()
        if s in ('true', '1', 'yes', 'y'):
            return True
        if s in ('false', '0', 'no', 'n'):
            return False
    elif t == 'array':
        items_schema = schema.get('items') or {}
        # Wrap scalar-instead-of-array.
        if not isinstance(value, list):
            value = [value]
        if isinstance(items_schema, dict):
            return [_coerce_one(v, items_schema) for v in value]
        return value
    elif t == 'object' and isinstance(value, dict):
        props = schema.get('properties') or {}
        if isinstance(props, dict):
            return {
                k: (_coerce_one(v, props[k]) if k in props else v)
                for k, v in value.items()
            }
    return value


def _coerce_args_to_schema(
    arguments: dict[str, Any], schema: dict[str, Any],
) -> dict[str, Any]:
    """Walk a tool-call arg dict and coerce each entry per the tool's input schema."""
    if not isinstance(arguments, dict) or not isinstance(schema, dict):
        return arguments
    props = schema.get('properties')
    if not isinstance(props, dict):
        return arguments
    out: dict[str, Any] = {}
    for k, v in arguments.items():
        sub = props.get(k)
        if isinstance(sub, dict):
            out[k] = _coerce_one(v, sub)
        else:
            out[k] = v
    return out


def _extract_read_only_hint(tool: Any) -> bool:
    """Return the MCP ``annotations.readOnlyHint`` for *tool* (default False).

    The MCP spec puts behavioural hints on ``Tool.annotations`` (a
    ``ToolAnnotations`` object with optional ``readOnlyHint`` / ``destructiveHint``
    / … fields). Older servers omit it entirely. We treat a tool as read-only
    ONLY when the hint is explicitly True — anything else (missing, False,
    unparsable) is conservatively treated as a write tool by the caller.
    """
    annotations = getattr(tool, 'annotations', None)
    if annotations is None:
        return False
    try:
        hint = getattr(annotations, 'readOnlyHint', None)
        if hint is None and isinstance(annotations, dict):
            hint = annotations.get('readOnlyHint')
        return hint is True
    except Exception as e:
        logger.debug('[MCP] readOnlyHint extraction failed for %s: %s',
                     getattr(tool, 'name', '?'), e)
        return False
