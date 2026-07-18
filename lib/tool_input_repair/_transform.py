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

from lib.tool_input_repair._schema import RepairLog

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
