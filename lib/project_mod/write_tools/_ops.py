"""lib/project_mod/write_tools/_ops.py — the write/edit OPERATIONS.

Extracted verbatim from the former flat ``write_tools.py``: the public tool
entry points (``tool_write_file`` / ``save_uploaded_file`` / ``tool_apply_diff``
/ ``tool_apply_diffs`` / ``tool_insert_content`` / ``tool_insert_contents``) plus
their single-edit cores (``_apply_one_diff`` / ``_insert_one``). Every operation
resolves + attributes through the ``_paths`` core and matches through the
``_text`` helpers.
"""

import os
import tempfile
import time
import ast
from difflib import SequenceMatcher

from lib.log import get_logger
from lib.project_mod.modifications import _record_modification
from lib.project_mod.scanner import _fmt_size

from ._paths import (
    _enforce_not_readonly,
    _mod_attribution,
    _resolve_write_path,
    _should_record_modification,
)
from ._text import (
    _decode_unicode_escapes,
    _describe_duplicate_matches,
    _find_closest_match,
    _touch_for_vscode,
)

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════
#  Atomic write primitive
# ═══════════════════════════════════════════════════════
# Every write path below funnels through _atomic_write_bytes: the new bytes
# land in a temp file in the SAME directory (same filesystem → os.replace is
# atomic), are fsync'd, then renamed over the target in ONE atomic step. A
# concurrent reader/importer therefore always sees the complete OLD file or
# the complete NEW one — never a half-written file. On a shared checkout
# (multiple conversations writing one tree), a half-written .py is an
# ImportError/IndentationError window for every OTHER conversation.
#
# Trade-off, noted: os.replace gives the file a NEW inode, so a hard-linked
# target would be un-linked rather than written through (vanishingly rare in
# practice); and the temp file needs the DIRECTORY to be writable, not just
# the file. Both are the price of never publishing a partial write.

def _new_file_mode():
    """Permission bits for a freshly created file: 0o666 masked by the umask.

    Reading the umask is destructive, so flip-and-restore. The race window
    (a concurrent open() seeing umask 0) is theoretical here — file creation
    in this module always applies an explicit mode right after.
    """
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def _atomic_write_bytes(target, data):
    """Write *data* (bytes) to *target* atomically (tmp file + os.replace).

    Preserves the target's permission bits when it already exists; new files
    get 0o666 & ~umask (matching plain open(..., 'w')). A symlinked target is
    written THROUGH to its referent — os.replace would otherwise replace the
    link itself, changing the historic write-through behaviour.

    Raises OSError on failure; the temp file is always cleaned up, so a
    failed write leaves the OLD content fully intact.
    """
    import stat as _stat
    import tempfile

    phys = os.path.realpath(target) if os.path.islink(target) else target
    try:
        mode = _stat.S_IMODE(os.stat(phys).st_mode)
    except OSError as _e:
        logger.debug('atomic write bytes: unreadable (%s)', _e)
        mode = None  # new file (or unreadable stat) → umask default below

    parent = os.path.dirname(phys) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.tofu_atomic_', dir=parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode if mode is not None else _new_file_mode())
        os.replace(tmp, phys)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as _e:
            logger.debug('atomic write bytes: unreadable (%s)', _e)
            pass
        raise


def _atomic_write_text(target, text):
    """Text flavour of _atomic_write_bytes (UTF-8, newline-verbatim)."""
    _atomic_write_bytes(target, text.encode('utf-8'))


def _record_write_freshness(conv_id, target, data=None):
    """Record the post-write freshness token for this conversation.

    Shared-HEAD overwrite guard (lib/write_freshness.py): after this write,
    ANY conversation holding an older token for the path is refused until it
    re-reads. When the just-written bytes are in hand they are hashed directly
    (no post-write re-read); otherwise the store re-fingerprints the file.
    Best-effort — must never break a successful write.
    """
    try:
        if data is None:
            from lib.write_freshness import record as _wf_record
            _wf_record(conv_id or '', target)
        else:
            from lib.write_freshness import record_written as _wf_record
            _wf_record(conv_id or '', target, data)
    except Exception as e:
        logger.debug('[WriteFreshness] record failed for %s: %s', target, e)
    # Keep the grep/find tree index exact for agent-authored writes — this is
    # the same choke point every project write path funnels through.
    try:
        from lib.project_mod import tree_index as _tree_index
        _tree_index.note_write(target)
    except Exception as e:
        logger.debug('[TreeIndex] note_write failed for %s: %s', target, e)


# ═══════════════════════════════════════════════════════
#  Failed-write payload salvage + path-shape guards
# ═══════════════════════════════════════════════════════
# 2026-08-05 incident: a model emitted write_file WITHOUT the required
# ``path`` argument. The empty path flowed through _safe_path and resolved to
# the project ROOT DIRECTORY itself; the atomic write staged its temp file in
# the root's PARENT directory and died with a bare EISDIR ("Is a directory").
# The model's whole payload was thrown away, and the error named neither the
# mistake nor a recovery — a blind retry just re-failed the same way.
#
# Two mechanisms fix the class, not the instance:
#   1. PATH-SHAPE GUARDS in tool_write_file refuse an empty/missing path and
#      any path resolving to an existing directory BEFORE any file I/O — the
#      .tofu_atomic_* temp file never even gets created next to a directory
#      target, so EISDIR can no longer occur here at all.
#   2. SALVAGE: any failed write with a non-empty payload stages the content
#      to <tmp>/tofu_write_salvage/ and the error message carries the staged
#      path + an exact run_command mv recovery, so the model recovers WITHOUT
#      re-emitting (possibly tens of KB of) tokens.
#
# Salvage lives in the OS temp dir, NOT inside the workspace: the workspace
# filesystem may itself be the failure cause (FUSE stalls), and the staged
# copy must stay reachable exactly then. Owner-only perms — staged content
# can hold secrets. /tmp vanishing on reboot is fine: the recovery window is
# the next tool rounds, not days.
#
# CONTRACT: errors produced here are surfaced as "Write failed: <err>" by
# _exec_write_file. That 'Write failed' PREFIX is load-bearing —
# lib/tasks_pkg/handlers/_read_gate.py::_result_indicates_success and
# lib/tools/meta.py::_build_write_file pattern-match it. The enrichment below
# always goes AFTER the cause sentence, never before.

_SALVAGE_MAX_BYTES = 8 * 1024 * 1024   # LLM text payloads are ≪1 MB; cap abuse
_SALVAGE_TTL_SEC = 24 * 3600           # recovery window is the next few rounds
_SALVAGE_MAX_FILES = 100               # bound a tight failure loop


def _salvage_root():
    """Directory holding staged payloads of failed writes (owner-only)."""
    return os.path.join(tempfile.gettempdir(), 'tofu_write_salvage')


def _sweep_salvage_dir(root):
    """Bound the salvage dir by TTL and file count. Never raises."""
    try:
        now = time.time()
        survivors = []
        for entry in os.scandir(root):
            try:
                if not entry.is_file():
                    continue
                mtime = entry.stat().st_mtime
            except OSError as e:
                logger.debug('[WriteTools] salvage sweep stat failed for %s: %s',
                             entry.path, e)
                continue
            if now - mtime > _SALVAGE_TTL_SEC:
                try:
                    os.unlink(entry.path)
                except OSError as e:
                    logger.debug('[WriteTools] salvage sweep unlink failed for %s: %s',
                                 entry.path, e)
            else:
                survivors.append((mtime, entry.path))
        if len(survivors) > _SALVAGE_MAX_FILES:
            survivors.sort()
            for _mtime, path in survivors[:len(survivors) - _SALVAGE_MAX_FILES]:
                try:
                    os.unlink(path)
                except OSError as e:
                    logger.debug('[WriteTools] salvage sweep unlink failed for %s: %s',
                                 path, e)
    except OSError as e:
        logger.debug('[WriteTools] salvage sweep failed for %s: %s', root, e)


def _salvage_failed_content(content, rel_path):
    """Stage the payload of a FAILED write; return the staged path or ''.

    Never raises — the original write error is what the caller must report;
    the salvage is a best-effort bonus on top of it.
    """
    if not isinstance(content, str) or not content:
        return ''
    data = content.encode('utf-8', errors='replace')
    if len(data) > _SALVAGE_MAX_BYTES:
        logger.warning('[WriteTools] payload too large to salvage (%d bytes) for %s',
                       len(data), rel_path)
        return ''
    try:
        root = _salvage_root()
        os.makedirs(root, exist_ok=True)
        os.chmod(root, 0o700)
        hint = ''.join(c if (c.isalnum() or c in '._-') else '_'
                       for c in (os.path.basename(rel_path or '') or 'payload'))[:30]
        ts = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
        fd, staged = tempfile.mkstemp(prefix=f'{ts}-{hint}-', suffix='.tmp', dir=root)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            os.chmod(staged, 0o600)
        except BaseException:
            try:
                os.unlink(staged)
            except OSError as e:
                logger.debug('[WriteTools] salvage tmp cleanup failed for %s: %s',
                             staged, e)
            raise
        logger.info('[WriteTools] salvaged failed-write payload (%d bytes) → %s',
                    len(data), staged)
        _sweep_salvage_dir(root)
        return staged
    except Exception as e:
        logger.debug('[WriteTools] salvage staging failed for %s: %s', rel_path, e)
        return ''


def _salvage_note(content, rel_path):
    """The recovery sentence appended to a write_file error ('' when no salvage)."""
    staged = _salvage_failed_content(content, rel_path)
    if not staged:
        return ''
    size = len(content.encode('utf-8', errors='replace'))
    return (f" Your content ({size} bytes) was NOT lost — it was staged to "
            f"'{staged}'. Recover it WITHOUT re-generating: "
            f"run_command(command=\"mv '{staged}' '<intended-target-file>'\"). "
            f"Or retry write_file with a corrected path and the full content.")


# ═══════════════════════════════════════════════════════
#  write_file
# ═══════════════════════════════════════════════════════

def tool_write_file(base, rel_path, content, description='', conv_id=None, task_id=None):
    """Write full content to a file. Creates parent dirs if needed.

    Accepts:
      * project-relative paths (sandboxed to *base*), and
      * absolute paths that resolve under a registered workspace root —
        including roots auto-registered by an earlier absolute-path write.
    """
    # Path-shape guards (see the salvage section above): refuse BEFORE any
    # file I/O. A missing/empty path used to flow through _safe_path and
    # resolve to the project ROOT DIRECTORY itself, dying EISDIR inside the
    # atomic write with the payload lost and no actionable error.
    if not isinstance(rel_path, str) or not rel_path.strip():
        logger.debug('[Tools] write_file refused: path missing/empty (base=%s)', base)
        return {'ok': False, 'action': 'write_file', 'path': rel_path,
                'error': ("path is required (it was missing or empty) — without a "
                          "file path, write_file would target the project root "
                          f"directory itself ({base}), which cannot work. Pass a "
                          "FILE path, e.g. path='docs/README.md'."
                          + _salvage_note(content, rel_path))}
    try:
        target = _resolve_write_path(base, rel_path, conv_id=conv_id)
    except ValueError as e:
        logger.debug('[Tools] write_file path rejected %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e) + _salvage_note(content, rel_path),
                'action': 'write_file', 'path': rel_path}

    if os.path.isdir(target):
        logger.debug('[Tools] write_file refused: %s resolves to directory %s',
                     rel_path, target)
        return {'ok': False, 'action': 'write_file', 'path': rel_path,
                'error': (f"path '{rel_path}' resolves to a directory ('{target}') — "
                          "write_file creates/overwrites FILES; choose a file path "
                          f"inside it, e.g. '{rel_path.rstrip('/')}/<filename>'."
                          + _salvage_note(content, rel_path))}

    existed = os.path.isfile(target)
    old_lines = 0
    old_content = None
    if existed:
        try:
            with open(target, errors='replace') as f:
                old_content = f.read()
                old_lines = old_content.count('\n') + 1
        except Exception as e:
            logger.debug('[Tools] write_file old content read failed for %s: %s', rel_path, e, exc_info=True)

    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:
            logger.warning('[Tools] makedirs failed for parent of %s: %s', rel_path, e, exc_info=True)
            return {'ok': False, 'action': 'write_file', 'path': rel_path,
                    'error': (f'Cannot create directory: {e}'
                              + _salvage_note(content, rel_path))}

    original_content = old_content if existed else None

    try:
        _atomic_write_text(target, content)
        _touch_for_vscode(target)
        _record_write_freshness(conv_id, target, content.encode('utf-8'))
        new_lines = content.count('\n') + 1
        sz = len(content.encode('utf-8'))

        if _should_record_modification(target, conv_id=conv_id):
            _mod_base, _mod_rel = _mod_attribution(target, base, rel_path, conv_id=conv_id)
            _record_modification(_mod_base, 'write_file', _mod_rel, original_content,
                                 conv_id=conv_id, task_id=task_id)

        result = {
            'ok': True, 'action': 'write_file', 'path': rel_path,
            'created': not existed, 'bytesWritten': sz,
            'lines': new_lines, 'oldLines': old_lines if existed else None,
            'description': description,
        }
        _guard = _py_syntax_guard(rel_path, old_content, content)
        if _guard:
            result['syntaxWarning'] = _guard
            logger.warning('[SyntaxGuard] %s', _guard)
        logger.info('write_file: %s (%dL, %s) %s', rel_path, new_lines, _fmt_size(sz),
              '[created]' if not existed else '[updated from %dL]' % old_lines)
        return result
    except Exception as e:
        logger.error('[Tools] write_file failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'action': 'write_file', 'path': rel_path,
                'error': str(e) + _salvage_note(content, rel_path)}


# ═══════════════════════════════════════════════════════
#  save_uploaded_file — binary-safe drag-and-drop into a project folder
# ═══════════════════════════════════════════════════════
# Backs POST /api/v1/project/upload. Unlike tool_write_file (text `content`),
# this writes RAW BYTES so images / PDFs / archives dropped onto the folder
# browser land on disk intact. It deliberately does NOT auto-register a new
# workspace root the way an absolute-path agent write does: a UI file-drop
# into a directory the user has not attached is a mistake we want to surface,
# not silently expand the workspace. The destination must therefore already
# resolve INSIDE a registered root (any form: primary, extra, or a subdir of
# one). Read-only roots are refused, name collisions auto-rename (never
# clobber), and the write is recorded so it appears in the file-changes bar
# and is undoable exactly like an agent write.

def _dedupe_target(target):
    """Return *target*, or a ``name (n).ext`` sibling if it already exists.

    Preserves the extension and inserts a `` (n)`` counter before it, mirroring
    the OS "copy" convention. Bounded to avoid an unbounded loop on a pathologic
    directory; falls back to a timestamp suffix past the cap.
    """
    if not os.path.exists(target):
        return target
    root, ext = os.path.splitext(target)
    for n in range(1, 1000):
        candidate = f'{root} ({n}){ext}'
        if not os.path.exists(candidate):
            return candidate
    import time as _t
    return f'{root} ({int(_t.time() * 1000)}){ext}'


def save_uploaded_file(base, rel_path, data, description='', conv_id=None,
                       task_id=None, on_conflict='rename'):
    """Save raw *data* bytes to a project file dropped via the folder browser.

    Args:
        base: The active project root (absolute path).
        rel_path: Destination path — a project-relative path OR an absolute
            path that MUST resolve inside an already-registered workspace root.
        data: The file bytes.
        description: Optional note (unused by the model; kept for symmetry).
        conv_id / task_id: Attribution for the undo journal.
        on_conflict: ``'rename'`` (default — auto-suffix ``name (1).ext``) or
            ``'overwrite'`` (replace in place, recording the pre-image so undo
            restores it).

    Returns:
        dict: ``{ok, action, path, created, renamed, bytesWritten}`` on success,
        or ``{ok: False, error, ...}`` on rejection/failure. Never raises for
        the expected cases (read-only root, unregistered path, IO error).
    """
    if not isinstance(data, (bytes, bytearray)):
        return {'ok': False, 'error': 'save_uploaded_file expects bytes',
                'action': 'upload_file', 'path': rel_path}

    # Resolve WITHOUT the auto-register branch: a UI drop must target an
    # already-attached root. We reuse _resolve_write_path only for the
    # relative-path + read-only enforcement; for an absolute path we first
    # verify it lives under a registered root ourselves so a stray drop can't
    # invent a workspace.
    is_abs = bool(rel_path) and (rel_path.startswith('/') or rel_path.startswith('~'))
    if is_abs:
        abs_path = os.path.abspath(os.path.expanduser(rel_path))
        from lib.project_mod.config import _lock, _roots
        with _lock:
            roots_snapshot = [rs['path'] for rs in _roots.values()]
        inside_root = False
        for root_path in roots_snapshot:
            norm_root = os.path.abspath(root_path).rstrip(os.sep) or root_path
            if abs_path == norm_root or abs_path.startswith(norm_root + os.sep):
                inside_root = True
                break
        if not inside_root:
            return {'ok': False, 'action': 'upload_file', 'path': rel_path,
                    'error': ('Destination is not inside any attached workspace '
                              'folder. Add it as a project folder first, then drop.')}
        try:
            _enforce_not_readonly(abs_path, conv_id=conv_id)
        except ValueError as e:
            logger.debug('[Tools] upload rejected (readonly) %s: %s', rel_path, e)
            return {'ok': False, 'error': str(e), 'action': 'upload_file', 'path': rel_path}
        target = abs_path
    else:
        try:
            target = _resolve_write_path(base, rel_path, conv_id=conv_id)
        except ValueError as e:
            logger.debug('[Tools] upload path rejected %s: %s', rel_path, e, exc_info=True)
            return {'ok': False, 'error': str(e), 'action': 'upload_file', 'path': rel_path}

    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:
            logger.warning('[Tools] upload makedirs failed for parent of %s: %s', rel_path, e, exc_info=True)
            return {'ok': False, 'error': f'Cannot create directory: {e}',
                    'action': 'upload_file', 'path': rel_path}

    renamed = False
    original_content = None
    existed = os.path.isfile(target)
    if existed and on_conflict == 'rename':
        new_target = _dedupe_target(target)
        renamed = new_target != target
        target = new_target
        existed = os.path.isfile(target)
    if existed:  # overwrite path — capture pre-image so undo restores bytes
        try:
            with open(target, 'rb') as f:
                original_content = f.read()
        except Exception as e:
            logger.debug('[Tools] upload pre-image read failed for %s: %s', target, e)

    try:
        _atomic_write_bytes(target, bytes(data))
        _touch_for_vscode(target)
        _record_write_freshness(conv_id, target, bytes(data))
        sz = len(data)

        if _should_record_modification(target, conv_id=conv_id):
            _mod_base, _mod_rel = _mod_attribution(target, base, target if is_abs else rel_path, conv_id=conv_id)
            _record_modification(_mod_base, 'write_file', _mod_rel, original_content,
                                 conv_id=conv_id, task_id=task_id)

        logger.info('upload_file: %s (%s) %s', target, _fmt_size(sz),
                    '[created]' if not (existed and original_content is not None) else '[overwrote]')
        return {
            'ok': True, 'action': 'upload_file', 'path': target,
            'name': os.path.basename(target),
            'created': original_content is None,
            'renamed': renamed, 'bytesWritten': sz,
            'description': description,
        }
    except Exception as e:
        logger.error('[Tools] upload_file failed for %s: %s', target, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'upload_file', 'path': rel_path}


# ═══════════════════════════════════════════════════════
#  apply_diff / apply_diffs
# ═══════════════════════════════════════════════════════

def _compute_diff(content, rel_path, search, replace, replace_all=False):
    """Pure match/compute half of ``_apply_one_diff`` (no disk I/O).

    Returns ``{'ok': True, ...}`` carrying everything the commit step needs,
    or ``{'ok': False, ...}`` in the exact error shapes ``_apply_one_diff``
    produced. Kept byte-for-byte identical to the old in-function matching so
    batch edits can sequence many diffs against one in-memory buffer.
    """
    pre_edit_text = content

    _tw_replaced = False
    count = content.count(search)
    if count == 0:
        norm_content = content.replace('\r\n', '\n')
        norm_search = search.replace('\r\n', '\n')
        count = norm_content.count(norm_search)
        if count == 0:
            def _rstrip_lines(s):
                return '\n'.join(l.rstrip() for l in s.split('\n'))

            tw_content = _rstrip_lines(norm_content)
            tw_search = _rstrip_lines(norm_search)
            tw_count = tw_content.count(tw_search)

            if tw_count >= 1:
                if tw_count > 1 and not replace_all:
                    locs = _describe_duplicate_matches(tw_content, tw_search)
                    error_msg = (f'Search text matches {tw_count} locations (after trailing-whitespace '
                                 f'normalization). Make it more specific (add surrounding lines so it '
                                 f'matches exactly once), or set replace_all=true to replace all occurrences.')
                    if locs:
                        error_msg += f'\n\n{locs}'
                    return {'ok': False, 'action': 'apply_diff', 'path': rel_path,
                            'error': error_msg}
                tw_lines = tw_content.split('\n')
                search_lines = tw_search.split('\n')
                n_sl = len(search_lines)
                content_lines = norm_content.split('\n')

                matched_starts = []
                for i in range(len(tw_lines) - n_sl + 1):
                    if tw_lines[i:i + n_sl] == search_lines:
                        matched_starts.append(i)

                if matched_starts:
                    replace_norm = replace.replace('\r\n', '\n')
                    replace_lines = replace_norm.split('\n')
                    for start_idx in reversed(matched_starts):
                        content_lines[start_idx:start_idx + n_sl] = replace_lines
                        if not replace_all:
                            break
                    content = '\n'.join(content_lines)
                    search = norm_search
                    count = tw_count
                    _tw_replaced = True
                    logger.debug('apply_diff: trailing-WS normalized match in %s '
                                 '(%d locations)', rel_path, tw_count)
                else:
                    tw_count = 0

            # ── Tier 4: unicode-escape normalization ──
            # The model often emits a real glyph (⏰, em-dash …) where the file
            # holds the literal escape sequence (\u23f0, \u2014), or vice-versa.
            # Decode \uXXXX / \UXXXXXXXX / \xXX on BOTH sides for the comparison
            # only, then splice the model's verbatim replacement into the real
            # file lines.
            if tw_count == 0:
                esc_content_lines = [_decode_unicode_escapes(l).rstrip()
                                     for l in norm_content.split('\n')]
                esc_search_lines = [_decode_unicode_escapes(l).rstrip()
                                    for l in norm_search.split('\n')]
                n_el = len(esc_search_lines)
                esc_starts = [i for i in range(len(esc_content_lines) - n_el + 1)
                              if esc_content_lines[i:i + n_el] == esc_search_lines]
                if esc_starts:
                    if len(esc_starts) > 1 and not replace_all:
                        esc_content = '\n'.join(esc_content_lines)
                        esc_search = '\n'.join(esc_search_lines)
                        locs = _describe_duplicate_matches(esc_content, esc_search)
                        error_msg = (f'Search text matches {len(esc_starts)} locations (after unicode-escape '
                                     f'normalization). Make it more specific (add surrounding lines so it '
                                     f'matches exactly once), or set replace_all=true to replace all occurrences.')
                        if locs:
                            error_msg += f'\n\n{locs}'
                        return {'ok': False, 'action': 'apply_diff', 'path': rel_path,
                                'error': error_msg}
                    content_lines = norm_content.split('\n')
                    replace_lines = replace.replace('\r\n', '\n').split('\n')
                    for start_idx in reversed(esc_starts):
                        content_lines[start_idx:start_idx + n_el] = replace_lines
                        if not replace_all:
                            break
                    content = '\n'.join(content_lines)
                    search = norm_search
                    count = len(esc_starts)
                    _tw_replaced = True
                    logger.debug('apply_diff: unicode-escape normalized match in %s '
                                 '(%d locations)', rel_path, count)

            if tw_count == 0 and not _tw_replaced:
                hint = _find_closest_match(norm_content, norm_search)
                error_msg = (f'Search text not found in {rel_path}. '
                             f'File has {content.count(chr(10))+1} lines. '
                             f'Use read_files to verify the exact content first.')
                if hint:
                    error_msg += f'\n\nMost similar block (line {hint["line"]}, {hint["similarity"]:.0%} match):\n```\n{hint["text"]}\n```'
                return {
                    'ok': False, 'action': 'apply_diff', 'path': rel_path,
                    'error': error_msg,
                    'searchLen': len(search),
                }
        else:
            content = norm_content
            search = norm_search

    if count > 1 and not replace_all:
        locs = _describe_duplicate_matches(content, search)
        error_msg = (f'Search text matches {count} locations. Make it more specific (add surrounding '
                     f'lines so it matches exactly once), or set replace_all=true to replace all occurrences.')
        if locs:
            error_msg += f'\n\n{locs}'
        return {'ok': False, 'action': 'apply_diff', 'path': rel_path,
                'error': error_msg}

    if _tw_replaced:
        new_content = content
        _orig_line_count = norm_content.count('\n') + 1
    else:
        new_content = content.replace(search, replace) if replace_all else content.replace(search, replace, 1)
        _orig_line_count = content.count('\n') + 1

    reverse_patch = {'search': replace, 'replace': search}
    if replace_all and count > 1:
        reverse_patch['replace_all'] = True

    return {
        'ok': True,
        'new_content': new_content,
        'original_content': content,
        'pre_edit_text': pre_edit_text,
        'reverse_patch': reverse_patch,
        'search': search,
        'count': count,
        'replace_all': replace_all,
        'old_lines': _orig_line_count,
    }


def _diff_result_from_comp(rel_path, comp, description):
    """Build the success result dict + logging for one computed diff (no I/O)."""
    new_content = comp['new_content']
    new_lines = new_content.count('\n') + 1
    diff_lines = len(comp['search'].split('\n'))
    result = {
        'ok': True, 'action': 'apply_diff', 'path': rel_path,
        'linesChanged': diff_lines,
        'oldLines': comp['old_lines'], 'newLines': new_lines,
        'description': description,
    }
    if comp['replace_all'] and comp['count'] > 1:
        result['replacedCount'] = comp['count']
    _guard = _py_syntax_guard(rel_path, comp['pre_edit_text'], new_content)
    if _guard:
        result['syntaxWarning'] = _guard
        logger.warning('[SyntaxGuard] %s', _guard)
    logger.info('apply_diff: %s (%d lines changed, %dL → %dL%s)',
          rel_path, diff_lines, comp['old_lines'], new_lines,
          f', {comp["count"]} replacements' if (comp['replace_all'] and comp['count'] > 1) else '')
    return result


def _commit_diff(target, base, rel_path, comp, description='', conv_id=None, task_id=None):
    """Atomic write + freshness/advisory + undo for one computed diff."""
    try:
        _atomic_write_text(target, comp['new_content'])
        _touch_for_vscode(target)
        _record_write_freshness(conv_id, target, comp['new_content'].encode('utf-8'))
        if _should_record_modification(target, conv_id=conv_id):
            _mod_base, _mod_rel = _mod_attribution(target, base, rel_path, conv_id=conv_id)
            _record_modification(_mod_base, 'apply_diff', _mod_rel,
                                 original_content=comp['original_content'],
                                 reverse_patch=comp['reverse_patch'],
                                 conv_id=conv_id, task_id=task_id)
        return _diff_result_from_comp(rel_path, comp, description)
    except Exception as e:
        logger.error('[Tools] apply_diff write failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'apply_diff', 'path': rel_path}


def _apply_one_diff(base, rel_path, search, replace, description='', conv_id=None, replace_all=False, task_id=None):
    """Apply a single search-and-replace to a file.

    Accepts project-relative paths and absolute paths under registered roots.
    """
    try:
        target = _resolve_write_path(base, rel_path, conv_id=conv_id)
    except ValueError as e:
        logger.debug('[Tools] apply_diff path rejected %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'apply_diff', 'path': rel_path}

    if not os.path.isfile(target):
        return {'ok': False, 'error': f'File not found: {rel_path}',
                'action': 'apply_diff', 'path': rel_path}

    try:
        with open(target, errors='replace') as f:
            content = f.read()
    except Exception as e:
        logger.warning('[Tools] apply_diff read failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': f'Cannot read file: {e}',
                'action': 'apply_diff', 'path': rel_path}

    comp = _compute_diff(content, rel_path, search, replace, replace_all=replace_all)
    if not comp['ok']:
        return comp
    return _commit_diff(target, base, rel_path, comp, description=description,
                        conv_id=conv_id, task_id=task_id)


def tool_apply_diff(base, rel_path, search, replace, description='', conv_id=None, replace_all=False, task_id=None):
    """Apply a single search-and-replace edit (backward-compatible entry point)."""
    result = _apply_one_diff(base, rel_path, search, replace, description, conv_id, replace_all=replace_all, task_id=task_id)
    _log_legacy_edit_efficiency(search, replace, ok=result.get('ok'))
    return result


def _invalid_edit_entry_msg(i, edit, expected='{path, search, replace}'):
    """Build an actionable FAIL line for a batch edit that isn't an object.

    The common cause is a model emitting the whole ``edits`` array as one
    escaped JSON *string* (often with unescaped inner quotes, so it can't be
    auto-parsed) — the harness then wraps that string into a single-element
    list, and each element is a str, not a dict. Tell the model exactly that
    so it re-emits a real array of objects instead of retrying blind.
    """
    if isinstance(edit, str):
        return (f'[{i}] FAIL Invalid edit entry: got a string, expected an '
                f'object with {expected}. The "edits" array '
                f'must be real JSON objects, not a single stringified-JSON '
                f'blob — re-send each edit as its own object.')
    return (f'[{i}] FAIL Invalid edit entry: expected an object with '
            f'{expected}, got {type(edit).__name__}.')


def _pure_addition_stats(search, replace):
    """Return content-only efficiency stats for an additive replacement.

    No source text is logged. ``repeated_unchanged_chars`` is a conservative
    estimate of what the legacy search+replace shape needlessly repeats: a
    unified insertion can use the old search as its anchor and omit that
    second copy even before choosing a shorter unique anchor.
    """
    if not isinstance(search, str) or not isinstance(replace, str):
        return None
    if len(replace) <= len(search) or len(search) + len(replace) > 200_000:
        return None
    matcher = SequenceMatcher(a=search, b=replace, autojunk=False)
    inserted = 0
    saw_insert = False
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'insert':
            inserted += j2 - j1
            saw_insert = True
        elif tag != 'equal':
            return None
    if not saw_insert:
        return None
    return {
        'anchor_chars': len(search),
        'content_chars': inserted,
        'legacy_arg_chars': len(search) + len(replace),
        'repeated_unchanged_chars': len(search),
    }


def _pure_wrap_insert(anchor, content):
    """Detect a replace whose content keeps the anchor verbatim at a boundary.

    Such a replace is a pure insertion wearing replace clothes: the anchor
    survives unchanged, so the edit is mechanically expressible as
    ``insert_after`` / ``insert_before`` carrying ONLY the appended/prepended
    text. Rejecting it pre-execution teaches the operation vocabulary at the
    one moment the model can still act on it — a post-execution hint arrives
    after the tokens and the write are already spent.

    Returns ``(operation, trimmed_content)`` or ``None``. Middle-additive
    replaces (insertion INSIDE the anchor) are deliberately NOT matched:
    no insert operation can express them with the same anchor, so a
    rejection there would have no mechanical fix.
    """
    if not anchor or not content or len(content) <= len(anchor):
        return None
    if content.startswith(anchor):
        return 'insert_after', content[len(anchor):]
    if content.endswith(anchor):
        return 'insert_before', content[:-len(anchor)]
    return None


def _log_legacy_edit_efficiency(search, replace, *, ok):
    stats = _pure_addition_stats(search, replace)
    logger.info(
        '[EditEfficiency] surface=legacy_apply_diff ok=%s pure_additive=%s '
        'anchor_chars=%d content_chars=%d arg_chars=%d repeated_unchanged_chars=%d',
        bool(ok), bool(stats),
        stats['anchor_chars'] if stats else len(search or ''),
        stats['content_chars'] if stats else len(replace or ''),
        stats['legacy_arg_chars'] if stats else len(search or '') + len(replace or ''),
        stats['repeated_unchanged_chars'] if stats else 0,
    )


def _commit_edit_group(target, base, recs, conv_id, task_id):
    """One atomic write for a file's batch + per-edit undo records.

    ``recs`` is the list of in-memory-successful edits for this file, in
    original edit order; each has ``new_content``, ``original_content``,
    ``reverse_patch``, ``attribution_base`` and ``attribution_rel``. Returns
    an error string on write failure, else None.
    """
    final = recs[-1]['new_content']
    try:
        _atomic_write_text(target, final)
    except Exception as e:
        logger.error('[Tools] batch write failed for %s: %s', target, e, exc_info=True)
        return str(e)
    _touch_for_vscode(target)
    _record_write_freshness(conv_id, target, final.encode('utf-8'))
    for rec in recs:
        if _should_record_modification(target, conv_id=conv_id):
            _mod_base, _mod_rel = _mod_attribution(
                target, rec['attribution_base'], rec['attribution_rel'],
                conv_id=conv_id)
            _record_modification(_mod_base, 'apply_diff', _mod_rel,
                                 original_content=rec['original_content'],
                                 reverse_patch=rec['reverse_patch'],
                                 conv_id=conv_id, task_id=task_id)
    return None


def tool_apply_diffs(base_path, edits, conv_id=None, task_id=None):
    """Apply multiple search-and-replace edits in one batch.

    Edits are grouped by resolved target: each file is read once, its diffs
    are applied in memory sequentially (edit N still sees the result of
    1..N-1), and the final bytes are written once — one atomic write, one
    freshness record and one advisory note per file instead of N.
    """
    if not edits:
        return 'No edits provided.'

    MAX_EDITS = 30
    if len(edits) > MAX_EDITS:
        edits = edits[:MAX_EDITS]

    # Import _resolve_base here (from tools.py) to avoid circular import
    from lib.project_mod.tools import _resolve_base
    from collections import OrderedDict

    ok_count = 0
    fail_count = 0
    strings = {}   # original 1-based edit index -> result line
    prepared = []  # edits that reached the diff core (in original order)

    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            strings[i] = _invalid_edit_entry_msg(i, edit)
            fail_count += 1
            continue

        rp = edit.get('path', '')
        search = edit.get('search', '')
        replace = edit.get('replace', '')
        desc = edit.get('description', '')

        if not rp or not search:
            strings[i] = f'[{i}] FAIL Missing required field (path or search)'
            fail_count += 1
            continue

        ra = bool(edit.get('replace_all', False))

        try:
            bp, resolved_rp = _resolve_base(base_path, rp)
        except ValueError as _rve:
            logger.debug('[write_tools] tool_apply_diffs caught %s: %s', type(_rve).__name__, _rve)
            fail_count += 1
            strings[i] = f'[{i}] FAIL {rp}: {_rve}'
            continue

        prepared.append({
            'i': i, 'rp': rp, 'resolved_rp': resolved_rp, 'bp': bp,
            'search': search, 'replace': replace, 'desc': desc,
            'replace_all': ra,
        })

    # Resolve the canonical target for each edit and group by it.
    groups = OrderedDict()
    for p in prepared:
        try:
            target = _resolve_write_path(p['bp'], p['resolved_rp'], conv_id=conv_id)
        except ValueError as e:
            p['resolve_err'] = str(e)
            continue
        p['target'] = target
        groups.setdefault(target, []).append(p)

    # One read + one write per file.
    for target, ops in groups.items():
        if not os.path.isfile(target):
            for p in ops:
                _log_legacy_edit_efficiency(p['search'], p['replace'], ok=False)
                strings[p['i']] = (
                    f'[{p["i"]}] FAIL {p["rp"]}: File not found: {p["resolved_rp"]}')
                fail_count += 1
            continue
        try:
            with open(target, errors='replace') as f:
                current = f.read()
        except Exception as e:
            logger.warning('[Tools] apply_diffs read failed for %s: %s', target, e, exc_info=True)
            for p in ops:
                _log_legacy_edit_efficiency(p['search'], p['replace'], ok=False)
                strings[p['i']] = f'[{p["i"]}] FAIL {p["rp"]}: Cannot read file: {e}'
                fail_count += 1
            continue

        comps = []  # (op, comp|None, err_dict|None)
        recs = []
        for p in ops:
            comp = _compute_diff(current, p['resolved_rp'], p['search'], p['replace'],
                                 replace_all=p['replace_all'])
            if comp['ok']:
                current = comp['new_content']
                recs.append({
                    'new_content': comp['new_content'],
                    'original_content': comp['original_content'],
                    'reverse_patch': comp['reverse_patch'],
                    'attribution_base': p['bp'],
                    'attribution_rel': p['resolved_rp'],
                })
                comps.append((p, comp, None))
            else:
                comps.append((p, None, comp))

        write_err = None
        if recs:
            write_err = _commit_edit_group(
                target, recs[0]['attribution_base'], recs, conv_id, task_id)

        for p, comp, err_dict in comps:
            if comp is not None:
                if write_err is None:
                    result = _diff_result_from_comp(p['resolved_rp'], comp, p['desc'])
                else:
                    result = {'ok': False, 'error': write_err,
                              'action': 'apply_diff', 'path': p['resolved_rp']}
            else:
                result = err_dict
            _log_legacy_edit_efficiency(p['search'], p['replace'], ok=result['ok'])

            if result['ok']:
                ok_count += 1
                extra = ''
                if result.get('replacedCount'):
                    extra = f' [{result["replacedCount"]} occurrences]'
                strings[p['i']] = (
                    f'[{p["i"]}] OK {result["path"]}: {result["linesChanged"]} lines changed '
                    f'({result["oldLines"]}L → {result["newLines"]}L){extra}'
                    + (f' — {p["desc"]}' if p['desc'] else '')
                )
            else:
                fail_count += 1
                strings[p['i']] = f'[{p["i"]}] FAIL {p["rp"]}: {result["error"]}'

    # Emit pre-fail and prepared results in the model's original edit order.
    for p in prepared:
        if 'resolve_err' in p:
            _log_legacy_edit_efficiency(p['search'], p['replace'], ok=False)
            strings[p['i']] = f'[{p["i"]}] FAIL {p["rp"]}: {p["resolve_err"]}'
            fail_count += 1

    results = [strings[i] for i in range(1, len(edits) + 1)]
    header = f'Applied {ok_count}/{ok_count + fail_count} edits'
    if fail_count:
        header += f' ({fail_count} failed)'
    return header + '\n' + '\n'.join(results)




# ═══════════════════════════════════════════════════════
#  insert boundary-echo repair + post-write .py syntax guard
# ═══════════════════════════════════════════════════════

_ECHO_REPAIR_MAX_CHARS = 2_000_000


def _py_parses(text):
    """(ok, err) for text as Python source. Never raises; unverifiable
    inputs (NUL bytes, pathological nesting) count as ok."""
    try:
        ast.parse(text)
        return True, None
    except SyntaxError as e:
        return False, e
    except (ValueError, RecursionError, MemoryError):
        return True, None


def _splice_insertion(file_content, anchor_idx, anchor, content, position):
    """Byte-identical insert splice (factored from _insert_one)."""
    if position == 'before':
        it = content if content.endswith('\n') else content + '\n'
        return file_content[:anchor_idx] + it + file_content[anchor_idx:], it
    after_idx = anchor_idx + len(anchor)
    it = content
    if after_idx < len(file_content) and file_content[after_idx] != '\n':
        it = '\n' + it
    elif after_idx < len(file_content):
        after_idx += 1
    if not it.endswith('\n'):
        it += '\n'
    return file_content[:after_idx] + it + file_content[after_idx:], it


def _line_window_after(file_content, anchor_idx, anchor):
    """Lines right after the anchor ([] at EOF); None if not at a line end."""
    after_idx = anchor_idx + len(anchor)
    if after_idx == len(file_content):
        return []
    if file_content[after_idx] != '\n':
        return None
    return file_content[after_idx + 1:].split('\n')


def _line_window_before(file_content, anchor_idx):
    """Lines right above the anchor ([] at BOF); None if anchor is not at
    a line start."""
    if anchor_idx == 0:
        return []
    if file_content[anchor_idx - 1] != '\n':
        return None
    return file_content[:anchor_idx - 1].split('\n')


def _strict_echo_run(content_lines, neighbour, position):
    """Exact boundary match count, no blank skipping."""
    if position == 'after':
        k = 0
        while (k < len(content_lines) and k < len(neighbour)
               and content_lines[k] == neighbour[k]):
            k += 1
        return k
    k = 0
    while (k < len(content_lines) and k < len(neighbour)
           and content_lines[-1 - k] == neighbour[-1 - k]):
        k += 1
    return k


def _neighbour_echo_run(content_lines, neighbour, position):
    """Blank-skipping boundary match. Returns (c_start, k): the matched run
    is content_lines[c_start:c_start+k]; lines outside it are blank-only on
    the near side."""
    if position == 'after':
        c0 = 0
        while c0 < len(content_lines) and not content_lines[c0].strip():
            c0 += 1
        n0 = 0
        while n0 < len(neighbour) and not neighbour[n0].strip():
            n0 += 1
        k = 0
        while (c0 + k < len(content_lines) and n0 + k < len(neighbour)
               and content_lines[c0 + k] == neighbour[n0 + k]):
            k += 1
        return c0, k
    ce = len(content_lines) - 1
    while ce >= 0 and not content_lines[ce].strip():
        ce -= 1
    ne = len(neighbour) - 1
    while ne >= 0 and not neighbour[ne].strip():
        ne -= 1
    k = 0
    while (ce - k >= 0 and ne - k >= 0
           and content_lines[ce - k] == neighbour[ne - k]):
        k += 1
    return ce - k + 1, k


def _strip_anchor_echo(file_content, anchor_idx, anchor, content, position):
    """Strip a verbatim anchor copy at content's anchor-facing boundary.
    Whole-line anchors only — a mid-line overlap is ambiguous."""
    if not content or len(content) < len(anchor):
        return content, 0
    after_idx = anchor_idx + len(anchor)
    at_ls = anchor_idx == 0 or file_content[anchor_idx - 1] == '\n'
    at_le = after_idx == len(file_content) or file_content[after_idx] == '\n'
    if not (at_ls and at_le):
        return content, 0
    if position == 'after':
        if not content.startswith(anchor):
            return content, 0
        rest = content[len(anchor):]
        if rest.startswith('\n'):
            rest = rest[1:]
        elif rest:
            return content, 0
        return rest, len(content) - len(rest)
    if not content.endswith(anchor):
        return content, 0
    rest = content[:-len(anchor)]
    if rest.endswith('\n'):
        rest = rest[:-1]
    elif rest:
        return content, 0
    return rest, len(content) - len(rest)


def _whole_echo_error(k, position):
    where = 'after' if position == 'after' else 'before'
    return (f'content is entirely a verbatim copy of the {k} line(s) '
            f'already sitting right {where} the anchor — the edit would '
            'duplicate existing text and add nothing. Re-issue with ONLY '
            'the new text; to duplicate those lines deliberately, use '
            'operation=replace with anchor=<those lines> and '
            'content=<both copies>.')


def _py_syntax_guard(rel_path, before_text, new_text):
    """Report-only .py guard: warn when a write breaks a previously
    parseable file (or creates a broken new one). Never raises/blocks."""
    if os.environ.get('TOFU_EDIT_SYNTAX_GUARD', '1') == '0':
        return ''
    if not isinstance(rel_path, str) or not rel_path.endswith('.py'):
        return ''
    if not isinstance(new_text, str) or len(new_text) > _ECHO_REPAIR_MAX_CHARS:
        return ''
    if before_text is not None:
        pre_ok, _e = _py_parses(before_text)
        if not pre_ok:
            return ''
    post_ok, err = _py_parses(new_text)
    if post_ok:
        return ''
    where = (f'line {err.lineno}: {err.msg}'
             if getattr(err, 'lineno', None) else str(err))
    if before_text is None:
        lead = f'{rel_path} was created with invalid Python syntax'
    else:
        lead = (f'this edit left {rel_path} syntactically invalid '
                '(it parsed cleanly before)')
    return (f'SYNTAX GUARD: {lead} — {where}. Fix it in a follow-up edit '
            f'(verify with run_command "python -m py_compile {rel_path}" '
            'on the project).')


def _neighbour_echo_strip(file_content, anchor_idx, anchor, content,
                          position, rel_path):
    """Parse-arbitered neighbour-echo repair for .py inserts.

    Returns (content, stripped_lines, fail_msg). Only fires when the
    as-given splice breaks a previously-parseable file; then the smallest
    boundary strip whose splice parses again wins."""
    if not rel_path.endswith('.py'):
        return content, 0, None
    if len(file_content) + len(content) > _ECHO_REPAIR_MAX_CHARS:
        return content, 0, None
    if position == 'after':
        neighbour = _line_window_after(file_content, anchor_idx, anchor)
    else:
        neighbour = _line_window_before(file_content, anchor_idx)
    if not neighbour:
        return content, 0, None
    content_lines = content.split('\n')
    c_start, k_max = _neighbour_echo_run(content_lines, neighbour, position)
    if not k_max:
        return content, 0, None
    pre_ok, _e = _py_parses(file_content)
    if not pre_ok:
        return content, 0, None
    as_given, _it = _splice_insertion(
        file_content, anchor_idx, anchor, content, position)
    as_ok, _e2 = _py_parses(as_given)
    if as_ok:
        return content, 0, None
    for k in range(1, k_max + 1):
        remaining = content_lines[:c_start] + content_lines[c_start + k:]
        if not any(line.strip() for line in remaining):
            return content, 0, _whole_echo_error(k, position)
        candidate = '\n'.join(remaining)
        new_text, _ins = _splice_insertion(
            file_content, anchor_idx, anchor, candidate, position)
        ok, _e3 = _py_parses(new_text)
        if ok:
            return candidate, k, None
    return content, 0, None


def _finalize_insertion(file_content, anchor_idx, anchor, content,
                        position, rel_path):
    """Echo repair + splice + syntax guard for one insert edit.

    Returns {'ok', 'content', 'new_content', 'insert_text', 'notes',
    'warning'} or {'ok': False, 'error'} (provably-contentless echo edits
    never touch the file). TOFU_EDIT_ECHO_REPAIR=0 disables the repair.
    """
    notes = []
    if os.environ.get('TOFU_EDIT_ECHO_REPAIR', '1') != '0':
        stripped, n_chars = _strip_anchor_echo(
            file_content, anchor_idx, anchor, content, position)
        if n_chars:
            if not stripped.strip():
                return {'ok': False, 'error': (
                    'content is just the anchor text repeated — '
                    f'insert_{position} keeps the anchor in place '
                    'automatically. Re-issue with ONLY the new text in '
                    'content (do not repeat the anchor).')}
            side = 'start' if position == 'after' else 'end'
            notes.append(
                f'auto-repaired: stripped the anchor copy ({n_chars} '
                f'chars) from the {side} of content — insert_{position} '
                'keeps the anchor automatically; pass ONLY the new text')
            content = stripped
        neighbour = (_line_window_after(file_content, anchor_idx, anchor)
                     if position == 'after'
                     else _line_window_before(file_content, anchor_idx))
        if neighbour:
            content_lines = content.split('\n')
            k = _strict_echo_run(content_lines, neighbour, position)
            if position == 'after':
                matched = content_lines[:k]
                remaining = content_lines[k:]
            elif k:
                matched = content_lines[-k:]
                remaining = content_lines[:-k]
            else:
                matched, remaining = [], content_lines
            if (k and any(line.strip() for line in matched)
                    and not any(line.strip() for line in remaining)):
                return {'ok': False, 'error': _whole_echo_error(k, position)}
        stripped2, k2, fail = _neighbour_echo_strip(
            file_content, anchor_idx, anchor, content, position, rel_path)
        if fail:
            return {'ok': False, 'error': fail}
        if k2:
            where = 'after' if position == 'after' else 'before'
            notes.append(
                f'auto-repaired: stripped {k2} echoed context line'
                f'{"s" if k2 > 1 else ""} already present right {where} '
                'the anchor — pass ONLY the new text')
            content = stripped2
    new_content, insert_text = _splice_insertion(
        file_content, anchor_idx, anchor, content, position)
    warning = _py_syntax_guard(rel_path, file_content, new_content)
    return {'ok': True, 'content': content, 'new_content': new_content,
            'insert_text': insert_text, 'notes': notes, 'warning': warning}


# ═══════════════════════════════════════════════════════
#  insert_content
# ═══════════════════════════════════════════════════════

def _compute_insert(file_content, rel_path, anchor, content, position='after'):
    """Pure match/splice half of ``_insert_one`` (no disk I/O).

    Returns ``{'ok': True, ...}`` with everything the commit step needs, or
    ``{'ok': False, ...}`` in the exact error shapes ``_insert_one`` produced.
    """
    # ── Locate anchor (same normalization strategy as apply_diff) ──
    norm_content = file_content
    norm_anchor = anchor
    _normalized = False

    count = file_content.count(anchor)
    if count == 0:
        # Try CRLF → LF normalization
        norm_content = file_content.replace('\r\n', '\n')
        norm_anchor = anchor.replace('\r\n', '\n')
        count = norm_content.count(norm_anchor)
        if count > 0:
            _normalized = True
        else:
            # Try trailing-whitespace normalization
            def _rstrip_lines(s):
                return '\n'.join(l.rstrip() for l in s.split('\n'))

            tw_content = _rstrip_lines(norm_content)
            tw_anchor = _rstrip_lines(norm_anchor)
            tw_count = tw_content.count(tw_anchor)

            # ── Tier 4: unicode-escape normalization ──
            # Glyph-vs-literal-escape drift (anchor "⏰" vs file "\u23f0").
            # Decode \uXXXX / \UXXXXXXXX / \xXX on both sides for matching,
            # then reconstruct the real anchor text from the file lines.
            if tw_count == 0:
                esc_content_lines = [_decode_unicode_escapes(l).rstrip()
                                     for l in norm_content.split('\n')]
                esc_anchor_lines = [_decode_unicode_escapes(l).rstrip()
                                    for l in norm_anchor.split('\n')]
                n_el = len(esc_anchor_lines)
                esc_starts = [i for i in range(len(esc_content_lines) - n_el + 1)
                              if esc_content_lines[i:i + n_el] == esc_anchor_lines]
                if len(esc_starts) == 1:
                    real_lines = norm_content.split('\n')[esc_starts[0]:esc_starts[0] + n_el]
                    norm_anchor = '\n'.join(real_lines)
                    count = 1
                    _normalized = True
                    logger.debug('insert_content: unicode-escape normalized match in %s', rel_path)
                elif len(esc_starts) > 1:
                    esc_content = '\n'.join(esc_content_lines)
                    esc_anchor = '\n'.join(esc_anchor_lines)
                    locs = _describe_duplicate_matches(esc_content, esc_anchor)
                    error_msg = (f'Anchor text matches {len(esc_starts)} locations (after unicode-escape '
                                 f'normalization). Make it more specific by adding surrounding lines so it '
                                 f'matches exactly once.')
                    if locs:
                        error_msg += f'\n\n{locs}'
                    return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                            'error': error_msg}

            if tw_count == 0 and not _normalized:
                hint = _find_closest_match(norm_content, norm_anchor)
                error_msg = (f'Anchor text not found in {rel_path}. '
                             f'File has {file_content.count(chr(10))+1} lines. '
                             f'Use read_files to verify the exact content first.')
                if hint:
                    error_msg += (f'\n\nMost similar block (line {hint["line"]}, '
                                  f'{hint["similarity"]:.0%} match):\n```\n{hint["text"]}\n```')
                return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                        'error': error_msg, 'anchorLen': len(anchor)}

            if tw_count > 1:
                locs = _describe_duplicate_matches(tw_content, tw_anchor)
                error_msg = (f'Anchor text matches {tw_count} locations (after trailing-whitespace '
                             f'normalization). Make it more specific by adding surrounding lines so it '
                             f'matches exactly once.')
                if locs:
                    error_msg += f'\n\n{locs}'
                return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                        'error': error_msg}

            # Single match after TW normalization — find the real position
            # by matching line-by-line in the original content.
            # Skipped when tier-4 escape normalization already resolved a match.
            if tw_count == 1 and not _normalized:
                tw_lines = tw_content.split('\n')
                anchor_lines = tw_anchor.split('\n')
                n_al = len(anchor_lines)
                content_lines = norm_content.split('\n')

                match_start = None
                for i in range(len(tw_lines) - n_al + 1):
                    if tw_lines[i:i + n_al] == anchor_lines:
                        match_start = i
                        break

                if match_start is not None:
                    # Reconstruct the original anchor text from the file
                    orig_anchor_lines = content_lines[match_start:match_start + n_al]
                    norm_anchor = '\n'.join(orig_anchor_lines)
                    count = 1
                    _normalized = True
                    logger.debug('insert_content: trailing-WS normalized match in %s', rel_path)
                else:
                    return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                            'error': 'Anchor matched after normalization but line mapping failed. '
                                     'Please use read_files to get the exact content.'}

    if _normalized:
        file_content = norm_content
        anchor = norm_anchor

    if count > 1:
        locs = _describe_duplicate_matches(file_content, anchor)
        error_msg = (f'Anchor text matches {count} locations. Make it more specific to identify a '
                     f'unique position by adding surrounding lines.')
        if locs:
            error_msg += f'\n\n{locs}'
        return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                'error': error_msg}

    # ── Boundary-echo repair + splice + syntax guard ──
    anchor_idx = file_content.index(anchor)
    fin = _finalize_insertion(file_content, anchor_idx, anchor, content,
                              position, rel_path)
    if not fin['ok']:
        return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                'error': fin['error']}
    content = fin['content']
    new_content = fin['new_content']
    insert_text = fin['insert_text']

    # ── Build reverse patch for undo ──
    if position == 'before':
        reverse_patch = {'search': insert_text + anchor, 'replace': anchor}
    else:
        chunk_start = anchor_idx
        chunk_end = anchor_idx + len(anchor) + len(insert_text)
        # Adjust if we consumed the trailing newline of anchor
        if file_content[anchor_idx + len(anchor):anchor_idx + len(anchor) + 1] == '\n':
            chunk_end = anchor_idx + len(anchor) + 1 + len(insert_text)
        inserted_block = new_content[chunk_start:chunk_end]
        reverse_patch = {'search': inserted_block, 'replace': file_content[anchor_idx:anchor_idx + len(anchor) + (1 if file_content[anchor_idx + len(anchor):anchor_idx + len(anchor) + 1] == '\n' else 0)]}

    return {
        'ok': True,
        'new_content': new_content,
        'original_content': file_content,
        'reverse_patch': reverse_patch,
        'position': position,
        'anchor_line': file_content[:anchor_idx].count('\n') + 1,
        'inserted_lines': content.count('\n') + 1,
        'old_lines': file_content.count('\n') + 1,
        'notes': fin['notes'],
        'warning': fin['warning'],
    }


def _insert_result_from_comp(rel_path, comp, description):
    """Build the success result dict + logging for one computed insert (no I/O)."""
    new_lines = comp['new_content'].count('\n') + 1
    result = {
        'ok': True, 'action': 'insert_content', 'path': rel_path,
        'position': comp['position'],
        'anchorLine': comp['anchor_line'],
        'linesInserted': comp['inserted_lines'],
        'oldLines': comp['old_lines'], 'newLines': new_lines,
        'description': description,
    }
    if comp['notes']:
        result['echoRepairs'] = comp['notes']
        for _note in comp['notes']:
            logger.info('[EditEchoRepair] %s: %s', rel_path, _note)
    if comp['warning']:
        result['syntaxWarning'] = comp['warning']
        logger.warning('[SyntaxGuard] %s', comp['warning'])
    logger.info('insert_content: %s (%d lines inserted %s anchor at L%d, %dL → %dL)',
                 rel_path, comp['inserted_lines'], comp['position'], comp['anchor_line'],
                 comp['old_lines'], new_lines)
    return result


def _commit_insert(target, base, rel_path, comp, description='', conv_id=None, task_id=None):
    """Atomic write + freshness/advisory + undo for one computed insert."""
    try:
        _atomic_write_text(target, comp['new_content'])
        _touch_for_vscode(target)
        _record_write_freshness(conv_id, target, comp['new_content'].encode('utf-8'))
        if _should_record_modification(target, conv_id=conv_id):
            _mod_base, _mod_rel = _mod_attribution(target, base, rel_path, conv_id=conv_id)
            _record_modification(_mod_base, 'apply_diff', _mod_rel,
                                 original_content=comp['original_content'],
                                 reverse_patch=comp['reverse_patch'],
                                 conv_id=conv_id, task_id=task_id)
        return _insert_result_from_comp(rel_path, comp, description)
    except Exception as e:
        logger.error('[Tools] insert_content write failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'insert_content', 'path': rel_path}


def _insert_one(base, rel_path, anchor, content, position='after', description='', conv_id=None, task_id=None):
    """Insert content before or after an anchor string in a file.

    Args:
        base: Project base path.
        rel_path: Relative file path.
        anchor: Literal string to locate the insertion point.
        content: New content to insert.
        position: 'before' or 'after' the anchor.
        description: Optional description.
        conv_id: Conversation ID for undo tracking.
        task_id: Task ID for undo tracking.

    Returns:
        dict with ok, action, path, error (on failure), or ok + line info (on success).
    """
    try:
        target = _resolve_write_path(base, rel_path, conv_id=conv_id)
    except ValueError as e:
        logger.debug('[Tools] insert_content path rejected %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'insert_content', 'path': rel_path}

    if not os.path.isfile(target):
        return {'ok': False, 'error': f'File not found: {rel_path}',
                'action': 'insert_content', 'path': rel_path}

    try:
        with open(target, errors='replace') as f:
            file_content = f.read()
    except Exception as e:
        logger.warning('[Tools] insert_content read failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': f'Cannot read file: {e}',
                'action': 'insert_content', 'path': rel_path}

    comp = _compute_insert(file_content, rel_path, anchor, content, position=position)
    if not comp['ok']:
        return comp
    return _commit_insert(target, base, rel_path, comp, description=description,
                          conv_id=conv_id, task_id=task_id)


def tool_insert_content(base, rel_path, anchor, content, position='after', description='', conv_id=None, task_id=None):
    """Insert content before or after an anchor string (single edit entry point)."""
    return _insert_one(base, rel_path, anchor, content, position, description, conv_id, task_id=task_id)


def tool_insert_contents(base_path, edits, conv_id=None, task_id=None):
    """Apply multiple insert_content edits in one batch."""
    if not edits:
        return 'No edits provided.'

    MAX_EDITS = 30
    if len(edits) > MAX_EDITS:
        edits = edits[:MAX_EDITS]

    from lib.project_mod.tools import _resolve_base

    results = []
    ok_count = 0
    fail_count = 0

    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            results.append(_invalid_edit_entry_msg(i, edit))
            fail_count += 1
            continue

        rp = edit.get('path', '')
        anchor = edit.get('anchor', '')
        content = edit.get('content', '')
        position = edit.get('position', 'after')
        desc = edit.get('description', '')

        if not rp or not anchor:
            results.append(f'[{i}] FAIL Missing required field (path or anchor)')
            fail_count += 1
            continue

        if position not in ('before', 'after'):
            results.append(f'[{i}] FAIL Invalid position: {position} (must be "before" or "after")')
            fail_count += 1
            continue

        try:
            bp, resolved_rp = _resolve_base(base_path, rp)
        except ValueError as _rve:
            logger.debug('[write_tools] tool_insert_contents caught %s: %s', type(_rve).__name__, _rve)
            fail_count += 1
            results.append(f'[{i}] FAIL {rp}: {_rve}')
            continue
        result = _insert_one(bp, resolved_rp, anchor, content, position, desc, conv_id, task_id=task_id)

        if result['ok']:
            ok_count += 1
            extra = ''.join(f' [{n}]' for n in result.get('echoRepairs') or [])
            if result.get('syntaxWarning'):
                extra += ' ' + result['syntaxWarning']
            results.append(
                f'[{i}] OK {result["path"]}: {result["linesInserted"]} lines inserted '
                f'{result["position"]} anchor at L{result["anchorLine"]} '
                f'({result["oldLines"]}L → {result["newLines"]}L){extra}'
                + (f' — {desc}' if desc else '')
            )
        else:
            fail_count += 1
            results.append(f'[{i}] FAIL {rp}: {result["error"]}')

    header = f'Inserted {ok_count}/{ok_count + fail_count} edits'
    if fail_count:
        header += f' ({fail_count} failed)'
    return header + '\n' + '\n'.join(results)


# ═══════════════════════════════════════════════════════
#  edit_file — unified model-facing edit entry point
# ═══════════════════════════════════════════════════════

_EDIT_OPERATIONS = frozenset({'insert_after', 'insert_before', 'replace'})


def _log_edit_efficiency(operation, anchor, content, *, ok):
    """Emit the per-edit efficiency line for the unified edit_file surface."""
    logger.info(
        '[EditEfficiency] surface=edit_file operation=%s ok=%s '
        'anchor_chars=%d content_chars=%d arg_chars=%d',
        operation, bool(ok), len(anchor), len(content),
        len(anchor) + len(content),
    )


def tool_edit_file(base_path, edits, conv_id=None, task_id=None):
    """Apply a sequential mixed batch of replacements and insertions.

    Edits are grouped by resolved target: each file is read once, its edits
    are applied in memory sequentially (edit N still sees the result of
    1..N-1), and the final bytes are written once — one atomic write, one
    freshness record and one advisory note per file instead of N.
    """
    if not edits:
        return 'No edits provided.'
    if not isinstance(edits, list):
        return 'Error: edit_file edits must be an array of objects.'

    MAX_EDITS = 30
    dropped = max(0, len(edits) - MAX_EDITS)
    edits = edits[:MAX_EDITS]

    from lib.project_mod.tools import _resolve_base
    from collections import OrderedDict

    ok_count = 0
    fail_count = 0
    strings = {}
    prepared = []

    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            strings[i] = _invalid_edit_entry_msg(
                i, edit, '{path, operation, anchor, content}')
            fail_count += 1
            continue

        rp = edit.get('path', '')
        operation = edit.get('operation', '')
        anchor = edit.get('anchor', '')
        content = edit.get('content')
        desc = edit.get('description', '')

        if (not isinstance(rp, str) or not rp
                or not isinstance(anchor, str) or not anchor
                or not isinstance(content, str)):
            strings[i] = f'[{i}] FAIL Missing required field (path, anchor, or content)'
            fail_count += 1
            continue
        if not isinstance(operation, str) or operation not in _EDIT_OPERATIONS:
            strings[i] = (f'[{i}] FAIL Invalid operation: {operation!r} '
                          '(must be insert_after, insert_before, or replace)')
            fail_count += 1
            continue
        # ``replace_all`` has no meaning for an insertion.  Normalize it away
        # instead of rejecting a unique insertion and spending another model
        # round on a parameter-only correction.  _compute_insert still rejects
        # ambiguous anchors, so a stray true value never enables multi-site
        # insertion.
        replace_all = operation == 'replace' and bool(edit.get('replace_all'))
        try:
            bp, resolved_rp = _resolve_base(base_path, rp, conv_id=conv_id)
        except ValueError as exc:
            logger.debug('[write_tools] tool_edit_file path rejected: %s', exc)
            strings[i] = f'[{i}] FAIL {rp}: {exc}'
            fail_count += 1
            continue

        if operation == 'replace':
            # Pre-execution wrap gate: a replace whose content repeats the
            # anchor verbatim at a boundary IS an insertion — refuse it so
            # the model re-issues with the insert vocabulary (the hint is
            # only meaningful BEFORE the edit lands). replace_all is exempt:
            # no insert op has multi-site semantics. Kill switch:
            # TOFU_EDIT_WRAP_GATE=0.
            wrap = None
            if (not replace_all
                    and os.environ.get('TOFU_EDIT_WRAP_GATE', '1') != '0'):
                wrap = _pure_wrap_insert(anchor, content)
            if wrap:
                op_name, trimmed = wrap
                logger.info(
                    '[EditEfficiency] surface=edit_file wrap_rejected=true '
                    'suggested_op=%s anchor_chars=%d trimmed_chars=%d',
                    op_name, len(anchor), len(trimmed))
                fail_count += 1
                boundary = 'start' if op_name == 'insert_after' else 'end'
                direction = ('appended' if op_name == 'insert_after'
                             else 'prepended')
                strings[i] = (
                    f'[{i}] FAIL {rp} [replace]: pure insertion rejected — '
                    f'the content keeps the anchor verbatim at its '
                    f'{boundary}, re-sending {len(anchor)} unchanged chars. '
                    f"Re-issue as operation='{op_name}' with ONLY the "
                    f'{direction} text in content ({len(trimmed)} chars — '
                    f'do NOT repeat the anchor).')
                continue

        prepared.append({
            'i': i, 'rp': rp, 'resolved_rp': resolved_rp, 'bp': bp,
            'operation': operation, 'anchor': anchor, 'content': content,
            'desc': desc, 'replace_all': replace_all,
            'position': ('after' if operation == 'insert_after' else 'before'),
        })

    # Resolve canonical targets and group edits by file.
    groups = OrderedDict()
    for p in prepared:
        try:
            target = _resolve_write_path(p['bp'], p['resolved_rp'], conv_id=conv_id)
        except ValueError as e:
            p['resolve_err'] = str(e)
            continue
        p['target'] = target
        groups.setdefault(target, []).append(p)

    # One read + one write per file.
    for target, ops in groups.items():
        if not os.path.isfile(target):
            for p in ops:
                _log_edit_efficiency(p['operation'], p['anchor'], p['content'], ok=False)
                strings[p['i']] = (
                    f'[{p["i"]}] FAIL {p["rp"]} [{p["operation"]}]: '
                    f'File not found: {p["resolved_rp"]}')
                fail_count += 1
            continue
        try:
            with open(target, errors='replace') as f:
                current = f.read()
        except Exception as e:
            logger.warning('[Tools] edit_file read failed for %s: %s', target, e, exc_info=True)
            for p in ops:
                _log_edit_efficiency(p['operation'], p['anchor'], p['content'], ok=False)
                strings[p['i']] = (
                    f'[{p["i"]}] FAIL {p["rp"]} [{p["operation"]}]: '
                    f'Cannot read file: {e}')
                fail_count += 1
            continue

        comps = []
        recs = []
        for p in ops:
            if p['operation'] == 'replace':
                comp = _compute_diff(current, p['resolved_rp'], p['anchor'],
                                     p['content'], replace_all=p['replace_all'])
            else:
                comp = _compute_insert(current, p['resolved_rp'], p['anchor'],
                                       p['content'], position=p['position'])
            if comp['ok']:
                current = comp['new_content']
                recs.append({
                    'new_content': comp['new_content'],
                    'original_content': comp['original_content'],
                    'reverse_patch': comp['reverse_patch'],
                    'attribution_base': p['bp'],
                    'attribution_rel': p['resolved_rp'],
                })
                comps.append((p, comp, None))
            else:
                comps.append((p, None, comp))

        write_err = None
        if recs:
            write_err = _commit_edit_group(
                target, recs[0]['attribution_base'], recs, conv_id, task_id)

        for p, comp, err_dict in comps:
            if comp is not None:
                if write_err is None:
                    if p['operation'] == 'replace':
                        result = _diff_result_from_comp(p['resolved_rp'], comp, p['desc'])
                    else:
                        result = _insert_result_from_comp(p['resolved_rp'], comp, p['desc'])
                else:
                    action = 'apply_diff' if p['operation'] == 'replace' else 'insert_content'
                    result = {'ok': False, 'error': write_err,
                              'action': action, 'path': p['resolved_rp']}
            else:
                result = err_dict
            _log_edit_efficiency(p['operation'], p['anchor'], p['content'], ok=result['ok'])

            if result['ok']:
                ok_count += 1
                if p['operation'] == 'replace':
                    detail = f'{result["linesChanged"]} lines changed'
                    if result.get('replacedCount'):
                        detail += f' [{result["replacedCount"]} occurrences]'
                else:
                    detail = (f'{result["linesInserted"]} lines inserted '
                              f'{result["position"]} anchor at L{result["anchorLine"]}')
                extra = ''.join(f' [{n}]' for n in result.get('echoRepairs') or [])
                if result.get('syntaxWarning'):
                    extra += ' ' + result['syntaxWarning']
                strings[p['i']] = (
                    f'[{p["i"]}] OK {result["path"]} [{p["operation"]}]: {detail} '
                    f'({result["oldLines"]}L → {result["newLines"]}L){extra}'
                    + (f' — {p["desc"]}' if p['desc'] else ''))
            else:
                fail_count += 1
                strings[p['i']] = (
                    f'[{p["i"]}] FAIL {p["rp"]} [{p["operation"]}]: '
                    f'{result["error"]}')

    # Edits whose path resolution failed at the write-core stage (readonly,
    # forbidden root, …) — same error the single-edit core would surface.
    for p in prepared:
        if 'resolve_err' in p:
            _log_edit_efficiency(p['operation'], p['anchor'], p['content'], ok=False)
            strings[p['i']] = (
                f'[{p["i"]}] FAIL {p["rp"]} [{p["operation"]}]: '
                f'{p["resolve_err"]}')
            fail_count += 1

    results = [strings[i] for i in range(1, len(edits) + 1)]
    header = f'Applied {ok_count}/{ok_count + fail_count} edits'
    if fail_count:
        header += f' ({fail_count} failed)'
    if dropped:
        header += f' ({dropped} over the 30-edit limit dropped)'
    return header + '\n' + '\n'.join(results)
