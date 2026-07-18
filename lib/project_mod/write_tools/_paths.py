"""lib/project_mod/write_tools/_paths.py — the write-path CORE.

Extracted verbatim from the former flat ``write_tools.py``. This holds the
tightly-coupled cluster every write/edit operation depends on: temp-dir
detection, the workspace-root auto-registration signal, ``create_project``, and
the path-resolution / modification-attribution helpers. These functions call
one another densely (``_resolve_write_path`` → ``_enforce_not_readonly`` /
``_is_temp_path`` / ``_signal_root_added`` / ``_nearest_existing_dir``), so they
stay in ONE module; ``_ops`` imports them from here.

NOTE (test seam): ``_temp_roots`` caches on its own function object
(``_temp_roots._cache``); ``test_temp_write_and_root_signal`` /
``test_project_bg_write_no_global_root_leak`` patch that attribute. The package
facade re-exports the SAME function object, so ``wt._temp_roots._cache = …``
still steers ``_is_temp_path`` here.
"""

import os
import tempfile
import threading

from lib.log import audit_log, get_logger
from lib.project_mod.config import _lock, _roots, _state
from lib.project_mod.scanner import _safe_path, add_project_root

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


def _save_model_added_root_to_recent(abs_path):
    """Persist a model-registered workspace root into the recent-projects list.

    The interactive UI already saves every folder the user opens (see
    static/js/project.js), but a root the ASSISTANT registers itself —
    ``create_project`` or the absolute-path-write auto-register (§2 of
    _resolve_write_path) — never went through that path, so it never appeared
    in "recent". Save it server-side here so it shows up regardless of whether
    the emitting conversation is the active one (the frontend
    ``workspace_root_added`` handler only refreshes for the ACTIVE conv).

    Temp-dir scratch paths are skipped — they are ephemeral and must not
    pollute the recent list (mirrors the untracked-root policy). Best-effort:
    a persistence failure is a debug-level, self-recovering fallback.
    """
    if not abs_path or _is_temp_path(abs_path):
        return
    try:
        from lib.project_mod.config import save_recent_project
        save_recent_project(abs_path)
    except Exception as e:
        logger.debug('[WriteTools] save_recent_project failed for %s: %s',
                     abs_path, e)


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

    _save_model_added_root_to_recent(abs_path)

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
                _anchor_norm = os.path.abspath(anchor).rstrip(os.sep) or anchor
                # ★ 2026-07-12 — scope the auto-register to the CALLER.
                #   A background run_task (conv_id present) MUST NOT mutate the
                #   process-global _state/_roots (committed 2026-06-22
                #   invariant): the global registry is the UI-facing "active
                #   project" every conversation's project bar reflects via
                #   get_state(), so a task's absolute-path write to a sibling
                #   repo used to spray that repo (e.g. tofu-search) onto every
                #   conv's bar. Register the anchor into THIS conv's OWN scoped
                #   registry only; the global registry stays reserved for the
                #   interactive / no-conv_id path (the human explicitly opening
                #   a folder). _mod_attribution / _should_record_modification are
                #   conv-scope-aware so the file-changes bar + undo journal
                #   still resolve the conv-scoped root correctly.
                if conv_id:
                    from lib.project_mod.config import (
                        add_conv_root, get_conv_roots, set_conv_roots,
                    )
                    _existing = any(
                        (os.path.abspath(rs['path']).rstrip(os.sep) or rs['path']) == _anchor_norm
                        for rs in get_conv_roots(conv_id).values()
                    )
                    _rname = add_conv_root(conv_id, anchor,
                                           name=os.path.basename(anchor))
                    if _rname is None:
                        # The conv owns no scoped registry yet. A background task
                        # MUST NOT fall back to writing the process-global _roots
                        # (that is the UI-facing "active project" every conv's
                        # bar reflects via get_state() — the exact leak this fix
                        # removes). Instead SEED a conv-scoped registry from the
                        # task's own primary (``base``), then register the anchor
                        # there. This keeps the auto-register isolated to the
                        # writing conv and never bleeds onto another conv's bar.
                        _base_abs = os.path.abspath(os.path.expanduser(base)) if base else ''
                        if _base_abs and os.path.isdir(_base_abs):
                            set_conv_roots(conv_id, _base_abs)
                            _rname = add_conv_root(conv_id, anchor,
                                                   name=os.path.basename(anchor))
                        if _rname is None:
                            # No usable primary to scope to (no base / vanished).
                            # Resolve the write directly without registering ANY
                            # root — degraded but never a global-registry mutation
                            # and never a hard failure. Attribution simply falls
                            # to no root (like a temp-dir write).
                            logger.warning('[WriteTools] conv=%s has no scoped '
                                           'registry and no usable primary; '
                                           'resolving %s without registering a '
                                           'root (no global pollution)',
                                           conv_id[:12], abs_path)
                            _enforce_not_readonly(abs_path, conv_id=conv_id)
                            return abs_path
                        logger.info('[WriteTools] seeded conv-scoped registry from '
                                    'primary %s and auto-registered root %s '
                                    '(conv=%s) for absolute-path write to %s',
                                    _base_abs, anchor, conv_id[:12], abs_path)
                    else:
                        logger.info('[WriteTools] Auto-registered conv-scoped workspace '
                                    'root %s (conv=%s) for absolute-path write to %s',
                                    anchor, conv_id[:12], abs_path)
                    if not _existing:
                        _signal_root_added(_rname or os.path.basename(anchor), anchor)
                else:
                    # Interactive / no-task path: the human explicitly drove this
                    # write, so expanding the shared UI workspace is correct.
                    _new_name = None
                    with _lock:
                        for rn, rs in _roots.items():
                            if (os.path.abspath(rs['path']).rstrip(os.sep) or rs['path']) == _anchor_norm:
                                _new_name = rn
                                break
                    # ★ If the anchor is NOT already a registered root, actually
                    #   REGISTER it globally so the interactive absolute-path
                    #   write expands the shared workspace (the project bar's
                    #   extraRoots). Gated on ``not conv_id`` so a background
                    #   task can never reach here and pollute the global registry
                    #   (that path is handled above). Without this the new root
                    #   was only *signalled* to the UI but never populated into
                    #   get_state().extraRoots.
                    if _new_name is None and not conv_id:
                        try:
                            _info = add_project_root(anchor,
                                                     name=os.path.basename(anchor))
                            # add_project_root dedups the name on collision; find
                            # the name it actually assigned for this anchor.
                            with _lock:
                                for rn, rs in _roots.items():
                                    if (os.path.abspath(rs['path']).rstrip(os.sep)
                                            or rs['path']) == _anchor_norm:
                                        _new_name = rn
                                        break
                        except Exception as e:
                            logger.warning('[WriteTools] global add_project_root '
                                           'failed for %s: %s', anchor, e)
                    # ★ Also register the new root into THIS conversation's own
                    #   scoped registry (not just the global _roots). Without
                    #   this, a subsequent ``newroot:rel/path`` namespaced write
                    #   in the SAME task hits the conv-scoped resolver, which —
                    #   per the 2026-05-05 isolation fix — does NOT fall through
                    #   to the global registry and raises UnknownWorkspaceRootError
                    #   for a root that only landed in the global one. add_conv_root
                    #   is a no-op when the conv has no registry (background task
                    #   must not conjure one) and never touches other convs.
                    if conv_id:
                        try:
                            from lib.project_mod.config import add_conv_root
                            add_conv_root(conv_id, anchor,
                                          name=_new_name or os.path.basename(anchor))
                        except Exception as e:
                            logger.debug('[WriteTools] add_conv_root failed conv=%s '
                                         'anchor=%s (non-fatal): %s',
                                         conv_id[:12] if conv_id else '?', anchor, e)
                    _signal_root_added(_new_name or os.path.basename(anchor), anchor)
                    _save_model_added_root_to_recent(anchor)
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


def _mod_attribution(target, base, rel_path, conv_id=None):
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
        if conv_id:
            from lib.project_mod.config import get_conv_roots
            roots_snapshot = [rs['path'] for rs in get_conv_roots(conv_id).values()]
        else:
            with _lock:
                roots_snapshot = [rs['path'] for rs in _roots.values()]
    except Exception as e:
        logger.debug('[WriteTools] _mod_attribution roots snapshot failed: %s', e)
        roots_snapshot = []
    for root_path in roots_snapshot:
        try:
            norm_root = os.path.abspath(root_path).rstrip(os.sep) or root_path
        except Exception as e:
            logger.debug('[WriteTools] _mod_attribution root normalize failed for %r: %s',
                         root_path, e)
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


def _should_record_modification(target, conv_id=None):
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
    if conv_id:
        from lib.project_mod.config import get_conv_roots
        roots_snapshot = [rs['path'] for rs in get_conv_roots(conv_id).values()]
    else:
        with _lock:
            roots_snapshot = [rs['path'] for rs in _roots.values()]
    for root_path in roots_snapshot:
        norm_root = os.path.abspath(root_path).rstrip(os.sep) or root_path
        if abs_target == norm_root or abs_target.startswith(norm_root + os.sep):
            return True
    return False
