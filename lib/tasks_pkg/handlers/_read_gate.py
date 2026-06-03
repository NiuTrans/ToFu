# HOT_PATH
"""Read-before-edit gate for ``apply_diff`` / ``insert_content``.

The model frequently issues an ``apply_diff`` for a file it has not read,
relying on remembered or guessed content. When the guess is wrong the
patch fails with "Search text not found", but inside the same parallel
turn the model often issued the bad patch alongside a ``read_files`` of
the same file — the read can't help because tool calls in one turn are
independent.

This gate refuses ``apply_diff`` / ``insert_content`` when the target file
has not been read (or written) earlier in the conversation, forcing the
model to read first and patch in a subsequent turn. Cheaper failure mode
than a wrong patch.

Recognised "fresh enough" sources for a target file:
  1. A successful ``read_files`` in ``task['toolRounds']`` (current turn,
     status=='done'). Sibling reads in the SAME turn don't count because
     they haven't completed yet — write tools run before parallel reads.
  2. A successful ``read_files`` / ``write_file`` / ``apply_diff`` /
     ``insert_content`` in a prior assistant turn (``task['messages']``).
  3. The file did not exist on disk at gate-check time — apply_diff would
     fail with a clearer "File not found" message, so we let it through.

Disable via env: ``TOFU_APPLY_DIFF_READ_GATE=0``.
"""

from __future__ import annotations

import json
import os

from lib.log import get_logger

logger = get_logger(__name__)


_GATED_TOOLS = ('apply_diff', 'apply_diffs', 'insert_content', 'insert_contents')

# Tools whose successful invocation gives the model authoritative content
# of the targeted file — they all satisfy the gate.
_SATISFYING_TOOLS = ('read_files', 'write_file', 'apply_diff', 'apply_diffs', 'insert_content', 'insert_contents')


def _gate_enabled() -> bool:
    val = os.environ.get('TOFU_APPLY_DIFF_READ_GATE', '1').strip().lower()
    return val not in ('0', 'false', 'no', 'off', '')


def _collect_target_paths(fn_name: str, fn_args: dict) -> list[str]:
    """Extract every path the gated tool call will touch.

    Handles batch shapes (``edits=[{path: ..}, ...]``) and the single-edit
    top-level shape. Returns the raw path strings as the model wrote them
    (with any ``rootname:`` prefix preserved) so we can resolve them.
    """
    if not isinstance(fn_args, dict):
        return []
    paths: list[str] = []
    edits = fn_args.get('edits')
    if isinstance(edits, list):
        for e in edits:
            if isinstance(e, dict):
                p = e.get('path')
                if isinstance(p, str) and p.strip():
                    paths.append(p.strip())
    p = fn_args.get('path')
    if isinstance(p, str) and p.strip() and p.strip() not in paths:
        paths.append(p.strip())
    return paths


def _resolve_abs(project_path: str | None, conv_id: str | None, raw_path: str) -> str:
    """Resolve ``raw_path`` (possibly ``rootname:rel`` or ~ or absolute)
    to an absolute filesystem path, returning '' on failure.

    Failures here are non-fatal — we degrade to a literal compare instead
    of blocking the gate on a bad lookup.
    """
    try:
        from lib.project_mod.tools import _resolve_base
        bp, rp = _resolve_base(project_path or '', raw_path, conv_id=conv_id)
        if rp and (rp.startswith('/') or rp.startswith('~')):
            return os.path.abspath(os.path.expanduser(rp))
        if bp:
            return os.path.abspath(os.path.join(bp, rp))
        return os.path.abspath(os.path.expanduser(rp)) if rp else ''
    except Exception as e:
        logger.debug('[ReadGate] _resolve_base failed for %r: %s', raw_path, e)
        return ''


def _collect_satisfied_paths_from_rounds(task: dict, project_path: str | None) -> set[str]:
    """Return the set of absolute paths satisfied by ``task['toolRounds']``.

    Only rounds with ``status == 'done'`` count — a sibling read_files in
    the SAME turn (still ``'searching'``) must not satisfy a gated edit,
    that's exactly the failure mode we're preventing.
    """
    out: set[str] = set()
    conv_id = task.get('convId')
    for r in task.get('toolRounds') or []:
        if r.get('status') != 'done':
            continue
        tn = r.get('toolName') or ''
        if tn not in _SATISFYING_TOOLS:
            continue
        args_str = r.get('toolArgs') or ''
        try:
            args = json.loads(args_str) if args_str else {}
        except (json.JSONDecodeError, TypeError) as _e_audit:
            logger.debug('[_read_gate] _collect_satisfied_paths_from_rounds caught %s: %s', type(_e_audit).__name__, _e_audit)
            continue
        if not isinstance(args, dict):
            continue
        # read_files supports batch ``reads`` array; the others use ``edits``
        # or top-level ``path``. Re-use the same collector.
        if tn == 'read_files':
            reads = args.get('reads')
            if isinstance(reads, list):
                for spec in reads:
                    if isinstance(spec, dict) and spec.get('path'):
                        ap = _resolve_abs(project_path, conv_id, str(spec['path']))
                        if ap:
                            out.add(ap)
                    elif isinstance(spec, str) and spec.strip():
                        ap = _resolve_abs(project_path, conv_id, spec.strip())
                        if ap:
                            out.add(ap)
            if isinstance(args.get('path'), str) and args['path'].strip():
                ap = _resolve_abs(project_path, conv_id, args['path'].strip())
                if ap:
                    out.add(ap)
        else:
            for p in _collect_target_paths(tn, args):
                ap = _resolve_abs(project_path, conv_id, p)
                if ap:
                    out.add(ap)
    return out


def _collect_satisfied_paths_from_messages(task: dict, project_path: str | None) -> set[str]:
    """Return the set of absolute paths satisfied by prior assistant turns
    in ``task['messages']``.

    Walks every assistant message with ``tool_calls`` and pairs each call
    with its corresponding ``role: tool`` result message by ``tool_call_id``.
    Only pairs whose tool result is non-empty AND does not start with the
    standard error markers count.
    """
    msgs = task.get('messages') or []
    if not msgs:
        return set()
    # Index tool result messages by tool_call_id for O(N) lookup.
    tool_results: dict[str, str] = {}
    for m in msgs:
        if m.get('role') == 'tool':
            tcid = m.get('tool_call_id') or ''
            if tcid:
                content = m.get('content') or ''
                if isinstance(content, list):
                    parts = [p.get('text', '') for p in content
                             if isinstance(p, dict) and p.get('type') == 'text']
                    content = ''.join(parts)
                tool_results[tcid] = content if isinstance(content, str) else str(content)

    out: set[str] = set()
    conv_id = task.get('convId')
    for m in msgs:
        if m.get('role') != 'assistant':
            continue
        tcs = m.get('tool_calls') or []
        for tc in tcs:
            fn = tc.get('function') or {}
            name = fn.get('name') or ''
            if name not in _SATISFYING_TOOLS:
                continue
            tcid = tc.get('id') or ''
            result_text = tool_results.get(tcid, '')
            if not _result_indicates_success(name, result_text):
                continue
            args_raw = fn.get('arguments') or ''
            try:
                args = json.loads(args_raw) if args_raw else {}
            except (json.JSONDecodeError, TypeError) as _e_audit:
                logger.debug('[_read_gate] _collect_satisfied_paths_from_messages caught %s: %s', type(_e_audit).__name__, _e_audit)
                continue
            if not isinstance(args, dict):
                continue
            if name == 'read_files':
                reads = args.get('reads')
                if isinstance(reads, list):
                    for spec in reads:
                        if isinstance(spec, dict) and spec.get('path'):
                            ap = _resolve_abs(project_path, conv_id, str(spec['path']))
                            if ap:
                                out.add(ap)
                        elif isinstance(spec, str) and spec.strip():
                            ap = _resolve_abs(project_path, conv_id, spec.strip())
                            if ap:
                                out.add(ap)
                if isinstance(args.get('path'), str) and args['path'].strip():
                    ap = _resolve_abs(project_path, conv_id, args['path'].strip())
                    if ap:
                        out.add(ap)
            else:
                for p in _collect_target_paths(name, args):
                    ap = _resolve_abs(project_path, conv_id, p)
                    if ap:
                        out.add(ap)
    return out


def _result_indicates_success(name: str, result_text: str) -> bool:
    """Return True when *result_text* suggests the tool succeeded.

    For read_files, an "Error:" prefix (e.g. "Error: File not found") means
    the model never actually saw the file — those don't satisfy the gate.
    For write_file / apply_diff / insert_content the success path begins
    with words like "File created" / "Applied" / "Inserted"; failures
    begin with "Write failed" / "Diff failed" / "Insert failed". We use
    a simple negative-prefix check for robustness.
    """
    if not result_text:
        return False
    s = result_text.lstrip()
    if s.startswith(('Error:', 'ERROR:')):
        return False
    if name in ('apply_diff', 'apply_diffs', 'insert_content', 'insert_contents', 'write_file'):
        if s.startswith(('Diff failed', 'Insert failed', 'Write failed', 'Failed')):
            return False
    return True


def check_read_before_edit(task: dict, fn_name: str, fn_args: dict,
                            project_path: str | None) -> str | None:
    """Gate: refuse apply_diff / insert_content for unread files.

    Returns ``None`` to allow the call through, or an error message string
    to surface back to the model (the call must NOT execute).
    """
    if not _gate_enabled():
        return None
    if fn_name not in _GATED_TOOLS:
        return None

    raw_paths = _collect_target_paths(fn_name, fn_args)
    if not raw_paths:
        return None

    conv_id = task.get('convId')
    targets: list[tuple[str, str]] = []  # (raw, abs)
    for rp in raw_paths:
        ap = _resolve_abs(project_path, conv_id, rp)
        if not ap:
            # Couldn't resolve — let downstream handle it (will likely
            # fail with the regular workspace-root error).
            continue
        targets.append((rp, ap))
    if not targets:
        return None

    satisfied = _collect_satisfied_paths_from_rounds(task, project_path)
    satisfied |= _collect_satisfied_paths_from_messages(task, project_path)

    unread: list[tuple[str, str]] = []
    for raw, ap in targets:
        if ap in satisfied:
            continue
        # Skip files that don't exist — downstream will return the cleaner
        # "File not found" error and the model can decide to write_file.
        if not os.path.isfile(ap):
            continue
        unread.append((raw, ap))

    if not unread:
        return None

    msg = _format_refusal(fn_name, [raw for raw, _ in unread])
    logger.info(
        '[ReadGate] Refused %s for unread file(s) %s (task=%s)',
        fn_name, ', '.join(raw for raw, _ in unread), task.get('id', '?')[:8],
    )
    return msg


def _format_refusal(fn_name: str, raw_paths: list[str]) -> str:
    """Build the model-facing refusal message naming the unread file(s)."""
    paths_list = ', '.join(raw_paths)
    return (
        f'Error: {fn_name} refused — must read each target file first.\n'
        f'Unread file(s): {paths_list}\n'
        f'Issue read_files for these path(s) in this turn, then re-issue '
        f'{fn_name} in the NEXT turn (a sibling read_files in the same '
        f'parallel batch does not count — its result is not visible to '
        f'this tool call). This guards against patches built from '
        f'guessed/remembered content. Set env TOFU_APPLY_DIFF_READ_GATE=0 '
        f'to disable this check.'
    )


def partition_batch_edits(task: dict, fn_name: str, fn_args: dict,
                          project_path: str | None) -> tuple[list[int], list[str]]:
    """Partition a batch edit call into read vs. unread targets.

    For ``apply_diffs`` / ``insert_contents`` (the ``edits=[...]`` shape),
    returns ``(skip_indices, unread_raw_paths)``:

      * ``skip_indices`` — 0-based indices into ``fn_args['edits']`` whose
        target file has NOT been read/written earlier in the conversation
        and so must be skipped.
      * ``unread_raw_paths`` — de-duplicated raw path strings (as the model
        wrote them), in first-seen order, for messaging.

    Returns ``([], [])`` when the gate is disabled, the tool is not a gated
    batch tool, or every target is satisfied. Edits whose path can't be
    resolved, or whose file doesn't exist on disk, are NOT skipped here —
    downstream surfaces the cleaner error for those.
    """
    if not _gate_enabled():
        return [], []
    if fn_name not in _GATED_TOOLS:
        return [], []
    edits = fn_args.get('edits')
    if not isinstance(edits, list) or not edits:
        return [], []

    conv_id = task.get('convId')
    satisfied = _collect_satisfied_paths_from_rounds(task, project_path)
    satisfied |= _collect_satisfied_paths_from_messages(task, project_path)

    skip_indices: list[int] = []
    unread_raw: list[str] = []
    seen_raw: set[str] = set()
    for idx, e in enumerate(edits):
        if not isinstance(e, dict):
            continue
        rp = (e.get('path') or '').strip()
        if not rp:
            continue
        ap = _resolve_abs(project_path, conv_id, rp)
        if not ap:
            continue
        if ap in satisfied:
            continue
        if not os.path.isfile(ap):
            continue
        skip_indices.append(idx)
        if rp not in seen_raw:
            seen_raw.add(rp)
            unread_raw.append(rp)
    return skip_indices, unread_raw
