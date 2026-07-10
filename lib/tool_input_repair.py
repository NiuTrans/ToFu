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


# ── Schema-guided object-array salvage ─────────────────────────────────
#
# Last-resort recovery for an array-of-objects arg (e.g. read_files ``reads``)
# whose stringified value is so structurally broken that even
# ``repair_json`` (trailing-comma / balance / delimiter / bracket-match)
# cannot parse it. Because the tool schema declares the item's exact keys and
# each key's type, we don't INFER structure from the (broken) punctuation — we
# anchor on the known key tokens and read each value BY ITS DECLARED TYPE. This
# is the "automaton" that reconstructs meaningless outer/record punctuation.
#
# Free-text item keys whose values can legitimately contain quotes, colons,
# braces and newlines: scanning for "the next key" inside such a value
# mis-splits records, and these belong to DESTRUCTIVE edit tools
# (apply_diffs / insert_contents) where a wrong salvage would silently corrupt
# a code edit. For any item schema containing one of these, salvage is REFUSED
# and the call falls through to an honest model retry.
_FREE_TEXT_ITEM_KEYS = frozenset({
    'search', 'replace', 'content', 'anchor', 'new_string', 'old_string',
    'new_content', 'body', 'text', 'code',
})

_SALVAGE_MISSING = object()  # private "no value parsed" sentinel


def _array_item_schema(tool_name: str, key: str) -> tuple[dict[str, str], set[str]] | None:
    """Return ``({item_key: json_type}, required_item_keys)`` for an
    array-typed parameter whose items are objects with a declared schema, or
    ``None`` when the parameter isn't an array-of-objects we can anchor on.
    """
    params = _schemas().get(tool_name) or {}
    spec = (params.get('properties') or {}).get(key)
    if not isinstance(spec, dict) or spec.get('type') != 'array':
        return None
    items = spec.get('items')
    if not isinstance(items, dict):
        return None
    props = items.get('properties')
    if not isinstance(props, dict) or not props:
        return None
    types = {k: v.get('type') for k, v in props.items()
             if isinstance(v, dict) and isinstance(v.get('type'), str)}
    if not types:
        return None
    req = items.get('required') or []
    return types, (set(req) if isinstance(req, list) else set())


def _parse_value_fragment(frag: str, json_type: str) -> Any:
    """Read ONE value out of the text between two key tokens, by declared type.

    A ``string`` value is read by its quote structure (first balanced
    ``"..."``), so embedded ``: , { }`` inside the value never mis-split a
    record; an ``integer`` / ``number`` / ``boolean`` is read as the obvious
    literal run. Returns ``_SALVAGE_MISSING`` when nothing plausible is found.
    """
    if json_type == 'string':
        m = re.search(r'"((?:[^"\\]|\\.)*)"', frag)
        if m:
            return m.group(1).replace('\\"', '"').replace('\\\\', '\\')
        # Unterminated quote — take up to the next structural delimiter.
        stripped = frag.strip().lstrip('"')
        cut = re.split(r'["\],}]', stripped, maxsplit=1)[0].strip()
        return cut if cut else _SALVAGE_MISSING
    if json_type in ('integer', 'number'):
        m = re.search(r'-?\d+(?:\.\d+)?', frag)
        if not m:
            return _SALVAGE_MISSING
        raw = m.group(0)
        try:
            return int(raw) if json_type == 'integer' and '.' not in raw else float(raw)
        except (ValueError, TypeError) as e:
            logger.debug('[ToolRepair] numeric fragment %r not coercible (%s) — '
                         'salvaging as missing', raw, e)
            return _SALVAGE_MISSING
    if json_type == 'boolean':
        low = frag.lower()
        t, f = low.find('true'), low.find('false')
        if t == -1 and f == -1:
            return _SALVAGE_MISSING
        return t != -1 and (f == -1 or t < f)
    return _SALVAGE_MISSING


def _salvage_object_array(
    raw: str, item_types: dict[str, str], required: set[str],
) -> list[dict[str, Any]] | None:
    """Reconstruct a list-of-objects from a structurally-broken string using
    the declared item schema as anchors. Returns ``None`` when it can't be
    done safely (no key tokens, or no record carries every required key).
    Never raises.
    """
    key_re = re.compile(
        r'"(' + '|'.join(re.escape(k) for k in item_types) + r')"\s*:?'
    )
    matches = list(key_re.finditer(raw))
    if not matches:
        return None
    records: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for idx, m in enumerate(matches):
        k = m.group(1)
        val_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        val = _parse_value_fragment(raw[m.end():val_end], item_types[k])
        if val is _SALVAGE_MISSING:
            continue
        if k in cur:  # a key reappears → new record boundary
            records.append(cur)
            cur = {}
        cur[k] = val
    if cur:
        records.append(cur)
    # Guardrail: every kept record must carry all required item keys, else the
    # split is untrustworthy — refuse and let the model re-emit.
    records = [r for r in records if required.issubset(r.keys())]
    return records or None


def _try_schema_array_salvage(tool_name: str, key: str, value: str) -> list[dict[str, Any]] | None:
    """Gate + run object-array salvage for one array-typed arg. Returns the
    salvaged list, or ``None`` to leave the value untouched (honest retry).
    """
    item = _array_item_schema(tool_name, key)
    if item is None:
        return None
    item_types, required = item
    if _FREE_TEXT_ITEM_KEYS & item_types.keys():
        # Destructive / free-text editor — never auto-salvage.
        return None
    if '"' not in value:  # no key tokens possible
        return None
    try:
        return _salvage_object_array(value, item_types, required)
    except Exception as e:  # pragma: no cover — defensive
        logger.debug('[ToolRepair] array salvage failed for %s.%s: %s', tool_name, key, e)
        return None


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
    'multiedit': 'apply_diffs',  # Claude Code's batch-edit tool
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
    'webfetch': 'fetch_url',  # Claude Code's web-fetch tool
    'browse': 'fetch_url',
    'open_url': 'fetch_url',
    'websearch': 'web_search',
    'google': 'web_search',
    # ── ask the user ──
    # Claude Code's native tool is ``AskUserQuestion`` (matched
    # case-insensitively). These only resolve when ``ask_human`` is in the
    # session's tool set (human-guidance enabled) — never invented.
    'askuserquestion': 'ask_human',
    'ask_user': 'ask_human',
    'ask_user_question': 'ask_human',
    'ask': 'ask_human',
}


# ══════════════════════════════════════════
#  Parameter-KEY alias resolution
# ══════════════════════════════════════════
#
# Distinct from the tool-NAME alias layer above: here the tool name is already
# correct, but the model emits the argument KEYS using another harness's naming.
# The canonical case (conv-debug screenshot): a model calls ``apply_diff`` — the
# right tool — but with Claude Code's built-in *Edit* tool keys
# ``{file_path, old_string, new_string}`` instead of Tofu's ``{path, search,
# replace}``. Every alias key is dropped on the floor by the schema walk (it
# only iterates DECLARED properties), so ``path`` resolves to ``''`` and the
# tool returns the baffling ``File not found:`` (empty — no path) seen in the
# debug panel. The model can't self-correct because the error names no key.
#
# This map renames a known wrong key → the canonical key, per tool, BEFORE the
# type-walk. Strict guards keep it safe (see :func:`_apply_param_aliases`):
# rename only when the canonical key is ABSENT and the alias key is NOT itself a
# declared property — so a legitimate call is never touched and we never clobber
# a real value. Only 1:1 unambiguous synonyms belong here.
_PARAM_ALIASES: dict[str, dict[str, str]] = {
    # Claude Code *Edit* / OpenAI str-replace keys → apply_diff schema
    'apply_diff': {
        'file_path': 'path', 'filepath': 'path', 'filename': 'path',
        'old_string': 'search', 'old_str': 'search', 'oldText': 'search',
        'new_string': 'replace', 'new_str': 'replace', 'newText': 'replace',
    },
    'apply_diffs': {'file_path': 'path', 'filepath': 'path'},
    # write_file: Claude *Write* uses file_path/content; others vary the body key
    'write_file': {
        'file_path': 'path', 'filepath': 'path', 'filename': 'path',
        'file_text': 'content', 'contents': 'content', 'text': 'content',
        'data': 'content',
    },
    'insert_content': {
        'file_path': 'path', 'filepath': 'path', 'filename': 'path',
        'text': 'content',
    },
    'insert_contents': {'file_path': 'path', 'filepath': 'path'},
    'read_files': {
        'file_path': 'path', 'filepath': 'path', 'filename': 'path',
        'paths': 'reads', 'file_paths': 'reads', 'files': 'reads',
    },
    'list_dir': {'file_path': 'path', 'directory': 'path', 'dir': 'path',
                 'dir_path': 'path'},
    'grep_search': {'regex': 'pattern', 'query': 'pattern', 'search': 'pattern'},
    'find_files': {'glob': 'pattern', 'name': 'pattern', 'file_path': 'path'},
    'run_command': {'cmd': 'command', 'shell_command': 'command',
                    'script': 'command'},
    'fetch_url': {'link': 'url'},
}


def _apply_param_aliases(
    tool_name: str, args: dict[str, Any], expected: dict[str, str],
) -> tuple[dict[str, Any], RepairLog]:
    """Rename wrong-harness argument keys to their canonical schema keys.

    Runs BEFORE the per-value type repair. For each ``alias -> canonical``
    entry of ``tool_name``: rename ``args[alias]`` to ``args[canonical]`` only
    when ALL of the following hold, so a valid call is never disturbed:

    * ``canonical`` is a real declared property of the tool (in ``expected``);
    * ``canonical`` is ABSENT from ``args`` (never overwrite a real value);
    * ``alias`` is present and is NOT itself a declared property of the tool
      (so we never rename a legitimate parameter away).

    Args:
        tool_name: Canonical tool name (already alias-resolved upstream).
        args: The (copied) argument dict — mutated in place.
        expected: ``{property: json_type}`` for this tool.

    Returns:
        ``(args, log)`` where ``log`` lists ``(canonical_key, 'param_alias')``
        for each rename applied. Empty when nothing matched.
    """
    alias_map = _PARAM_ALIASES.get(tool_name)
    if not alias_map:
        return args, []
    log: RepairLog = []
    for alias, canonical in alias_map.items():
        if canonical not in expected:
            continue
        if alias not in args or canonical in args or alias in expected:
            continue
        args[canonical] = args.pop(alias)
        log.append((canonical, 'param_alias'))
    return args, log


# ══════════════════════════════════════════
#  Structural transforms (cross-harness shape reshape)
# ══════════════════════════════════════════
#
# Distinct from BOTH alias layers above: here the model called the RIGHT
# (already name-resolved) tool but emitted another harness's whole-payload
# STRUCTURE, not just renamed keys. The canonical cases are Claude Code's
# ``MultiEdit`` (one top-level ``file_path`` + an ``edits[]`` whose items carry
# no path) and ``AskUserQuestion`` (a ``questions[]`` array wrapping the
# prompt). A flat key-rename can't express either — they need a nested reshape.
#
# Each transform is shape-GUARDED: it fires only when the args clearly match
# the FOREIGN shape and NOT the canonical one, so a correct native call is
# never disturbed. Transforms run at the TOP of :func:`validate_then_repair`,
# BEFORE the param-key alias pass and the per-value type-walk (which then mop
# up any residual key/type mismatch inside the reshaped payload).


def _transform_multiedit_to_apply_diffs(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Reshape a Claude Code *MultiEdit* payload into ``apply_diffs`` args.

    MultiEdit:  ``{file_path, edits: [{old_string, new_string, replace_all?}]}``
    apply_diffs: ``{edits: [{path, search, replace, replace_all?}], description?}``

    The single top-level ``file_path`` is pushed down into every edit (our
    batch tool is multi-file, so each edit carries its own ``path``), and the
    per-edit ``old_string``/``new_string`` keys are renamed to ``search`` /
    ``replace``. Fires only when ``edits`` is a non-empty list AND either a
    top-level file path is present OR an edit uses the MultiEdit item keys —
    so a native ``apply_diffs`` call (no top-level path, items already
    ``{path, search, replace}``) is returned untouched.
    """
    edits = args.get('edits')
    if not isinstance(edits, list) or not edits:
        return args, False
    shared_path = ''
    for k in ('file_path', 'filepath', 'filename', 'path'):
        v = args.get(k)
        if isinstance(v, str) and v:
            shared_path = v
            break
    looks_multiedit = any(
        isinstance(e, dict) and any(
            k in e for k in ('old_string', 'old_str', 'oldText',
                             'new_string', 'new_str', 'newText'))
        for e in edits
    )
    if not looks_multiedit and not shared_path:
        return args, False
    _item_renames = (('old_string', 'search'), ('old_str', 'search'),
                     ('oldText', 'search'), ('new_string', 'replace'),
                     ('new_str', 'replace'), ('newText', 'replace'),
                     ('file_path', 'path'), ('filepath', 'path'),
                     ('filename', 'path'))
    new_edits: list[Any] = []
    for e in edits:
        if not isinstance(e, dict):
            new_edits.append(e)
            continue
        ne = dict(e)
        for src, dst in _item_renames:
            if src in ne and dst not in ne:
                ne[dst] = ne.pop(src)
        if shared_path and not ne.get('path'):
            ne['path'] = shared_path
        new_edits.append(ne)
    out = {k: v for k, v in args.items()
           if k not in ('file_path', 'filepath', 'filename', 'path')}
    out['edits'] = new_edits
    return out, True


def _transform_askuserquestion_to_ask_human(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Reshape a Claude Code *AskUserQuestion* payload into ``ask_human`` args.

    AskUserQuestion: ``{questions: [{question, header?, options?[]}]}`` (an
    array — Claude Code can batch several questions). ``ask_human`` asks ONE
    question: ``{question, response_type, options?: [{label, description}]}``.

    Lifts ``questions[0]`` to the top level (a lossy-but-actionable reshape —
    asking the first question beats a hard rejection; a second question, if
    any, is dropped with a debug log). Fires only when ``questions`` is a
    non-empty list AND no native top-level ``question`` is already present.
    """
    if args.get('question'):
        return args, False
    questions = args.get('questions')
    if not isinstance(questions, list) or not questions:
        return args, False
    q0 = questions[0]
    if not isinstance(q0, dict):
        return args, False
    question = q0.get('question') or q0.get('header') or ''
    if not question:
        return args, False
    if len(questions) > 1:
        logger.debug('[ToolRepair] AskUserQuestion→ask_human: dropping %d extra '
                     'question(s) (ask_human is single-question)', len(questions) - 1)
    out: dict[str, Any] = {'question': question}
    options = q0.get('options')
    if isinstance(options, list) and options:
        norm: list[Any] = []
        for o in options:
            if isinstance(o, dict):
                norm.append(o)
            elif isinstance(o, str):
                norm.append({'label': o})
        out['options'] = norm
        out['response_type'] = 'choice'
    else:
        out['response_type'] = q0.get('response_type') or 'free_text'
    return out, True


# Registry keyed by the CANONICAL (already name-resolved) tool name.
_STRUCTURAL_TRANSFORMS: dict[str, Any] = {
    'apply_diffs': _transform_multiedit_to_apply_diffs,
    'ask_human': _transform_askuserquestion_to_ask_human,
}


def _apply_structural_transform(
    tool_name: str, args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Run the registered cross-harness shape transform for ``tool_name``.

    Returns ``(maybe_reshaped_args, changed)``. Total / never raises — a
    transform that throws is logged and treated as a no-op so dispatch is
    never blocked by a repair attempt.
    """
    fn = _STRUCTURAL_TRANSFORMS.get(tool_name)
    if fn is None:
        return args, False
    try:
        return fn(args)
    except Exception as e:
        logger.warning('[ToolRepair] structural transform for %s failed '
                       '(passing through): %s', tool_name, e)
        return args, False


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

    The main chat dispatcher (``lib/tasks_pkg/tool_dispatch.py``) keeps its own
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


# ══════════════════════════════════════════
#  Hallucinated-tool classification (unified rejection)
# ══════════════════════════════════════════
#
# After alias resolution (:func:`resolve_tool_name`) fails to map a name to a
# real session tool, the call is a *hallucination*: the model invented a tool
# that does not exist in this session (e.g. ``search_web`` when only
# ``web_search`` is registered, or a tool from a different harness with no
# alias). Historically these fell through to the executor's bare
# ``Error: unknown tool "X"`` string returned as a normal tool result — the
# frontend rendered it as an ordinary completed tool, with no signal that the
# call was rejected and never executed. This is the single classifier so the
# dispatcher can reject uniformly and the UI can style it distinctly.


def _name_similarity(a: str, b: str) -> float:
    """Cheap similarity in [0, 1] between two tool names (no deps).

    Combines a substring-containment boost with difflib's ratio so that
    ``search_web`` scores highly against ``web_search`` (shared tokens) and
    ``read`` against ``read_files`` (prefix). Pure-stdlib, total, never raises.
    """
    a_l, b_l = a.lower(), b.lower()
    if not a_l or not b_l:
        return 0.0
    from difflib import SequenceMatcher
    ratio = SequenceMatcher(None, a_l, b_l).ratio()
    # Token-overlap boost: split on non-alphanumerics, compare the sets.
    a_tok = set(re.split(r'[^a-z0-9]+', a_l)) - {''}
    b_tok = set(re.split(r'[^a-z0-9]+', b_l)) - {''}
    if a_tok and b_tok:
        overlap = len(a_tok & b_tok) / len(a_tok | b_tok)
        ratio = max(ratio, 0.5 * ratio + 0.5 * overlap)
    if a_l in b_l or b_l in a_l:
        ratio = max(ratio, 0.85)
    return ratio


def suggest_tool_names(name: str, known: set[str], *, limit: int = 3,
                       threshold: float = 0.45) -> list[str]:
    """Return up to ``limit`` real tool names most similar to ``name``.

    Used to make a hallucinated-tool rejection actionable ("did you mean
    web_search?"). Only names scoring above ``threshold`` are returned, so a
    name with no plausible match yields ``[]`` rather than noise.
    """
    if not name or not known:
        return []
    scored = sorted(
        ((t, _name_similarity(name, t)) for t in known),
        key=lambda kv: kv[1], reverse=True,
    )
    return [t for t, s in scored[:limit] if s >= threshold]


def classify_tool_call(name: str, known: set[str]) -> dict[str, Any] | None:
    """Classify a tool name against the live session tool set.

    Call this AFTER :func:`resolve_tool_name` has already failed to alias the
    name to a real tool. ``known`` MUST be the live set of tools shipped to
    the model this turn (built-ins + MCP + swarm + memory + custom-env), so a
    legitimate dynamically-registered tool is never flagged.

    Returns:
        ``None`` when ``name`` is a real tool (no rejection). Otherwise a
        descriptor ``{kind:'hallucinated', attempted, suggestions}`` the
        dispatcher stamps onto the tool round and the frontend renders as a
        distinct "not a real tool" state.
    """
    if not name or not isinstance(name, str):
        return {'kind': 'hallucinated', 'attempted': str(name), 'suggestions': []}
    if name in known:
        return None
    return {
        'kind': 'hallucinated',
        'attempted': name,
        'suggestions': suggest_tool_names(name, known),
    }


# ══════════════════════════════════════════
#  Repeat-rejection circuit breaker
# ══════════════════════════════════════════
#
# A model that invents a tool with NO similar real tool (suggestions==[]) gets
# a rejection that can't point it anywhere — so under autopilot it re-emits the
# SAME fake call every round (the screenshot bug: ``module_buffer_manager`` ×7,
# ~30–50s apart, pure token burn). The flat ``build_rejection_message`` text
# ("use the exact names") gives no actionable target. The breaker tracks how
# many times a given fake name has been rejected within ONE conversation and,
# after a threshold, ESCALATES the rejection to enumerate the real tools the
# model may actually call. The count is keyed ``(convId, tool_name)`` so it
# spans autopilot follow-up tasks (separate task ids, same conversation).

# Escalate (inject the live tool list) starting at the Nth consecutive
# rejection of the same name. N=2 → the first repeat already gets the list.
REJECTION_ESCALATE_THRESHOLD = 2
# Autopilot hard-abort threshold — DELIBERATELY HIGHER than the escalate
# threshold. Decoupling matters: if abort fired at the same count as the
# tool-list injection, the autopilot task would die in the SAME round the list
# was injected, so the model would never get a turn to USE the list — the
# graceful-recovery path would be dead code exactly where it's needed most.
# With ABORT=4 vs ESCALATE=2, the model gets ~2 rounds holding the enumerated
# tool list to self-correct before abort kicks in as a true last resort. Worst
# case converges at 4 rounds (still bounded) instead of 7+.
HALLUCINATION_ABORT_THRESHOLD = 4
# Cap the enumerated tool list so a 170-tool MCP session doesn't dump a wall of
# names into the result — list the built-ins/common ones, truncate the rest.
_REJECTION_TOOL_LIST_CAP = 60

# In-memory repeat counter: {(conv_id, tool_name): consecutive_reject_count}.
# Bounded by distinct fake names per conversation (tiny); entries are best-
# effort and never persisted — a process restart resetting them is harmless.
_REJECT_COUNTS: dict[tuple[str, str], int] = {}
_REJECT_COUNTS_MAX = 4096


def record_rejection(conv_id: str, tool_name: str) -> int:
    """Increment and return the consecutive-rejection count for a fake name.

    Keyed ``(conv_id, tool_name)`` so the count survives across autopilot
    follow-up tasks (which share the conversation but get fresh task ids).
    Total / never raises. A soft cap evicts arbitrary entries if the map ever
    grows pathologically (it won't in practice — distinct fake names per conv
    are few).
    """
    if not tool_name:
        return 0
    key = (conv_id or '', tool_name)
    n = _REJECT_COUNTS.get(key, 0) + 1
    _REJECT_COUNTS[key] = n
    if len(_REJECT_COUNTS) > _REJECT_COUNTS_MAX:
        try:
            for _k in list(_REJECT_COUNTS.keys())[:_REJECT_COUNTS_MAX // 2]:
                _REJECT_COUNTS.pop(_k, None)
        except Exception as e:
            logger.debug('[ToolRepair] reject-count eviction skipped: %s', e)
    return n


def clear_rejection(conv_id: str, tool_name: str) -> None:
    """Reset the consecutive-rejection count for one ``(conv_id, tool_name)``.

    Called when the same name is NO LONGER rejected (the model corrected
    itself), so a later unrelated reuse starts the count fresh rather than
    inheriting a stale streak.
    """
    if not tool_name:
        return
    _REJECT_COUNTS.pop((conv_id or '', tool_name), None)


def build_rejection_message(descriptor: dict[str, Any], *,
                            repeat_count: int = 1,
                            known_tools: set[str] | None = None) -> str:
    """Build the standardized model-facing rejection text for a fake tool call.

    One source of truth for the message returned to the LLM (as the tool
    result) so it can self-correct, instead of the ad-hoc per-site strings
    that existed before. Mentions the closest real tools when known.

    Args:
        descriptor: ``classify_tool_call`` output (``attempted`` + ``suggestions``).
        repeat_count: How many times this fake name has been rejected in a row
            within the conversation (1 = first time). At or above
            :data:`REJECTION_ESCALATE_THRESHOLD`, AND only when there are no
            ``suggestions`` (a pure invention with no nearby real tool to point
            at), the message ESCALATES to enumerate ``known_tools`` so the model
            has a concrete, correctable target instead of looping the same name.
        known_tools: The live REAL-tool set for this turn. Used only for the
            escalation path.
    """
    attempted = descriptor.get('attempted') or '?'
    suggestions = descriptor.get('suggestions') or []
    msg = (
        f'Error: `{attempted}` is not a real tool and was NOT executed. '
        f'It is not in the list of tools available to you this turn.'
    )
    if suggestions:
        hint = ', '.join(f'`{s}`' for s in suggestions)
        msg += f' Did you mean one of: {hint}? '
        msg += 'Call only tools from the provided tool list, using their exact names.'
        return msg

    # No suggestion to offer. On repeated invention of the SAME phantom name,
    # stop repeating the useless generic line — enumerate the real tools so the
    # model has a concrete target (the only way to break a no-suggestion loop).
    if repeat_count >= REJECTION_ESCALATE_THRESHOLD and known_tools:
        names = sorted(known_tools)
        shown = names[:_REJECTION_TOOL_LIST_CAP]
        listed = ', '.join(f'`{n}`' for n in shown)
        if len(names) > len(shown):
            listed += f', … (+{len(names) - len(shown)} more)'
        msg += (
            f' You have now called this non-existent tool {repeat_count} times — '
            f'STOP calling `{attempted}`. The ONLY tools you may call this turn are: '
            f'{listed}. Pick one of these exact names, or if none fits, reply to '
            f'the user in plain text WITHOUT a tool call.'
        )
        return msg

    msg += ' '
    msg += 'Call only tools from the provided tool list, using their exact names.'
    return msg


def report_hallucinated(name: str, descriptor: dict[str, Any], *, model: str = '') -> None:
    """Emit a ``tool_hallucinated`` audit event for a rejected fake tool call.

    Lets the nightly optimizer cluster which non-existent tool names a given
    model keeps inventing (e.g. a model that persistently calls ``search_web``)
    so the alias table or system prompt can be tuned.
    """
    audit_log(
        'tool_hallucinated',
        tool=name,
        model=model,
        suggestions=descriptor.get('suggestions') or [],
    )


def report_tool_name_aliased(attempted: str, resolved: str, alias_kind: str,
                             *, model: str = '') -> None:
    """Emit a ``tool_name_aliased`` audit event when a wrong name was rewritten.

    Quantifies which cross-harness tool names (Claude Code's ``Read`` / ``Bash``
    / ``MultiEdit`` / ``AskUserQuestion`` …) models actually emit, broken down
    by model — the data needed to decide whether a presentation-level schema
    rename (per model family) would pay off, vs. keeping the alias layer.
    """
    audit_log(
        'tool_name_aliased',
        attempted=attempted,
        resolved=resolved,
        kind=alias_kind,
        model=model,
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


# ══════════════════════════════════════════
#  Unified tool-call ingestion seam
# ══════════════════════════════════════════
#
# THE single front door every dispatch path funnels a raw ``tool_call`` through
# before executing it. Historically the ingestion preamble (name-drop guards,
# name-alias, JSON decode + repair, schema/param repair, hallucination reject)
# was hand-reimplemented at four sites — the main chat dispatcher
# (``lib/tasks_pkg/tool_dispatch.py::parse_tool_calls``), the paper report/QA
# engines (via ``parse_and_repair_tool_args``, which only did decode+schema),
# the swarm sub-agent (``lib/swarm/agent.py``), and the timer poll
# (``lib/scheduler/timer.py``). Each covered only a SUBSET, so a guard added to
# one path silently skipped the others (the ``WebFetch`` unknown-tool wall on
# the swarm/timer paths; missing schema/param repair on both). This function is
# the ONE place all five stages live so parity is structural, not a checklist.
#
# It is pure orchestration over the existing primitives — it adds NO new repair
# logic. Presentation concerns (UI "auto-fixed" badges, SSE early-announce,
# autopilot loop-break, phantom empty-arg dedup) stay in the caller; they read
# the returned descriptor and layer their own behaviour on top.

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
    3. **JSON decode + repair** — ``json.loads`` then ``repair_json`` fallback
       for truncated/malformed args. Unrecoverable → ``parse_error`` (with a
       :func:`schema_hint`) and ``fn_args={}``.
    4. **Schema/param repair** — :func:`validate_then_repair` (structural
       transforms, param-key alias, the six value-repair patterns). Skipped
       when step 3 failed. Never touches valid inputs.
    5. **Hallucination reject** — when the (post-alias) name is not in
       ``known_tools`` and ``reject_hallucinated`` is set:
       :func:`classify_tool_call` + :func:`record_rejection` (streak) →
       ``rejection`` descriptor + a :func:`build_rejection_message` as
       ``parse_error`` so the caller returns it to the LLM and skips execution.
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


__all__ = ['validate_then_repair', 'parse_and_repair_tool_args', 'report_invalid',
           'resolve_tool_name', 'schema_hint',
           'classify_tool_call', 'suggest_tool_names', 'build_rejection_message',
           'record_rejection', 'clear_rejection', 'REJECTION_ESCALATE_THRESHOLD',
           'HALLUCINATION_ABORT_THRESHOLD',
           'report_hallucinated', 'report_tool_name_aliased', 'RepairLog',
           'ingest_tool_call', 'IngestedToolCall']
