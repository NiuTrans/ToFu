"""Cross-harness shape repair: parameter-KEY aliases and structural transforms.

Two distinct layers, both running on a call whose (already name-resolved) tool
is correct but whose ARGUMENTS came from a different harness:

* :func:`_apply_param_aliases` renames wrong-harness argument KEYS to their
  canonical schema keys (e.g. Claude Code's *Edit* keys
  ``{file_path, old_string, new_string}`` → ``apply_diff``'s
  ``{path, search, replace}``).
* :func:`_apply_structural_transform` reshapes another harness's whole-payload
  STRUCTURE (Claude Code ``MultiEdit`` / ``AskUserQuestion``) into our schema.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

from lib.tool_input_repair._schema import RepairLog, _array_item_schema

logger = get_logger(__name__)


# ══════════════════════════════════════════
#  Parameter-KEY alias resolution
# ══════════════════════════════════════════
#
# Distinct from the tool-NAME alias layer: here the tool name is already
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
        # OpenCode *edit* uses camelCase keys (filePath/oldString/newString/
        # replaceAll) with identical semantics.
        'filePath': 'path', 'oldString': 'search', 'newString': 'replace',
        'replaceAll': 'replace_all',
    },
    'apply_diffs': {'file_path': 'path', 'filepath': 'path'},
    # write_file: Claude *Write* uses file_path/content; others vary the body key
    'write_file': {
        'file_path': 'path', 'filepath': 'path', 'filename': 'path',
        'filePath': 'path',
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
        'filePath': 'path',
        'paths': 'reads', 'file_paths': 'reads', 'files': 'reads',
    },
    'list_dir': {'file_path': 'path', 'directory': 'path', 'dir': 'path',
                 'dir_path': 'path'},
    # Kimi CLI *Grep* uses ``glob`` for the file filter (≈ our ``include``)
    # and ``head_limit`` for the result cap; its ``ignore_case`` knob has no
    # counterpart (grep_search is case-insensitive by default) and passes
    # through inert.
    'grep_search': {'regex': 'pattern', 'query': 'pattern', 'search': 'pattern',
                    'glob': 'include', 'head_limit': 'max_results'},
    # Kimi CLI *Glob* names its root directory ``directory``.
    'find_files': {'glob': 'pattern', 'name': 'pattern', 'file_path': 'path',
                   'directory': 'path'},
    'run_command': {'cmd': 'command', 'shell_command': 'command',
                    'script': 'command', 'workdir': 'working_dir',
                    'cwd': 'working_dir',
                    # Legacy spelling of timeout_s — ambiguous unit, so the
                    # millisecond guard above only fires for this key.
                    'timeout': 'timeout_s'},
    'fetch_url': {'link': 'url'},
    'read_tool_artifact': {'ref': 'artifact_ref'},
    'search_tool_artifact': {'ref': 'artifact_ref'},
    # OpenCode's skill loader is ``skill`` ({name}); Claude Code's ``Skill``
    # carries {skill, args?} — ``args`` stays inert.
    'load_skill': {'name': 'skill_id', 'skill': 'skill_id'},
}


# Same alias problem one level DEEPER: the top-level keys are right, but each
# ``edits[]`` item carries another harness's item keys. Claude Code *Edit*
# emits ``{file_path, old_string, new_string}`` per item, so ``content`` is
# reported missing while the replacement text sits in ``new_string`` — a
# recoverable value the top-level pass cannot see. Item schemas never set
# ``additionalProperties: false``, so the alias key also survives validation
# and only the canonical key is reported missing — the observed retry loop.
_ITEM_PARAM_ALIASES: dict[str, dict[str, dict[str, str]]] = {
    # tool -> array param -> {alias: canonical item key}
    'edit_file': {'edits': {
        'file_path': 'path', 'filepath': 'path', 'filename': 'path',
        'filePath': 'path',
        'old_string': 'anchor', 'old_str': 'anchor', 'oldText': 'anchor',
        'oldString': 'anchor',
        'new_string': 'content', 'new_str': 'content', 'newText': 'content',
        'newString': 'content',
        'replaceAll': 'replace_all',
    }},
    # ``apply_diffs`` is already owned by _transform_multiedit_to_apply_diffs.
}


def _apply_item_param_aliases(
    tool_name: str, args: dict[str, Any],
) -> tuple[dict[str, Any], RepairLog]:
    """Rename wrong-harness keys inside one array param's object items.

    Same guards as :func:`_apply_param_aliases`, applied per item: rename only
    when the canonical item key is ABSENT, the alias key is NOT itself a
    declared item property, and the item is an object. Non-list values are
    left to the type walk.

    Returns:
        ``(args, log)`` with ``(f'{array_key}[{i}].{canonical}',
        'item_param_alias')`` entries. Empty when nothing matched.
    """
    alias_map = _ITEM_PARAM_ALIASES.get(tool_name)
    if not alias_map:
        return args, []
    log: RepairLog = []
    for array_key, item_aliases in alias_map.items():
        items = args.get(array_key)
        item_schema = _array_item_schema(tool_name, array_key)
        if not isinstance(items, list) or item_schema is None:
            continue
        expected, _required = item_schema
        # Copy-on-write: the caller's items stay untouched (see the
        # ``validate_then_repair`` no-mutation contract).
        repaired_items = None
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            renames = [
                (alias, canonical)
                for alias, canonical in item_aliases.items()
                if canonical in expected and alias in item
                and canonical not in item and alias not in expected
            ]
            if not renames:
                continue
            if repaired_items is None:
                repaired_items = list(items)
            renamed = dict(item)
            for alias, canonical in renames:
                renamed[canonical] = renamed.pop(alias)
                log.append((f'{array_key}[{index}].{canonical}',
                            'item_param_alias'))
            repaired_items[index] = renamed
        if repaired_items is not None:
            args[array_key] = repaired_items
    return args, log


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


# Top-level edit_file schema keys that are not edit-item data and must
# survive the reshape (see the call-site comment below). Declared in
# PROJECT_TOOL_EDIT_FILE, so downstream additionalProperties:false contract
# validation accepts them.
_EDIT_FILE_ENVELOPE_PASSTHROUGH_KEYS = ('allow_outside_workspace',)


def _distribute_shared_edit_path(
    edits: list[Any], shared_path: str,
) -> tuple[list[Any], bool]:
    """Give each edit its file path when the batch names exactly ONE file.

    Models routinely encode "same file as the rest of this batch" as an empty
    or omitted per-item ``path``: Claude Code *MultiEdit* carries one top-level
    ``file_path``, and the native single-file tools carry one path for the whole
    payload. Our batch schema declares ``path`` on every item with
    ``minLength: 1``, so those calls were rejected for a blank path and the
    model had to re-emit the whole batch — measured repeatedly across models
    (26 live rejections, all recovered on the next round).

    Fill ONLY from a path the CALL itself already declares: the envelope's
    shared path, or the single distinct non-empty item path. A multi-file batch
    with a blank path stays fail-closed — guessing which of several files was
    meant is not repair, it is a wrong-file write. The read gate, write
    freshness and unique-anchor match still decide whether the filled path is
    actually writable.
    """
    declared = [edit.get('path') for edit in edits if isinstance(edit, dict)]
    if isinstance(shared_path, str) and shared_path.strip():
        path = shared_path
    else:
        unique = list(dict.fromkeys(
            value for value in declared
            if isinstance(value, str) and value.strip()))
        if len(unique) != 1:
            return edits, False
        path = unique[0]
    distributed = False
    filled: list[Any] = list(edits)
    for index, edit in enumerate(filled):
        if not isinstance(edit, dict):
            continue
        current = edit.get('path')
        if isinstance(current, str):
            if current.strip():
                continue
        elif current is not None:
            continue  # non-string path is a type error, not a hole to fill
        updated = dict(edit)
        updated['path'] = path
        filled[index] = updated
        distributed = True
    return filled, distributed


def _transform_legacy_edit_to_edit_file(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Normalize native/legacy/foreign edit shapes into unified edits."""
    source_edits = args.get('edits')
    if isinstance(source_edits, list) and source_edits:
        candidates = source_edits
        shared_path = next((args.get(k) for k in (
            'path', 'file_path', 'filepath', 'filename', 'filePath')
            if args.get(k)), '')
    elif isinstance(args.get('edit'), (dict, list)):
        # Kimi CLI StrReplaceFile envelope: {path, edit: {old, new} | [...]}
        raw = args['edit']
        candidates = ([e for e in raw if isinstance(e, dict)]
                      if isinstance(raw, list) else [raw])
        if not candidates:
            return args, False
        shared_path = next((args.get(k) for k in (
            'path', 'file_path', 'filepath', 'filename', 'filePath')
            if args.get(k)), '')
    elif any(k in args for k in (
            'anchor', 'search', 'old_string', 'old_str', 'oldText',
            'oldString')):
        candidates = [args]
        shared_path = ''
    else:
        return args, False

    normalized = []
    changed = not (
        isinstance(source_edits, list)
        and all(isinstance(e, dict) and e.get('operation') for e in source_edits)
    )
    for edit in candidates:
        if not isinstance(edit, dict):
            normalized.append(edit)
            continue
        if edit.get('operation'):
            normalized.append(dict(edit))
            continue
        path = next((edit.get(k) for k in (
            'path', 'file_path', 'filepath', 'filename', 'filePath')
            if edit.get(k)), shared_path)
        search = next((edit.get(k) for k in (
            'search', 'old_string', 'old_str', 'oldText', 'oldString', 'old')
            if k in edit), None)
        replacement = next((edit.get(k) for k in (
            'replace', 'new_string', 'new_str', 'newText', 'newString', 'new')
            if k in edit), None)
        if search is not None:
            unified = {
                'path': path, 'operation': 'replace',
                'anchor': search, 'content': replacement if replacement is not None else '',
            }
        elif 'anchor' in edit:
            position = edit.get('position', 'after')
            unified = {
                'path': path,
                'operation': ('insert_before' if position == 'before'
                              else 'insert_after'),
                'anchor': edit.get('anchor'),
                'content': edit.get('content', edit.get('text', '')),
            }
        else:
            normalized.append(dict(edit))
            continue
        for optional in ('replace_all', 'description'):
            if optional in edit:
                unified[optional] = edit[optional]
        if 'replaceAll' in edit and 'replace_all' not in unified:
            unified['replace_all'] = edit['replaceAll']
        normalized.append(unified)

    # The envelope's shared path is not a declared top-level key, so it is
    # dropped from ``out`` below; every edit that still lacks a path inherits
    # it (or the batch's single distinct item path) BEFORE that drop, or the
    # call fails contract validation for a path the model did supply.
    normalized, _paths_filled = _distribute_shared_edit_path(
        normalized, shared_path)

    out = {'edits': normalized}
    if args.get('description'):
        out['description'] = args['description']
    # Envelope-scoped declared keys authorize the whole batch; they are never
    # edit-item data, so the reshape must carry them through. Dropping them
    # silently downgraded a confirmed call (conv mtq3ev44samfhr: edit_file
    # with allow_outside_workspace=true was refused in a loop — the refusal
    # text told the model to re-issue the exact call whose flag the repair
    # layer kept stripping).
    for key in _EDIT_FILE_ENVELOPE_PASSTHROUGH_KEYS:
        if key in args:
            out[key] = args[key]
    return out, changed or _paths_filled


def _transform_todo_write_nested_envelope(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Unwrap a todo_write envelope nested inside its own ``todos`` array.

    Observed live (conv ``mtdqz4bkuyitzj``, kimi-k3): the model emitted
    ``{"todos": [{"todos": [<real items>], "operation": "sync",
    "reason": "...", "id": ""}]}`` — the whole envelope pushed one level
    down into the array's only element, padded with a junk empty ``id``
    that half-satisfies the item schema. The intent is unambiguous, so
    lift it back up: ``todos`` becomes the inner list and envelope keys
    (``operation`` / ``reason`` / ``parent_todo_id``) are hoisted when the
    top level lacks them; junk item-level keys die with the wrapper.

    Fires only when ``todos`` is a single-element list whose element is a
    dict carrying its own ``todos`` list AND is not itself a genuine item
    (a real item always has ``content``) — a correct call is never
    touched. Anything still invalid after the unwrap fails closed at
    contract validation exactly as before.
    """
    todos = args.get('todos')
    if not isinstance(todos, list) or len(todos) != 1:
        return args, False
    element = todos[0]
    if not isinstance(element, dict) or 'content' in element:
        return args, False
    inner = element.get('todos')
    if not isinstance(inner, list):
        return args, False
    out = {k: v for k, v in args.items() if k != 'todos'}
    out['todos'] = inner
    for key in ('operation', 'reason', 'parent_todo_id', 'explanation'):
        if key not in out and isinstance(element.get(key), str):
            out[key] = element[key]
    return out, True

_TODO_LIST_COMMANDS = frozenset({
    'create', 'complete', 'load', 'add', 'remove', 'lookup',
})


def _transform_todo_list_command_to_todo_write(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Adapt an Amazon Q Developer CLI ``todo_list`` payload for todo_write.

    Amazon Q's built-in checklist tool is named ``todo_list`` (the name-alias
    layer maps it onto ``todo_write``) and speaks a command vocabulary —
    ``{command: create|complete|load|add|remove|lookup, tasks?,
    completed_indices?, new_tasks?, remove_indices?, insert_indices?,
    todo_list_description?}`` (aws/amazon-q-developer-cli
    ``crates/chat-cli/src/cli/chat/tools/todo.rs``) — instead of a full-list
    ``todos`` array. Our declared schema requires ``todos``, so an unadapted
    payload dies at contract validation before the state machine can
    interpret the command. Inject an explicitly marked empty placeholder;
    ``lib.tools.todo.apply_todo_operation`` expands it against current progress.
    The marker distinguishes a command from a native explicit clear. Fires only when ``command`` is a
    known todo_list verb and ``todos`` is absent — a native call is never
    touched.
    """
    command = args.get('command')
    if not isinstance(command, str) \
            or command.strip().lower() not in _TODO_LIST_COMMANDS:
        return args, False
    if 'todos' in args:
        return args, False
    out = dict(args)
    out['todos'] = []
    out['_todo_list_command'] = True
    return out, True


def _transform_kimi_todo_item_vocabulary(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Map Kimi CLI ``SetTodoList`` item vocabulary onto todo_write items.

    Kimi CLI's checklist tool (MoonshotAI/kimi-cli
    ``src/kimi_cli/tools/todo/__init__.py``) types items as
    ``{title: str, status: pending|in_progress|done}`` — no ``id``, ``title``
    instead of ``content``, ``done`` instead of ``completed``. Rename title
    only when content is absent and map done to completed. Neither harness
    requires item IDs. Native rows are untouched; invalid rows fail validation.
    """
    todos = args.get('todos')
    if not isinstance(todos, list) or not todos:
        return args, False
    items: list[Any] = []
    foreign = False
    for item in todos:
        if not isinstance(item, dict):
            items.append(item)
            continue
        new = dict(item)
        if 'content' not in new and isinstance(new.get('title'), str):
            new['content'] = new.pop('title')
            foreign = True
        if new.get('status') == 'done':
            new['status'] = 'completed'
            foreign = True
        items.append(new)
    if not foreign:
        return args, False
    out = dict(args)
    out['todos'] = items
    return out, True


def _transform_update_plan_to_todo_write(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Adapt a Codex CLI ``update_plan`` envelope for todo_write.

    Codex CLI's plan tool (openai/codex
    ``codex-rs/core/src/tools/handlers/plan_spec.rs``) takes
    ``{plan: [{step, status: pending|in_progress|completed}], explanation?}``
    — the list lives under ``plan``, not ``todos``, so an unadapted payload
    dies fail-closed on the required ``todos`` key. Rename the envelope key;
    the per-item ``step``→``content`` mapping is the item-vocabulary
    transform's job (it runs next in the chain). Optional explanation is
    already native. Only an absent todos key permits envelope translation;
    malformed native input cannot silently fall back to a foreign list.
    """
    plan = args.get('plan')
    if not isinstance(plan, list) or 'todos' in args:
        return args, False
    out = {k: v for k, v in args.items() if k != 'plan'}
    out['todos'] = plan
    return out, True


# Item keys/statuses that never appear in a native todo_write list item.
# Foreign presentation fields remain tolerated; text aliases map to content.
# No positional ID synthesis is needed by any of these full-list interfaces.
_FOREIGN_TODO_ITEM_MARKERS = frozenset({
    'step', 'description', 'priority', 'activeForm',
})


def _transform_harness_todo_item_vocabulary(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Map Claude Code / OpenCode / Gemini / Codex todo item vocabulary.

    Per dict item: rename Codex's ``step`` or Gemini's ``description`` to
    ``content`` only when ``content`` is absent (never clobber real text),
    and drop ``cancelled``/``canceled`` items outright — in Gemini CLI and
    OpenCode a cancelled task leaves the tracked list. Full snapshots match
    that meaning (completed would lie about work done). Only recognizable
    text-bearing cancelled rows may be removed; malformed rows must fail
    validation rather than accidentally becoming an empty list.
    """
    todos = args.get('todos')
    if not isinstance(todos, list) or not todos:
        return args, False
    items: list[Any] = []
    foreign = False
    for item in todos:
        if not isinstance(item, dict):
            items.append(item)
            continue
        if item.get('status') in ('cancelled', 'canceled'):
            text = next((item[k] for k in ('content', 'step', 'description') if k in item), None)
            if isinstance(text, str) and text.strip():
                foreign = True
                continue
        new = dict(item)
        if any(k in new for k in _FOREIGN_TODO_ITEM_MARKERS):
            foreign = True
        if 'content' not in new:
            for src in ('step', 'description'):
                if isinstance(new.get(src), str):
                    new['content'] = new.pop(src)
                    break
        items.append(new)
    if not foreign:
        return args, False
    out = dict(args)
    out['todos'] = items
    return out, True


def _transform_todo_write(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Run every todo_write cross-harness shape transform in order."""
    changed_any = False
    for fn in (_transform_todo_write_nested_envelope,
               _transform_todo_list_command_to_todo_write,
               _transform_update_plan_to_todo_write,
               _transform_kimi_todo_item_vocabulary,
               _transform_harness_todo_item_vocabulary):
        args, changed = fn(args)
        changed_any = changed_any or changed
    return args, changed_any

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


def _transform_str_replace_file_to_apply_diff(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Unwrap a Kimi CLI ``StrReplaceFile`` envelope for ``apply_diff``.

    StrReplaceFile (MoonshotAI/kimi-cli src/kimi_cli/tools/file/replace.py):
    ``{path, edit: {old, new, replace_all?} | [...]}``. A single-dict
    ``edit`` unwraps 1:1 onto ``{search, replace, replace_all?}``; the list
    form can't be expressed by single-edit apply_diff and passes through to
    fail closed at contract validation (edit_file sessions route the whole
    payload to the unified transform, which handles the list form). Fires
    only when ``edit`` is a dict AND neither ``search`` nor ``replace`` is
    already present — a native call is never touched.
    """
    edit = args.get('edit')
    if not isinstance(edit, dict):
        return args, False
    if 'search' in args or 'replace' in args:
        return args, False
    old, new = edit.get('old'), edit.get('new')
    if not isinstance(old, str) or not isinstance(new, str):
        return args, False
    out = {k: v for k, v in args.items() if k != 'edit'}
    out['search'] = old
    out['replace'] = new
    if isinstance(edit.get('replace_all'), bool):
        out['replace_all'] = edit['replace_all']
    return out, True


def _transform_foreign_read_range(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Map foreign harness read ranges onto ``start_line``/``end_line``.

    Claude Code *Read* and OpenCode *read* use ``{offset, limit}`` (1-based
    first line + line count); Kimi CLI *ReadFile* uses ``{line_offset,
    n_lines}`` with the same semantics. Both map exactly onto our inclusive
    ``start_line``/``end_line`` pair. Fires only when the native keys are
    absent AND at least one foreign key carries a positive int — a native
    call (or a native key winning over a stray foreign one) is never
    touched; foreign keys themselves pass through inert.
    """
    if 'start_line' in args or 'end_line' in args:
        return args, False
    start = args.get('line_offset', args.get('offset'))
    count = args.get('n_lines', args.get('limit'))
    if start is None and count is None:
        return args, False
    if start is not None and (type(start) is not int or start < 1):
        return args, False
    if count is not None and (type(count) is not int or count < 1):
        return args, False
    out = dict(args)
    if start is not None:
        out['start_line'] = start
    if count is not None:
        out['end_line'] = (start if start is not None else 1) + count - 1
    return out, True


# ══════════════════════════════════════════
#  Claude Code / Codex base-tool vocabulary
# ══════════════════════════════════════════

# Key spellings that exist only in a FOREIGN harness's edit/write schema —
# Claude Code Edit/Write (file_path, old_string, new_string), OpenCode edit
# (filePath/oldString/newString/replaceAll), Kimi StrReplaceFile (edit
# envelope), OpenAI str-replace variants, foreign write bodies (file_text/
# contents/text/data). Their presence marks the payload as
# foreign-schema-shaped, which licenses the description backfill below.
_FOREIGN_EDIT_KEY_SPELLINGS = frozenset({
    'file_path', 'filepath', 'filename', 'filePath',
    'old_string', 'old_str', 'oldText', 'oldString',
    'new_string', 'new_str', 'newText', 'newString',
    'file_text', 'contents', 'text', 'data', 'edit',
})
# For apply_diff the Kimi ``edit`` envelope is already covered by the str_replace
# unwrap's changed-flag, so a NATIVE call with a stray ``edit`` key must not
# count as foreign (test_apply_diff_native_call_with_stray_edit_key_untouched).
_APPLY_DIFF_FOREIGN_SPELLINGS = _FOREIGN_EDIT_KEY_SPELLINGS - {'edit'}

_PATH_SPELLINGS = ('path', 'file_path', 'filepath', 'filename', 'filePath')


def _first_path_spelling(args: dict[str, Any]) -> str:
    for k in _PATH_SPELLINGS:
        v = args.get(k)
        if isinstance(v, str) and v:
            return v
    return ''


def _edit_payload_is_foreign(args: dict[str, Any]) -> bool:
    if any(k in args for k in _FOREIGN_EDIT_KEY_SPELLINGS):
        return True
    edits = args.get('edits')
    return isinstance(edits, list) and any(
        isinstance(e, dict) and any(k in e for k in _FOREIGN_EDIT_KEY_SPELLINGS)
        for e in edits)


# Claude Code Write/Edit/MultiEdit never carry ``description`` — that key is a
# presentational UI caption, not execution semantics, so for payloads that
# evidently came from a foreign schema (where the key does not exist) it is
# derived from the target path instead of hard-failing the most common foreign
# tool calls. A NATIVE-schema call missing description still fails closed
# naming the key it was told about: the backfill never fires without a foreign
# key spelling, keeping the pipeline's "native payloads untouched" invariant
# even for broken native calls. An existing description is never clobbered.
def _backfill_caption(
    args: dict[str, Any], verb: str, foreign: bool, paths: list[str],
) -> tuple[dict[str, Any], bool]:
    if not foreign or args.get('description'):
        return args, False
    unique = list(dict.fromkeys(p for p in paths if p))
    if len(unique) == 1:
        caption = f'{verb} {unique[0]}'
    elif unique:
        caption = f'{verb} {len(unique)} files'
    else:
        caption = f'{verb} file'
    out = dict(args)
    out['description'] = caption
    return out, True


def _transform_write_file(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    return _backfill_caption(args, 'Write', _edit_payload_is_foreign(args),
                             [_first_path_spelling(args)])


def _transform_apply_diff(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    foreign = any(k in args for k in _APPLY_DIFF_FOREIGN_SPELLINGS)
    args, changed = _transform_str_replace_file_to_apply_diff(args)
    args, filled = _backfill_caption(args, 'Edit', foreign or changed,
                                     [_first_path_spelling(args)])
    return args, changed or filled


def _transform_apply_diffs(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    foreign = _edit_payload_is_foreign(args)
    args, changed = _transform_multiedit_to_apply_diffs(args)
    if not foreign:
        return args, changed
    edits = args.get('edits')
    if not isinstance(edits, list):
        return args, changed
    filled = False
    new_edits: list[Any] = []
    for e in edits:
        if (isinstance(e, dict) and not e.get('description')
                and isinstance(e.get('path'), str) and e.get('path')):
            e = dict(e)
            e['description'] = f"Edit {e['path']}"
            filled = True
        new_edits.append(e)
    if filled:
        args = dict(args)
        args['edits'] = new_edits
    return args, changed or filled


def _transform_edit_file(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    foreign = _edit_payload_is_foreign(args)
    args, changed = _transform_legacy_edit_to_edit_file(args)
    edits = args.get('edits')
    paths = ([e.get('path') for e in edits
              if isinstance(e, dict) and isinstance(e.get('path'), str)]
             if isinstance(edits, list) else [])
    args, filled = _backfill_caption(args, 'Edit', foreign, paths)
    return args, changed or filled


# Codex's legacy ``shell`` carried ``command`` as an argv array; Claude Code
# ``Bash`` and OpenCode ``bash`` express ``timeout`` in MILLISECONDS while this
# schema is seconds — a value above one day is unambiguously milliseconds (no
# harness sets a >24h tool timeout; Claude Code's own default is 120000 ms),
# and passing it through would turn a 2-minute budget into a 33-hour one.
_TIMEOUT_MS_THRESHOLD_S = 86400


def _transform_run_command(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    out = dict(args)
    changed = False
    for key in ('command', 'cmd'):
        value = out.get(key)
        if (isinstance(value, list) and value
                and all(isinstance(part, str) for part in value)):
            out[key] = ' '.join(value)
            changed = True
    timeout = out.get('timeout')
    if type(timeout) in (int, float) and timeout > _TIMEOUT_MS_THRESHOLD_S:
        out['timeout'] = max(1, round(timeout / 1000))
        changed = True
    return (out, True) if changed else (args, False)


# Claude Code ``Grep`` and Kimi ``Grep`` shape results with an ``output_mode``
# enum; only the count modes have a native counterpart. ``content`` and
# ``files_with_matches`` stay inert — the native default returns content
# matches, a safe superset of files_with_matches.
_GREP_COUNT_OUTPUT_MODES = frozenset({'count', 'count_matches'})


def _transform_grep_search(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    if args.get('count_only') is True:
        return args, False
    mode = args.get('output_mode')
    if isinstance(mode, str) and mode.strip().lower() in _GREP_COUNT_OUTPUT_MODES:
        out = dict(args)
        out['count_only'] = True
        return out, True
    return args, False


# Claude Code ``Task`` / OpenCode ``task`` / Kimi ``Agent`` share one
# single-subagent envelope {description, prompt, subagent_type?}. The
# ``subagent_type`` values are harness-defined freeform strings; when one
# names a spawnable native role (researcher/coder/...) it selects that role,
# anything else falls back to the role default. The self-contained ``prompt``
# becomes the objective; the rest passes through inert.
def _transform_spawn_agents(
    args: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    if isinstance(args.get('agents'), list):
        return args, False
    prompt = args.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        return args, False
    out = dict(args)
    agent: dict[str, Any] = {'objective': prompt.strip()}
    subagent_type = args.get('subagent_type')
    if isinstance(subagent_type, str) and subagent_type.strip():
        # Local import: the repair layer must not initialize the swarm
        # registry at module import time.
        from lib.swarm.registry import resolve_spawnable_role
        role = resolve_spawnable_role(subagent_type)
        if role != 'general':
            agent['role'] = role
    out['agents'] = [agent]
    return out, True


# Registry keyed by the CANONICAL (already name-resolved) tool name.
_STRUCTURAL_TRANSFORMS: dict[str, Any] = {
    'edit_file': _transform_edit_file,
    'apply_diff': _transform_apply_diff,
    'apply_diffs': _transform_apply_diffs,
    'write_file': _transform_write_file,
    'run_command': _transform_run_command,
    'grep_search': _transform_grep_search,
    'spawn_agents': _transform_spawn_agents,
    'read_files': _transform_foreign_read_range,
    'ask_human': _transform_askuserquestion_to_ask_human,
    'todo_write': _transform_todo_write,
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
