"""Unified JSON file I/O with atomic writes, locking, and graceful errors.

Replaces three separate ``_atomic_write`` implementations
(``code_server_excludes``, ``file_history/store``, ``optimizer/actions``)
and standardises the read-modify-write pattern across the project.

Public API
----------
  read_json(path, default=None, jsonc=False)  → dict | list | default
  write_json_atomic(path, data, fsync=True, indent=2, mode=0o644)
  update_json_atomic(path, mutator, default=None, jsonc=False, ...)

  read_text(path, default='')
  write_text_atomic(path, text, fsync=True, mode=0o644)
  write_bytes_atomic(path, data, fsync=True, mode=0o644)
  atomic_output_path(path, fsync=True, mode=0o644)  → temporary path context
  temporary_output_path(path, suffix='.tmp')       → cleanup-only context
  locked_path(path)                                → RMW lock context

Design notes
------------
* **Atomic writes** use ``tempfile.mkstemp(dir=parent_of(path))`` →
  ``write+flush+fsync`` → ``os.replace``. This survives a crash mid-write
  on POSIX and Windows.
* **Per-path locking**: ``update_json_atomic`` serialises read-modify-write
  cycles for the same path so concurrent callers don't lose updates —
  both WITHIN a process (a ``threading.Lock``) and ACROSS processes (a
  blocking ``fcntl.flock`` on a sidecar ``<path>.lock`` file). The
  cross-process lock degrades to a no-op where advisory locking is
  unavailable (Windows / no ``fcntl`` / FS without flock support), leaving
  the in-process guarantee intact.
* **JSONC tolerance**: pass ``jsonc=True`` to strip ``//``-line comments,
  ``/* */``-block comments, and trailing commas before parsing.
* **Errors are logged, not silenced**: read failures return ``default``
  but log a warning; write failures raise.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
from typing import Any, Callable

from lib.log import get_logger

logger = get_logger(__name__)


class JsonStoreReadError(RuntimeError):
    """An existing JSON store could not be read without data loss."""


# ── Per-path locks for read-modify-write atomicity ──────────────────

_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_MUTEX = threading.Lock()


def _path_lock(path: str) -> threading.Lock:
    """Get a per-path lock keyed by absolute path. Created lazily."""
    key = os.path.abspath(path)
    with _PATH_LOCKS_MUTEX:
        lk = _PATH_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _PATH_LOCKS[key] = lk
        return lk


# ── Inter-process lock (sidecar flock) ──────────────────────────────
# The thread lock above only serialises read-modify-write WITHIN one
# process. When two PROCESSES touch the same JSON file (two server
# instances on a shared FUSE/NFS mount, or the server + a CLI tool), their
# RMW cycles can interleave and lose updates. ``_interprocess_lock`` adds a
# blocking POSIX ``fcntl.flock(LOCK_EX)`` on a sidecar ``<path>.lock`` file
# so the RMW is atomic across processes too.
#
# We lock a SIDECAR file rather than the data file itself because
# ``write_json_atomic`` swaps the data file's inode via ``os.replace`` — a
# lock held on the old inode would not cover the new one. The sidecar inode
# is stable.
#
# On Windows (no portable ``fcntl``), when ``fcntl`` is unavailable, or when the filesystem
# doesn't support advisory locks, we degrade to a no-op (the thread lock
# still protects the common single-process case) rather than ship a
# half-reliable path or newly regress hosts.

@contextlib.contextmanager
def _interprocess_lock(path: str):
    """Hold an exclusive cross-process advisory lock for ``path``'s RMW.

    Blocking (waits for the lock); always yields, degrading to a no-op when
    OS-level locking is unavailable. The lock file is created next to the
    data file and is never deleted (deleting it races other holders).
    """
    try:
        import fcntl  # POSIX only
    except ImportError:
        # Windows / no fcntl — thread lock is the only protection.
        yield
        return

    lock_path = os.path.abspath(path) + '.lock'
    parent = os.path.dirname(lock_path) or '.'
    if parent != '.' and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            logger.debug('[json_store] flock parent mkdir failed for %s: %s',
                         lock_path, e)
            yield
            return

    fd = None
    locked = False
    try:
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            logger.debug('[json_store] could not open lock file %s: %s — '
                         'degrading to thread-lock-only', lock_path, e)
            yield
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
        except (OSError, IOError) as e:
            # Filesystem (e.g. some FUSE/NFS backends) doesn't support
            # advisory locks — degrade to no-op, don't regress.
            logger.debug('[json_store] flock unsupported on %s (%s) — '
                         'thread-lock-only', lock_path, e)
        yield
    finally:
        if fd is not None:
            if locked:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (OSError, IOError) as e:
                    logger.debug('[json_store] flock unlock failed for %s: %s',
                                 lock_path, e)
            try:
                os.close(fd)
            except OSError as e:
                logger.debug('[json_store] lock fd close failed for %s: %s',
                             lock_path, e)


@contextlib.contextmanager
def locked_path(path: str):
    """Serialize an arbitrary read-modify-write cycle for ``path``.

    This is the public counterpart to :func:`update_json_atomic`'s locking
    contract for non-JSON stores (for example, bounded line journals).  The
    context is deliberately non-reentrant; callers must perform their I/O
    directly rather than invoke another lock-taking helper for the same path.
    """
    with _path_lock(path), _interprocess_lock(path):
        yield


# ── JSONC stripping ────────────────────────────────────────────────

_TRAILING_COMMA_RE = re.compile(r',(\s*[}\]])')


def _strip_jsonc(text: str) -> str:
    """Remove JSONC line/block comments + trailing commas, string-aware.

    Walks the input character-by-character so glob patterns like
    ``"**/data/**"`` (which contain ``*/`` and ``//``) inside JSON
    strings are not mistaken for comment delimiters.
    """
    out = []
    i = 0
    n = len(text)
    in_string = False
    string_quote = ''
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == string_quote:
                in_string = False
            i += 1
            continue
        if ch == '"' or ch == "'":
            in_string = True
            string_quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == '/' and i + 1 < n:
            nxt = text[i + 1]
            if nxt == '/':
                j = text.find('\n', i + 2)
                i = n if j == -1 else j
                continue
            if nxt == '*':
                j = text.find('*/', i + 2)
                i = n if j == -1 else j + 2
                continue
        out.append(ch)
        i += 1
    return _TRAILING_COMMA_RE.sub(r'\1', ''.join(out))


# ── Reads ──────────────────────────────────────────────────────────

def read_json(path: str, default: Any = None, *, jsonc: bool = False,
              strict: bool = False) -> Any:
    """Read and parse a JSON file. Returns ``default`` on any error.

    Parameters
    ----------
    path : str
        Path to the JSON file.
    default : Any
        Value returned on FileNotFoundError, parse failure, or unreadable.
    jsonc : bool
        If True, strip ``//`` line comments, ``/* */`` block comments,
        and trailing commas before parsing.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError as _e_audit:
        logger.debug('[json_store] read_json caught %s: %s', type(_e_audit).__name__, _e_audit)
        return default
    except OSError as e:
        if strict:
            raise JsonStoreReadError(
                f'failed to read existing JSON store {path}') from e
        logger.warning('[json_store] Read failed for %s: %s', path, e)
        return default
    return _parse_json_text(
        text, path, default, jsonc=jsonc, strict=strict)


def _parse_json_text(text: str, path: str, default: Any, *, jsonc: bool,
                     strict: bool = False):
    if not text.strip():
        if strict:
            raise JsonStoreReadError(f'existing JSON store is empty: {path}')
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if not jsonc:
            if strict:
                raise JsonStoreReadError(
                    f'invalid JSON in existing store {path}')
            logger.warning('[json_store] Invalid JSON at %s — returning default', path)
            return default
    # Retry with JSONC stripping
    try:
        stripped = _strip_jsonc(text)
        return json.loads(stripped)
    except json.JSONDecodeError as e:
        if strict:
            raise JsonStoreReadError(
                f'invalid JSONC in existing store {path}') from e
        logger.warning('[json_store] Invalid JSONC at %s: %s — returning default',
                       path, e)
        return default


def read_text(path: str, default: str = '') -> str:
    """Read a text file. Returns ``default`` on missing file or read error."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError as _e_audit:
        logger.debug('[json_store] read_text caught %s: %s', type(_e_audit).__name__, _e_audit)
        return default
    except OSError as e:
        logger.warning('[json_store] Read text failed for %s: %s', path, e)
        return default


# ── Atomic writes ──────────────────────────────────────────────────

def write_text_atomic(path: str, text: str, *, fsync: bool = True,
                      mode: int = 0o644) -> None:
    """Atomically write ``text`` to ``path``.

    Strategy: ``mkstemp`` in the same directory → write+flush(+fsync)
    → ``os.replace``. The replace is atomic on POSIX and Windows.

    Parameters
    ----------
    fsync : bool
        If True (default), call ``os.fsync()`` so data is on disk before
        the rename. Slightly slower; required for data that must survive
        a crash within seconds of the call.
    mode : int
        Octal file mode to chmod the temp file to before rename. The
        default 0o644 matches typical Unix expectations.
    """
    parent = os.path.dirname(path) or '.'
    if parent != '.' and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.jsonstore-', suffix='.tmp', dir=parent)
    try:
        try:
            os.chmod(tmp, mode)
        except OSError as _e_audit:
            logger.debug('[json_store] write_text_atomic caught %s: %s', type(_e_audit).__name__, _e_audit)
            pass
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            if fsync:
                try:
                    os.fsync(f.fileno())
                except OSError as _e_audit:
                    logger.debug('[json_store] write_text_atomic caught %s: %s', type(_e_audit).__name__, _e_audit)
                    pass
            # Big one-shot JSON (server_config, history, manifests) is
            # write-once/read-rarely — drop its just-written pages from the
            # page cache so they stop counting against a shared cgroup limit
            # (lib/cgroup_guard context, 2026-07-27). Pure hint, no-op off-Linux.
            if len(text) >= 262144:
                try:
                    os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                except (OSError, AttributeError) as e:
                    logger.debug('[json_store] fadvise DONTNEED skipped: %s', e)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_bytes_atomic(path: str, data: bytes, *, fsync: bool = True,
                       mode: int = 0o644) -> None:
    """Atomically replace ``path`` with a bytes-like payload.

    Binary asset stores historically reimplemented this with one shared
    ``<dest>.tmp`` name.  Two workers publishing the same content-addressed
    object then raced: the first replace consumed the shared temp file and the
    second failed with FileNotFoundError.  A unique same-directory mkstemp
    gives binary writers the same crash/concurrency contract as JSON/text.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError('write_bytes_atomic data must be bytes-like')
    payload = bytes(data)
    parent = os.path.dirname(path) or '.'
    if parent != '.' and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.binstore-', suffix='.tmp', dir=parent)
    try:
        try:
            os.chmod(tmp, mode)
        except OSError as error:
            logger.debug('[json_store] binary chmod skipped for %s: %s',
                         tmp, error)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            if fsync:
                try:
                    os.fsync(handle.fileno())
                except OSError as error:
                    logger.debug('[json_store] binary fsync skipped for %s: %s',
                                 tmp, error)
            if len(payload) >= 262144:
                try:
                    os.posix_fadvise(
                        handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                except (OSError, AttributeError) as error:
                    logger.debug('[json_store] binary fadvise skipped: %s',
                                 error)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def atomic_output_path(path: str, *, fsync: bool = True,
                       mode: int = 0o644):
    """Yield a unique sibling path and atomically publish it on success.

    Some writers (``zipfile``, ``shutil.copy2``, media encoders) require a
    filesystem path and cannot target :func:`write_bytes_atomic` directly.
    Historically each caller used one shared ``<destination>.tmp`` file, so
    concurrent publishers consumed or corrupted each other's staging file.
    This context owns the complete lifecycle: unique same-directory creation,
    optional fsync, atomic replace, and cleanup after *any* exception.
    """
    parent = os.path.dirname(path) or '.'
    if parent != '.' and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.output-', suffix='.tmp', dir=parent)
    os.close(fd)
    try:
        try:
            os.chmod(tmp, mode)
        except OSError as error:
            logger.debug('[json_store] atomic output chmod skipped for %s: %s',
                         tmp, error)
        yield tmp
        if fsync:
            sync_fd = None
            try:
                sync_fd = os.open(tmp, os.O_RDONLY)
                os.fsync(sync_fd)
            except OSError as error:
                logger.debug('[json_store] atomic output fsync skipped for %s: %s',
                             tmp, error)
            finally:
                if sync_fd is not None:
                    os.close(sync_fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def temporary_output_path(path: str, *, suffix: str = '.tmp'):
    """Yield a unique sibling staging path and always clean it up.

    Use this for external writers whose output must be validated before it is
    promoted.  Unlike :func:`atomic_output_path`, normal context exit does not
    replace the destination: the caller explicitly calls ``os.replace`` only
    after its semantic checks pass.  Keeping the destination basename in the
    prefix aids diagnostics and tools that infer metadata from file names;
    ``suffix`` should preserve any format-significant extension.
    """
    if os.sep in suffix or (os.altsep and os.altsep in suffix):
        raise ValueError('temporary output suffix must not contain separators')
    parent = os.path.dirname(path) or '.'
    if parent != '.' and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    basename = os.path.basename(path)[:64] or 'output'
    fd, tmp = tempfile.mkstemp(
        prefix=f'.{basename}-', suffix=suffix, dir=parent)
    os.close(fd)
    try:
        yield tmp
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def write_json_atomic(path: str, data: Any, *, fsync: bool = True,
                      indent: int | None = 2, sort_keys: bool = False,
                      mode: int = 0o644, allow_nan: bool = True) -> None:
    """Atomically write ``data`` as JSON to ``path``.

    Adds a trailing newline (matches the convention used by
    code-server / VS Code settings files).
    """
    text = json.dumps(data, indent=indent, ensure_ascii=False,
                       sort_keys=sort_keys, allow_nan=allow_nan) + '\n'
    write_text_atomic(path, text, fsync=fsync, mode=mode)


# ── Read-modify-write helper ───────────────────────────────────────

def update_json_atomic(path: str, mutator: Callable[[Any], Any], *,
                        default: Any = None, jsonc: bool = False,
                        strict: bool = False,
                        fsync: bool = True, indent: int | None = 2,
                        sort_keys: bool = False,
                        mode: int = 0o644) -> Any:
    """Read JSON, apply mutator, write atomically. Locked per-path.

    The ``mutator`` callable receives the current value (or ``default``
    if the file is missing/unparseable) and must return the new value
    to persist. If the mutator returns ``None``, the file is left
    untouched (useful for conditional updates).

    Returns the value the mutator returned, or ``None`` if no write
    occurred.

    Example
    -------
    >>> def add_domain(cfg):
    ...     cfg.setdefault('search', {}).setdefault('skip_domains', []).append('x.com')
    ...     return cfg
    >>> update_json_atomic('config.json', add_domain, default={})
    """
    lock = _path_lock(path)
    # Thread lock (outer, in-process) THEN flock (inner, cross-process): the
    # cheap in-process contention resolves first; only one thread per process
    # then contends for the OS-level lock. ``_interprocess_lock`` is a no-op
    # where advisory locking is unavailable, so single-process behaviour is
    # unchanged.
    with lock, _interprocess_lock(path):
        current = read_json(
            path, default=default, jsonc=jsonc, strict=strict)
        # Passing a deep copy is the caller's responsibility — we don't
        # pay the deepcopy cost by default. Mutators that want pre/post
        # diff comparisons should make their own copy.
        new_value = mutator(current)
        if new_value is None:
            return None
        write_json_atomic(path, new_value, fsync=fsync,
                           indent=indent, sort_keys=sort_keys,
                           mode=mode,
                           allow_nan=not strict)
        return new_value


__all__ = [
    'read_json', 'read_text',
    'write_json_atomic', 'write_text_atomic', 'write_bytes_atomic',
    'atomic_output_path', 'temporary_output_path', 'locked_path',
    'update_json_atomic',
    'JsonStoreReadError',
    '_strip_jsonc',  # exported for tests + code_server_excludes use
]
