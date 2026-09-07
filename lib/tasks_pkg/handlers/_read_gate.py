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
     Stored arguments are the VERBATIM model emission — the dispatch-time
     repair layer (lib/tool_input_repair) renames wrong-harness keys
     (``file_path``→``path``, ``old_string``→``search``, Claude MultiEdit
     shape→``apply_diffs``) only in its own copy — so the collectors here
     run the same alias/structural normalization before extracting paths,
     or a read performed under an aliased key would be invisible to the
     gate (production-observed refusal loop, 2026-07-25).
  3. A NON-STALE write-freshness token for (this conv, the file). The
     message/toolRounds scan is ephemeral — compaction rewrites
     ``task['messages']`` mid-run, persisted histories get compacted to
     zero tool_calls, and a new task starts with empty toolRounds — so
     sources 1–2 "forget" reads that demonstrably happened. The token
     store (lib/write_freshness.py) is keyed by (conv_key, abs_path),
     survives compaction AND restart, is only written after a SUCCESSFUL
     read/write, and re-fingerprints the file on check — a non-stale
     token is strictly STRONGER evidence than a message scan (read +
     byte-unchanged since). A STALE token never satisfies: the companion
     FreshGate owns the precise "changed on disk" refusal, and refusing
     here too keeps the composition fail-closed when FreshGate is off.
  4. The file did not exist on disk at gate-check time — apply_diff would
     fail with a clearer "File not found" message, so we let it through.

Disable via env: ``TOFU_APPLY_DIFF_READ_GATE=0``.
"""

from __future__ import annotations

import json
import os
import threading

from lib.log import get_logger
from lib.project_mod.path_resolution import _resolve_base
from lib.tool_history_pairing import adjacent_tool_call_result_pairs

logger = get_logger(__name__)

# Incremental satisfied-path cache for the O(history) messages scan. Keyed
# by (conv namespace, project_path); the value records the message count +
# list identity at scan time so appends fold only NEW messages and a
# reassigned/rewritten messages list invalidates to a full rescan. The
# per-turn ``toolRounds`` list is deliberately NOT cached here: it is reset
# every turn, is small, and its round entries mutate status in place
# (``searching`` → ``done``), so a count/identity key would not see those
# flips. The toolRounds scan stays a fresh cheap per-turn pass.
from lib.ttl_cache import TTLCache
_satisfied_cache = TTLCache(
    ttl=600.0,
    max_size=512,
    name='read_gate_satisfied',
)
_satisfied_cache_lock = threading.Lock()


_GATED_TOOLS = (
    'edit_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
)

# Tools whose successful invocation gives the model authoritative content
# of the targeted file — they all satisfy the gate.
_SATISFYING_TOOLS = (
    'read_files', 'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
)


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
        bp, rp = _resolve_base(project_path or '', raw_path, conv_id=conv_id)
        if rp and (rp.startswith('/') or rp.startswith('~')):
            return os.path.abspath(os.path.expanduser(rp))
        if bp:
            return os.path.abspath(os.path.join(bp, rp))
        return os.path.abspath(os.path.expanduser(rp)) if rp else ''
    except Exception as e:
        logger.debug('[ReadGate] _resolve_base failed for %r: %s', raw_path, e)
        return ''


# Canonical argument keys across the gate's satisfying tools. Passed as the
# ``expected`` schema to the repair layer's alias renamer: its guards only
# require ``canonical in expected`` and ``alias not in expected``, and this
# superset satisfies both for every satisfying tool (aliases are foreign
# names like ``file_path`` / ``old_string`` / ``paths`` / ``file_text``).
_GATE_ARG_KEYS = {k: 'string' for k in (
    'path', 'reads', 'edits', 'search', 'replace', 'content',
    'anchor', 'position', 'description', 'replace_all',
)}


def _normalize_historical_args(name: str, args: dict) -> dict:
    """Canonicalize a STORED tool call's args for path extraction.

    Messages persist the arguments exactly as the model emitted them; the
    dispatch-time repair layer renames wrong-harness keys (and reshapes
    Claude MultiEdit payloads) only in its execution copy. Run the SAME
    transforms here so a successful historical read/write performed under
    aliased keys still satisfies the gate. Fail-open: any error returns
    the args unchanged (the old blind behaviour — never worse).
    """
    if not isinstance(args, dict):
        return args
    try:
        from lib.tool_input_repair import (
            _apply_param_aliases,
            _apply_structural_transform,
        )
        out, _changed = _apply_structural_transform(name, args)
        out, _log = _apply_param_aliases(name, dict(out), _GATE_ARG_KEYS)
        return out
    except Exception as e:
        logger.debug('[ReadGate] historical-arg normalization failed for %s: %s',
                     name, e)
        return args


def _freshness_token_covers(task: dict, abs_path: str) -> bool:
    """True when the write-freshness store holds a NON-STALE token for
    (this conversation, ``abs_path``) — compaction/restart-proof evidence
    that the file was read/written here AND is byte-unchanged since.

    Fail-open on any error (the guard must never become an availability
    risk); a stale token returns False so the write is still refused.
    """
    try:
        from lib import write_freshness
        # Same namespace discipline as handlers/_write_freshness_gate.py
        # (_conv_key): convId, falling back to the task id for sub-tasks
        # (e.g. the autopilot virtual-user) so they don't leak into the
        # shared '' bucket. Kept inline — importing _write_freshness_gate
        # here would be circular (it already imports from this module).
        key = task.get('convId') or task.get('id') or ''
        if not key:
            return False
        return (write_freshness.has_token(key, abs_path)
                and not write_freshness.is_stale(key, abs_path))
    except Exception as e:
        logger.debug('[ReadGate] freshness-token probe failed for %s: %s',
                     abs_path, e)
        return False


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
        args = _normalize_historical_args(tn, args)
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


def _collect_satisfied_paths_from_messages(task: dict, project_path: str | None,
                                           start: int = 0) -> set[str]:
    """Return the set of absolute paths satisfied by prior assistant turns
    in ``task['messages'][start:]``.

    Walks every assistant message with ``tool_calls`` and pairs each call
    with its adjacent ``role: tool`` result message.  IDs select a queue only
    inside that provider batch; repeated legacy positional IDs are consumed
    by occurrence instead of collapsing into one conversation-global value.
    Only pairs whose tool result is non-empty AND does not start with the
    standard error markers count. ``start`` is the incremental-cache seam:
    the suffix it cuts is always a whole-message boundary, and a tool result
    always follows its assistant message, so indexing only the suffix is
    equivalent to indexing the whole list.
    """
    msgs = (task.get('messages') or [])[start:]
    if not msgs:
        return set()
    out: set[str] = set()
    conv_id = task.get('convId')
    for tc, result_message in adjacent_tool_call_result_pairs(msgs):
        fn = tc.get('function') or {}
        if not isinstance(fn, dict):
            continue
        name = fn.get('name') or ''
        if name not in _SATISFYING_TOOLS:
            continue
        content = result_message.get('content') or ''
        if isinstance(content, list):
            parts = [
                part.get('text', '') for part in content
                if isinstance(part, dict) and part.get('type') == 'text'
            ]
            content = ''.join(parts)
        result_text = content if isinstance(content, str) else str(content)
        if not _result_indicates_success(name, result_text):
            continue
        args_raw = fn.get('arguments') or ''
        try:
            args = json.loads(args_raw) if args_raw else {}
        except (json.JSONDecodeError, TypeError) as _e_audit:
            logger.debug(
                '[_read_gate] historical tool arguments failed to parse: '
                '%s: %s', type(_e_audit).__name__, _e_audit)
            continue
        if not isinstance(args, dict):
            continue
        args = _normalize_historical_args(name, args)
        if name == 'read_files':
            reads = args.get('reads')
            if isinstance(reads, list):
                for spec in reads:
                    if isinstance(spec, dict) and spec.get('path'):
                        ap = _resolve_abs(
                            project_path, conv_id, str(spec['path']))
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
            for path in _collect_target_paths(name, args):
                absolute_path = _resolve_abs(project_path, conv_id, path)
                if absolute_path:
                    out.add(absolute_path)
    return out


def _cached_satisfied_paths_from_messages(task: dict, project_path: str | None) -> set[str]:
    """Incremental wrapper over ``_collect_satisfied_paths_from_messages``.

    The message-history scan JSON-parses every historical tool call on each
    gated write; on a long conversation that is the dominant gate cost. This
    cache makes the scan append-only: an unchanged prefix is reused and only
    newly-appended messages are folded in. A replaced or shrunk messages list
    invalidates to a full rescan.
    """
    conv_key = task.get('convId') or task.get('id') or ''
    key = (conv_key, os.path.abspath(project_path or ''))
    msgs = task.get('messages') or []
    msg_count = len(msgs)
    msg_list_id = id(msgs)

    with _satisfied_cache_lock:
        entry = _satisfied_cache.get(key)
        if entry is not None and entry['msg_list_id'] == msg_list_id:
            if msg_count == entry['msg_count']:
                return set(entry['satisfied'])
            if msg_count > entry['msg_count']:
                start = entry['msg_count']
                base = entry['satisfied']
                # A caller may observe the assistant carrier before its tool
                # receipts are appended.  A suffix beginning with ``tool`` has
                # crossed that semantic pair boundary; rescan rather than
                # treating the orphan result as permanently unprovable.
                if (start < msg_count
                        and isinstance(msgs[start], dict)
                        and msgs[start].get('role') == 'tool'):
                    start = 0
                    base = set()
            else:
                start = 0
                base = set()
        else:
            start = 0
            base = set()

    if start == 0:
        satisfied = _collect_satisfied_paths_from_messages(task, project_path)
    else:
        satisfied = base | _collect_satisfied_paths_from_messages(
            task, project_path, start=start)

    with _satisfied_cache_lock:
        _satisfied_cache.set(key, {
            'msg_count': msg_count,
            'msg_list_id': msg_list_id,
            'satisfied': set(satisfied),
        })
    return set(satisfied)


def _reset_satisfied_cache_for_tests() -> None:
    """Clear the incremental cache — test isolation helper (process-global)."""
    with _satisfied_cache_lock:
        _satisfied_cache.clear()


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
    if name in ('edit_file', 'apply_diff', 'apply_diffs',
                'insert_content', 'insert_contents', 'write_file'):
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
    satisfied |= _cached_satisfied_paths_from_messages(task, project_path)

    unread: list[tuple[str, str]] = []
    for raw, ap in targets:
        if ap in satisfied:
            continue
        # Skip files that don't exist — downstream will return the cleaner
        # "File not found" error and the model can decide to write_file.
        if not os.path.isfile(ap):
            continue
        # Compaction/restart-proof evidence: a non-stale freshness token
        # proves this conv read/wrote the file AND it is unchanged since.
        if _freshness_token_covers(task, ap):
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
    satisfied |= _cached_satisfied_paths_from_messages(task, project_path)

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
        if _freshness_token_covers(task, ap):
            continue
        skip_indices.append(idx)
        if rp not in seen_raw:
            seen_raw.add(rp)
            unread_raw.append(rp)
    return skip_indices, unread_raw
