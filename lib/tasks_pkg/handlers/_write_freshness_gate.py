# HOT_PATH
"""Write-freshness gate for the project write tools.

Companion to the read-before-edit gate (``_read_gate.py``). That gate proves
"you read this file at SOME point in this conversation"; THIS gate proves
"it has not changed SINCE". On a shared working tree the classic clobber
is: conversation A reads a file at T0, thinks for many rounds, and at T2
writes from its stale memory — silently discarding sibling B's T1 change.

The check is an optimistic-concurrency token (``lib/write_freshness.py``):
successful reads AND writes record a fingerprint per
``(conv_key, abs_path)`` — content-addressed (blake2b) for files ≤ 4 MiB,
``(mtime_ns, size)`` above — and a write whose recorded fingerprint no
longer matches the disk is refused with a re-read instruction. The content
signal matters because this deployment's FUSE mount has 1-SECOND mtime
granularity: ``(mtime_ns, size)`` alone is blind to same-second same-size
edits (measured; see lib/write_freshness.py).

Deliberate scope (fail-open everywhere else):

  * NO token → allowed. Blind full-rewrite flows keep working; the
    read-before-edit gate owns the "must read first" axis for edit tools.
  * File VANISHED since the token → allowed (creation semantics; the edit
    tools' own "File not found" path is the clearer error).
  * Check failure (resolution error, store error) → allowed, logged at
    WARNING. A guard must never become an availability risk.

Only a positively-known STALE token refuses, because that is the exact
clobber pattern. Env: ``TOFU_WRITE_FRESHNESS_GATE=0`` disables.
"""

from __future__ import annotations

import os

from lib import write_freshness
from lib.log import get_logger
from lib.tasks_pkg.handlers._read_gate import _collect_target_paths, _resolve_abs

logger = get_logger(__name__)

# Single-target write tools gated wholesale; batch tools are partitioned
# per-path (mirrors the read gate's two shapes).
_GATED_SINGLE_TOOLS = ('write_file', 'apply_diff', 'insert_content')
_GATED_BATCH_TOOLS = ('edit_file', 'apply_diffs', 'insert_contents')


def _conv_key(task: dict) -> str:
    """The freshness-token namespace for this task: convId-or-task-id.

    Mirrors the workspace-root registration key discipline (see the big
    comment in ``handlers/project.py``): a sub-task with convId='' (e.g. the
    autopilot virtual-user) gets its own namespace instead of leaking into
    the shared '' bucket.
    """
    return (task.get('convId') or task.get('id') or '')


def _stale_targets(task: dict, raw_paths: list, project_path: str | None) -> list:
    """The subset of ``raw_paths`` whose recorded fingerprint moved."""
    key = _conv_key(task)
    if not key:
        return []
    stale = []
    for rp in raw_paths:
        ap = _resolve_abs(project_path, task.get('convId'), rp)
        if not ap or not os.path.isfile(ap):
            # Unresolvable or vanished → fail-open (see module docstring).
            continue
        if write_freshness.is_stale(key, ap):
            stale.append(rp)
    return stale


def check_write_freshness(task: dict, fn_name: str, fn_args: dict,
                          project_path: str | None) -> str | None:
    """Gate: refuse a single-target write against a stale-known file.

    Returns ``None`` to allow the call through, or an error message string
    to surface back to the model (the call must NOT execute).
    """
    if fn_name not in _GATED_SINGLE_TOOLS:
        return None
    raw_paths = _collect_target_paths(fn_name, fn_args)
    if not raw_paths:
        return None
    stale = _stale_targets(task, raw_paths, project_path)
    if not stale:
        return None
    msg = _format_stale_refusal(fn_name, stale)
    logger.info('[FreshGate] Refused %s for stale file(s) %s (task=%s)',
                fn_name, ', '.join(stale), task.get('id', '?')[:8])
    return msg


def partition_stale_edits(task: dict, fn_args: dict,
                          project_path: str | None) -> tuple[list[int], list[str]]:
    """Partition a batch write call into fresh vs. stale targets.

    Returns ``(skip_indices, stale_raw_paths)`` — 0-based indices into
    ``fn_args['edits']`` whose target file changed on disk since this
    conversation's last token, plus the de-duplicated raw path strings for
    messaging. Edits whose path can't be resolved, or whose file vanished,
    are NOT skipped here (fail-open; downstream surfaces the cleaner error).
    """
    edits = fn_args.get('edits') if isinstance(fn_args, dict) else None
    if not isinstance(edits, list) or not edits:
        return [], []
    key = _conv_key(task)
    if not key:
        return [], []
    skip_indices: list[int] = []
    stale_raw: list[str] = []
    seen: set[str] = set()
    for idx, e in enumerate(edits):
        if not isinstance(e, dict):
            continue
        rp = (e.get('path') or '').strip()
        if not rp:
            continue
        ap = _resolve_abs(project_path, task.get('convId'), rp)
        if not ap or not os.path.isfile(ap):
            continue
        if not write_freshness.is_stale(key, ap):
            continue
        skip_indices.append(idx)
        if rp not in seen:
            seen.add(rp)
            stale_raw.append(rp)
    return skip_indices, stale_raw


def _format_stale_refusal(fn_name: str, raw_paths: list) -> str:
    """Build the model-facing refusal message naming the stale file(s)."""
    paths_list = ', '.join(raw_paths)
    return (
        f'Error: {fn_name} refused — file changed on disk since this '
        f'conversation last read/wrote it.\n'
        f'Stale file(s): {paths_list}\n'
        f'Another conversation or process modified the file after your last '
        f'read/write of it here, so writing now would silently discard '
        f'their change. Re-read the file(s) with read_files, reconcile your '
        f'edit against the current content, then re-issue {fn_name}. This '
        f'is the shared-worktree overwrite guard; set env '
        f'TOFU_WRITE_FRESHNESS_GATE=0 to disable it.'
    )


# File-reading tools whose result is authoritative per-file content — the
# only tools the freshness token tracks (a search/listing covers no single
# file's bytes).
FILE_READ_TOOLS = ('read_files', 'inspect_image')


def _covered_read_paths(fn_args: dict) -> list[str]:
    """Every raw path a read_files/inspect_image call will surface."""
    raws: list[str] = []
    reads = fn_args.get('reads')
    if isinstance(reads, list):
        for spec in reads:
            if isinstance(spec, dict) and spec.get('path'):
                raws.append(str(spec['path']).strip())
            elif isinstance(spec, str) and spec.strip():
                raws.append(spec.strip())
    p = fn_args.get('path')
    if isinstance(p, str) and p.strip():
        raws.append(p.strip())
    return [r for r in raws if r]


def cached_read_is_stale(task: dict, fn_args: dict,
                         project_path: str | None) -> bool:
    """True when serving a dedup/prefetch-cached read result would hand the
    model STALE bytes: a covered file changed on disk since this
    conversation's freshness token.

    The streaming pre-exec + dedup-hit path bypasses the project-tool
    handler, so a cached read can be arbitrarily older than the disk. The
    tool pipeline consults this before serving a cached
    read_files/inspect_image result: True → drop the entry and re-execute
    for real — the real read also re-stamps the token, which is what lets
    a previously-refused write recover (pt_26c703c5, the 'stable file,
    3× refused' loop). Fail-open everywhere, mirroring
    ``check_write_freshness``: no token / unresolvable / vanished → False.
    """
    key = _conv_key(task)
    if not key or not isinstance(fn_args, dict):
        return False
    for rp in _covered_read_paths(fn_args):
        ap = _resolve_abs(project_path, task.get('convId'), rp)
        if not ap or not os.path.isfile(ap):
            continue
        if write_freshness.is_stale(key, ap):
            return True
    return False


def _read_result_ok(tool_content) -> bool:
    """True when a read_files/inspect_image result carries real content.

    Dict results (image/binary descriptors) count as success. String results
    fail only on the standard error prefix — a batch read with a per-file
    error mid-string still counts (same imprecision the read gate accepts
    for its satisfied-set; the stale-token direction stays safe).
    """
    if isinstance(tool_content, dict):
        return True
    if not isinstance(tool_content, str) or not tool_content:
        return False
    return not tool_content.lstrip().startswith(('Error:', 'ERROR:'))


def record_read_paths(task: dict, fn_args: dict, project_path: str | None,
                      tool_content) -> int:
    """Record freshness tokens for the paths a successful read just surfaced.

    Called by the project-tool handler AFTER read_files/inspect_image
    execution. Imprecision is fail-safe: recording a token for a path whose
    individual read failed can only ever ALLOW a later write (never refuse
    a good one), and the read-before-edit gate independently owns the
    "never actually saw it" axis. Returns the number of tokens recorded.
    """
    if not isinstance(fn_args, dict) or not _read_result_ok(tool_content):
        return 0
    key = _conv_key(task)
    if not key:
        return 0
    recorded = 0
    for rp in _covered_read_paths(fn_args):
        ap = _resolve_abs(project_path, task.get('convId'), rp)
        if not ap or not os.path.isfile(ap):
            continue
        write_freshness.record(key, ap)
        recorded += 1
    return recorded


__all__ = [
    'check_write_freshness', 'partition_stale_edits', 'record_read_paths',
    'cached_read_is_stale', 'FILE_READ_TOOLS',
]
