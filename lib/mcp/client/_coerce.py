"""lib/mcp/client/_coerce.py — tool-argument coercion + annotation extraction.

Best-effort coercion of LLM-shaped argument values to a tool's declared JSON
schema types, plus extraction of the MCP tool annotations and ``outputSchema``.
Pure leaf module.
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


# MCP ToolAnnotations behavioural hints: (wire camelCase, v2 SDK snake_case).
# The execution partition (read vs write) continues to key off readOnlyHint
# alone — see lib/tasks_pkg/tool_dispatch/_flags.py — so exposing the other
# hints here must never widen that partition.
_ANNOTATION_HINTS = (
    ('readOnlyHint', 'read_only_hint'),
    ('destructiveHint', 'destructive_hint'),
    ('idempotentHint', 'idempotent_hint'),
    ('openWorldHint', 'open_world_hint'),
)


def _read_annotation_hint(annotations: Any, camel: str, snake: str) -> bool:
    """Read one ToolAnnotations boolean hint across dict and model spellings.

    The WIRE name is always camelCase.  The PYTHON ATTRIBUTE name is not: MCP
    SDK v1 exposes ``readOnlyHint`` etc. directly, while v2 moved every model
    field to snake_case and kept camelCase only as a serialization alias.
    Accepts both spellings; only an explicit ``True`` is trusted (missing,
    False, or truthy junk all stay False) so a rename can never flip a safety
    default.
    """
    if isinstance(annotations, dict):
        # Raw wire object: camelCase is canonical, snake_case accepted too.
        value = annotations.get(camel)
        if value is None:
            value = annotations.get(snake)
        return value is True
    # Parsed model: attribute name differs by SDK major.
    for attr in (camel, snake):
        value = getattr(annotations, attr, None)
        if value is not None:
            return value is True
    return False


def _extract_read_only_hint(tool: Any) -> bool:
    """Return the MCP ``annotations.readOnlyHint`` for *tool* (default False).

    The MCP spec puts behavioural hints on ``Tool.annotations`` (a
    ``ToolAnnotations`` object with optional ``readOnlyHint`` / ``destructiveHint``
    / … fields). Older servers omit it entirely. We treat a tool as read-only
    ONLY when the hint is explicitly True — anything else (missing, False,
    unparsable) is conservatively treated as a write tool by the caller.

    BOTH SPELLINGS ARE CHECKED, AND THAT IS LOAD-BEARING
    ----------------------------------------------------
    The WIRE name is always camelCase ``readOnlyHint``. The PYTHON ATTRIBUTE
    name is not: MCP SDK v1 exposes the field as ``readOnlyHint``, while v2
    moved every model field to snake_case (``read_only_hint``) and kept
    camelCase only as a serialization alias. Measured against both SDKs::

        v1: getattr(ann, 'readOnlyHint')   -> True    'read_only_hint' -> ABSENT
        v2: getattr(ann, 'readOnlyHint')   -> ABSENT  'read_only_hint' -> True

    A single-spelling lookup therefore does not raise on the other SDK — it
    silently returns False for EVERY tool. Because False is also the honest
    answer for an un-annotated server, the failure is indistinguishable from
    "this server declares no hints": every read-only tool would quietly drop
    out of the parallel pool and start demanding write approval, with nothing
    in any log. Checking both names costs one ``getattr`` and removes the
    entire failure mode.

    The dict branch additionally covers servers/transports that hand us a raw
    JSON object rather than a parsed model — there the key is the wire name.
    """
    annotations = getattr(tool, 'annotations', None)
    if annotations is None:
        return False
    try:
        return _read_annotation_hint(
            annotations, 'readOnlyHint', 'read_only_hint')
    except Exception as e:
        logger.debug('[MCP] readOnlyHint extraction failed for %s: %s',
                     getattr(tool, 'name', '?'), e)
        return False


def _extract_annotations(tool: Any) -> dict[str, bool]:
    """Return the tool's MCP annotations as a camelCase-keyed bool dict.

    Emits all four ``ToolAnnotations`` hints (``readOnlyHint`` /
    ``destructiveHint`` / ``idempotentHint`` / ``openWorldHint``) with ``False``
    for any field the server omits, matching MCP's own defaults.  This is a
    superset of :func:`_extract_read_only_hint`; the read/write execution
    partition still keys off ``read_only_hint`` alone.
    """
    annotations = getattr(tool, 'annotations', None)
    if annotations is None:
        return {camel: False for camel, _ in _ANNOTATION_HINTS}
    try:
        return {
            camel: _read_annotation_hint(annotations, camel, snake)
            for camel, snake in _ANNOTATION_HINTS
        }
    except Exception as e:
        logger.debug('[MCP] annotation extraction failed for %s: %s',
                     getattr(tool, 'name', '?'), e)
        return {camel: False for camel, _ in _ANNOTATION_HINTS}


def _extract_output_schema(tool: Any) -> dict[str, Any]:
    """Return the tool's MCP ``outputSchema`` across SDK spellings ({} absent).

    The wire name is ``outputSchema``; the v2 SDK model field is
    ``output_schema`` — the same rename hazard as ``annotations.readOnlyHint``.
    Non-dict / absent values normalise to ``{}`` so callers never branch on a
    missing field.
    """
    for attr in ('output_schema', 'outputSchema'):
        schema = getattr(tool, attr, None)
        if schema is not None:
            return schema if isinstance(schema, dict) else {}
    return {}
