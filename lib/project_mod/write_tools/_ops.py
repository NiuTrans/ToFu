"""lib/project_mod/write_tools/_ops.py — the write/edit OPERATIONS.

Extracted verbatim from the former flat ``write_tools.py``: the public tool
entry points (``tool_write_file`` / ``save_uploaded_file`` / ``tool_apply_diff``
/ ``tool_apply_diffs`` / ``tool_insert_content`` / ``tool_insert_contents``) plus
their single-edit cores (``_apply_one_diff`` / ``_insert_one``). Every operation
resolves + attributes through the ``_paths`` core and matches through the
``_text`` helpers.
"""
from lib.project_mod.write_intents import recovery_file_bytes as recovery_file_bytes

import os
import tempfile
import time
import ast
from difflib import SequenceMatcher

from lib.log import get_logger
from lib.tools.execution_timing import timing_phase
from lib.project_mod.modifications import _record_modification
from lib.project_mod.session_lock import SessionLockBusy
from lib.project_mod.path_resolution import _resolve_base
from lib.project_mod.scanner import _fmt_size

from . import _post_edit_checks
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
    _reindent_block,
    _uniform_indent_offset,
    _touch_for_vscode,
)

logger = get_logger(__name__)


def _with_workspace_writer(function):
    """Lease before preimage reads; retain existing path/auth/result policy."""
    import functools
    import inspect
    from contextlib import ExitStack
    from lib.project_mod.post_commit import defer_publication
    from lib.project_mod.workspace_lease import (
        WorkspaceBusyError, workspace_claims_lease,
    )

    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs).arguments
        base = bound.get('base', bound.get('base_path'))
        edits = bound.get('edits')
        # An edit may take its file from the call-level `path`; the lease must
        # claim the file this call will actually write.
        batch_path = bound.get('batch_path')
        paths = ([(edit.get('path') or batch_path)
                  for edit in edits if isinstance(edit, dict)]
                 if isinstance(edits, list) else [bound.get('rel_path')])
        targets = set()
        lexical_targets = {}


        def changed_precondition(kind, message):
            if edits is not None:
                return f'Error [{kind}]: {message}. No edits applied.'
            return {'ok': False, 'error': message, 'error_kind': kind,
                    'retryable': True, 'path': bound.get('rel_path', ''),
                    'mutationApplied': False}

        for path in paths:
            if not isinstance(path, str) or not path:
                continue
            try:
                if edits is not None:
                    resolution_conv = (None if function.__name__ in ('tool_apply_diffs', 'tool_insert_contents')
                                       else bound.get('conv_id'))
                    root, relative = _resolve_base(base, path, conv_id=resolution_conv)
                else:
                    root, relative = base, path
                lexical = os.path.abspath(os.path.join(root, os.path.expanduser(relative)))
                target = os.path.realpath(lexical)
                _enforce_not_readonly(target, conv_id=bound.get('conv_id'))
            except (TypeError, ValueError):
                # Invalid/readonly entries retain the original per-edit refusal.
                continue
            targets.add(target)
            lexical_targets[lexical] = target
        with defer_publication(), ExitStack() as stack:
            try:
                from lib.project_mod.tool_admission import admission_timeout, cancellation
                cancel = cancellation()
                wait_timeout = admission_timeout()
                with timing_phase('workspace_admission'):
                    claims = [('file', target) for target in targets]
                    stack.enter_context(workspace_claims_lease(claims,
                        timeout=wait_timeout, cancel_event=cancel))
                # Admission may have waited behind a real writer. Revalidate
                # the exact previously-read content before reading a preimage.
                for lexical, target in lexical_targets.items():
                    if os.path.realpath(lexical) != target:
                        return changed_precondition('workspace-target-changed',
                            'File target changed during write admission; '
                            're-resolve and re-read the target before retrying: ' + lexical)
                from lib import write_freshness
                # Only append consumes the current preimage rather than the
                # caller's observed replacement version. Keep admission and
                # target revalidation; skip just the stale-token precondition.
                pure_append = (function.__name__ == 'tool_write_file'
                               and bound.get('append') is True)
                for target in targets:
                    if (not pure_append and bound.get('conv_id') and os.path.isfile(target)
                            and write_freshness.is_stale(bound['conv_id'], target)):
                        return changed_precondition('write-freshness-stale',
                            'File changed on disk; the observed content version differs; '
                            're-read and reconcile before retrying (waiting alone will not '
                            'refresh the read): ' + target)
            except WorkspaceBusyError as error:
                if edits is not None:
                    return f'Error [workspace-busy]: {error}. No edits applied; retry after the writer finishes.'
                return {'ok': False, 'error': str(error), 'error_kind': 'workspace-busy',
                        'retryable': True, 'path': bound.get('rel_path', '')}
            return function(*args, **kwargs)

    return wrapped


class _AppliedWriteFailure(OSError):
    """A replacement happened; retry/rollback must not be inferred from failure."""

    error_kind = 'write-durability-unconfirmed'


class _WriteDurabilityFailed(_AppliedWriteFailure):
    def __init__(self):
        super().__init__(
            'File change was already applied, but directory synchronization failed; '
            'crash durability is unconfirmed. Do not retry the mutation automatically; '
            'read the current file and resolve the storage error first.')


class _UndoRecordingFailed(_AppliedWriteFailure):
    """The file changed, but its required undo journal did not commit."""

    error_kind = 'undo-record-failed'

    def __init__(self):
        super().__init__(
            'File change was already applied, but its undo record could not be confirmed. '
            'Do not retry the mutation automatically; read the current file and '
            'repair journal storage before making further changes. Automatic undo is not '
            'guaranteed for this change.')


def _record_modification_checked(*args, **kwargs):
    try:
        recorded = _record_modification(*args, **kwargs)
    except Exception as exc:
        raise _UndoRecordingFailed() from exc
    if recorded is not True:
        raise _UndoRecordingFailed()


def _applied_write_failure(action, path, error):
    result = {'ok': False, 'action': action, 'path': path, 'error': str(error),
              'errorKind': error.error_kind, 'mutationApplied': True,
              'undoRecorded': False}
    if isinstance(error, _WriteDurabilityFailed):
        result['durabilityConfirmed'] = False
    return result


def _journal_busy_failure(action, path, error):
    from lib.project_mod.write_intents import contract
    admission = contract()['x-admission']
    return {'ok': False, 'action': action, 'path': path, 'error': str(error),
            'errorKind': admission['busyErrorKind'],
            'mutationApplied': admission['busyMutationApplied'],
            'retryable': admission['busyRetryable'],
            'waitedSeconds': round(error.waited_seconds, 3)}


# ── Edit-failure categories ──
# Every edit-surface failure carries one of these kinds (``errorKind`` on the
# result dict) and the kind is rendered as a bracketed tag in the
# model-visible line — ``Diff failed [match-not-found]: …`` /
# ``[2] FAIL x.py [replace] [ambiguous-match]: …`` — so the model can tell at
# a glance WHICH repair move applies (re-read the file, widen the anchor,
# fix the path, …) instead of parsing prose. The tag never changes the
# ``Write failed`` / ``Diff failed`` / ``Insert failed`` prefixes or the
# ``[N] FAIL`` batch marker: the read-gate success probe and the meta parser
# contract on those.
EK_MATCH_NOT_FOUND = 'match-not-found'   # search/anchor text absent from the file
EK_AMBIGUOUS_MATCH = 'ambiguous-match'   # text matches more than one location
EK_FILE_NOT_FOUND = 'file-not-found'     # target file does not exist
EK_PATH_INVALID = 'path-invalid'         # path rejected (outside roots, readonly, …)
EK_READ_FAILED = 'read-failed'           # file exists but could not be read
EK_WRITE_FAILED = 'write-failed'         # computed edit could not be persisted
EK_ECHO_CONTENT = 'echo-content'         # insert content merely repeats anchor/context
EK_MAPPING_FAILED = 'mapping-failed'     # normalized match lost its line mapping
EK_INVALID_EDIT = 'invalid-edit'         # batch entry malformed (fields/operation)


def _fail_tag(result):
    """Bracketed error-kind tag for one failure line ('' when unclassified)."""
    kind = result.get('errorKind') if isinstance(result, dict) else None
    return f' [{kind}]' if kind else ''


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

def _create_atomic_temp(parent, *, new_file):
    """Exclusively create a bounded random staging name; kernel applies umask."""
    import secrets
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    for _ in range(32):
        path = os.path.join(parent, '.tofu_atomic_' + secrets.token_hex(16))
        try:
            descriptor = os.open(path, flags, 0o666 if new_file else 0o600)
        except FileExistsError:
            continue
        return descriptor, path
    raise FileExistsError('Atomic write staging name allocation exhausted')


def _atomic_write_bytes(target, data, *, before_replace=None, after_replace=None):
    """Write *data* (bytes) to *target* atomically (tmp file + os.replace).

    Preserves the target's permission bits when it already exists; new files
    get 0o666 & ~umask (matching plain open(..., 'w')). A symlinked target is
    written THROUGH to its referent — os.replace would otherwise replace the
    link itself, changing the historic write-through behaviour.

    Failures before replacement preserve the original. A failed directory
    flush after replacement raises _WriteDurabilityFailed: new content is
    already visible, but crash durability is unconfirmed.
    """
    import stat as _stat

    phys = os.path.realpath(target) if os.path.islink(target) else target
    try:
        mode = _stat.S_IMODE(os.stat(phys).st_mode)
    except OSError as _e:
        logger.debug('atomic write bytes: unreadable (%s)', _e)
        mode = None  # new file (or unreadable stat) → umask default below

    parent = os.path.dirname(phys) or '.'
    directory = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    tmp = None
    try:
        fd, tmp = _create_atomic_temp(parent, new_file=mode is None)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            if mode is not None:
                os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        if before_replace is not None:
            before_replace(tmp)
        os.replace(tmp, phys)
        tmp = None
        if after_replace is not None:
            after_replace()
        try:
            os.fsync(directory)
        except OSError as exc:
            logger.error('[WriteTools] directory sync failed after replacing %s', phys, exc_info=True)
            raise _WriteDurabilityFailed() from exc
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError as exc:
                logger.debug('atomic write temporary cleanup failed: %s', exc)
        os.close(directory)


def _atomic_write_text(target, text, *, before_replace=None, after_replace=None):
    """Text flavour of _atomic_write_bytes (UTF-8, newline-verbatim)."""
    if before_replace is None and after_replace is None:
        _atomic_write_bytes(target, text.encode('utf-8'))
    else:
        _atomic_write_bytes(target, text.encode('utf-8'),
                            before_replace=before_replace, after_replace=after_replace)


def _write_destination(target, base, rel_path, *, conv_id=None, attribution=None):
    """Resolve the existing undo authority before admission or publication."""
    from lib.project_mod import modifications as mods
    from lib.project_mod.write_intents import IntentError, validate_relative_path

    mod_base, mod_rel = attribution or _mod_attribution(target, base, rel_path, conv_id=conv_id)
    mod_base = os.path.abspath(mod_base)
    physical_root = os.path.realpath(mod_base)
    physical = os.path.realpath(target)
    if os.path.commonpath([physical_root, physical]) != physical_root:
        # Retain the existing same-repository checkout admission; attribution
        # remains owned by _paths rather than this batch planner.
        from lib.project_mod.write_tools._paths import (
            _git_common_dir, _is_same_repo_worktree, _nearest_existing_dir)
        anchor = _nearest_existing_dir(physical)
        worktree_ok = (anchor is not None
                       and _is_same_repo_worktree(physical, anchor, [physical_root])
                       and _git_common_dir(anchor) is not None
                       and _git_common_dir(physical_root) == _git_common_dir(anchor))
        if not worktree_ok:
            raise IntentError('Prepared write target escapes its attributed root')
    mod_rel = validate_relative_path(os.path.relpath(physical, physical_root))
    return mod_base, mod_rel, mods._get_session_dir(mod_base)


def _write_with_undo(target, content, base, rel_path, original_content, *,
                     mod_type='write_file', reverse_patch=None, conv_id=None,
                     task_id=None, tracked=None, attribution=None):
    """One admitted prepare -> replace -> durable journal -> acknowledgement."""
    from lib.project_mod import modifications as mods
    from lib.project_mod.write_intents import IntentError, locked_intents
    from lib.project_mod.session_lock import locked_session
    from lib.project_mod.write_recovery import recover_locked, post_image
    writer = _atomic_write_text if isinstance(content, str) else _atomic_write_bytes
    tracked = (_should_record_modification(target, conv_id=conv_id)
               if tracked is None else tracked)
    if not tracked:
        writer(target, content)
        return
    data = content.encode('utf-8') if isinstance(content, str) else bytes(content)
    original = (original_content.encode('utf-8') if isinstance(original_content, str)
                else original_content)
    mod_base, mod_rel, session_dir = _write_destination(
        target, base, rel_path, conv_id=conv_id, attribution=attribution)
    with locked_session(session_dir) as state, locked_intents(session_dir) as store:
        if state.get('writing') or state.get('recovering'):
            raise IntentError('Nested project write during journal publication is not allowed')
        state['recovering'] = True
        try:
            recover_locked(mod_base, store)
        finally:
            state.pop('recovering', None)
        # Reject a corrupt/unreadable undo authority before preparing or replacing
        # any user bytes. Prefer an identity-validated cached snapshot (copy) so a
        # repeated writer's critical section does not re-read the full history;
        # only a cache miss pays for an authoritative disk read. A cached entry
        # exists only after a confirmed durable flush (_publish_cache), so a torn
        # or corrupt journal forces the fail-loud disk read below and never
        # persists as reusable state. The snapshot is consumed by the one RMW
        # below while this same admission remains held (never a stale projection).
        # Validate the undo authority before replacing user bytes, without
        # paying a full-history parse per edit. Rows materialized here are handed
        # to this admission's commit, so a fold never re-reads the same journal.
        state['preparedJournal'] = mods._assert_journal_ready(session_dir)
        state['writing'] = True
        record = None
        published = False
        def remember(intent):
            nonlocal record
            record = intent
        def prepare(stage):
            from lib.project_mod.tool_admission import journal_conv_id
            prepared = store.prepare(base_path=mod_base, path=mod_rel, original=original,
                          post_content=data, stage_name=os.path.basename(stage),
                          conv_id=journal_conv_id(conv_id), task_id=task_id, on_intent=remember)
            from lib.project_mod.effect_reconciliation import bind_prepared_write
            bind_prepared_write(prepared)
        def applied():
            nonlocal published
            published = True
        try:
            with timing_phase('file_publish', bytes_written=len(data)):
                writer(target, content, before_replace=prepare, after_replace=applied)
            _record_modification_checked(mod_base, mod_type, mod_rel,
                original_content=original_content, reverse_patch=reverse_patch,
                conv_id=conv_id, task_id=task_id, change_id=record['changeId'],
                post_image=post_image(record))
            try:
                store.acknowledge(record)
            except OSError:
                # The undo record is already durable. Retain the intent for an
                # idempotent cleanup on the next access, never repeat the write.
                logger.warning('Undo committed; prepared write cleanup remains pending', exc_info=True)
        except Exception as exc:
            if not published and record is not None:
                try:
                    store.discard_aborted(store.abort(record))
                except OSError:
                    logger.warning('Unpublished write recovery evidence retained', exc_info=True)
            if published and not isinstance(exc, _AppliedWriteFailure):
                raise _UndoRecordingFailed() from exc
            raise
        finally:
            state.pop('writing', None)
            state.pop('preparedJournal', None)


def _read_original_text(target):
    """Read the complete source before strict UTF-8 decoding; preserve newline bytes."""
    with open(target, 'rb') as handle:
        data = handle.read()
    return data.decode('utf-8')


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

@_with_workspace_writer
def tool_write_file(base, rel_path, content, description='', conv_id=None, task_id=None,
                    allow_outside=False, append=False):
    """Write full content to a file. Creates parent dirs if needed.

    Accepts:
      * project-relative paths (sandboxed to *base*), and
      * absolute paths that resolve under a registered workspace root —
        including roots auto-registered by an earlier absolute-path write.

    ``append=True`` appends ``content`` to the existing file instead of
    replacing it (a missing file is created with just ``content``).
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
        target = _resolve_write_path(base, rel_path, conv_id=conv_id,
                                     allow_outside=allow_outside)
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
    original_content = None
    if existed:
        try:
            with open(target, 'rb') as f:
                original_bytes = f.read()
            old_lines = original_bytes.count(b'\n') + 1
            try:
                old_content = original_bytes.decode('utf-8')
                # UTF-8 round-trips exactly, including CR/LF and BOM. Keep
                # normal text history in its existing compact representation.
                original_content = old_content
            except UnicodeDecodeError:
                if append:
                    raise  # Appending text must not rewrite undecodable bytes.
                original_content = original_bytes
                old_content = None  # No lossy text is used as undo evidence.
            del original_bytes
        except Exception as e:
            logger.warning('[Tools] write_file pre-image read failed for %s: %s', rel_path, e)
            return {'ok': False, 'action': 'write_file', 'path': rel_path,
                    'errorKind': EK_READ_FAILED, 'mutationApplied': False,
                    'error': f'Cannot read original file safely; no write was performed: {e}'}

    parent = os.path.dirname(target)
    if parent:
        try:
            from lib.project_mod.modifications import _ensure_durable_session_directory
            _ensure_durable_session_directory(parent)
        except Exception as e:
            logger.warning('[Tools] makedirs failed for parent of %s: %s', rel_path, e, exc_info=True)
            return {'ok': False, 'action': 'write_file', 'path': rel_path,
                    'error': (f'Cannot create directory: {e}'
                              + _salvage_note(content, rel_path))}

    final_content = (old_content + content) if (append and old_content) else content

    try:
        _write_with_undo(target, final_content, base, rel_path, original_content,
                         conv_id=conv_id, task_id=task_id)
        _touch_for_vscode(target)
        _record_write_freshness(conv_id, target, final_content.encode('utf-8'))
        new_lines = final_content.count('\n') + 1
        sz = len(final_content.encode('utf-8'))

        result = {
            'ok': True, 'action': 'write_file', 'path': rel_path,
            'absolutePath': os.path.abspath(target),
            'created': not existed, 'bytesWritten': sz,
            'lines': new_lines, 'oldLines': old_lines if existed else None,
            'description': description,
        }
        if append:
            result['appended'] = True
            result['appendedBytes'] = len(content.encode('utf-8'))
        _guard = _post_edit_guard(rel_path, old_content, final_content,
                                  abs_path=target)
        if _guard:
            result['syntaxWarning'] = _guard
            logger.warning('[SyntaxGuard] %s', _guard)
        logger.info('write_file: %s (%dL, %s) %s%s', rel_path, new_lines, _fmt_size(sz),
              '[created]' if not existed else '[updated from %dL]' % old_lines,
              ' [append]' if append else '')
        return result
    except SessionLockBusy as e:
        return _journal_busy_failure('write_file', rel_path, e)
    except _AppliedWriteFailure as e:
        return _applied_write_failure('write_file', rel_path, e)
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


@_with_workspace_writer
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
    if parent:
        try:
            from lib.project_mod.modifications import _ensure_durable_session_directory
            _ensure_durable_session_directory(parent)
        except Exception as e:
            logger.warning('[Tools] upload makedirs failed for parent of %s: %s', rel_path, e, exc_info=True)
            return {'ok': False, 'error': f'Cannot create directory: {e}',
                    'action': 'upload_file', 'path': rel_path}

    renamed = False
    if os.path.isfile(target) and on_conflict == 'rename':
        new_target = _dedupe_target(target)
        renamed = new_target != target
        target = new_target
    return _publish_uploaded_target(base, target, data, original_path=rel_path,
        description=description, conv_id=conv_id, task_id=task_id,
        is_abs=is_abs, renamed=renamed, on_conflict=on_conflict)


@_with_workspace_writer
def _publish_uploaded_target(base, rel_path, data, *, original_path,
                             description, conv_id, task_id, is_abs,
                             renamed, on_conflict):
    """Revalidate the actual upload name under its own canonical file lease."""
    target = rel_path
    if renamed and on_conflict == 'rename' and os.path.lexists(target):
        return {'ok': False, 'action': 'upload_file', 'path': target,
                'error_kind': 'workspace-busy', 'retryable': True,
                'mutationApplied': False,
                'error': 'Upload rename destination became occupied; no upload was applied. '
                         'Retry to choose a new available filename: ' + target}
    original_content = None
    existed = os.path.isfile(target)
    if existed:  # overwrite path — capture pre-image so undo restores bytes
        try:
            with open(target, 'rb') as f:
                original_content = f.read()
        except Exception as e:
            logger.debug('[Tools] upload pre-image read failed for %s: %s', target, e)
            return {'ok': False, 'action': 'upload_file', 'path': rel_path,
                    'errorKind': EK_READ_FAILED, 'mutationApplied': False,
                    'error': f'Cannot read original file safely; no write was performed: {e}'}

    try:
        _write_with_undo(target, bytes(data), base, target if is_abs else original_path,
                         original_content, conv_id=conv_id, task_id=task_id)
        _touch_for_vscode(target)
        _record_write_freshness(conv_id, target, bytes(data))
        sz = len(data)

        logger.info('upload_file: %s (%s) %s', target, _fmt_size(sz),
                    '[created]' if not (existed and original_content is not None) else '[overwrote]')
        return {
            'ok': True, 'action': 'upload_file', 'path': target,
            'name': os.path.basename(target),
            'created': original_content is None,
            'renamed': renamed, 'bytesWritten': sz,
            'description': description,
        }
    except SessionLockBusy as e:
        return _journal_busy_failure('upload_file', rel_path, e)
    except _AppliedWriteFailure as e:
        return _applied_write_failure('upload_file', rel_path, e)
    except Exception as e:
        logger.error('[Tools] upload_file failed for %s: %s', target, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'upload_file', 'path': rel_path}


# ═══════════════════════════════════════════════════════
#  apply_diff / apply_diffs
# ═══════════════════════════════════════════════════════

def _indent_tolerant_splice(norm_content, norm_search, replace, replace_all,
                            *, decode, rel_path):
    """Uniform-indent-tolerant matching rung for ``_compute_diff``.

    The most common model drift after representation issues is a block emitted
    out-dented or over-indented relative to the file (aider's relative-indent
    finding: tolerating it is where most edit reliability lives). A match must
    preserve every nonblank line's relative indentation: exactly one literal
    leading prefix is added or removed across the entire block. The replacement
    receives that same offset, never an independent per-line deindent.

    Returns ``{'ok': True, ...}`` with the spliced content, ``{'ok': False,
    'errorKind': EK_AMBIGUOUS_MATCH, ...}`` on multiple matches, or
    ``{'ok': False}`` on a clean miss (the caller then tries the next rung or
    emits the not-found error). Never touches the filesystem.
    """
    if not norm_search.strip():
        return {'ok': False}

    def _form(line):
        if decode:
            line = _decode_unicode_escapes(line)
        return line.strip()

    content_lines = norm_content.split('\n')
    search_lines = norm_search.split('\n')
    n = len(search_lines)
    if n == 0 or len(content_lines) < n:
        return {'ok': False}
    cmp_content = [_form(l) for l in content_lines]
    cmp_search = [_form(l) for l in search_lines]
    offsets = {}
    for i in range(len(cmp_content) - n + 1):
        if cmp_content[i:i + n] != cmp_search:
            continue
        offset = _uniform_indent_offset(
            search_lines, content_lines[i:i + n], decode=decode)
        if offset is not None:
            offsets[i] = offset
    starts = list(offsets)
    if not starts:
        return {'ok': False}
    note = ('unicode-escape + indentation' if decode else 'indentation')
    if len(starts) > 1 and not replace_all:
        locs = _describe_duplicate_matches(
            '\n'.join(cmp_content), '\n'.join(cmp_search))
        error_msg = (f'Search text matches {len(starts)} locations (after {note} '
                     f'normalization). Make it more specific (add surrounding lines so it '
                     f'matches exactly once), or set replace_all=true to replace all occurrences.')
        if locs:
            error_msg += f'\n\n{locs}'
        return {'ok': False, 'action': 'apply_diff', 'path': rel_path,
                'error': error_msg, 'errorKind': EK_AMBIGUOUS_MATCH}
    replace_lines = replace.replace('\r\n', '\n').split('\n')
    work = list(content_lines)

    if any(removed and any(line.strip() and not line.startswith(removed)
                           for line in replace_lines)
           for removed, _added in offsets.values()):
        return {'ok': False}
    first_matched = None
    for start in reversed(starts):
        block = content_lines[start:start + n]
        if first_matched is None:
            first_matched = '\n'.join(block)
        removed, added = offsets[start]
        work[start:start + n] = _reindent_block(
            replace_lines, from_prefix=removed, to_prefix=added)
        if not replace_all:
            break
    return {'ok': True,
            'new_content': '\n'.join(work),
            # An exact original anchor keeps the inverse patch round-trippable;
            # multi-location splices fall back to the whole-file inverse below.
            'matched_search': first_matched if len(starts) == 1 else norm_search,
            'count': len(starts),
            'note': note}


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
                            'error': error_msg,
                            'errorKind': EK_AMBIGUOUS_MATCH}
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
                                'error': error_msg,
                                'errorKind': EK_AMBIGUOUS_MATCH}
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
                # ── Tier 5: uniform-indent (relative-indent) drift ──
                # The model often emits the block out-dented/over-indented
                # relative to the file (e.g. a method body at column 0). Match
                # one uniform leading-prefix offset without changing relative
                # indentation, then apply that same offset to the replacement.
                outcome = _indent_tolerant_splice(norm_content, norm_search,
                                                  replace, replace_all,
                                                  decode=False, rel_path=rel_path)
                if outcome['ok']:
                    content = outcome['new_content']
                    search = outcome['matched_search']
                    count = outcome['count']
                    _tw_replaced = True
                    logger.debug('apply_diff: indentation-normalized match in %s '
                                 '(%d locations)', rel_path, count)
                elif outcome.get('errorKind'):
                    return outcome

            if tw_count == 0 and not _tw_replaced:
                # ── Tier 6: unicode-escape + indent drift combined ──
                outcome = _indent_tolerant_splice(norm_content, norm_search,
                                                  replace, replace_all,
                                                  decode=True, rel_path=rel_path)
                if outcome['ok']:
                    content = outcome['new_content']
                    search = outcome['matched_search']
                    count = outcome['count']
                    _tw_replaced = True
                    logger.debug('apply_diff: unicode-escape+indentation '
                                 'normalized match in %s (%d locations)',
                                 rel_path, count)
                elif outcome.get('errorKind'):
                    return outcome

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
                    'errorKind': EK_MATCH_NOT_FOUND,
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
                'error': error_msg,
                'errorKind': EK_AMBIGUOUS_MATCH}

    if _tw_replaced:
        new_content = content
        _orig_line_count = norm_content.count('\n') + 1
    else:
        new_content = content.replace(search, replace) if replace_all else content.replace(search, replace, 1)
        _orig_line_count = content.count('\n') + 1

    reverse_patch = {'search': replace, 'replace': search}
    if new_content.replace(reverse_patch['search'], reverse_patch['replace'], 1) != pre_edit_text:
        # Matching normalization or an ambiguous inverse can change more than
        # the nominal anchor. Preserve a proven exact inverse for fallback undo.
        reverse_patch = {'search': new_content, 'replace': pre_edit_text}
    if replace_all and count > 1:
        reverse_patch['replace_all'] = True

    return {
        'ok': True,
        'new_content': new_content,
        'original_content': pre_edit_text,
        'pre_edit_text': pre_edit_text,
        'reverse_patch': reverse_patch,
        'search': search,
        'count': count,
        'replace_all': replace_all,
        'old_lines': _orig_line_count,
    }


def _diff_result_from_comp(rel_path, comp, description, *, syntax_guard=True,
                           abs_path=None):
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
    _guard = (_post_edit_guard(rel_path, comp['pre_edit_text'], new_content,
                               abs_path=abs_path)
              if syntax_guard else '')
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
        _write_with_undo(target, comp['new_content'], base, rel_path,
                         comp['original_content'], mod_type='apply_diff',
                         reverse_patch=comp['reverse_patch'], conv_id=conv_id, task_id=task_id)
        _touch_for_vscode(target)
        _record_write_freshness(conv_id, target, comp['new_content'].encode('utf-8'))
        return _diff_result_from_comp(rel_path, comp, description,
                                      abs_path=target)
    except SessionLockBusy as e:
        return _journal_busy_failure('apply_diff', rel_path, e)
    except _AppliedWriteFailure as e:
        return _applied_write_failure('apply_diff', rel_path, e)
    except Exception as e:
        logger.error('[Tools] apply_diff write failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'apply_diff', 'path': rel_path,
                'errorKind': EK_WRITE_FAILED}


def _apply_one_diff(base, rel_path, search, replace, description='', conv_id=None, replace_all=False, task_id=None,
                    allow_outside=False):
    """Apply a single search-and-replace to a file.

    Accepts project-relative paths and absolute paths under registered roots.
    """
    try:
        target = _resolve_write_path(base, rel_path, conv_id=conv_id,
                                     allow_outside=allow_outside)
    except ValueError as e:
        logger.debug('[Tools] apply_diff path rejected %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'apply_diff', 'path': rel_path,
                'errorKind': EK_PATH_INVALID}

    if not os.path.isfile(target):
        return {'ok': False, 'error': f'File not found: {rel_path}',
                'action': 'apply_diff', 'path': rel_path,
                'errorKind': EK_FILE_NOT_FOUND}

    try:
        content = _read_original_text(target)
    except Exception as e:
        logger.warning('[Tools] apply_diff read failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': f'Cannot read file: {e}',
                'action': 'apply_diff', 'path': rel_path,
                'errorKind': EK_READ_FAILED}

    comp = _compute_diff(content, rel_path, search, replace, replace_all=replace_all)
    if not comp['ok']:
        return comp
    return _commit_diff(target, base, rel_path, comp, description=description,
                        conv_id=conv_id, task_id=task_id)


@_with_workspace_writer
def tool_apply_diff(base, rel_path, search, replace, description='', conv_id=None, replace_all=False, task_id=None,
                    allow_outside=False):
    """Apply a single search-and-replace edit (backward-compatible entry point)."""
    result = _apply_one_diff(base, rel_path, search, replace, description, conv_id, replace_all=replace_all, task_id=task_id,
                             allow_outside=allow_outside)
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
        return (f'[{i}] FAIL [{EK_INVALID_EDIT}] Invalid edit entry: got a '
                f'string, expected an object with {expected}. The "edits" '
                f'array must be real JSON objects, not a single '
                f'stringified-JSON blob — re-send each edit as its own '
                f'object.')
    return (f'[{i}] FAIL [{EK_INVALID_EDIT}] Invalid edit entry: expected an '
            f'object with {expected}, got {type(edit).__name__}.')


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


def _commit_edit_group(target, base, recs, conv_id, task_id, *, write_options=None):
    """One physical replacement and one verifiable journal transition per file.

    Per-edit results remain ordered at the caller. Intermediate computations
    never existed on disk and must not mint separate physical post-images.
    """
    first = recs[0]
    final = recs[-1]['new_content']
    try:
        tracked = (_should_record_modification(target, conv_id=conv_id)
                   if write_options is None else write_options['tracked'])
        if tracked:
            _mod_base, _mod_rel = (write_options['attribution'] if write_options is not None
                                  else _mod_attribution(target, first['attribution_base'],
                                                        first['attribution_rel'], conv_id=conv_id))
            reverse_patch = first['reverse_patch']
            if len(recs) > 1:
                original = first['original_content']
                reverse_patch = {'search': final, 'replace': original}
    except Exception as exc:
        logger.warning('[Tools] batch undo evidence preparation failed for %s: %s',
                       target, exc)
        return f'Cannot prepare batch undo evidence; no write was performed: {exc}'
    try:
        _write_with_undo(target, final, first['attribution_base'], first['attribution_rel'],
                         first['original_content'], mod_type='apply_diff',
                         reverse_patch=reverse_patch if tracked else None,
                         conv_id=conv_id, task_id=task_id, tracked=tracked,
                         attribution=(_mod_base, _mod_rel) if tracked else None)
    except (SessionLockBusy, _AppliedWriteFailure) as error:
        return error
    except Exception as e:
        logger.error('[Tools] batch write failed for %s: %s', target, e, exc_info=True)
        return str(e)
    _touch_for_vscode(target)
    _record_write_freshness(conv_id, target, final.encode('utf-8'))
    return None


@_with_workspace_writer
def tool_apply_diffs(base_path, edits, conv_id=None, task_id=None,
                     allow_outside=False, atomic=False):
    """Apply multiple search-and-replace edits in one batch.

    Edits are grouped by resolved target: each file is read once, its diffs
    are applied in memory sequentially (edit N still sees the result of
    1..N-1), and the final bytes are written once — one atomic write, one
    freshness record and one advisory note per file instead of N.

    Default failure semantics isolate per edit: a failed edit never halts the
    batch and applied edits stay applied (each file's batch is one undo
    transition). With ``atomic=True`` the whole call is all-or-nothing: every
    edit is pre-flight computed first, and when ANY edit fails, no file is
    written at all — the report says what would have applied so the model can
    fix the failures and re-send the batch (aider's git-per-edit discipline,
    without the git dependency).
    """
    if not edits:
        return 'No edits provided.'

    from collections import OrderedDict

    ok_count = 0
    partial_count = 0
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
            strings[i] = (f'[{i}] FAIL [{EK_INVALID_EDIT}] '
                          'Missing required field (path or search)')
            fail_count += 1
            continue

        ra = bool(edit.get('replace_all', False))

        try:
            bp, resolved_rp = _resolve_base(base_path, rp)
        except ValueError as _rve:
            logger.debug('[write_tools] tool_apply_diffs caught %s: %s', type(_rve).__name__, _rve)
            fail_count += 1
            strings[i] = f'[{i}] FAIL {rp} [{EK_PATH_INVALID}]: {_rve}'
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
            target = _resolve_write_path(p['bp'], p['resolved_rp'], conv_id=conv_id,
                                         allow_outside=allow_outside)
        except ValueError as e:
            p['resolve_err'] = str(e)
            continue
        p['target'] = target
        groups.setdefault(target, []).append(p)

    # One read per file; compute every group's edits fully before any commit
    # so an atomic batch can be rejected without touching the filesystem.
    plans = []      # {'target', 'comps': [(op, comp|None, err_dict|None)], 'recs'}
    comp_failures = 0
    for target, ops in groups.items():
        if not os.path.isfile(target):
            for p in ops:
                _log_legacy_edit_efficiency(p['search'], p['replace'], ok=False)
                strings[p['i']] = (
                    f'[{p["i"]}] FAIL {p["rp"]} [{EK_FILE_NOT_FOUND}]: '
                    f'File not found: {p["resolved_rp"]}')
                fail_count += 1
            continue
        try:
            current = _read_original_text(target)
        except Exception as e:
            logger.warning('[Tools] apply_diffs read failed for %s: %s', target, e, exc_info=True)
            for p in ops:
                _log_legacy_edit_efficiency(p['search'], p['replace'], ok=False)
                strings[p['i']] = (
                    f'[{p["i"]}] FAIL {p["rp"]} [{EK_READ_FAILED}]: '
                    f'Cannot read file: {e}')
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
                comp_failures += 1
        plans.append({'target': target, 'comps': comps, 'recs': recs})

    resolve_failures = sum(1 for p in prepared if 'resolve_err' in p)
    blocked = atomic and (fail_count > 0 or comp_failures > 0
                          or resolve_failures > 0)

    for plan in plans:
        write_err = None
        if plan['recs'] and not blocked:
            write_err = _commit_edit_group(
                plan['target'], plan['recs'][0]['attribution_base'],
                plan['recs'], conv_id, task_id)

        for p, comp, err_dict in plan['comps']:
            if comp is not None:
                if blocked or write_err is None:
                    result = _diff_result_from_comp(p['resolved_rp'], comp, p['desc'],
                                                    abs_path=plan['target'])
                elif isinstance(write_err, SessionLockBusy):
                    result = _journal_busy_failure('apply_diff', p['resolved_rp'], write_err)
                elif isinstance(write_err, _AppliedWriteFailure):
                    result = _applied_write_failure('apply_diff', p['resolved_rp'], write_err)
                else:
                    result = {'ok': False, 'error': write_err,
                              'action': 'apply_diff', 'path': p['resolved_rp'],
                              'errorKind': EK_WRITE_FAILED}
            else:
                result = err_dict
            _log_legacy_edit_efficiency(p['search'], p['replace'],
                                        ok=bool(result['ok']) and not blocked)

            partial_count += int(bool(result.get('mutationApplied')))
            if result['ok']:
                ok_count += 1
                extra = ''
                if result.get('replacedCount'):
                    extra = f' [{result["replacedCount"]} occurrences]'
                strings[p['i']] = (
                    f'[{p["i"]}] OK {result["path"]}: {result["linesChanged"]} lines changed '
                    f'({result["oldLines"]}L → {result["newLines"]}L){extra}'
                    + (f' — {p["desc"]}' if p['desc'] else '')
                    + (' (would apply — batch rejected, nothing written)' if blocked else '')
                )
            else:
                fail_count += 1
                strings[p['i']] = (
                    f'[{p["i"]}] FAIL {p["rp"]}'
                    f'{_fail_tag(result)}: {result["error"]}')

    # Emit pre-fail and prepared results in the model's original edit order.
    for p in prepared:
        if 'resolve_err' in p:
            _log_legacy_edit_efficiency(p['search'], p['replace'], ok=False)
            strings[p['i']] = (
                f'[{p["i"]}] FAIL {p["rp"]}'
                f' [{EK_PATH_INVALID}]: {p["resolve_err"]}')
            fail_count += 1

    results = [strings[i] for i in range(1, len(edits) + 1)]
    if blocked:
        header = (f'Atomic batch rejected: {fail_count} of {ok_count + fail_count} '
                  'edits failed — NO files were modified. Fix the FAIL edits and '
                  're-send the whole batch, or re-send with atomic=false to apply '
                  'the edits that already match.')
    else:
        header = f'Applied {ok_count}/{ok_count + fail_count} edits'
        if fail_count:
            header += f' ({fail_count} failed)'
        if partial_count:
            header = header.replace('Applied ', 'Completed ', 1).replace('Inserted ', 'Completed ', 1)
            header += f'; {partial_count} edits were applied without confirmed undo'
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
    elif after_idx and file_content[after_idx - 1] != '\n':
        # An unterminated final line still needs a separator before the new
        # line. An anchor that already includes its final newline does not.
        it = '\n' + it
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


def _anchor_echo_lines(anchor, content, *, from_end):
    """Whole-line boundary copy of the complete, non-structural anchor.

    A partial overlap cannot prove duplication: independent blocks share
    returns, closing tags, braces and blank lines. Even a complete anchor made
    only of punctuation/whitespace is not identifying context. Preserve those
    bytes; deliberate duplication of an identifying anchor uses ``replace``.
    """
    anchor_lines = anchor.split('\n')
    body = content[:-1] if content.endswith('\n') else content
    content_lines = body.split('\n') if body else []
    count = len(anchor_lines)
    run = content_lines[-count:] if from_end else content_lines[:count]
    if run == anchor_lines and any(char.isalnum() for char in anchor):
        return count
    return 0


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
    """Repair one insert's whole-line anchor echo.

    Returns ``(content, stripped_chars, error)``. The anchor always stays, so a
    complete copy at the insertion edge is duplication and is stripped.
    Partial overlaps and structural-only anchors stay literal: equality of
    common syntax cannot establish that a new block is copied context.

    An echo on the FAR side is refused instead: content then says both "insert
    this next to the anchor" and "and here is the anchor again", which either
    means the block belongs on the other side (``insert_before``) or that a
    deliberate copy was intended (``operation=replace``). Picking one silently
    either duplicates the anchor or moves the anchor's own body into the
    inserted block, and both results parse.

    Whole-line anchors only — a mid-line overlap stays ambiguous and untouched.
    """
    if not content or not anchor:
        return content, 0, None
    after_idx = anchor_idx + len(anchor)
    at_ls = anchor_idx == 0 or file_content[anchor_idx - 1] == '\n'
    at_le = after_idx == len(file_content) or file_content[after_idx] == '\n'
    if not (at_ls and at_le):
        return content, 0, None
    trailing_newline = content.endswith('\n')
    body = content[:-1] if trailing_newline else content
    lines = body.split('\n') if body else []
    start = _anchor_echo_lines(anchor, content, from_end=False)
    end = _anchor_echo_lines(anchor, content, from_end=True)
    near, far = (start, end) if position == 'after' else (end, start)
    if not near and not far:
        return content, 0, None
    if near + far >= len(lines):
        return '', len(content), None
    if far:
        other = 'insert_before' if position == 'after' else 'insert_after'
        side = 'end' if position == 'after' else 'start'
        return content, 0, (
            f'content repeats anchor text at its {side}; insert_{position} '
            'already keeps the anchor. Use operation=replace with anchor=the '
            'existing text and content=the complete desired replacement, '
            'including retained anchor text and newlines. If only adding lines, '
            f'keep insert_{position} for that side or choose {other} for the '
            'opposite side, with content=new lines only. Direction was not guessed.')
    kept = lines[start:len(lines) - end]
    rest = '\n'.join(kept) + ('\n' if trailing_newline else '')
    return rest, len(content) - len(rest), None


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
        if len(before_text) > _ECHO_REPAIR_MAX_CHARS:
            return ''
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


def _post_edit_guard(rel_path, before_text, new_text, abs_path=None):
    """Unified report-only post-write guard: in-process Python syntax, then
    JSON / C/C++ via ``_post_edit_checks``. *abs_path* enables the C/C++
    check (cwd + compile_commands discovery); without it that check abstains.
    Never raises, never blocks — warnings ride the ``syntaxWarning`` field."""
    guard = _py_syntax_guard(rel_path, before_text, new_text)
    if guard:
        return guard
    if not isinstance(rel_path, str):
        return ''
    return _post_edit_checks.post_edit_check(
        rel_path, before_text, new_text, abs_path=abs_path)


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
                        position, rel_path, *, syntax_guard=True,
                        abs_path=None):
    """Echo repair + splice + syntax guard for one insert edit.

    Returns {'ok', 'content', 'new_content', 'insert_text', 'notes',
    'warning'} or {'ok': False, 'error'} (provably-contentless echo edits
    never touch the file). TOFU_EDIT_ECHO_REPAIR=0 disables the repair.
    """
    notes = []
    if os.environ.get('TOFU_EDIT_ECHO_REPAIR', '1') != '0':
        stripped, n_chars, echo_error = _strip_anchor_echo(
            file_content, anchor_idx, anchor, content, position)
        if echo_error:
            return {'ok': False, 'error': echo_error,
                    'errorKind': EK_ECHO_CONTENT}
        if n_chars:
            if not stripped.strip():
                return {'ok': False, 'error': (
                    'content is just the anchor text repeated — '
                    f'insert_{position} keeps the anchor in place '
                    'automatically. Re-issue with ONLY the new text in '
                    'content (do not repeat the anchor).'),
                    'errorKind': EK_ECHO_CONTENT}
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
                return {'ok': False, 'error': _whole_echo_error(k, position),
                        'errorKind': EK_ECHO_CONTENT}
        stripped2, k2, fail = _neighbour_echo_strip(
            file_content, anchor_idx, anchor, content, position, rel_path)
        if fail:
            return {'ok': False, 'error': fail,
                    'errorKind': EK_ECHO_CONTENT}
        if k2:
            where = 'after' if position == 'after' else 'before'
            notes.append(
                f'auto-repaired: stripped {k2} echoed context line'
                f'{"s" if k2 > 1 else ""} already present right {where} '
                'the anchor — pass ONLY the new text')
            content = stripped2
    new_content, insert_text = _splice_insertion(
        file_content, anchor_idx, anchor, content, position)
    warning = (_post_edit_guard(rel_path, file_content, new_content,
                                abs_path=abs_path)
               if syntax_guard else '')
    return {'ok': True, 'content': content, 'new_content': new_content,
            'insert_text': insert_text, 'notes': notes, 'warning': warning}


# ═══════════════════════════════════════════════════════
#  insert_content
# ═══════════════════════════════════════════════════════

def _compute_insert(file_content, rel_path, anchor, content, position='after',
                    *, syntax_guard=True, abs_path=None):
    """Pure match/splice half of ``_insert_one`` (no disk I/O).

    Returns ``{'ok': True, ...}`` with everything the commit step needs, or
    ``{'ok': False, ...}`` in the exact error shapes ``_insert_one`` produced.
    """
    # Inserts retain their line-oriented universal-newline behavior. The
    # unified batch now reads raw line endings so exact replacements preserve
    # CRLF; do not let that change make an insert splice in front of '\r'.
    original_file_content = file_content
    file_content = file_content.replace('\r\n', '\n').replace('\r', '\n')
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
                            'error': error_msg,
                            'errorKind': EK_AMBIGUOUS_MATCH}

            if tw_count == 0 and not _normalized:
                hint = _find_closest_match(norm_content, norm_anchor)
                error_msg = (f'Anchor text not found in {rel_path}. '
                             f'File has {file_content.count(chr(10))+1} lines. '
                             f'Use read_files to verify the exact content first.')
                if hint:
                    error_msg += (f'\n\nMost similar block (line {hint["line"]}, '
                                  f'{hint["similarity"]:.0%} match):\n```\n{hint["text"]}\n```')
                return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                        'error': error_msg, 'anchorLen': len(anchor),
                        'errorKind': EK_MATCH_NOT_FOUND}

            if tw_count > 1:
                locs = _describe_duplicate_matches(tw_content, tw_anchor)
                error_msg = (f'Anchor text matches {tw_count} locations (after trailing-whitespace '
                             f'normalization). Make it more specific by adding surrounding lines so it '
                             f'matches exactly once.')
                if locs:
                    error_msg += f'\n\n{locs}'
                return {'ok': False, 'action': 'insert_content', 'path': rel_path,
                        'error': error_msg,
                        'errorKind': EK_AMBIGUOUS_MATCH}

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
                                     'Please use read_files to get the exact content.',
                            'errorKind': EK_MAPPING_FAILED}

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
                'error': error_msg,
                'errorKind': EK_AMBIGUOUS_MATCH}

    # ── Boundary-echo repair + splice + syntax guard ──
    anchor_idx = file_content.index(anchor)
    fin = _finalize_insertion(file_content, anchor_idx, anchor, content,
                              position, rel_path, syntax_guard=syntax_guard,
                              abs_path=abs_path)
    if not fin['ok']:
        err = {'ok': False, 'action': 'insert_content', 'path': rel_path,
               'error': fin['error']}
        if fin.get('errorKind'):
            err['errorKind'] = fin['errorKind']
        return err
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

    if new_content.replace(reverse_patch['search'], reverse_patch['replace'], 1) != original_file_content:
        reverse_patch = {'search': new_content, 'replace': original_file_content}

    return {
        'ok': True,
        'new_content': new_content,
        'original_content': original_file_content,
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
        _write_with_undo(target, comp['new_content'], base, rel_path,
                         comp['original_content'], mod_type='apply_diff',
                         reverse_patch=comp['reverse_patch'], conv_id=conv_id, task_id=task_id)
        _touch_for_vscode(target)
        _record_write_freshness(conv_id, target, comp['new_content'].encode('utf-8'))
        return _insert_result_from_comp(rel_path, comp, description)
    except SessionLockBusy as e:
        return _journal_busy_failure('insert_content', rel_path, e)
    except _AppliedWriteFailure as e:
        return _applied_write_failure('insert_content', rel_path, e)
    except Exception as e:
        logger.error('[Tools] insert_content write failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'insert_content', 'path': rel_path,
                'errorKind': EK_WRITE_FAILED}


def _insert_one(base, rel_path, anchor, content, position='after', description='', conv_id=None, task_id=None,
                allow_outside=False):
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
        target = _resolve_write_path(base, rel_path, conv_id=conv_id,
                                     allow_outside=allow_outside)
    except ValueError as e:
        logger.debug('[Tools] insert_content path rejected %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': str(e), 'action': 'insert_content', 'path': rel_path,
                'errorKind': EK_PATH_INVALID}

    if not os.path.isfile(target):
        return {'ok': False, 'error': f'File not found: {rel_path}',
                'action': 'insert_content', 'path': rel_path,
                'errorKind': EK_FILE_NOT_FOUND}

    try:
        file_content = _read_original_text(target)
    except Exception as e:
        logger.warning('[Tools] insert_content read failed for %s: %s', rel_path, e, exc_info=True)
        return {'ok': False, 'error': f'Cannot read file: {e}',
                'action': 'insert_content', 'path': rel_path,
                'errorKind': EK_READ_FAILED}

    comp = _compute_insert(file_content, rel_path, anchor, content,
                           position=position, abs_path=target)
    if not comp['ok']:
        return comp
    return _commit_insert(target, base, rel_path, comp, description=description,
                          conv_id=conv_id, task_id=task_id)


@_with_workspace_writer
def tool_insert_content(base, rel_path, anchor, content, position='after', description='', conv_id=None, task_id=None,
                        allow_outside=False):
    """Insert content before or after an anchor string (single edit entry point)."""
    return _insert_one(base, rel_path, anchor, content, position, description, conv_id, task_id=task_id,
                       allow_outside=allow_outside)


@_with_workspace_writer
def tool_insert_contents(base_path, edits, conv_id=None, task_id=None,
                         allow_outside=False):
    """Apply multiple insert_content edits in one batch."""
    if not edits:
        return 'No edits provided.'

    results = []
    ok_count = 0
    partial_count = 0
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
            results.append(f'[{i}] FAIL [{EK_INVALID_EDIT}] Missing required field (path or anchor)')
            fail_count += 1
            continue

        if position not in ('before', 'after'):
            results.append(
                f'[{i}] FAIL [{EK_INVALID_EDIT}] Invalid position: '
                f'{position} (must be "before" or "after")')
            fail_count += 1
            continue

        try:
            bp, resolved_rp = _resolve_base(base_path, rp)
        except ValueError as _rve:
            logger.debug('[write_tools] tool_insert_contents caught %s: %s', type(_rve).__name__, _rve)
            fail_count += 1
            results.append(f'[{i}] FAIL {rp} [{EK_PATH_INVALID}]: {_rve}')
            continue
        result = _insert_one(bp, resolved_rp, anchor, content, position, desc, conv_id, task_id=task_id,
                             allow_outside=allow_outside)

        partial_count += int(bool(result.get('mutationApplied')))
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
            results.append(f'[{i}] FAIL {rp}{_fail_tag(result)}: {result["error"]}')

    header = f'Inserted {ok_count}/{ok_count + fail_count} edits'
    if fail_count:
        header += f' ({fail_count} failed)'
    if partial_count:
        header = header.replace('Applied ', 'Completed ', 1).replace('Inserted ', 'Completed ', 1)
        header += f'; {partial_count} edits were applied without confirmed undo'
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


def _admit_edit_groups(groups, conv_id, stack):
    """Hold every tracked destination's existing journal admission before writes.

    The enclosing workspace owner already leased the complete physical write
    set. Sort journal sessions as a set, not per-file nested acquisitions, and
    use the same recovery/cache authority as the individual writer. No new
    intents or user-file writes are prepared during this preflight.
    """
    from lib.project_mod import modifications as mods
    from lib.project_mod.session_lock import locked_session
    from lib.project_mod.write_intents import IntentError, locked_intents
    from lib.project_mod.write_recovery import recover_locked

    options, errors, sessions = {}, {}, {}
    for target, ops in groups.items():
        try:
            # Canonical paths identify shared buffers, not workspace authority.
            # A project symlink's referent must not become untracked scratch.
            destinations = []
            seen = set()
            for op in ops:
                lexical = op['target']
                if lexical in seen:
                    continue
                seen.add(lexical)
                if _should_record_modification(lexical, conv_id=conv_id):
                    destinations.append(_write_destination(
                        lexical, op['bp'], op['resolved_rp'], conv_id=conv_id))
            options[target] = {'tracked': bool(destinations)}
            if not destinations:
                continue
            options[target]['attribution'] = destinations[0][:2]
            for mod_base, _mod_rel, session_dir in destinations:
                session = sessions.setdefault(session_dir, {'base': mod_base, 'targets': []})
                if target not in session['targets']:
                    session['targets'].append(target)
        except Exception as error:
            errors[target] = error
    for directory, session in sorted(sessions.items()):
        try:
            state = stack.enter_context(locked_session(directory))
            store = stack.enter_context(locked_intents(directory))
            if state.get('writing') or state.get('recovering'):
                raise IntentError('Nested project write during journal publication is not allowed')
            state['recovering'] = True
            try:
                recover_locked(session['base'], store)
            finally:
                state.pop('recovering', None)
            # A later root's corrupt journal must not follow an earlier write.
            # Validation by identity, not a full materialization: an edit must not
            # pay for re-parsing the whole history before writing one file.
            mods._assert_journal_ready(directory)
        except Exception as error:
            for target in session['targets']:
                errors[target] = error
    return options, errors


def _edit_group_failure(operation, path, error):
    action = 'apply_diff' if operation == 'replace' else 'insert_content'
    if isinstance(error, SessionLockBusy):
        return _journal_busy_failure(action, path, error)
    if isinstance(error, _AppliedWriteFailure):
        return _applied_write_failure(action, path, error)
    return {'ok': False, 'error': str(error), 'action': action, 'path': path,
            'errorKind': EK_WRITE_FAILED}


@_with_workspace_writer
def tool_edit_file(base_path, edits, conv_id=None, task_id=None,
                   allow_outside=False, atomic=True, batch_path=None):
    """Preflight a sequential mixed batch, then publish one transition per file.

    Atomic preflight is the default: an invalid operation, destination or match
    prevents every write. Only explicit ``atomic=False`` permits legacy partial
    application. This does not promise cross-file rollback after I/O failure.
    Later edits see earlier in-memory changes; receipts keep original indexes.

    ``batch_path`` is the call's declared file for every edit that omits its
    own ``path`` — one batch may still span files by naming a path per edit.
    """
    if not edits:
        return 'No edits provided.'
    if not isinstance(edits, list):
        return 'Error: edit_file edits must be an array of objects.'

    from collections import OrderedDict

    strings = {}
    prepared = []

    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            strings[i] = _invalid_edit_entry_msg(
                i, edit, '{path, operation, anchor, content}')
            continue

        own_path = edit.get('path')
        rp = own_path if isinstance(own_path, str) and own_path.strip() \
            else (batch_path or '')
        operation = edit.get('operation', '')
        anchor = edit.get('anchor', '')
        content = edit.get('content')
        desc = edit.get('description', '')

        if (not isinstance(rp, str) or not rp
                or not isinstance(anchor, str) or not anchor
                or not isinstance(content, str)):
            strings[i] = (
                f'[{i}] FAIL [{EK_INVALID_EDIT}] '
                'Missing required field (path, anchor, or content) — name the '
                'file once in the batch `path`, or on this edit')
            continue
        if not isinstance(operation, str) or operation not in _EDIT_OPERATIONS:
            strings[i] = (f'[{i}] FAIL [{EK_INVALID_EDIT}] Invalid operation: '
                          f'{operation!r} '
                          '(must be insert_after, insert_before, or replace)')
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
            strings[i] = f'[{i}] FAIL {rp} [{EK_PATH_INVALID}]: {exc}'
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
            target = _resolve_write_path(p['bp'], p['resolved_rp'], conv_id=conv_id,
                                         allow_outside=allow_outside)
        except ValueError as e:
            p['resolve_err'] = str(e)
            continue
        p['target'] = target
        groups.setdefault(os.path.realpath(target), []).append(p)

    # Path failures join the same whole-batch preflight, before any publication.
    for p in prepared:
        if 'resolve_err' in p:
            _log_edit_efficiency(p['operation'], p['anchor'], p['content'], ok=False)
            strings[p['i']] = (
                f'[{p["i"]}] FAIL {p["rp"]} [{p["operation"]}] '
                f'[{EK_PATH_INVALID}]: {p["resolve_err"]}')

    return _execute_edit_file_plan(
        groups, strings, len(edits), conv_id, task_id, atomic=atomic is not False)


def _execute_edit_file_plan(groups, strings, total, conv_id, task_id, *, atomic):
    """Compute all file buffers under existing admissions, then publish/report."""
    from contextlib import ExitStack

    with ExitStack() as stack:
        options, admission_errors = _admit_edit_groups(groups, conv_id, stack)
        return _preflight_and_commit_edits(
            groups, strings, total, conv_id, task_id, atomic=atomic,
            options=options, admission_errors=admission_errors)


def _preflight_and_commit_edits(groups, strings, total, conv_id, task_id, *,
                              atomic, options, admission_errors):
    plans = []
    fail_count = len(strings)
    ok_count = partial_count = skipped_count = 0

    # No file publication, history, freshness or syntax-success receipt here.
    for target, ops in groups.items():
        if not os.path.isfile(target):
            for p in ops:
                _log_edit_efficiency(p['operation'], p['anchor'], p['content'], ok=False)
                strings[p['i']] = (
                    f'[{p["i"]}] FAIL {p["rp"]} [{p["operation"]}] '
                    f'[{EK_FILE_NOT_FOUND}]: '
                    f'File not found: {p["resolved_rp"]}')
                fail_count += 1
            continue
        try:
            current = _read_original_text(target)
        except Exception as e:
            logger.warning('[Tools] edit_file read failed for %s: %s', target, e, exc_info=True)
            for p in ops:
                _log_edit_efficiency(p['operation'], p['anchor'], p['content'], ok=False)
                strings[p['i']] = (
                    f'[{p["i"]}] FAIL {p["rp"]} [{p["operation"]}] '
                    f'[{EK_READ_FAILED}]: '
                    f'Cannot read file: {e}')
                fail_count += 1
            continue

        before_batch = current
        comps = []
        recs = []
        for p in ops:
            if p['operation'] == 'replace':
                comp = _compute_diff(current, p['resolved_rp'], p['anchor'],
                                     p['content'], replace_all=p['replace_all'])
            else:
                comp = _compute_insert(current, p['resolved_rp'], p['anchor'],
                                       p['content'], position=p['position'],
                                       syntax_guard=False)
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

        if recs:
            try:
                # UTF-8 encoding is deterministic validation, not later I/O.
                # Validate final buffers before any preceding file is published.
                current.encode('utf-8')
            except UnicodeEncodeError as error:
                comps = [(p, None, err or {
                    'ok': False, 'errorKind': EK_INVALID_EDIT,
                    'error': f'Cannot encode planned UTF-8 content: {error}',
                }) for p, _comp, err in comps]
                recs = []
        if target in admission_errors:
            comps = [(p, None, err or _edit_group_failure(
                p['operation'], p['resolved_rp'], admission_errors[target]))
                for p, _comp, err in comps]
            recs = []
        plans.append((target, ops, before_batch, current, comps, recs))

    rejected = atomic and (fail_count or any(
        comp is None for _target, _ops, _before, _after, comps, _recs in plans
        for _p, comp, _err in comps))

    for target, ops, before_batch, current, comps, recs in plans:
        write_err = None
        if recs and not rejected:
            write_err = _commit_edit_group(
                target, recs[0]['attribution_base'], recs, conv_id, task_id,
                write_options=options.get(target))

        for p, comp, err_dict in comps:
            if comp is not None:
                if rejected:
                    strings[p['i']] = (
                        f'[{p["i"]}] SKIP {p["rp"]} [{p["operation"]}]: '
                        'Not applied because atomic preflight failed.')
                    skipped_count += 1
                    _log_edit_efficiency(p['operation'], p['anchor'], p['content'], ok=False)
                    continue
                if write_err is None:
                    if p['operation'] == 'replace':
                        result = _diff_result_from_comp(
                            p['resolved_rp'], comp, p['desc'], syntax_guard=False)
                    else:
                        result = _insert_result_from_comp(p['resolved_rp'], comp, p['desc'])
                else:
                    result = _edit_group_failure(p['operation'], p['resolved_rp'], write_err)
            else:
                result = err_dict
            _log_edit_efficiency(p['operation'], p['anchor'], p['content'], ok=result['ok'])

            partial_count += int(bool(result.get('mutationApplied')))
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
                    f'[{p["i"]}] FAIL {p["rp"]} [{p["operation"]}]'
                    f'{_fail_tag(result)}: {result["error"]}')

        # Only committed final bytes can justify follow-up work. Checking
        # intermediate replacements would warn about syntax already repaired
        # by a later edit and spend another model round on a non-problem.
        if recs and write_err is None and not rejected:
            warning = _post_edit_guard(ops[0]['resolved_rp'], before_batch,
                                       current, abs_path=target)
            if warning:
                last_ok = next(p['i'] for p, comp, _err in reversed(comps)
                               if comp is not None)
                strings[last_ok] += ' ' + warning
                logger.warning('[SyntaxGuard] %s', warning)

    results = [strings[i] for i in range(1, total + 1)]
    header = f'Applied {ok_count}/{total} edits'
    if fail_count:
        header += f' ({fail_count} failed)'
    if rejected:
        header += f'; atomic preflight failed; no files written; {skipped_count} skipped'
    if partial_count:
        header = header.replace('Applied ', 'Completed ', 1).replace('Inserted ', 'Completed ', 1)
        header += f'; {partial_count} edits were applied without confirmed undo'
    return header + '\n' + '\n'.join(results)
