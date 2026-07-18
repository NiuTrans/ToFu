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

Before the per-value type-walk, a separate **parameter-key alias** pass
(:func:`_apply_param_aliases`) renames wrong-harness argument KEYS to their
canonical schema keys (e.g. Claude Code's *Edit* keys
``{file_path, old_string, new_string}`` → ``apply_diff``'s
``{path, search, replace}``). This is what stops the empty ``File not found:``
failure when the model calls the right tool with another harness's arg names.

**Critical:** ``stringified_json`` MUST run before ``bare_string_to_array``
— otherwise ``'["a","b"]'`` would be wrapped to ``['["a","b"]']``,
double-wrapping a recoverable input into garbage.

The orchestrator never raises: a tool whose schema we can't find passes
through untouched, so adding new tools doesn't accidentally break dispatch.

See ``CLAUDE.md`` §4.4 (tool execution) and the ``open-model-harness``
discussion for the design rationale.

──────────────────────────────────────────────────────────────────────────
This module is a FACADE over a cohesively-split package (CLAUDE.md §3.1):

    _schema.py     — schema index + type introspection + RepairLog alias
    _salvage.py    — schema-guided broken object-array salvage
    _transform.py  — parameter-key aliases + structural cross-harness reshape
    _repair.py     — repair primitives, per-value stack, validate_then_repair,
                     parse_and_repair_tool_args, schema_hint
    _classify.py   — tool-NAME alias resolution + hallucination classification
    _rejection.py  — repeat-rejection breaker + rejection message + audit
    _ingest.py     — the unified ingest_tool_call seam + IngestedToolCall

Every public symbol (and the private symbols the test-suite imports) is
re-exported here so all existing ``from lib.tool_input_repair import X`` call
sites keep working unchanged.
"""

from __future__ import annotations

from lib.log import get_logger

from lib.tool_input_repair._schema import (
    RepairLog,
    _array_item_schema,
    _build_schema_index,
    _expected_types,
    _required_keys,
    _schemas,
)
from lib.tool_input_repair._salvage import (
    _parse_value_fragment,
    _salvage_object_array,
    _try_schema_array_salvage,
)
from lib.tool_input_repair._transform import (
    _apply_param_aliases,
    _apply_structural_transform,
    _transform_askuserquestion_to_ask_human,
    _transform_multiedit_to_apply_diffs,
)
from lib.tool_input_repair._repair import (
    _coerce_primitive,
    _json_type_of,
    _lenient_json,
    _repair_one_value,
    _strip_leaked_tool_call_syntax,
    _try_parse_json,
    parse_and_repair_tool_args,
    schema_hint,
    validate_then_repair,
)
from lib.tool_input_repair._classify import (
    _name_similarity,
    classify_tool_call,
    resolve_tool_name,
    suggest_tool_names,
)
from lib.tool_input_repair._rejection import (
    HALLUCINATION_ABORT_THRESHOLD,
    REJECTION_ESCALATE_THRESHOLD,
    build_rejection_message,
    clear_rejection,
    record_rejection,
    report_hallucinated,
    report_invalid,
    report_tool_name_aliased,
)
from lib.tool_input_repair._ingest import (
    IngestedToolCall,
    ingest_tool_call,
    _tool_name_drop_reason,
)

logger = get_logger(__name__)

# ── Repeat-rejection counter: exposed at the package level for the test-suite
#    (tests/test_tool_hallucination.py mutates ``_tir._REJECT_COUNTS``). It must
#    be the SAME dict object the rejection module reads, so bind the reference.
from lib.tool_input_repair import _rejection as _rejection_mod  # noqa: E402
_REJECT_COUNTS = _rejection_mod._REJECT_COUNTS


__all__ = [
    'validate_then_repair', 'parse_and_repair_tool_args', 'report_invalid',
    'resolve_tool_name', 'schema_hint',
    'classify_tool_call', 'suggest_tool_names', 'build_rejection_message',
    'record_rejection', 'clear_rejection', 'REJECTION_ESCALATE_THRESHOLD',
    'HALLUCINATION_ABORT_THRESHOLD',
    'report_hallucinated', 'report_tool_name_aliased', 'RepairLog',
    'ingest_tool_call', 'IngestedToolCall',
    # private symbols imported by the test-suite
    '_salvage_object_array', '_array_item_schema', '_try_schema_array_salvage',
]
