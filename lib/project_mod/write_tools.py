"""Project write tools — write_file, apply_diff, apply_diffs, create_project.

Extracted from tools.py for modularity. Re-exported via tools.py for backward compat.
"""

import os
import re
import tempfile
import threading
from difflib import SequenceMatcher

from lib.log import audit_log, get_logger
from lib.project_mod.config import _lock, _roots, _state
from lib.project_mod.modifications import _record_modification
from lib.project_mod.scanner import _fmt_size, _safe_path, add_project_root

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════
#  Temp-directory detection (scratch writes are untracked)
# ═══════════════════════════════════════════════════════
# Absolute-path writes into the OS temp dir are TRUE scratch: they must not
# auto-register a workspace root (which would invent a bogus ``tmp:`` project
# in the file-changes bar) and must not be recorded in the undo history.
# This mirrors run_command, whose snapshot/diff only walks ``base_path`` and
# therefore already ignores anything written under /tmp.
#
# Sourced from ``tempfile.gettempdir()`` (honours $TMPDIR/$TEMP/$TMP) plus the
# conventional Unix temp roots as a fallback — never a single hardcoded path,
# per the no-hardcoded-environment-values rule.

def _temp_roots():
    """Return the normalized set of temp-directory roots (cached).

    Includes the platform temp dir (``tempfile.gettempdir()``, which respects
    ``$TMPDIR``/``$TEMP``/``$TMP``) plus ``/tmp`` and ``/var/tmp`` as Unix
    fallbacks.  Each is realpath-normalized so symlinked temp dirs (e.g. macOS
    ``/tmp`` → ``/private/tmp``) match a realpath'd target.
    """
    cached = getattr(_temp_roots, '_cache', None)
    if cached is not None:
        return cached
    roots = set()
    try:
        roots.add(os.path.realpath(tempfile.gettempdir()))
    except Exception as e:
        logger.debug('[WriteTools] gettempdir failed: %s', e)
    for fallback in ('/tmp', '/var/tmp'):
        try:
            if os.path.isdir(fallback):
                roots.add(os.path.realpath(fallback))
        except OSError as e:
            logger.debug('[WriteTools] temp fallback %s stat failed: %s', fallback, e)
    roots.discard('')
    _temp_roots._cache = roots
    return roots


def _is_temp_path(abs_path):
    """True if *abs_path* lives inside an OS temp-directory root.

    Used to treat absolute-path writes under /tmp (etc.) as untracked scratch:
    no root auto-registration, no modification record.
    """
    if not abs_path:
        return False
    try:
        real = os.path.realpath(abs_path)
    except Exception as e:
        logger.debug('[WriteTools] realpath failed for %s: %s', abs_path, e)
        real = os.path.abspath(abs_path)
    for troot in _temp_roots():
        if real == troot or real.startswith(troot + os.sep):
            return True
    return False


# ═══════════════════════════════════════════════════════
#  Workspace-root auto-registration signal (observability)
# ═══════════════════════════════════════════════════════
# When an absolute-path write auto-registers a NEW extra root (§2 of
# _resolve_write_path), that expansion of the workspace was historically
# invisible — no tool round, no SSE event, only an app.log line. The handler
# layer (lib/tasks_pkg/handlers/project.py) owns the ``task`` dict and emits
# the ``workspace_root_added`` event; the write layer can't (it only has
# conv_id/task_id). We bridge the two via a per-thread collector: the
# synchronous handler→execute_tool→_resolve_write_path call chain runs in one
# thread, so a thread-local list is the race-free seam (the dict result of the
# write is stringified by execute_tool, dropping any extra signal fields).

_root_signal = threading.local()


def _signal_root_added(root_name, root_path):
    """Record that an absolute-path write auto-registered a new extra root.

    Appends to the current thread's pending-signal list (created lazily). The
    handler drains it via :func:`drain_root_added_signals` after the tool runs.
    """
    pending = getattr(_root_signal, 'pending', None)
    if pending is None:
        pending = []
        _root_signal.pending = pending
    pending.append({'rootName': root_name, 'path': root_path})


def drain_root_added_signals():
    """Return and clear the current thread's pending root-added signals.

    Returns a list of ``{rootName, path}`` dicts (empty when none). Called by
    the project tool handler immediately after a write tool executes, so the
    auto-registration can be surfaced as a ``workspace_root_added`` SSE event.
    """
    pending = getattr(_root_signal, 'pending', None)
    if not pending:
        return []
    _root_signal.pending = []
    return pending


# ═══════════════════════════════════════════════════════
#  create_project — bootstrap a new workspace root
# ═══════════════════════════════════════════════════════

# System paths where a user-facing project MUST NOT be created.  These are
# either OS-owned directories (where writing files would likely corrupt the
# system) or special filesystems (/proc, /sys, /dev) where creating a
# directory is meaningless or actively harmful.
#
# Note: we block both exact matches AND any path under these system roots
# (e.g. '/etc/myproj' is rejected).  Windows equivalents are included for
# cross-platform safety, though the dominant deployment is Linux/macOS.
_FORBIDDEN_CREATE_ROOTS = (
    '/', '/etc', '/usr', '/bin', '/sbin', '/boot',
    '/sys', '/proc', '/dev', '/var', '/lib', '/lib32', '/lib64', '/root',
    'C:\\', 'C:\\Windows', 'C:\\Program Files', 'C:\\Program Files (x86)',
)


def _is_forbidden_create_path(abs_path):
    """Return True if *abs_path* must not host a new project.

    Rejects:
      - the filesystem root itself ('/' or 'C:\\')
      - exact match with any entry in ``_FORBIDDEN_CREATE_ROOTS``
      - any descendant of the Unix-style system roots
      - the user's ``$HOME`` itself (a project at ~ would shadow many files)
    """
    if not abs_path:
        return True
    p = os.path.normpath(abs_path)
    # Strip trailing separator for comparison, but preserve '/' and 'C:\\'.
    p_cmp = p.rstrip(os.sep) if len(p) > 1 and not (len(p) == 3 and p[1] == ':') else p

    for forb in _FORBIDDEN_CREATE_ROOTS:
        forb_cmp = forb.rstrip(os.sep) if len(forb) > 1 and not (len(forb) == 3 and forb[1] == ':') else forb
        if p_cmp == forb_cmp:
            return True
        # Descendant check — only for Unix-style system roots where
        # every child is system-managed (not '/home', '/opt', '/tmp', etc.).
        if forb in ('/etc', '/usr', '/bin', '/sbin', '/boot',
                    '/sys', '/proc', '/dev', '/lib', '/lib32', '/lib64'):
            if p_cmp.startswith(forb + os.sep):
                return True

    # Reject '$HOME' itself (but allow children like '~/projects/foo').
    try:
        home = os.path.expanduser('~')
        if home and home != '~':
            home_cmp = home.rstrip(os.sep) or home
            if p_cmp == home_cmp:
                return True
    except (OSError, KeyError) as _e_audit:
        # Can't determine HOME — skip this check rather than blocking.
        logger.debug('[write_tools] _is_forbidden_create_path caught %s: %s', type(_e_audit).__name__, _e_audit)
        pass

    return False


def tool_create_project(path, name=None, overwrite=False, conv_id=None, task_id=None):
    """Create a new project directory and register it as an extra workspace root.

    After this call, the new path can be addressed with the ``<name>:<rel>``
    prefix in any path-taking tool (``write_file``, ``apply_diff``,
    ``read_files``, ``run_command``, …), or by the absolute path directly.

    Args:
        path: Target directory (may start with ``~``).  Created if missing.
        name: Short root name used as the ``name:`` prefix.  Defaults to
            the directory basename; collisions get a numeric suffix.
        overwrite: If True, accept a non-empty existing directory (files are
            NOT deleted — only the "non-empty" guard is bypassed so the root
            can still be registered).
        conv_id: Conversation ID (for audit log only).
        task_id: Task ID (for audit log only).

    Returns:
        dict with keys: ok, action, path, rootName, created, message, error.
    """
    if not path or not isinstance(path, str):
        return {'ok': False, 'error': 'path is required (non-empty string)',
                'action': 'create_project', 'path': path}

    # Normalise & expand.  abspath(expanduser(...)) handles '~/foo', relative
    # paths resolved against CWD, and trailing separators.
    try:
        abs_path = os.path.abspath(os.path.expanduser(path.strip()))
    except Exception as e:
        logger.warning('[Project] create_project: invalid path %r: %s', path, e)
        return {'ok': False, 'action': 'create_project', 'path': path,
                'error': f'Invalid path: {e}'}

    # ── Safety gate: forbid system paths ──
    if _is_forbidden_create_path(abs_path):
        msg = (f'Refusing to create a project at system path: {abs_path}. '
               f'Choose a user-writable location (e.g. under ~/projects or '
               f'a sibling of the current project).')
        logger.warning('[Project] create_project blocked (system path): %s', abs_path)
        return {'ok': False, 'action': 'create_project', 'path': abs_path, 'error': msg}

    # ── Require an active project session (for audit context & session dir) ──
    with _lock:
        primary = _state.get('path')
    if not primary:
        return {'ok': False, 'action': 'create_project', 'path': abs_path,
                'error': 'No primary project is set. Open a project before calling create_project.'}

    # ── Read-only guard: refuse to scaffold inside a read-only root ──
    from lib.project_mod.config import is_readonly_path
    if is_readonly_path(abs_path, conv_id=conv_id):
        return {'ok': False, 'action': 'create_project', 'path': abs_path,
                'error': (f'Refusing to create a project at {abs_path}: it is '
                          f'inside a READ-ONLY workspace root. Choose a '
                          f'writable location.')}

    # ── Create or verify directory ──
    already_existed = os.path.exists(abs_path)
    if already_existed:
        if not os.path.isdir(abs_path):
            return {'ok': False, 'action': 'create_project', 'path': abs_path,
                    'error': f'Path exists but is not a directory: {abs_path}'}
        try:
            has_entries = any(True for _ in os.scandir(abs_path))
        except OSError as e:
            logger.warning('[Project] create_project scandir failed %s: %s', abs_path, e)
            return {'ok': False, 'action': 'create_project', 'path': abs_path,
                    'error': f'Cannot inspect directory: {e}'}
        if has_entries and not overwrite:
            return {'ok': False, 'action': 'create_project', 'path': abs_path,
                    'error': (f'Directory exists and is not empty: {abs_path}. '
                              f'Set overwrite=true to register it as a workspace root anyway '
                              f'(existing files are NOT deleted).')}
        created = False
    else:
        try:
            os.makedirs(abs_path, exist_ok=True)
        except OSError as e:
            logger.error('[Project] create_project makedirs failed for %s: %s',
                         abs_path, e, exc_info=True)
            return {'ok': False, 'action': 'create_project', 'path': abs_path,
                    'error': f'Cannot create directory: {e}'}
        created = True

    # ── Register as extra root (never replace primary) ──
    # add_project_root auto-handles name collisions by appending a suffix
    # and is a no-op if an existing root already maps to the same path.
    try:
        add_project_root(abs_path, name=name)
    except Exception as e:
        logger.error('[Project] create_project: add_project_root failed for %s: %s',
                     abs_path, e, exc_info=True)
        # Don't try to rm_rf the directory we just made — user may want it.
        return {'ok': False, 'action': 'create_project', 'path': abs_path,
                'error': f'Directory ready but failed to register as workspace root: {e}'}

    # Look up the actually-assigned root name (may differ from `name` on collision).
    root_name = None
    with _lock:
        for rn, rs in _roots.items():
            if rs['path'] == abs_path:
                root_name = rn
                break
    if not root_name:
        # Shouldn't happen — add_project_root always adds or finds the entry.
        root_name = (name or os.path.basename(abs_path) or 'root')
        logger.warning('[Project] create_project: root lookup fell through for %s, '
                       'using fallback name %s', abs_path, root_name)

    audit_log('project_create',
              path=abs_path, root_name=root_name,
              created=created, overwrite=bool(overwrite),
              conv_id=conv_id, task_id=task_id)
    logger.info('[Project] create_project: path=%s root=%s created=%s overwrite=%s',
                abs_path, root_name, created, bool(overwrite))

    hint = (f'Use path prefix "{root_name}:<rel>" (e.g. '
            f'write_file(path=\'{root_name}:README.md\', ...)) or absolute paths '
            f'under {abs_path} for subsequent write operations.')
    msg = (f'{"Created" if created else "Registered existing directory"} "{abs_path}" '
           f'as workspace root "{root_name}". {hint}')

    return {
        'ok': True,
        'action': 'create_project',
        'path': abs_path,
        'rootName': root_name,
        'created': created,
        'overwrite': bool(overwrite),
        'message': msg,
    }


# ═══════════════════════════════════════════════════════
#  Fuzzy match helper
# ═══════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════
#  Absolute-path write safety
# ═══════════════════════════════════════════════════════

def _nearest_existing_dir(abs_path):
    """Return the deepest already-existing ancestor directory of *abs_path*.

    Walks up from the file's parent until an existing directory is found.
    Returns None only in the degenerate case where even the filesystem
    root doesn't exist (shouldn't happen on a sane system).
    """
    d = os.path.dirname(abs_path)
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:  # reached the root without finding anything
            return None
        d = parent
    return d or None


def _enforce_not_readonly(target, conv_id=None):
    """Raise :class:`ReadOnlyRootError` if *target* sits in a read-only root.

    The single guard every write/edit tool passes through (via
    ``_resolve_write_path``).  Resolution is conv-scoped so a concurrent
    task's root policy never leaks in.
    """
    from lib.project_mod.config import ReadOnlyRootError, is_readonly_path
    if is_readonly_path(target, conv_id=conv_id):
        raise ReadOnlyRootError(
            f'Refusing to write to {target}: it is inside a READ-ONLY '
            f'workspace root. This root was attached for reference only — '
            f'reads/greps are allowed but edits are not. Write to a '
            f'writable root instead (or ask the user to make this root '
            f'writable).')


def _resolve_write_path(base, rel_path, conv_id=None):
    """Return the on-disk target for a write/edit tool, accepting either
    a project-relative path or an absolute path.

    Symmetrically mirrors ``read_files`` which also accepts absolute paths.
    Resolution rules for an absolute path:

      1. If it already resolves INSIDE some registered root, use it directly.
      2. Otherwise, as long as the destination is NOT a forbidden system
         path (``/etc``, ``/usr``, ``$HOME`` itself, …), auto-register the
         deepest existing ancestor directory as an extra workspace root and
         allow the write.  This makes absolute-path writes "just work" the
         same way absolute-path reads already do — without forcing the model
         to call ``create_project`` first.
      3. Forbidden system paths are still rejected outright.

    Raises ``ValueError`` on rejection so callers can surface the error
    consistently with the existing ``_safe_path`` code path.
    """
    if rel_path and (rel_path.startswith('/') or rel_path.startswith('~')):
        abs_path = os.path.abspath(os.path.expanduser(rel_path))
        # Restricted (remote API) callers may only write inside an already
        # registered root — never auto-register a new one from tool input.
        # Enforced before the root scan + auto-register below so a remote
        # principal can neither escape the sandbox nor expand it.
        from lib.project_mod.abs_path_guard import enforce_abs_write
        enforce_abs_write(abs_path)
        # 1) Already under a registered root → use directly.
        with _lock:
            roots_snapshot = [rs['path'] for rs in _roots.values()]
        for root_path in roots_snapshot:
            norm_root = os.path.abspath(root_path).rstrip(os.sep) or root_path
            if abs_path == norm_root or abs_path.startswith(norm_root + os.sep):
                _enforce_not_readonly(abs_path, conv_id=conv_id)
                return abs_path

        # 1.5) Temp-dir scratch writes: allow the write but NEVER register a
        #      workspace root for it.  A write to /tmp/x.py is ephemeral
        #      scratch — auto-registering would invent a bogus ``tmp:`` project
        #      in the file-changes bar and undo history.  Returning the abs
        #      path WITHOUT add_project_root means _mod_attribution finds no
        #      containing root → the caller's _should_record_modification()
        #      skips the journal entry, matching run_command (which only
        #      snapshots base_path and so already ignores /tmp).
        if _is_temp_path(abs_path):
            logger.debug('[WriteTools] temp-dir scratch write (untracked, no root '
                         'registered): %s', abs_path)
            return abs_path

        # 3) Refuse system paths (same policy as create_project).
        if _is_forbidden_create_path(abs_path) or _is_forbidden_create_path(os.path.dirname(abs_path)):
            raise ValueError(
                f'Refusing to write to system path {abs_path}. '
                f'Choose a user-writable location.'
            )

        # 2) Auto-register the nearest existing ancestor as an extra root.
        anchor = _nearest_existing_dir(abs_path)
        if anchor and not _is_forbidden_create_path(anchor):
            try:
                # Detect whether this anchor is genuinely NEW (vs. an existing
                # root re-resolved) so we only signal real workspace expansion.
                with _lock:
                    _anchor_norm = os.path.abspath(anchor).rstrip(os.sep) or anchor
                    _existing = any(
                        (os.path.abspath(rs['path']).rstrip(os.sep) or rs['path']) == _anchor_norm
                        for rs in _roots.values()
                    )
                add_project_root(anchor)
                logger.info('[WriteTools] Auto-registered workspace root %s for '
                            'absolute-path write to %s', anchor, abs_path)
                if not _existing:
                    # Look up the assigned root name so the signal carries it.
                    _new_name = None
                    with _lock:
                        for rn, rs in _roots.items():
                            if (os.path.abspath(rs['path']).rstrip(os.sep) or rs['path']) == _anchor_norm:
                                _new_name = rn
                                break
                    _signal_root_added(_new_name or os.path.basename(anchor), anchor)
                _enforce_not_readonly(abs_path, conv_id=conv_id)
                return abs_path
            except Exception as e:
                logger.warning('[WriteTools] Auto-register of %s failed: %s', anchor, e)
                raise ValueError(
                    f'Cannot write to {abs_path}: failed to register a workspace '
                    f'root for it ({e}).'
                ) from e

        raise ValueError(
            f'Absolute path {abs_path} could not be resolved to a writable '
            f'workspace location. Use a "rootname:relative" prefix or call '
            f'create_project(path=...).'
        )
    target = _safe_path(base, rel_path)
    _enforce_not_readonly(target, conv_id=conv_id)
    return target


def _mod_attribution(target, base, rel_path):
    """Map a resolved on-disk ``target`` back to the workspace root that owns it.

    Returns ``(mod_base, mod_rel_path)`` for the modifications journal so that
    :func:`_record_modification` records the CORRECT root name and a clean
    root-relative path.

    Why this is needed: :func:`_resolve_write_path` accepts an absolute path
    that may live under an EXTRA workspace root (auto-registered on first
    absolute-path write).  In that case it returns the absolute target, but the
    caller still holds the PRIMARY ``base`` and the original (absolute)
    ``rel_path``.  Recording those verbatim makes the journal attribute the
    write to the primary root (e.g. ``chatui:``) and stores the full absolute
    path — which surfaces as a wrong ``rootname:`` prefix in the file-changes
    bar.  We re-derive attribution from the deepest matching registered root.

    For a non-absolute ``rel_path`` (the common case), attribution is already
    correct, so we return ``(base, rel_path)`` unchanged.
    """
    if not (rel_path and (rel_path.startswith('/') or rel_path.startswith('~'))):
        return base, rel_path
    try:
        abs_target = os.path.abspath(os.path.expanduser(target))
    except Exception as e:
        logger.debug('[WriteTools] _mod_attribution abspath failed for %s: %s', target, e)
        return base, rel_path
    best_path = None
    try:
        with _lock:
            roots_snapshot = [rs['path'] for rs in _roots.values()]
    except Exception as e:
        logger.debug('[WriteTools] _mod_attribution roots snapshot failed: %s', e)
        roots_snapshot = []
    for root_path in roots_snapshot:
        try:
            norm_root = os.path.abspath(root_path).rstrip(os.sep) or root_path
        except Exception:
            continue
        if abs_target == norm_root or abs_target.startswith(norm_root + os.sep):
            # Prefer the DEEPEST (longest) matching root so a nested extra
            # root wins over a parent root.
            if best_path is None or len(norm_root) > len(best_path):
                best_path = norm_root
    if best_path is None:
        return base, rel_path
    mod_rel = os.path.relpath(abs_target, best_path)
    return best_path, mod_rel


def _should_record_modification(target):
    """False when *target* is an OS temp-dir scratch write OUTSIDE all roots.

    Temp scratch writes are deliberately untracked: no undo journal entry and
    no file-changes-bar prefix (there is no registered root containing them).
    Mirrors run_command, which never snapshots files outside ``base_path``.

    A temp path that DOES fall under a registered root (e.g. the user
    legitimately opened a project under ``/tmp/proj``) is still tracked — the
    skip applies only to true out-of-workspace scratch.
    """
    try:
        abs_target = os.path.abspath(os.path.expanduser(target))
    except Exception as e:
        logger.debug('[WriteTools] _should_record_modification abspath failed for %s: %s',
                     target, e)
        return True
    if not _is_temp_path(abs_target):
        return True
    # Temp path — record only if it sits inside a registered workspace root.
    with _lock:
        roots_snapshot = [rs['path'] for rs in _roots.values()]
    for root_path in roots_snapshot:
        norm_root = os.path.abspath(root_path).rstrip(os.sep) or root_path
        if abs_target == norm_root or abs_target.startswith(norm_root + os.sep):
            return True
    return False


# Match \uXXXX, \UXXXXXXXX and \xXX escape sequences (literal backslash form).
_UNICODE_ESCAPE_RE = re.compile(r'\\U[0-9a-fA-F]{8}|\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}')


def _decode_unicode_escapes(s):
    """Decode literal ``\\uXXXX`` / ``\\UXXXXXXXX`` / ``\\xXX`` escapes to the
    characters they denote, leaving all other text untouched.

    Models frequently emit a real glyph (e.g. ``⏰``, em-dash ``—``) in an
    ``apply_diff`` search where the file on disk holds the literal escape text
    (``\\u23f0``, ``\\u2014``) — or the reverse. Decoding both sides before
    comparison lets the matcher see through this representation drift. Only the
    three numeric-escape forms are decoded; ``\\n`` / ``\\t`` and other C-style
    escapes are deliberately left alone to avoid surprising false matches.
    """
    def _repl(m):
        try:
            return chr(int(m.group(0)[2:], 16))
        except (ValueError, OverflowError) as e:
            logger.debug('[write_tools] undecodable unicode escape %r: %s', m.group(0), e)
            return m.group(0)
    return _UNICODE_ESCAPE_RE.sub(_repl, s)


def _find_closest_match(content, search, threshold=0.6):
    """Find the most similar block in content to the search string."""
    search_lines = search.split('\n')
    n = len(search_lines)
    if n == 0 or not content.strip():
        return None

    content_lines = content.split('\n')
    if len(content_lines) < n:
        return None

    best_ratio = 0.0
    best_start = 0

    search_first_stripped = search_lines[0].strip()[:40]
    search_last_stripped = search_lines[-1].strip()[:40] if n > 1 else search_first_stripped
    candidate_starts = set()
    for i, line in enumerate(content_lines):
        ls = line.strip()
        if (search_first_stripped and search_first_stripped in ls) or \
           (search_last_stripped and search_last_stripped in ls):
            for offset in range(max(0, i - n + 1), min(len(content_lines) - n + 1, i + 1)):
                candidate_starts.add(offset)

    if not candidate_starts:
        candidate_starts = set(range(0, len(content_lines) - n + 1, max(1, (len(content_lines) - n) // 500 + 1)))

    for start in candidate_starts:
        window = '\n'.join(content_lines[start:start + n])
        ratio = SequenceMatcher(None, search, window, autojunk=False).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = start

    if best_ratio >= threshold:
        best_text = '\n'.join(content_lines[best_start:best_start + n])
        if len(best_text) > 600:
            best_text = best_text[:600] + '\n… (truncated)'
        return {
            'text': best_text,
            'line': best_start + 1,
            'similarity': best_ratio,
        }
    return None


def _describe_duplicate_matches(content, search, context=1, max_show=5):
    """Build a human/LLM-friendly listing of where *search* matches in *content*.

    For each occurrence, shows the 1-based line number plus a few lines of
    surrounding context so the caller can pick a unique anchor.

    Args:
        content: The file text the search was run against.
        search: The (already line-normalized) search block.
        context: Lines of context to show before/after each match.
        max_show: Cap on how many matches to render in detail.

    Returns:
        A formatted multi-line string, or '' if no line-aligned match exists.
    """
    search_lines = search.split('\n')
    n = len(search_lines)
    content_lines = content.split('\n')
    if n == 0 or len(content_lines) < n:
        return ''

    starts = [i for i in range(len(content_lines) - n + 1)
              if content_lines[i:i + n] == search_lines]
    if not starts:
        return ''

    parts = []
    for idx, start in enumerate(starts[:max_show], 1):
        lo = max(0, start - context)
        hi = min(len(content_lines), start + n + context)
        block = []
        for ln in range(lo, hi):
            marker = '>' if start <= ln < start + n else ' '
            block.append(f'{marker} {ln + 1}: {content_lines[ln]}')
        parts.append(f'Match {idx} (line {start + 1}):\n' + '\n'.join(block))

    out = '\n\n'.join(parts)
    if len(starts) > max_show:
        out += f'\n\n… and {len(starts) - max_show} more match(es).'
    return out


# ═══════════════════════════════════════════════════════
#  VS Code file-watcher nudge
# ═══════════════════════════════════════════════════════

def _touch_for_vscode(filepath):
    """Bump mtime to ensure VS Code's file watcher picks up external writes."""
    try:
        st = os.stat(filepath)
        new_mtime = st.st_mtime + 0.000001
        os.utime(filepath, (st.st_atime, new_mtime))
    except OSError as e:
        logger.debug('[WriteTools] Failed to bump mtime for VS Code watcher on %s: %s', filepath, e)


# ═══════════════════════════════════════════════════════
#  write_file
# ═══════════════════════════════════════════════════════

def tool_write_file(base, rel_path, content, description='', conv_id=None, task_id=None):
    """Write full content to a file. Creates parent dirs if needed.

    Accepts:
      * project-relative paths (sandboxed to *base*), and
      * absolute paths that resolve under a registered workspace root —
        useful for writing into directories created by ``create_project``.
    """
    try:
        target = _resolve_write_path(base, rel_path, conv_id=conv_id)
    except ValueError as e:
        logger.debug('[Tools] write_file path rejected %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'write_file', 'path': rel_path}

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
            return {'ok': False, 'error': f'Cannot create directory: {e}',
                    'action': 'write_file', 'path': rel_path}

    original_content = old_content if existed else None

    try:
        with open(target, 'w', newline='') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        _touch_for_vscode(target)
        new_lines = content.count('\n') + 1
        sz = len(content.encode('utf-8'))

        if _should_record_modification(target):
            _mod_base, _mod_rel = _mod_attribution(target, base, rel_path)
            _record_modification(_mod_base, 'write_file', _mod_rel, original_content,
                                 conv_id=conv_id, task_id=task_id)

        result = {
            'ok': True, 'action': 'write_file', 'path': rel_path,
            'created': not existed, 'bytesWritten': sz,
            'lines': new_lines, 'oldLines': old_lines if existed else None,
            'description': description,
        }
        logger.info('write_file: %s (%dL, %s) %s', rel_path, new_lines, _fmt_size(sz),
              '[created]' if not existed else '[updated from %dL]' % old_lines)
        return result
    except Exception as e:
        logger.error('[Tools] write_file failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'write_file', 'path': rel_path}


# ═══════════════════════════════════════════════════════
#  apply_diff / apply_diffs
# ═══════════════════════════════════════════════════════

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

    try:
        with open(target, 'w', newline='') as f:
            f.write(new_content)
            f.flush()
            os.fsync(f.fileno())
        _touch_for_vscode(target)
        old_lines = _orig_line_count
        new_lines = new_content.count('\n') + 1
        diff_lines = len(search.split('\n'))

        if _should_record_modification(target):
            _mod_base, _mod_rel = _mod_attribution(target, base, rel_path)
            _record_modification(_mod_base, 'apply_diff', _mod_rel,
                                 original_content=content,
                                 reverse_patch=reverse_patch,
                                 conv_id=conv_id, task_id=task_id)

        result = {
            'ok': True, 'action': 'apply_diff', 'path': rel_path,
            'linesChanged': diff_lines,
            'oldLines': old_lines, 'newLines': new_lines,
            'description': description,
        }
        if replace_all and count > 1:
            result['replacedCount'] = count
        logger.info('apply_diff: %s (%d lines changed, %dL → %dL%s)',
              rel_path, diff_lines, old_lines, new_lines,
              f', {count} replacements' if (replace_all and count > 1) else '')
        return result
    except Exception as e:
        logger.error('[Tools] apply_diff write failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'apply_diff', 'path': rel_path}


def tool_apply_diff(base, rel_path, search, replace, description='', conv_id=None, replace_all=False, task_id=None):
    """Apply a single search-and-replace edit (backward-compatible entry point)."""
    return _apply_one_diff(base, rel_path, search, replace, description, conv_id, replace_all=replace_all, task_id=task_id)


def _invalid_edit_entry_msg(i, edit):
    """Build an actionable FAIL line for a batch edit that isn't an object.

    The common cause is a model emitting the whole ``edits`` array as one
    escaped JSON *string* (often with unescaped inner quotes, so it can't be
    auto-parsed) — the harness then wraps that string into a single-element
    list, and each element is a str, not a dict. Tell the model exactly that
    so it re-emits a real array of objects instead of retrying blind.
    """
    if isinstance(edit, str):
        return (f'[{i}] FAIL Invalid edit entry: got a string, expected an '
                f'object with {{path, search, replace}}. The "edits" array '
                f'must be real JSON objects, not a single stringified-JSON '
                f'blob — re-send each edit as its own object.')
    return (f'[{i}] FAIL Invalid edit entry: expected an object with '
            f'{{path, search, replace}}, got {type(edit).__name__}.')


def tool_apply_diffs(base_path, edits, conv_id=None, task_id=None):
    """Apply multiple search-and-replace edits in one batch."""
    if not edits:
        return 'No edits provided.'

    MAX_EDITS = 30
    if len(edits) > MAX_EDITS:
        edits = edits[:MAX_EDITS]

    # Import _resolve_base here (from tools.py) to avoid circular import
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
        search = edit.get('search', '')
        replace = edit.get('replace', '')
        desc = edit.get('description', '')

        if not rp or not search:
            results.append(f'[{i}] FAIL Missing required field (path or search)')
            fail_count += 1
            continue

        ra = bool(edit.get('replace_all', False))

        try:
            bp, resolved_rp = _resolve_base(base_path, rp)
        except ValueError as _rve:
            logger.debug('[write_tools] tool_apply_diffs caught %s: %s', type(_rve).__name__, _rve)
            fail_count += 1
            results.append(f'[{i}] FAIL {rp}: {_rve}')
            continue
        result = _apply_one_diff(bp, resolved_rp, search, replace, desc, conv_id, replace_all=ra, task_id=task_id)

        if result['ok']:
            ok_count += 1
            extra = ''
            if result.get('replacedCount'):
                extra = f' [{result["replacedCount"]} occurrences]'
            results.append(
                f'[{i}] OK {result["path"]}: {result["linesChanged"]} lines changed '
                f'({result["oldLines"]}L → {result["newLines"]}L){extra}'
                + (f' — {desc}' if desc else '')
            )
        else:
            fail_count += 1
            results.append(f'[{i}] FAIL {rp}: {result["error"]}')

    header = f'Applied {ok_count}/{ok_count + fail_count} edits'
    if fail_count:
        header += f' ({fail_count} failed)'
    return header + '\n' + '\n'.join(results)


# ═══════════════════════════════════════════════════════
#  insert_content
# ═══════════════════════════════════════════════════════

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

    # ── Build new content ──
    anchor_idx = file_content.index(anchor)

    if position == 'before':
        # Insert content before the anchor
        # Ensure proper newline separation
        insert_text = content
        if not insert_text.endswith('\n'):
            insert_text += '\n'
        new_content = file_content[:anchor_idx] + insert_text + file_content[anchor_idx:]
    else:  # 'after'
        # Insert content after the anchor
        after_idx = anchor_idx + len(anchor)
        insert_text = content
        # Ensure a newline between anchor and inserted content
        if after_idx < len(file_content) and file_content[after_idx] != '\n':
            insert_text = '\n' + insert_text
        elif after_idx < len(file_content):
            # anchor ends, next char is \n — insert after that newline
            after_idx += 1
        if not insert_text.endswith('\n'):
            insert_text += '\n'
        new_content = file_content[:after_idx] + insert_text + file_content[after_idx:]

    # ── Build reverse patch for undo ──
    # For undo, we just need to remove the inserted content.
    # We can do this as an apply_diff-style reverse patch:
    # search = anchor + inserted content (or inserted content + anchor)
    # replace = anchor
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

    # ── Write ──
    try:
        with open(target, 'w', newline='') as f:
            f.write(new_content)
            f.flush()
            os.fsync(f.fileno())
        _touch_for_vscode(target)
        old_lines = file_content.count('\n') + 1
        new_lines = new_content.count('\n') + 1
        inserted_lines = content.count('\n') + 1

        if _should_record_modification(target):
            _mod_base, _mod_rel = _mod_attribution(target, base, rel_path)
            _record_modification(_mod_base, 'apply_diff', _mod_rel,
                                 original_content=file_content,
                                 reverse_patch=reverse_patch,
                                 conv_id=conv_id, task_id=task_id)

        # Calculate which line the insertion happened at
        anchor_line = file_content[:anchor_idx].count('\n') + 1

        result = {
            'ok': True, 'action': 'insert_content', 'path': rel_path,
            'position': position,
            'anchorLine': anchor_line,
            'linesInserted': inserted_lines,
            'oldLines': old_lines, 'newLines': new_lines,
            'description': description,
        }
        logger.info('insert_content: %s (%d lines inserted %s anchor at L%d, %dL → %dL)',
                     rel_path, inserted_lines, position, anchor_line,
                     old_lines, new_lines)
        return result
    except Exception as e:
        logger.error('[Tools] insert_content write failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'insert_content', 'path': rel_path}


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
            results.append(
                f'[{i}] OK {result["path"]}: {result["linesInserted"]} lines inserted '
                f'{result["position"]} anchor at L{result["anchorLine"]} '
                f'({result["oldLines"]}L → {result["newLines"]}L)'
                + (f' — {desc}' if desc else '')
            )
        else:
            fail_count += 1
            results.append(f'[{i}] FAIL {rp}: {result["error"]}')

    header = f'Inserted {ok_count}/{ok_count + fail_count} edits'
    if fail_count:
        header += f' ({fail_count} failed)'
    return header + '\n' + '\n'.join(results)
