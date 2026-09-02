"""Desktop Agent — project (share-root) command implementations.

Remote-project contract (docs/modules/remote_execution.md):

* **Path validation lives on the agent** (constraint ⑤): commands address
  files as ``root`` (a name from the agent's own declared ``share_roots``)
  + a root-RELATIVE path. The realpath of the resolved target must stay
  inside the root — ``..``, absolute paths, sibling-prefix attacks and
  symlink escapes are all refused. The server-side ``abs_path_guard``
  never applies to remote paths.
* **Snapshot-before-write** (constraint ③): every write/apply_diff to an
  existing file first copies it to
  ``<root>/.tofu/file-history/<md5(abspath)>/<epoch_ns>`` so a bad agent
  edit can be rolled back by hand.
* **Freshness gate** (constraint ③): a write to an EXISTING file requires
  a read token from this agent session, and is refused when the file
  changed on disk since that read (external IDE/user edit). A fresh
  ``project_read_files`` re-arms the token; the agent's own successful
  write re-arms it too.

Read-side listings/searches reuse ``lib/project_mod`` (same codebase, so
the ignore rules — ``IGNORE_DIRS`` incl. ``.tofu`` — are import-level
shared, never re-implemented).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import time
from datetime import datetime

from lib.log import get_logger
logger = get_logger(__name__)

# Freshness tokens: abspath -> {'mtime_ns', 'size'} for files this agent
# session has read (in-memory; an agent restart simply requires a re-read,
# which is the safe direction).
_freshness: dict = {}

_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
_BINARY_EXTS = _IMAGE_EXTS | {'.pdf'}
_MAX_READ_CHARS = 400_000
_MAX_BINARY_BYTES = 8_000_000


class ProjectError(Exception):
    """Refusal that becomes ``{'error': ...}`` on the wire (honest, model-visible)."""


# ── Roots & path validation (constraint ⑤) ─────────────────────────

def _declared_roots():
    """The agent's OWN declared share roots (config ``share_roots``)."""
    from lib.desktop_agent.config import load_config
    roots = []
    for r in (load_config().get('share_roots') or []):
        if isinstance(r, dict) and r.get('path'):
            roots.append({
                'name': str(r.get('name') or ''),
                'path': os.path.realpath(os.path.expanduser(str(r['path']))),
            })
    return roots


def _is_within(root_real, target_real, case_insensitive=False):
    """True when target_real is root_real itself or lives beneath it."""
    if case_insensitive:
        root_real, target_real = root_real.lower(), target_real.lower()
    try:
        return os.path.commonpath((root_real, target_real)) == root_real
    except ValueError as _e:
        logger.debug('is within: unparseable (%s)', _e)
        return False  # different drives (Windows)


def _resolve(root_name, rel_path, roots=None):
    """Resolve (root name, root-relative path) → (root_real, abspath).

    Every escape vector is refused with an honest error: unknown root,
    absolute path, ``..`` climb, sibling-prefix, symlink pointing out.
    """
    roots = _declared_roots() if roots is None else roots
    if not roots:
        raise ProjectError(
            'no share_roots declared in the agent config — '
            'project commands are disabled on this machine')
    match = [r for r in roots if r['name'] == root_name]
    if not match:
        names = ', '.join(r['name'] or r['path'] for r in roots)
        raise ProjectError(
            f'unknown share root {root_name!r} (declared: {names})')
    root_real = match[0]['path']
    rel = (rel_path or '').strip() or '.'
    if os.path.isabs(rel) or (len(rel) > 1 and rel[1] == ':'):
        raise ProjectError(
            f'path must be root-relative, got absolute: {rel!r}')
    target = os.path.realpath(os.path.join(root_real, rel))
    if not _is_within(root_real, target):
        raise ProjectError(
            f'path escapes share root {root_name!r}: {rel!r}')
    return root_real, target


# ── Freshness gate + snapshots (constraint ③) ──────────────────────

def _stamp_read(abspath):
    try:
        st = os.stat(abspath)
    except OSError as e:
        logger.debug('[Project] freshness stamp skipped for %s: %s', abspath, e)
        return
    _freshness[abspath] = {'mtime_ns': st.st_mtime_ns, 'size': st.st_size}


def _check_write_allowed(abspath):
    """freshness 门 + read-before-edit。已存在文件需要有效令牌;新文件放行。"""
    if not os.path.exists(abspath):
        return  # creating a new file needs no token
    tok = _freshness.get(abspath)
    if tok is None:
        raise ProjectError(
            'read-before-write: read the file with project_read_files first '
            '(this agent session has no record of it)')
    st = os.stat(abspath)
    if st.st_mtime_ns != tok['mtime_ns'] or st.st_size != tok['size']:
        raise ProjectError(
            'file changed on disk since last read — re-read it with '
            'project_read_files to re-arm the freshness token')


def _snapshot(root_real, abspath):
    """Copy an existing file into the root's file-history before mutating it."""
    if not os.path.isfile(abspath):
        return None
    digest = hashlib.md5(abspath.encode('utf-8')).hexdigest()
    dest_dir = os.path.join(root_real, '.tofu', 'file-history', digest)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, str(time.time_ns()))
    shutil.copy2(abspath, dest)
    logger.info('[Project] snapshot %s → %s', abspath, dest)
    return dest


def _atomic_write(abspath, content):
    os.makedirs(os.path.dirname(abspath), exist_ok=True)
    tmp = f'{abspath}.tofu-tmp-{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(content)
    os.replace(tmp, abspath)


def _guarded(fn, params):
    try:
        return fn(params)
    except ProjectError as e:
        logger.debug('guarded: ProjectError (%s)', e)
        return {'error': str(e)}


# ── Commands (wire types are the function names' project_* keys) ────

def cmd_project_list_dir(params):
    def _go(p):
        _, target = _resolve(p.get('root', ''), p.get('path') or '.')
        if not os.path.isdir(target):
            raise ProjectError(f'not a directory: {p.get("path")!r}')
        from lib.project_mod.read_tools import _scan_directory_entries
        try:
            rows, truncated, scanned = _scan_directory_entries(
                target,
                show_hidden=bool(p.get('show_hidden')),
                include_directory_stat=not bool(p.get('shell_compatible')),
                respect_project_ignores=not bool(
                    p.get('shell_compatible')),
                include_non_regular=bool(p.get('shell_compatible')),
            )
        except (PermissionError, OSError) as exc:
            raise ProjectError(
                f'unable to list directory {p.get("path")!r}: {exc}') from exc
        entries = []
        for row in rows:
            modified = row.get('modified')
            entries.append({
                'name': row['name'],
                'type': (
                    'dir' if row['is_dir'] else
                    'file' if row['is_file'] else
                    'symlink' if row['is_symlink'] else 'special'),
                'size': row.get('size'),
                'modified': (
                    datetime.fromtimestamp(modified).isoformat(
                        timespec='seconds')
                    if modified is not None else None),
            })
        return {
            'path': p.get('path') or '.',
            'entries': entries,
            'truncated': truncated,
            'scanned': scanned,
        }
    return _guarded(_go, params)


def cmd_project_read_files(params):
    def _go(p):
        _, target = _resolve(p.get('root', ''), p.get('path', ''))
        if not os.path.isfile(target):
            raise ProjectError(f'not a file: {p.get("path")!r}')
        ext = os.path.splitext(target)[1].lower()
        if ext in _BINARY_EXTS:
            with open(target, 'rb') as f:
                data = f.read(_MAX_BINARY_BYTES + 1)
            _stamp_read(target)
            return {
                'path': p.get('path'),
                'media': ext,
                'bytes': os.path.getsize(target),
                'truncated': len(data) > _MAX_BINARY_BYTES,
                'base64': base64.b64encode(data[:_MAX_BINARY_BYTES]).decode('ascii'),
            }
        with open(target, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read(_MAX_READ_CHARS + 1)
        truncated = len(content) > _MAX_READ_CHARS
        _stamp_read(target)
        return {
            'path': p.get('path'),
            'content': content[:_MAX_READ_CHARS],
            'truncated': truncated,
            'size': os.path.getsize(target),
        }
    return _guarded(_go, params)


def cmd_project_write_file(params):
    def _go(p):
        root_real, target = _resolve(p.get('root', ''), p.get('path', ''))
        content = p.get('content')
        if not isinstance(content, str):
            raise ProjectError('content must be a string')
        _check_write_allowed(target)
        snap = _snapshot(root_real, target)
        _atomic_write(target, content)
        _stamp_read(target)  # our own write re-arms the token
        out = {'path': p.get('path'),
               'bytes': len(content.encode('utf-8'))}
        if snap:
            out['snapshot'] = snap
        return out
    return _guarded(_go, params)


def cmd_project_apply_diff(params):
    def _go(p):
        root_real, target = _resolve(p.get('root', ''), p.get('path', ''))
        search = p.get('search')
        replace = p.get('replace', '')
        if not isinstance(search, str) or not search:
            raise ProjectError('search must be a non-empty string')
        if not isinstance(replace, str):
            raise ProjectError('replace must be a string')
        if not os.path.isfile(target):
            raise ProjectError('apply_diff edits an existing file — '
                               f'not found: {p.get("path")!r}')
        _check_write_allowed(target)
        with open(target, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
        n = text.count(search)
        if n == 0:
            raise ProjectError('search text not found')
        replace_all = bool(p.get('replace_all'))
        if n > 1 and not replace_all:
            raise ProjectError(
                f'search text matches {n} locations — narrow it or '
                'set replace_all=true')
        snap = _snapshot(root_real, target)
        new = text.replace(search, replace) if replace_all \
            else text.replace(search, replace, 1)
        _atomic_write(target, new)
        _stamp_read(target)
        out = {'path': p.get('path'),
               'replacements': n if replace_all else 1}
        if snap:
            out['snapshot'] = snap
        return out
    return _guarded(_go, params)


def cmd_project_edit_file(params):
    """Apply the unified mixed edit batch inside a declared remote root."""
    def _go(p):
        edits = p.get('edits')
        if not isinstance(edits, list) or not edits:
            raise ProjectError('edits must be a non-empty array')
        edits = edits[:30]
        lines = []
        ok_count = 0
        fail_count = 0
        for i, edit in enumerate(edits, 1):
            try:
                if not isinstance(edit, dict):
                    raise ProjectError('edit must be an object')
                operation = edit.get('operation')
                anchor = edit.get('anchor')
                content = edit.get('content')
                if operation not in ('insert_after', 'insert_before', 'replace'):
                    raise ProjectError(f'invalid operation: {operation!r}')
                if not isinstance(anchor, str) or not anchor:
                    raise ProjectError('anchor must be a non-empty string')
                if not isinstance(content, str):
                    raise ProjectError('content must be a string')
                root_real, target = _resolve(
                    p.get('root', ''), edit.get('path', ''))
                if not os.path.isfile(target):
                    raise ProjectError(
                        f'edit_file edits an existing file — not found: '
                        f'{edit.get("path")!r}')
                _check_write_allowed(target)
                # Parity with the server-side tool_edit_file wrap gate
                # (TOFU_EDIT_WRAP_GATE=0 disables both): a replace whose
                # content keeps the anchor verbatim at a boundary is a pure
                # insertion — refuse it pre-execution so the model re-issues
                # with the insert vocabulary instead of learning nothing.
                if (operation == 'replace' and not edit.get('replace_all')
                        and os.environ.get('TOFU_EDIT_WRAP_GATE', '1') != '0'):
                    from lib.project_mod.write_tools._ops import (
                        _pure_wrap_insert)
                    wrap = _pure_wrap_insert(anchor, content)
                    if wrap:
                        op_name, trimmed = wrap
                        raise ProjectError(
                            f'pure insertion rejected — re-issue as '
                            f"operation='{op_name}' with ONLY the new text "
                            f'({len(trimmed)} chars — do not repeat the '
                            f'anchor)')
                with open(target, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
                matches = text.count(anchor)
                # Match the server backend: ignore replace_all for a unique
                # insert, while keeping multi-site insertion unavailable.
                replace_all = (operation == 'replace'
                               and bool(edit.get('replace_all')))
                if matches == 0:
                    raise ProjectError('anchor text not found')
                if matches > 1 and not replace_all:
                    raise ProjectError(
                        f'anchor text matches {matches} locations — narrow it')

                if operation == 'replace':
                    new_text = (text.replace(anchor, content) if replace_all
                                else text.replace(anchor, content, 1))
                    # Parity with the server-side .py syntax guard
                    # (TOFU_EDIT_SYNTAX_GUARD=0 disables both).
                    from lib.project_mod.write_tools._ops import (
                        _py_syntax_guard)
                    _warn = _py_syntax_guard(
                        edit.get('path', ''), text, new_text)
                    extra = f' {_warn}' if _warn else ''
                else:
                    anchor_idx = text.index(anchor)
                    # Parity with the server-side boundary-echo auto-repair
                    # (TOFU_EDIT_ECHO_REPAIR=0 disables both): anchor/neighbour
                    # context echoed inside content is stripped when provably
                    # safe, and provably-contentless echo edits fail here.
                    from lib.project_mod.write_tools._ops import (
                        _finalize_insertion)
                    fin = _finalize_insertion(
                        text, anchor_idx, anchor, content,
                        'before' if operation == 'insert_before' else 'after',
                        edit.get('path', ''))
                    if not fin['ok']:
                        raise ProjectError(fin['error'])
                    new_text = fin['new_content']
                    extra = ''.join(f' [{n}]' for n in fin['notes'])
                    if fin['warning']:
                        extra += ' ' + fin['warning']

                _snapshot(root_real, target)
                _atomic_write(target, new_text)
                _stamp_read(target)
                ok_count += 1
                lines.append(
                    f'[{i}] OK {edit.get("path")} [{operation}]{extra}')
            except (ProjectError, OSError, UnicodeError) as exc:
                fail_count += 1
                path = edit.get('path', '?') if isinstance(edit, dict) else '?'
                lines.append(f'[{i}] FAIL {path}: {exc}')

        header = f'Applied {ok_count}/{ok_count + fail_count} edits'
        if fail_count:
            header += f' ({fail_count} failed)'
        return header + '\n' + '\n'.join(lines)
    return _guarded(_go, params)


def cmd_project_grep_search(params):
    def _go(p):
        pattern = p.get('pattern', '')
        if not isinstance(pattern, str) or not pattern.strip():
            raise ProjectError('pattern must be a non-empty string')
        _, target = _resolve(p.get('root', ''), p.get('path') or '.')
        from lib.project_mod.read_tools import tool_grep
        return {'matches': tool_grep(
            target, pattern,
            include=p.get('include'),
            context_lines=p.get('context_lines'),
            max_results=p.get('max_results'),
        )}
    return _guarded(_go, params)


def cmd_project_find_files(params):
    def _go(p):
        root_real, target = _resolve(
            p.get('root', ''), p.get('path') or '.')
        from lib.project_mod.read_tools import tool_find_files
        shell_output = bool(p.get('shell_output'))
        if shell_output and not os.path.isdir(target):
            raise ProjectError(f'not a directory: {p.get("path")!r}')
        return {'files': tool_find_files(
            root_real if shell_output else target,
            p.get('pattern') or '*',
            rel_path=(p.get('path') or '.') if shell_output else None,
            max_results=p.get('max_results'),
            case_sensitive=bool(p.get('case_sensitive')),
            shell_output=shell_output,
            respect_project_ignores=bool(
                p.get('respect_project_ignores', True)),
        )}
    return _guarded(_go, params)


def _check_delete_targets_within(command, root_real):
    """Refuse delete commands whose absolute/~/env target escapes the root.

    ``command_analysis._is_catastrophic_delete``'s workspace-containment
    rule only engages for server-side restricted principals; on the agent
    EVERY delete of an absolute/~ target must stay inside the share root —
    this is what stops the ``rm -rf ~`` class (constraint ④, P2).
    Relative targets stay inside the already-confined cwd and pass.
    """
    from lib.project_mod.command_analysis import (
        _DELETE_COMMANDS,
        _split_pipeline,
        _unwrap_command_parts,
    )
    for seg in _split_pipeline(command):
        seg = seg.strip()
        if not seg:
            continue
        while re.match(r'^\w+=\S*\s', seg):
            seg = re.sub(r'^\w+=\S*\s+', '', seg, count=1)
        # See through a leading sudo/doas — ``sudo rm -rf <outside>`` must be
        # judged by the rm, not skipped because the first word is ``sudo``.
        parts = _unwrap_command_parts(seg.split())
        if not parts:
            continue
        if parts[0].split('/')[-1] not in _DELETE_COMMANDS:
            continue
        for arg in parts[1:]:
            if arg.startswith('-'):
                continue
            if not (arg.startswith('/') or arg.startswith('~')
                    or arg.startswith('$')):
                continue  # relative → stays inside the confined cwd
            cleaned = arg.rstrip('/*')
            expanded = os.path.expanduser(os.path.expandvars(cleaned))
            if expanded.startswith('$') or not expanded:
                raise ProjectError(
                    f'command blocked: unresolvable delete target {arg!r}')
            if not expanded.startswith('/') \
                    and not (len(expanded) > 1 and expanded[1] == ':'):
                continue
            tgt_real = os.path.realpath(expanded)
            if not _is_within(root_real, tgt_real):
                raise ProjectError(
                    f'command blocked: delete target outside share root: {arg!r}')


def _validate_project_run(params):
    """Shared validation for sync + streamed project_run_command.

    Returns ``{'command', 'cwd', 'timeout'}`` confined to the share root;
    raises ProjectError on any refusal.
    """
    root_real, target = _resolve(params.get('root', ''),
                                 params.get('workdir') or '.')
    if not os.path.isdir(target):
        raise ProjectError(
            f'workdir is not a directory: {params.get("workdir")!r}')
    command = params.get('command', '')
    if not isinstance(command, str) or not command.strip():
        raise ProjectError('command must be a non-empty string')
    from lib.project_mod.command_analysis import (
        _is_catastrophic_delete,
        _is_dangerous_command,
    )
    if _is_dangerous_command(command):
        raise ProjectError('command blocked by dangerous-pattern guard')
    bad = _is_catastrophic_delete(command, cwd=target)
    if bad:
        raise ProjectError(
            f'command blocked: catastrophic delete target {bad!r}')
    _check_delete_targets_within(command, root_real)
    try:
        timeout = float(params.get('timeout', 300))
    except (TypeError, ValueError) as _e:
        logger.debug('validate project run: unexpected type/unparseable (%s)', _e)
        timeout = 300.0
    timeout = min(max(timeout, 1.0), 3600.0)
    return {
        'command': command,
        'cwd': target,
        'root': root_real,
        'timeout': timeout,
    }


def _invalidate_project_index_after_command(spec):
    """Keep indexed reads honest after an opaque shell mutation."""
    from lib.project_mod.command_analysis import _is_destructive_command
    if not _is_destructive_command(spec['command']):
        return
    from lib.project_mod import tree_index
    tree_index.invalidate(spec['root'])


def cmd_project_run_command(params):
    """Sync (blocking) fallback — the poll loop uses start_project_run."""
    def _go(p):
        spec = _validate_project_run(p)
        from lib.desktop_agent._exec import cmd_run_local
        try:
            return cmd_run_local(spec)
        finally:
            _invalidate_project_index_after_command(spec)
    return _guarded(_go, params)


def start_project_run(cmd_id, params, on_chunk, on_exit):
    """Validate + start a STREAMED project_run_command (RWA P2).

    Returns an error string on validation refusal, else None — the
    process runs on background threads; ``on_chunk(stream, data)`` streams
    output and ``on_exit(outcome)`` delivers the final capped result.
    """
    try:
        spec = _validate_project_run(params)
    except ProjectError as e:
        logger.debug('start project run: ProjectError (%s)', e)
        return str(e)
    from lib.desktop_agent._exec import start_streamed_command
    def _finish(outcome):
        try:
            _invalidate_project_index_after_command(spec)
        finally:
            on_exit(outcome)

    start_streamed_command(spec['command'], spec['cwd'], spec['timeout'],
                           on_chunk, _finish)
    return None
