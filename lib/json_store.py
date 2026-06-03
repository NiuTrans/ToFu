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

Design notes
------------
* **Atomic writes** use ``tempfile.mkstemp(dir=parent_of(path))`` →
  ``write+flush+fsync`` → ``os.replace``. This survives a crash mid-write
  on POSIX and Windows.
* **Per-path locking**: ``update_json_atomic`` serialises read-modify-write
  cycles for the same path so concurrent callers don't lose updates.
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

def read_json(path: str, default: Any = None, *, jsonc: bool = False) -> Any:
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
        logger.warning('[json_store] Read failed for %s: %s', path, e)
        return default
    return _parse_json_text(text, path, default, jsonc=jsonc)


def _parse_json_text(text: str, path: str, default: Any, *, jsonc: bool):
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if not jsonc:
            logger.warning('[json_store] Invalid JSON at %s — returning default', path)
            return default
    # Retry with JSONC stripping
    try:
        stripped = _strip_jsonc(text)
        return json.loads(stripped)
    except json.JSONDecodeError as e:
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
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_json_atomic(path: str, data: Any, *, fsync: bool = True,
                      indent: int | None = 2, sort_keys: bool = False,
                      mode: int = 0o644) -> None:
    """Atomically write ``data`` as JSON to ``path``.

    Adds a trailing newline (matches the convention used by
    code-server / VS Code settings files).
    """
    text = json.dumps(data, indent=indent, ensure_ascii=False,
                       sort_keys=sort_keys) + '\n'
    write_text_atomic(path, text, fsync=fsync, mode=mode)


# ── Read-modify-write helper ───────────────────────────────────────

def update_json_atomic(path: str, mutator: Callable[[Any], Any], *,
                        default: Any = None, jsonc: bool = False,
                        fsync: bool = True, indent: int | None = 2,
                        sort_keys: bool = False) -> Any:
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
    with lock:
        current = read_json(path, default=default, jsonc=jsonc)
        # Passing a deep copy is the caller's responsibility — we don't
        # pay the deepcopy cost by default. Mutators that want pre/post
        # diff comparisons should make their own copy.
        new_value = mutator(current)
        if new_value is None:
            return None
        write_json_atomic(path, new_value, fsync=fsync,
                           indent=indent, sort_keys=sort_keys)
        return new_value


__all__ = [
    'read_json', 'read_text',
    'write_json_atomic', 'write_text_atomic',
    'update_json_atomic',
    '_strip_jsonc',  # exported for tests + code_server_excludes use
]
